"""Managed ServerQuery transport using the pinned dependency's protocol codecs.

The upstream Client starts unowned tasks before connecting, does not await event
callbacks on shutdown, and discards fields needed for client and channel changes.
This adapter owns transport tasks and preserves wire fields without patching globals.
"""

import asyncio
import time
from collections import deque
from collections.abc import Callable
from uuid import uuid4

from astrbot.api import logger
from ts_async_api.server_query.cmd.base import CmdRes
from ts_async_api.server_query.exception import CmdException
from ts_async_api.server_query.msg import parse_msg
from ts_async_api.server_query.utils import escape, unescape

from .config import ConnectionPolicy, ServerConfig
from .event_handler import EventHandler, Fields
from .message_manager import MessageManager
from .types import ConnectionState, Disconnected, Ready

QUERY_READ_LIMIT = 4 * 1024 * 1024


def decode_fields(payload: bytes) -> dict[str, str | None]:
    """Decode wire values, normalizing explicitly empty fields at the boundary.

    Args:
        payload: One ServerQuery record, without the command or event prefix.

    Returns:
        Decoded field values; absent content is represented by None.

    Raises:
        ValueError: The record has invalid syntax.
    """
    normalized = b" ".join(
        token[:-1] if token.endswith(b"=") and token.count(b"=") == 1 else token
        for token in payload.split()
    )
    parsed = parse_msg(normalized)
    if parsed is None:
        raise ValueError("invalid ServerQuery record")
    return {
        key: unescape(value).decode("utf-8") if value is not None else None
        for key, value in parsed.items()
    }


class QueryConnection:
    """Own one transport and its tasks; reconnection requires a new instance."""

    def __init__(self, invalidate: Callable[[], None], *, log_commands: bool = False) -> None:
        self.invalidate = invalidate
        self.log_commands = log_commands
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.responses: asyncio.Queue[bytes | Exception] = asyncio.Queue()
        self.events: asyncio.Queue[tuple[str, dict[str, str | None]]] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        self.command_lock = asyncio.Lock()
        self.command_times: deque[float] = deque()
        self.alive = False

    def _start(self, coroutine, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=f"teamspeak:{name}")
        self._tasks.add(task)
        return task

    async def open(self, host: str, port: int) -> None:
        async with asyncio.timeout(30):
            self.reader, self.writer = await asyncio.open_connection(
                host, port, limit=QUERY_READ_LIMIT
            )
            await self.reader.readuntil(b"specific command.\n\r")
        self.alive = True
        self._start(self.receive(), "receive")

    async def run(self, config: ServerConfig, handler: EventHandler) -> None:
        """Supervise synchronization and the subsequent transport workers.

        Args:
            config: ServerQuery connection and login settings.
            handler: Connection-scoped state and notification handler.

        Raises:
            ConnectionError: A transport worker stops unexpectedly.
            CmdException: A ServerQuery command fails.
        """
        await self.open(config.host, config.port)
        sync = self._start(self.synchronize(config, handler), "synchronize")
        done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if sync not in done or not self.alive:
            raise ConnectionError("TeamSpeak disconnected during synchronization")
        self._tasks.remove(sync)
        self._start(self.dispatch(handler), "events")
        self._start(self.keepalive(), "keepalive")
        done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise ConnectionError("TeamSpeak transport stopped")

    async def synchronize(self, config: ServerConfig, handler: EventHandler) -> None:
        await self.command(
            "login", client_login_name=config.username, client_login_password=config.password
        )
        await self.command("use", sid=config.server_id, client_nickname=config.client_nickname)
        await self.command("servernotifyregister", event="server")
        await self.command("servernotifyregister", event="channel", id=0)
        channels = await self.command("channellist")
        clients = await self.command("clientlist", flags=("uid", "ip", "info"))
        handler.synchronize(channels, clients)
        # Replay startup events as baseline while the receive task handles query responses.
        while not self.events.empty():
            name, fields = self.events.get_nowait()
            await handler.handle(name, fields, self.query_info)
        if not self.alive:
            raise ConnectionError("TeamSpeak disconnected during synchronization")
        handler.ready = True
        handler.publish(handler.snapshot())
        logger.info("TeamSpeak initial synchronization completed")

    async def receive(self) -> None:
        assert self.reader is not None
        try:
            while True:
                line = (await self.reader.readuntil(b"\n\r"))[:-2].strip()
                if line.startswith(b"notify"):
                    try:
                        name, payload = line.split(b" ", 1)
                        self.events.put_nowait((name.decode("ascii"), decode_fields(payload)))
                    except Exception:
                        logger.exception("Ignoring malformed TeamSpeak notification")
                elif line:
                    self.responses.put_nowait(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.alive = False
            self.invalidate()
            self.responses.put_nowait(ConnectionError("TeamSpeak connection lost"))
            raise

    async def command(
        self, name: str, *, flags: tuple[str, ...] = (), **arguments: str | int
    ) -> list[dict[str, str | None]]:
        assert self.writer is not None
        async with self.command_lock:
            async with asyncio.timeout(30):
                now = time.monotonic()
                while self.command_times and now - self.command_times[0] >= 3:
                    self.command_times.popleft()
                if len(self.command_times) >= 9:
                    await asyncio.sleep(max(0, 3 - (now - self.command_times[0])))
                    self.command_times.popleft()
                self.command_times.append(time.monotonic())
                parts = [name.encode()]
                parts.extend(
                    key.encode() + b"=" + escape(str(value).encode())
                    for key, value in arguments.items()
                )
                parts.extend(b"-" + flag.encode() for flag in flags)
                # Never log command arguments: login contains the password.
                if self.log_commands:
                    logger.debug("Executing ServerQuery command %s", name)
                self.writer.write(b" ".join(parts) + b"\n")
                await self.writer.drain()
                rows = []
                while True:
                    response = await self.responses.get()
                    if isinstance(response, Exception):
                        raise response
                    if response.startswith(b"error "):
                        result = CmdRes.from_payload(response[len(b"error ") :])
                        if not result:
                            raise CmdException(name, result)
                        return [decode_fields(row) for row in rows]
                    # Consume the terminator before decoding so malformed data cannot
                    # leave a response queued for the next command.
                    rows.extend(response.split(b"|"))

    async def query_info(self, clid: int) -> Fields | None:
        try:
            rows = await self.command("clientinfo", clid=clid)
            return rows[0] if rows else None
        except CmdException as exc:
            if exc.res.id == 512:
                return None
            raise

    async def dispatch(self, handler: EventHandler) -> None:
        while True:
            name, fields = await self.events.get()
            await handler.handle(name, fields, self.query_info)

    async def keepalive(self) -> None:
        while True:
            await asyncio.sleep(10)
            await self.command("version")

    async def close(self) -> None:
        self.alive = False
        self.invalidate()
        writer, tasks = self.writer, tuple(self._tasks)
        try:
            # Release the socket before cancellation can interrupt an await.
            if writer is not None:
                writer.close()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if writer is not None:
                async with asyncio.timeout(5):
                    await writer.wait_closed()
        except asyncio.CancelledError:
            if writer is not None:
                writer.transport.abort()
            raise
        except (OSError, TimeoutError):
            if writer is not None:
                writer.transport.abort()
        finally:
            self.writer = None
            self.reader = None
            self._tasks.clear()


class TeamSpeakClient:
    def __init__(
        self, config: ServerConfig, policy: ConnectionPolicy, messages: MessageManager
    ) -> None:
        self.config = config
        self.policy = policy
        self.messages = messages
        self.state: ConnectionState = Disconnected()
        self.connection: QueryConnection | None = None
        self._running = False

    def invalidate(self) -> None:
        self.state = Disconnected()
        self.messages.stop()

    def publish(self, state: Ready) -> None:
        if self._running and self.connection is not None and self.connection.alive:
            self.state = state
            self.messages.resume()

    async def run(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            while self._running:
                retry = self.policy.reconnect_interval
                connection = QueryConnection(
                    self.invalidate, log_commands=self.config.log_level == "DEBUG"
                )
                self.connection = connection
                handler = EventHandler(uuid4(), self.messages.add, self.publish)
                try:
                    await connection.run(self.config, handler)
                except asyncio.CancelledError:
                    raise
                except CmdException as exc:
                    if exc.res.id == 3329:
                        retry = self.policy.banned_retry_interval
                    logger.warning(
                        "TeamSpeak command failed: error %s; retry in %s seconds", exc.res.id, retry
                    )
                except Exception:
                    logger.exception("TeamSpeak connection failed; retry in %s seconds", retry)
                finally:
                    handler.ready = False
                    self.invalidate()
                    try:
                        await connection.close()
                    finally:
                        self.connection = None
                if self._running:
                    await asyncio.sleep(retry)
        finally:
            self._running = False
            self.invalidate()

    def stop(self) -> None:
        self._running = False
        self.invalidate()
