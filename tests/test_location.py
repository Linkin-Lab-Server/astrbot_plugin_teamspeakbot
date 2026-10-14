import asyncio
import json
from email.utils import formatdate
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from core import location
from core.location import Located, LocationResolver, Unavailable, normalize_ip

CHINA = {
    "success": True,
    "country_code": "CN",
    "country": "中国",
    "region": "浙江省",
    "city": "杭州",
}


@pytest.fixture
async def resolver_factory(tmp_path):
    created = []

    def create(handler, *, clock=lambda: 1700000000.0, path=None):
        resolver = LocationResolver(
            tmp_path / "location_cache.json" if path is None else path,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            clock=clock,
        )
        created.append(resolver)
        return resolver

    yield create
    for resolver in created:
        await resolver.close()


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "bad",
        "8.8.8.8:10011",
        " 8.8.8.8 ",
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "192.0.2.1",
        "100.64.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "239.0.0.1",
        "::1",
        "::",
        "fe80::1",
        "fd00::1",
        "2001:db8::1",
        "ff02::1",
        "2606:4700:4700::1111%eth0",
    ],
)
async def test_nonpublic_or_invalid_addresses_do_not_query(resolver_factory, value):
    def forbidden(request):
        raise AssertionError("Invalid address reached HTTP transport")

    resolver = resolver_factory(forbidden)
    assert normalize_ip(value) is None
    assert await resolver.resolve(value) is None
    assert not resolver.entries


@pytest.mark.parametrize(
    "data,expected",
    [
        (CHINA, "浙江省 杭州"),
        (CHINA | {"region": "北京", "city": " 北京 "}, "北京"),
        (CHINA | {"region": None}, "杭州"),
        (CHINA | {"city": " "}, "浙江省"),
        (
            {"success": True, "country_code": "US", "country": "美国", "region": "加州"},
            "美国 加州",
        ),
        ({"success": True, "country": "新加坡", "region": "新加坡", "city": "新加坡"}, "新加坡"),
    ],
)
async def test_region_format_and_fixed_request(resolver_factory, data, expected):
    def response(request):
        assert request.url.host == "ipwho.is"
        assert request.url.scheme == "https"
        assert request.url.path == "/8.8.8.8"
        assert request.url.params["lang"] == "zh-CN"
        assert request.url.params["fields"] == "success,country_code,country,region,city"
        return httpx.Response(200, json=data)

    resolver = resolver_factory(response)
    assert await resolver.resolve("8.8.8.8") == expected
    assert isinstance(resolver.entries["8.8.8.8"], Located)


async def test_exact_addresses_and_normalized_ipv6_share_only_identical_keys(resolver_factory):
    requests = []

    def response(request):
        requests.append(request.url.path)
        city = "杭州" if request.url.path == "/8.8.8.8" else "北京"
        return httpx.Response(200, json=CHINA | {"city": city})

    resolver = resolver_factory(response)
    assert await resolver.resolve("8.8.8.8") == "浙江省 杭州"
    assert await resolver.resolve("8.8.8.9") == "浙江省 北京"
    assert await resolver.resolve("2606:4700:4700:0000:0000:0000:0000:1111") == "浙江省 北京"
    assert await resolver.resolve("2606:4700:4700::1111") == "浙江省 北京"
    assert await resolver.resolve("2606:4700:4700::1112") == "浙江省 北京"
    assert len(requests) == 4
    assert set(resolver.entries) == {
        "8.8.8.8",
        "8.8.8.9",
        "2606:4700:4700::1111",
        "2606:4700:4700::1112",
    }
    assert normalize_ip("::ffff:8.8.8.8") == "8.8.8.8"


async def test_success_has_no_time_expiration_and_survives_restart(resolver_factory):
    now = [1700000000.0]
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, json=CHINA)

    original = resolver_factory(response, clock=lambda: now[0])
    assert await original.resolve("8.8.8.8") == "浙江省 杭州"
    await original.save()
    payload = json.loads(original.path.read_text())
    assert payload["entries"]["8.8.8.8"] == {"kind": "located", "location": "浙江省 杭州"}
    now[0] += 86400 * 365
    restored = resolver_factory(response, clock=lambda: now[0])
    await restored.load()
    assert await restored.resolve("8.8.8.8") == "浙江省 杭州"
    assert len(calls) == 1


async def test_failure_retries_after_ten_minutes_and_survives_restart(resolver_factory):
    now = [1700000000.0]
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=CHINA)

    original = resolver_factory(response, clock=lambda: now[0])
    assert await original.resolve("8.8.8.8") is None
    assert original.entries["8.8.8.8"].retry_at == now[0] + 600
    await original.save()
    restored = resolver_factory(response, clock=lambda: now[0])
    await restored.load()
    assert await restored.resolve("8.8.8.8") is None
    assert len(calls) == 1
    now[0] += 600
    assert await restored.resolve("8.8.8.8") == "浙江省 杭州"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        "[]",
        '{"success":false}',
        '{"success":"true","city":"杭州"}',
        '{"success":true}',
        '{"success":true,"city":123}',
        '{"success":true,"city":" "}',
    ],
)
async def test_bad_responses_cache_temporary_failure(resolver_factory, body, caplog):
    resolver = resolver_factory(lambda request: httpx.Response(200, text=body))
    assert await resolver.resolve("8.8.8.8") is None
    assert isinstance(resolver.entries["8.8.8.8"], Unavailable)
    assert "IPWhois lookup failed" in caplog.text


@pytest.mark.parametrize(
    "retry", [None, "invalid", "nan", "-1", "120", formatdate(1700000120, usegmt=True)]
)
async def test_rate_limit_cooldown_is_persistent(resolver_factory, retry):
    now = [1700000000.0]
    calls = []

    def response(request):
        calls.append(request)
        headers = {} if retry is None else {"Retry-After": retry}
        return httpx.Response(429, headers=headers)

    original = resolver_factory(response, clock=lambda: now[0])
    assert await original.resolve("8.8.8.8") is None
    duration = 120 if retry in ("120", formatdate(1700000120, usegmt=True)) else 86400
    assert original.retry_after_until == now[0] + duration
    assert await original.resolve("8.8.4.4") is None
    await original.save()
    restored = resolver_factory(response, clock=lambda: now[0])
    await restored.load()
    assert restored.retry_after_until == now[0] + duration
    assert await restored.resolve("1.1.1.1") is None
    assert len(calls) == 1
    now[0] += duration
    assert await restored.resolve("1.1.1.1") is None
    assert len(calls) == 2


async def test_cache_capacity_uses_last_access_order(resolver_factory, monkeypatch):
    monkeypatch.setattr(location, "CACHE_LIMIT", 2)
    resolver = resolver_factory(lambda request: httpx.Response(200, json=CHINA))
    await resolver.resolve("8.8.8.8")
    await resolver.resolve("8.8.4.4")
    await resolver.resolve("8.8.8.8")
    await resolver.save()
    restored = resolver_factory(lambda request: httpx.Response(200, json=CHINA))
    await restored.load()
    await restored.resolve("1.1.1.1")
    assert list(restored.entries) == ["8.8.8.8", "1.1.1.1"]


async def test_duplicate_queries_share_one_request(resolver_factory):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def response(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json=CHINA)

    resolver = resolver_factory(response)
    queries = asyncio.gather(resolver.resolve("8.8.8.8"), resolver.resolve("8.8.8.8"))
    await asyncio.wait_for(started.wait(), 1)
    release.set()
    assert await queries == ["浙江省 杭州", "浙江省 杭州"]
    assert len(calls) == 1
    assert not resolver._pending


async def test_initial_batch_deduplicates_and_limits_concurrency(resolver_factory):
    active = 0
    peak = 0
    calls = []

    async def response(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(request)
        try:
            await asyncio.sleep(0.005)
            return httpx.Response(200, json=CHINA)
        finally:
            active -= 1

    resolver = resolver_factory(response)
    addresses = [f"8.8.8.{index}" for index in range(1, 13)]
    results = await resolver.resolve_many(addresses + addresses + [None, "127.0.0.1"])
    assert results == dict.fromkeys(addresses, "浙江省 杭州")
    assert len(calls) == 12 and peak == 4


async def test_individual_lookup_timeout_is_temporary(resolver_factory, monkeypatch):
    monkeypatch.setattr(location, "LOOKUP_TIMEOUT", 0.03)
    cancelled = asyncio.Event()

    async def response(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    resolver = resolver_factory(response)
    assert await asyncio.wait_for(resolver.resolve("8.8.8.8"), 1) is None
    assert cancelled.is_set()
    assert isinstance(resolver.entries["8.8.8.8"], Unavailable)
    assert not resolver._pending


async def test_batch_timeout_retains_success_and_cancels_remaining_queries(
    resolver_factory, monkeypatch
):
    monkeypatch.setattr(location, "LOOKUP_TIMEOUT", 0.03)
    active = 0

    async def response(request):
        nonlocal active
        if request.url.path == "/8.8.8.8":
            return httpx.Response(200, json=CHINA)
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    resolver = resolver_factory(response)
    results = await asyncio.wait_for(
        resolver.resolve_many(["8.8.8.8", "8.8.8.9", "8.8.8.10", "8.8.8.11", "8.8.8.12"]), 1
    )
    assert results["8.8.8.8"] == "浙江省 杭州"
    assert isinstance(resolver.entries["8.8.8.8"], Located)
    assert active == 0 and not resolver._pending


async def test_cancellation_propagates_and_leaves_no_failure(resolver_factory):
    started = asyncio.Event()

    async def response(request):
        started.set()
        await asyncio.Event().wait()

    resolver = resolver_factory(response)
    query = asyncio.create_task(resolver.resolve("8.8.8.8"))
    await asyncio.wait_for(started.wait(), 1)
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    assert not resolver.entries and not resolver._pending


@pytest.mark.parametrize("payload", ["{", "[]", '{"version":2,"entries":{}}'])
async def test_corrupt_cache_does_not_stop_queries(resolver_factory, payload, caplog):
    resolver = resolver_factory(lambda request: httpx.Response(200, json=CHINA))
    resolver.path.write_text(payload)
    await resolver.load()
    assert not resolver.entries
    assert "Could not load IPWhois cache" in caplog.text
    assert await resolver.resolve("8.8.8.8") == "浙江省 杭州"


async def test_load_discards_expired_invalid_and_nonpublic_entries(resolver_factory):
    resolver = resolver_factory(lambda request: httpx.Response(200, json=CHINA))
    resolver.path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "8.8.8.8": {"kind": "located", "location": "浙江省 杭州"},
                    "8.8.4.4": {"kind": "unavailable", "retry_at": 1700000000},
                    "1.1.1.1": {"kind": "located", "location": None},
                    "1.0.0.1": {"kind": "unavailable", "retry_at": None},
                    "127.0.0.1": {"kind": "located", "location": "浙江省 杭州"},
                },
                "retry_after_until": 1699999999,
            }
        )
    )
    await resolver.load()
    assert list(resolver.entries) == ["8.8.8.8"]
    assert resolver.retry_after_until is None


async def test_failed_atomic_replace_preserves_old_file_and_can_retry(
    resolver_factory, monkeypatch, caplog
):
    resolver = resolver_factory(lambda request: httpx.Response(200, json=CHINA))
    await resolver.resolve("8.8.8.8")
    await resolver.save()
    before = resolver.path.read_bytes()
    await resolver.resolve("8.8.4.4")
    with monkeypatch.context() as patch:
        patch.setattr(location.aiofiles.os, "replace", AsyncMock(side_effect=PermissionError()))
        await resolver.save()
    assert resolver.path.read_bytes() == before
    assert "Could not save IPWhois cache" in caplog.text
    assert "8.8.4.4" in resolver.entries
    await resolver.save()
    assert "8.8.4.4" in json.loads(resolver.path.read_text())["entries"]


async def test_writer_saves_within_delay_and_shutdown_flushes_changes(
    resolver_factory, monkeypatch
):
    monkeypatch.setattr(location, "SAVE_DELAY", 0.01)
    resolver = resolver_factory(lambda request: httpx.Response(200, json=CHINA))
    saved = asyncio.Event()
    original = resolver.save

    async def save():
        await original()
        saved.set()

    monkeypatch.setattr(resolver, "save", save)
    resolver.start()
    await resolver.resolve("8.8.8.8")
    await asyncio.wait_for(saved.wait(), 1)
    assert "8.8.8.8" in json.loads(resolver.path.read_text())["entries"]
    await resolver.resolve("8.8.4.4")
    await resolver.close()
    assert resolver._writer.done() and resolver.http.is_closed
    assert "8.8.4.4" in json.loads(resolver.path.read_text())["entries"]


async def test_cancelled_close_drains_writer_before_propagating(resolver_factory, monkeypatch):
    resolver = resolver_factory(lambda request: httpx.Response(200, json=CHINA))
    writing, release = asyncio.Event(), asyncio.Event()
    original = resolver.save

    async def save():
        writing.set()
        await release.wait()
        await original()

    monkeypatch.setattr(resolver, "save", save)
    resolver.start()
    await resolver.resolve("8.8.8.8")
    closing = asyncio.create_task(resolver.close())
    await asyncio.wait_for(writing.wait(), 1)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert resolver._writer.done() and resolver.http.is_closed
    assert "8.8.8.8" in json.loads(resolver.path.read_text())["entries"]


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "located", "location": None},
        {"kind": "located", "location": " "},
        {"kind": "located", "location": "杭州", "retry_at": 1700000001},
        {"kind": "unavailable", "retry_at": None},
        {"kind": "unavailable", "retry_at": 0},
        {"kind": "unavailable", "retry_at": float("nan")},
    ],
)
def test_cache_entry_types_reject_illegal_states(entry):
    with pytest.raises(ValidationError):
        location.ENTRY.validate_python(entry)
