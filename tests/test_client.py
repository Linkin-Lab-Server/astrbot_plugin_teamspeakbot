import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from core.client import QueryConnection, TeamSpeakClient
from core.config import ConnectionPolicy, NotificationConfig, ServerConfig
from core.message_manager import MessageManager
from core.templates import Templates
from core.types import Disconnected, Left, Ready


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110 -- Observe external protocol state in tests.
            await asyncio.sleep(0.001)


class FakeServer:
    def __init__(self, *, greeting=True, login_error=None, sync_event=False, chunk_size=None):
        self.greeting = greeting
        self.login_error = login_error
        self.sync_event = sync_event
        self.chunk_size = chunk_size
        self.commands = []
        self.peers = []
        self.tasks = set()
        self.clientinfo_error = 512
        self.clientinfo_events = ()
        self.disconnect_on = None
        self.client_rows = (
            b"clid=1 cid=1 client_type=0 client_nickname=Alice client_unique_identifier=uid "
            b"connection_client_ip=127.0.0.1 client_version=3.6|"
            b"clid=2 cid=1 client_type=1 client_nickname=Query"
        )

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.peers.append(writer)
        try:
            if self.greeting:
                writer.write(b"TS3\n\rWelcome. Use help for information on a specific command.\n\r")
                await writer.drain()
            while line := await reader.readline():
                self.commands.append(line)
                command = line.split()[0]
                if command == self.disconnect_on:
                    return
                if command == b"login" and self.login_error is not None:
                    writer.write(f"error id={self.login_error} msg=banned\n\r".encode())
                elif command == b"clientinfo":
                    for event in self.clientinfo_events:
                        writer.write(event + b"\n\r")
                    writer.write(f"error id={self.clientinfo_error} msg=invalid\n\r".encode())
                else:
                    if command == b"channellist":
                        writer.write(b"cid=1 channel_name=A|cid=2 channel_name=B\n\r")
                    elif command == b"clientlist":
                        if self.sync_event:
                            writer.write(
                                b"notifycliententerview clid=9 ctid=1 client_type=0 "
                                b"client_nickname=Transient client_unique_identifier=transient\n\r"
                            )
                        if self.client_rows:
                            response = self.client_rows + b"\n\r"
                            chunk_size = (
                                len(response) if self.chunk_size is None else self.chunk_size
                            )
                            for offset in range(0, len(response), chunk_size):
                                writer.write(response[offset : offset + chunk_size])
                                await writer.drain()
                    writer.write(b"error id=0 msg=ok\n\r")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)

    async def emit(self, *events):
        self.peers[-1].write(b"\n\r".join(events) + b"\n\r")
        await self.peers[-1].drain()


@asynccontextmanager
async def running_client(*, log_level="INFO", **kwargs):
    fake = FakeServer(**kwargs)
    server = await asyncio.start_server(fake.accept, "127.0.0.1", 0)
    messages = MessageManager(
        NotificationConfig(targets=["bot:GroupMessage:target"], window={"quiet_seconds": 0}),
        Templates({}),
        AsyncMock(return_value=True),
    )
    client = TeamSpeakClient(
        ServerConfig(
            host="127.0.0.1",
            port=server.sockets[0].getsockname()[1],
            username="admin",
            password="test-password",
            server_id=7,
            log_level=log_level,
        ),
        ConnectionPolicy(reconnect_interval=1, banned_retry_interval=2),
        messages,
    )
    task = asyncio.create_task(client.run(), name="teamspeak:test-service")
    try:
        yield fake, client, messages
    finally:
        client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        for peer in fake.peers:
            peer.close()
        await server.wait_closed()
        if fake.tasks:
            await asyncio.gather(*fake.tasks, return_exceptions=True)
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("teamspeak:")]


async def test_initial_sync_server_id_and_protocol_client_filter():
    async with running_client() as (fake, client, messages):
        await wait_until(lambda: isinstance(client.state, Ready))
        assert len(client.state.clients) == 1
        assert client.state.clients[0].user.nickname == "Alice"
        assert any(b"use sid=7 " in command for command in fake.commands)
        assert any(b"clientlist -uid -ip -info" in command for command in fake.commands)
        assert messages.window is None


async def test_wire_cancellation_move_and_channel_changes(caplog):
    async with running_client() as (fake, client, messages):
        await wait_until(lambda: isinstance(client.state, Ready))
        await fake.emit(
            b"notifycliententerview clid=9 ctid=1 client_type=0 "
            b"client_nickname=Visitor client_unique_identifier=visitor",
            b"notifyclientmoved clid=9 ctid=2 reasonid=0",
            b"notifyclientleftview clid=9 cfid=2 ctid=0",
        )
        await wait_until(
            lambda: (
                messages.window is not None
                and isinstance(next(iter(messages.window.records.values())).latest, Left)
            )
        )
        assert len(client.state.clients) == 1
        await messages.flush_due()
        messages.send.assert_not_awaited()
        assert messages.window is None
        await fake.emit(b"notifychanneledited cid=1 channel_name=Renamed")
        await wait_until(lambda: client.state.clients[0].channel.name == "Renamed")
        await fake.emit(b"notifyclientmoved invalid", b"notifyclientmoved clid=1 ctid=2 reasonid=0")
        await wait_until(lambda: client.state.clients[0].channel.id == 2)
        assert "Ignoring invalid" in caplog.text


async def test_disconnect_invalidates_and_reconnect_resynchronizes():
    async with running_client() as (fake, client, messages):
        await wait_until(lambda: isinstance(client.state, Ready))
        await fake.emit(b"notifyclientmoved clid=1 ctid=2 reasonid=0")
        await wait_until(lambda: messages.window is not None)
        old_identity = client.state.clients[0].user.identity
        fake.client_rows = (
            b"clid=4 cid=1 client_type=0 client_nickname=Bob client_unique_identifier=uid"
        )
        fake.peers[-1].close()
        await wait_until(lambda: isinstance(client.state, Disconnected))
        assert messages.window is None
        await wait_until(lambda: len(fake.peers) == 2 and isinstance(client.state, Ready))
        assert client.state.clients[0].user.nickname == "Bob"
        assert client.state.clients[0].user.identity != old_identity


async def test_cancel_during_greeting_cleans_socket():
    async with running_client(greeting=False) as (fake, client, _):
        await wait_until(lambda: bool(fake.peers))
        assert isinstance(client.state, Disconnected)
    assert all(peer.is_closing() for peer in fake.peers)


async def test_initial_event_failure_reaches_connection_loop(caplog):
    async with running_client(sync_event=True) as (fake, client, _):
        fake.clientinfo_error = 3329
        await wait_until(lambda: "retry in 2 seconds" in caplog.text)
        assert isinstance(client.state, Disconnected)
        await wait_until(lambda: all(peer.is_closing() for peer in fake.peers))


async def test_banned_policy_and_no_password_logging(caplog):
    async with running_client(login_error=3329) as (_, client, _):
        await wait_until(lambda: "retry in 2 seconds" in caplog.text)
        assert isinstance(client.state, Disconnected)
        assert "test-password" not in caplog.text
        assert all(
            record.name == "astrbot.plugin.astrbot_plugin_teamspeakbot" for record in caplog.records
        )


@pytest.mark.parametrize(
    "protocol_level,plugin_level,expected",
    [
        ("DEBUG", "DEBUG", True),
        ("DEBUG", "INFO", False),
        ("INFO", "DEBUG", False),
        ("WARNING", "DEBUG", False),
        ("ERROR", "DEBUG", False),
        ("CRITICAL", "DEBUG", False),
    ],
)
async def test_protocol_logs_follow_plugin_level_without_exposing_credentials(
    caplog, protocol_level, plugin_level, expected
):
    with caplog.at_level(plugin_level, logger="astrbot.plugin.astrbot_plugin_teamspeakbot"):
        async with running_client(log_level=protocol_level) as (_, client, _):
            await wait_until(lambda: isinstance(client.state, Ready))
            records = [
                record
                for record in caplog.records
                if record.getMessage().startswith("Executing ServerQuery command")
            ]
            assert bool(records) is expected
            assert all(
                record.name == "astrbot.plugin.astrbot_plugin_teamspeakbot" for record in records
            )
            assert "test-password" not in caplog.text
            assert "client_login_password" not in caplog.text
            assert "admin" not in caplog.text


async def test_initial_events_are_baseline_not_notifications():
    async with running_client(sync_event=True) as (_, client, messages):
        await wait_until(lambda: isinstance(client.state, Ready))
        assert len(client.state.clients) == 2
        assert messages.window is None


async def test_events_during_initial_clientinfo_are_drained_before_live_notifications():
    async with running_client(sync_event=True) as (fake, client, messages):
        fake.clientinfo_events = (
            b"notifyclientmoved clid=9 ctid=2 reasonid=0",
            b"notifyclientleftview clid=9 cfid=2 ctid=0",
        )
        await wait_until(lambda: isinstance(client.state, Ready))
        assert len(client.state.clients) == 1
        assert messages.window is None
        await fake.emit(b"notifyclientmoved clid=1 ctid=2 reasonid=0")
        await wait_until(lambda: messages.window is not None)
        assert client.state.clients[0].channel.id == 2
        await messages.flush_due()
        messages.send.assert_awaited_once()


@pytest.mark.parametrize("command", [b"clientlist", b"clientinfo"])
async def test_disconnect_during_initial_sync_releases_connection_promptly(command):
    async with running_client(sync_event=True) as (fake, client, messages):
        fake.disconnect_on = command
        await wait_until(lambda: any(line.split()[0] == command for line in fake.commands))
        await wait_until(lambda: client.connection is None)
        assert isinstance(client.state, Disconnected)
        assert messages.window is None
        assert all(peer.is_closing() for peer in fake.peers)


async def test_live_event_failure_uses_banned_retry_policy(caplog):
    async with running_client() as (fake, client, messages):
        await wait_until(lambda: isinstance(client.state, Ready))
        fake.clientinfo_error = 3329
        await fake.emit(
            b"notifycliententerview clid=9 ctid=1 client_type=0 "
            b"client_nickname=Visitor client_unique_identifier=visitor"
        )
        await wait_until(lambda: "retry in 2 seconds" in caplog.text)
        await wait_until(lambda: client.connection is None)
        assert isinstance(client.state, Disconnected)
        assert messages.window is None


async def test_keepalive_disconnect_releases_connection(monkeypatch):
    async def keepalive(self):
        await self.command("version")

    monkeypatch.setattr(QueryConnection, "keepalive", keepalive)
    async with running_client() as (fake, client, messages):
        fake.disconnect_on = b"version"
        await wait_until(lambda: any(line.startswith(b"version") for line in fake.commands))
        await wait_until(lambda: client.connection is None)
        assert isinstance(client.state, Disconnected)
        assert messages.window is None


async def test_empty_server_snapshot():
    async with running_client() as (fake, client, _):
        fake.client_rows = None
        await wait_until(lambda: isinstance(client.state, Ready))
        assert not client.state.clients


async def test_query_connection_close_is_idempotent():
    connection = QueryConnection(lambda: None)
    await connection.close()
    await connection.close()
    assert not connection._tasks and connection.writer is None


@pytest.mark.parametrize("chunk_size", [None, 4093])
async def test_large_client_list_synchronizes_and_preserves_response_boundary(chunk_size):
    async with running_client(chunk_size=chunk_size) as (fake, client, messages):
        fake.client_rows = b"|".join(
            (
                f"clid={index} cid=1 client_type=0 client_nickname=Client{index} "
                f"client_unique_identifier=uid{index} connection_client_ip=192.0.2.1 "
                "client_version=3.6.2 client_platform=Linux client_input_muted=0 "
                "client_output_muted=0"
            ).encode()
            for index in range(1, 513)
        )
        assert len(fake.client_rows) > 64 * 1024
        await wait_until(lambda: isinstance(client.state, Ready))
        assert len(client.state.clients) == 512
        for index, client_snapshot in enumerate(client.state.clients, start=1):
            assert client_snapshot.user.nickname == f"Client{index}"
            assert client_snapshot.user.identity.uid == f"uid{index}"
            assert client_snapshot.user.ip == "192.0.2.1"
            assert client_snapshot.user.client_version == "3.6.2"
        assert messages.window is None
        assert await client.connection.command("version") == []
        assert len(fake.peers) == 1


async def test_long_response_with_split_delimiter(monkeypatch):
    writer = Mock(spec=asyncio.StreamWriter)
    writer.wait_closed = AsyncMock()

    async def open_stream(host, port, *, limit):
        reader = asyncio.StreamReader(limit=limit)
        reader.feed_data(b"TS3\n\rWelcome. Use help for information on a specific command.\n\r")
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", open_stream)
    connection = QueryConnection(lambda: None)
    await connection.open("test-host", 10011)
    try:
        payload = b"x" * (128 * 1024)
        connection.reader.feed_data(payload + b"\n")
        await asyncio.sleep(0)
        assert connection.responses.empty()
        connection.reader.feed_data(b"\rerror id=0 msg=ok\n\r")
        assert await asyncio.wait_for(connection.responses.get(), 1) == payload
        assert await asyncio.wait_for(connection.responses.get(), 1) == b"error id=0 msg=ok"
        assert connection.alive
    finally:
        await connection.close()


def assert_connection_released(connection):
    assert not connection.alive
    assert connection.reader is None and connection.writer is None
    assert not connection._tasks


@pytest.mark.parametrize("phase", ["children", "writer"])
async def test_cancel_during_close_releases_transport_and_propagates(phase):
    connection = QueryConnection(Mock())
    writer = Mock(spec=asyncio.StreamWriter)
    writer.wait_closed = AsyncMock()
    connection.writer = writer
    connection.reader = asyncio.StreamReader()
    connection.alive = True
    connection.command_times.append(1.0)
    connection.responses.put_nowait(b"stale")
    waiting = asyncio.Event()
    started = asyncio.Event()

    async def child():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            waiting.set()
            await asyncio.Event().wait()

    async def wait_closed():
        waiting.set()
        await asyncio.Event().wait()

    child_task = None
    if phase == "children":
        child_task = connection._start(child(), "test-child")
        await started.wait()
    else:
        writer.wait_closed.side_effect = wait_closed
    closing = asyncio.create_task(connection.close())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        writer.close.assert_called_once()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        writer.transport.abort.assert_called_once()
        assert_connection_released(connection)
        if child_task is not None:
            assert child_task.done()
        await connection.close()
        writer.close.assert_called_once()
    finally:
        closing.cancel()
        if child_task is not None:
            child_task.cancel()
        await asyncio.gather(
            closing, *(() if child_task is None else (child_task,)), return_exceptions=True
        )


@pytest.mark.parametrize("error", [OSError("closed"), TimeoutError("close timed out")])
async def test_close_error_aborts_transport(error):
    connection = QueryConnection(lambda: None)
    writer = Mock(spec=asyncio.StreamWriter)
    writer.wait_closed = AsyncMock(side_effect=error)
    connection.writer = writer
    await connection.close()
    writer.close.assert_called_once()
    writer.transport.abort.assert_called_once()
    assert_connection_released(connection)


async def test_stop_during_failed_connection_cleanup_clears_reference(monkeypatch):
    writer = Mock(spec=asyncio.StreamWriter)
    writer.wait_closed = AsyncMock()
    started, cleaning = asyncio.Event(), asyncio.Event()
    connections = []

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await asyncio.Event().wait()

    async def open_connection(self, host, port):
        connections.append(self)
        self.writer = writer
        self.alive = True
        self._start(worker(), "test-failure-worker")
        await started.wait()

    monkeypatch.setattr(QueryConnection, "open", open_connection)
    monkeypatch.setattr(
        QueryConnection, "synchronize", AsyncMock(side_effect=ConnectionError("command failed"))
    )
    messages = MessageManager(NotificationConfig(), Templates({}), AsyncMock())
    client = TeamSpeakClient(
        ServerConfig(username="admin", password="test"), ConnectionPolicy(), messages
    )
    running = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        client.stop()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert client.connection is None
        assert isinstance(client.state, Disconnected)
        assert len(connections) == 1
        assert_connection_released(connections[0])
        writer.close.assert_called_once()
        writer.transport.abort.assert_called_once()
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("teamspeak:")]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
