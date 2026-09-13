import asyncio
from dataclasses import replace
from itertools import pairwise, product
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.config import NotificationConfig
from core.message_manager import MessageManager, Pending, project
from core.templates import Templates
from core.types import Channel, Enter, Left, Move, SessionIdentity, UidIdentity, User

A, B, C = Channel(1, "A"), Channel(2, "B"), Channel(3, "C")
USER = User(UidIdentity(uuid4(), "uid"), "Alice", None, None)


def enter(channel=A, user=USER):
    return Enter(user, channel)


def left(channel=A, user=USER):
    return Left(user, channel)


def move(origin=A, destination=B, user=USER):
    return Move(user, origin, destination)


def manager(**config):
    now = [0.0]
    send = AsyncMock(return_value=True)
    result = MessageManager(
        NotificationConfig.model_validate({"targets": ["bot:GroupMessage:target"], **config}),
        Templates({}),
        send,
        lambda: now[0],
    )
    result.resume()
    return result, now, send


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([enter(), move()], enter(B)),
        ([move(), move(B, C)], move(A, C)),
        ([move(), move(B, A)], None),
        ([enter(), left()], None),
        ([enter(), move(), left(B)], None),
        ([move(), left(B)], left(A)),
        ([left(), enter()], None),
        ([left(), enter(B)], move()),
        ([enter(), left(), enter(B), left(B)], None),
        ([enter(), left(), enter(B)], enter(B)),
        ([move(), left(B), enter(B)], move(A, B)),
        ([move(), left(B), enter(C)], move(A, C)),
        ([left(), enter(B), left(B)], left(A)),
        ([move(), move(B, A), move(A, C)], move(A, C)),
    ],
)
def test_endpoint_rules(events, expected):
    service, now, _ = manager()
    for event in events:
        service.add(event)
        now[0] += 1
    assert service.window is not None
    assert project(next(iter(service.window.records.values()))) == expected


def test_all_legal_short_paths():
    # Independent reference: enumerate physical locations, including offline.
    for locations in product((None, A, B, C), repeat=6):
        if any(a == b for a, b in pairwise(locations)):
            continue
        events = []
        for origin, destination in pairwise(locations):
            if origin is None:
                events.append(enter(destination))
            elif destination is None:
                events.append(left(origin))
            else:
                events.append(move(origin, destination))
        start, end = locations[0], locations[-1]
        expected = None
        if start != end:
            if start is None:
                expected = enter(end)
            elif end is None:
                expected = left(start)
            else:
                expected = move(start, end)
        service, _, _ = manager()
        for event in events:
            service.add(event)
        assert project(next(iter(service.window.records.values()))) == expected


def test_snapshot_and_channel_identity():
    latest_user = replace(USER, nickname="Renamed")
    renamed_a = Channel(A.id, "Renamed A")
    assert project(Pending(move(), move(B, renamed_a, latest_user))) is None
    result = project(Pending(move(), left(B, latest_user)))
    assert result == left(A, latest_user)


@pytest.mark.parametrize(
    "identity",
    [
        UidIdentity(USER.identity.connection, "different"),
        UidIdentity(uuid4(), "uid"),
        SessionIdentity(uuid4()),
    ],
)
def test_identity_isolation(identity):
    service, _, _ = manager()
    service.add(enter())
    service.add(left(user=replace(USER, identity=identity)))
    assert len(service.window.records) == 2


def test_session_id_reuse():
    service, _, _ = manager()
    first = replace(USER, identity=SessionIdentity(uuid4()))
    second = replace(USER, identity=SessionIdentity(uuid4()))
    service.add(left(user=first))
    service.add(enter(user=second))
    assert len(service.window.records) == 2


async def test_quiet_window():
    service, now, send = manager()
    service.add(enter())
    now[0] = 9
    await service.flush_due()
    send.assert_not_awaited()
    service.add(move())
    assert service.window.opened_at == 0
    now[0] = 18
    await service.flush_due()
    send.assert_not_awaited()
    now[0] = 19
    await service.flush_due()
    assert send.await_count == 1
    assert "加入频道 B" in send.call_args.args[1]


async def test_maximum_window():
    service, now, send = manager()
    service.add(enter())
    current = A
    for timestamp in range(9, 61, 9):
        now[0] = timestamp
        destination = B if current == A else A
        service.add(move(current, destination))
        current = destination
        await service.flush_due()
    send.assert_not_awaited()
    now[0] = 60
    service.add(move(current, C))
    await service.flush_due()
    assert send.await_count == 1


async def test_zero_window_and_cancellation():
    service, _, send = manager(window={"quiet_seconds": 0})
    service.add(move())
    service.add(move(B, A))
    await service.flush_due()
    send.assert_not_awaited()
    service.add(enter())
    send.assert_not_awaited()
    await service.flush_due()
    assert send.await_count == 1


@pytest.mark.parametrize("kind,event", [("enter", enter()), ("left", left()), ("move", move())])
async def test_change_subswitches(kind, event):
    service, now, send = manager(events={kind: False})
    service.add(event)
    now[0] = 10
    await service.flush_due()
    send.assert_not_awaited()


async def test_filter_after_projection():
    service, now, send = manager(events={"enter": False, "left": False})
    service.add(left())
    service.add(enter(B))
    now[0] = 10
    await service.flush_due()
    assert send.await_count == 1
    assert "切换到频道 B" in send.call_args.args[1]


async def test_new_events_during_delivery():
    service, now, send = manager(window={"quiet_seconds": 0})
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(target, text):
        started.set()
        await release.wait()
        return True

    send.side_effect = blocked
    service.add(enter())
    task = asyncio.create_task(service.flush_due())
    await started.wait()
    service.add(left(A))
    release.set()
    await task
    assert len(service.window.records) == 1
    await service.flush_due()
    assert send.await_count == 2


async def test_targets_failure_and_batch_wrapper(caplog):
    service, _, send = manager(
        targets=["bot:GroupMessage:bad", "bot:GroupMessage:false", "bot:GroupMessage:good"],
        window={"quiet_seconds": 0},
    )
    service.templates = Templates({"change_batch": "变动：{messages}"})
    send.side_effect = [RuntimeError("failed"), False, True]
    service.add(enter())
    await service.flush_due()
    assert [c.args[0] for c in send.call_args_list] == [
        "bot:GroupMessage:bad",
        "bot:GroupMessage:false",
        "bot:GroupMessage:good",
    ]
    assert all(c.args[1].startswith("变动：") for c in send.call_args_list)
    assert "bot:GroupMessage:bad" in caplog.text and "bot:GroupMessage:false" in caplog.text
    assert all(
        record.name == "astrbot.plugin.astrbot_plugin_teamspeakbot" for record in caplog.records
    )
    await service.flush_due()
    assert send.await_count == 3


async def test_stop_discards_detached_batch():
    service, _, send = manager(
        targets=["bot:GroupMessage:first", "bot:GroupMessage:second"], window={"quiet_seconds": 0}
    )

    async def disconnect(target, text):
        service.stop()
        service.resume()
        return True

    send.side_effect = disconnect
    service.add(enter())
    service.add(left())
    service.add(enter())
    await service.flush_due()
    assert send.await_count == 1
    assert service.window is None


async def test_empty_targets_disable_delivery():
    service, _, send = manager(targets=[], window={"quiet_seconds": 0})
    service.add(enter())
    await service.flush_due()
    assert service.window is None
    send.assert_not_awaited()


async def test_cancellation_keeps_global_deadline_and_order():
    service, now, send = manager()
    other = replace(USER, identity=UidIdentity(USER.identity.connection, "bob"), nickname="Bob")
    service.add(move())
    now[0] = 9
    service.add(move(B, A))
    current = A
    for timestamp in range(18, 55, 9):
        now[0] = timestamp
        destination = C if current == B else B
        service.add(move(current, destination, other))
        current = destination
        await service.flush_due()
    now[0] = 59
    service.add(move(A, C))
    now[0] = 60
    await service.flush_due()
    text = send.call_args.args[1]
    assert text.index("Alice") < text.index("Bob")
    assert service.window is None


async def test_empty_result_closes_window():
    service, now, send = manager()
    service.add(enter())
    service.add(left())
    assert service.window is not None
    now[0] = 10
    await service.flush_due()
    assert service.window is None
    send.assert_not_awaited()
    service.add(enter(B))
    assert service.window.opened_at == 10


@pytest.mark.parametrize("config", [{"enabled": False}, {"targets": []}])
async def test_disabled_does_not_open_window(config):
    service, _, send = manager(**config)
    service.add(enter())
    await service.flush_due()
    assert service.window is None
    send.assert_not_awaited()


async def test_different_user_resets_quiet_deadline():
    service, now, send = manager()
    other = replace(USER, identity=UidIdentity(USER.identity.connection, "bob"))
    service.add(enter())
    now[0] = 9
    service.add(enter(B, other))
    now[0] = 10
    await service.flush_due()
    send.assert_not_awaited()
    now[0] = 19
    await service.flush_due()
    assert send.await_count == 1
    assert len(send.call_args.args[1].splitlines()) == 2
