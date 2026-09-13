from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.client import decode_fields
from core.event_handler import EventHandler
from core.types import Channel, Enter, Left, Move, SessionIdentity


@pytest.fixture
def handler():
    emitted, snapshots = [], []
    handler = EventHandler(uuid4(), emitted.append, snapshots.append)
    handler.synchronize([{"cid": "1", "channel_name": "A"}, {"cid": "2", "channel_name": "B"}], [])
    handler.ready = True
    return handler, emitted, snapshots


def fields(**overrides):
    return {
        "clid": "5",
        "ctid": "1",
        "client_type": "0",
        "client_nickname": "Alice",
        "client_unique_identifier": "uid",
        **overrides,
    }


async def test_capture_before_removal_and_channel_rename(handler):
    service, emitted, _ = handler
    query = AsyncMock(
        return_value={"client_unique_identifier": "uid", "connection_client_ip": "ip"}
    )
    await service.handle("notifycliententerview", fields(), query)
    await service.handle("notifychanneledited", {"cid": "1", "channel_name": "New"}, query)
    await service.handle("notifyclientmoved", {"clid": "5", "ctid": "2"}, query)
    await service.handle("notifyclientleftview", {"clid": "5", "cfid": "2"}, query)
    assert isinstance(emitted[0], Enter) and emitted[0].channel == Channel(1, "A")
    assert isinstance(emitted[1], Move) and emitted[1].from_channel == Channel(1, "New")
    assert isinstance(emitted[2], Left) and emitted[2].channel == Channel(2, "B")
    assert emitted[2].user.ip == "ip"
    assert not service.clients


async def test_rapid_visit_keeps_wire_identity_if_client_already_gone(handler):
    service, emitted, _ = handler
    query = AsyncMock(return_value=None)
    await service.handle("notifycliententerview", fields(), query)
    await service.handle("notifyclientleftview", {"clid": "5", "cfid": "1"}, query)
    assert len(emitted) == 2
    assert emitted[0].user.nickname == "Alice"
    assert emitted[0].user.identity == emitted[1].user.identity


async def test_do_not_enrich_from_reused_id(handler):
    service, emitted, _ = handler
    query = AsyncMock(
        return_value={"client_unique_identifier": "other", "connection_client_ip": "wrong"}
    )
    await service.handle("notifycliententerview", fields(), query)
    assert emitted[0].user.ip is None
    assert emitted[0].user.identity.uid == "uid"


async def test_repeated_uid_refreshes_snapshot_without_duplicate_enter(handler):
    service, emitted, snapshots = handler
    query = AsyncMock(return_value=None)
    await service.handle("notifycliententerview", fields(), query)
    await service.handle(
        "notifycliententerview", fields(client_nickname="Renamed", ctid="2"), query
    )
    assert len(emitted) == 1
    assert snapshots[-1].clients[0].user.nickname == "Renamed"
    assert snapshots[-1].clients[0].channel == Channel(2, "B")
    assert snapshots[-1].clients[0].user.identity == emitted[0].user.identity


async def test_missing_uid_session_reuse(handler):
    service, emitted, _ = handler
    query = AsyncMock()
    await service.handle("notifycliententerview", fields(client_unique_identifier=None), query)
    await service.handle("notifyclientleftview", {"clid": "5"}, query)
    await service.handle("notifycliententerview", fields(client_unique_identifier=None), query)
    assert isinstance(emitted[0].user.identity, SessionIdentity)
    assert emitted[0].user.identity != emitted[2].user.identity
    query.assert_not_awaited()


async def test_channel_create_edit_delete_missing_cache(handler):
    service, emitted, snapshots = handler
    query = AsyncMock(return_value=None)
    await service.handle("notifycliententerview", fields(ctid="9"), query)
    assert snapshots[-1].clients[0].channel.label == "9"
    await service.handle("notifychannelcreated", {"cid": "9", "channel_name": "New"}, query)
    assert snapshots[-1].clients[0].channel.label == "New"
    await service.handle("notifychanneledited", {"cid": "9", "channel_topic": "topic"}, query)
    assert snapshots[-1].clients[0].channel.label == "New"
    await service.handle("notifychanneldeleted", {"cid": "9"}, query)
    assert snapshots[-1].clients[0].channel.label == "9"
    assert emitted[0].channel.name is None


async def test_malformed_event_does_not_block_next(handler, caplog):
    service, emitted, _ = handler
    query = AsyncMock(return_value=None)
    await service.handle("notifyclientmoved", {"clid": "bad", "ctid": "2"}, query)
    await service.handle("notifyclientleftview", {"clid": "999"}, query)
    await service.handle("notifycliententerview", fields(), query)
    assert len(emitted) == 1
    assert "Ignoring invalid" in caplog.text
    assert all(
        record.name == "astrbot.plugin.astrbot_plugin_teamspeakbot" for record in caplog.records
    )


async def test_query_clients_filtered_by_type_not_nickname(handler):
    service, emitted, _ = handler
    query = AsyncMock(return_value=None)
    await service.handle("notifycliententerview", fields(client_type="1"), query)
    assert not emitted and not service.clients
    await service.handle(
        "notifycliententerview", fields(client_nickname="AstrBot TS Monitor"), query
    )
    assert len(service.clients) == 1


def test_initial_snapshot_filters_query_clients(handler):
    service, _, _ = handler
    service.synchronize(
        [], [{**fields(), "cid": "1"}, {**fields(clid="8", client_type="1"), "cid": "1"}]
    )
    assert len(service.snapshot().clients) == 1


def test_wire_decoding_retains_uid_padding_and_empty_values():
    assert decode_fields(rb"client_unique_identifier=YWJj== client_nickname=Alice\sBob ip=") == {
        "client_unique_identifier": "YWJj==",
        "client_nickname": "Alice Bob",
        "ip": None,
    }


async def test_invalid_enrichment_does_not_drop_valid_enter(handler):
    service, emitted, _ = handler
    query = AsyncMock(side_effect=ValueError("malformed clientinfo"))
    await service.handle("notifycliententerview", fields(), query)
    await service.handle("notifyclientleftview", {"clid": "5", "cfid": "1"}, query)
    assert len(emitted) == 2
    assert emitted[0].user.nickname == "Alice"
    assert emitted[0].user.ip is None
    assert not service.clients


async def test_new_enter_without_uid_always_starts_new_session(handler):
    service, emitted, _ = handler
    query = AsyncMock()
    await service.handle("notifycliententerview", fields(client_unique_identifier=None), query)
    await service.handle("notifycliententerview", fields(client_unique_identifier=None), query)
    assert len(emitted) == 2
    assert emitted[0].user.identity != emitted[1].user.identity
