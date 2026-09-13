"""First/last event projection and bounded global notification windows."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from astrbot.api import logger

from .config import NotificationConfig, WindowConfig
from .templates import Templates
from .types import Change, Enter, Identity, Left, Move


@dataclass(frozen=True)
class Pending:
    first: Change
    latest: Change


def project(record: Pending) -> Change | None:
    """Project window endpoints, using the initial channel for departures."""
    first, latest = record.first, record.latest
    origin = (
        None
        if isinstance(first, Enter)
        else first.from_channel
        if isinstance(first, Move)
        else first.channel
    )
    destination = (
        None
        if isinstance(latest, Left)
        else latest.to_channel
        if isinstance(latest, Move)
        else latest.channel
    )
    if origin is None:
        return None if destination is None else Enter(latest.user, destination)
    if destination is None:
        return Left(latest.user, origin)
    if origin.id == destination.id:
        return None
    return Move(latest.user, origin, destination)


@dataclass
class Window:
    opened_at: float
    last_event_at: float
    records: dict[Identity, Pending] = field(default_factory=dict)

    def add(self, event: Change, now: float) -> None:
        self.last_event_at = now
        identity = event.user.identity
        previous = self.records.get(identity)
        self.records[identity] = Pending(event if previous is None else previous.first, event)

    def deadline(self, config: WindowConfig) -> float:
        return min(
            self.last_event_at + config.quiet_seconds,
            self.opened_at + config.max_seconds,
        )


class MessageManager:
    def __init__(
        self,
        config: NotificationConfig,
        templates: Templates,
        send: Callable[[str, str], Awaitable[bool]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.templates = templates
        self.send = send
        self.clock = clock
        self.window: Window | None = None
        self._generation = 0
        self._accepting = False

    def resume(self) -> None:
        self._accepting = True

    def stop(self) -> None:
        self._accepting = False
        self._generation += 1
        self.window = None

    def add(self, event: Change) -> None:
        if not self._accepting or not self.config.enabled or not self.config.targets:
            return
        now = self.clock()
        if self.window is None:
            self.window = Window(now, now)
        self.window.add(event, now)

    async def flush_due(self) -> None:
        window = self.window
        if not self._accepting or window is None:
            return
        if self.clock() < window.deadline(self.config.window):
            return
        generation = self._generation
        self.window = None
        batch = tuple(
            event for record in window.records.values() if (event := project(record)) is not None
        )
        enabled = {
            Enter: self.config.events.enter,
            Left: self.config.events.left,
            Move: self.config.events.move,
        }
        lines = [self.templates.event(event) for event in batch if enabled[type(event)]]
        if not lines:
            return
        text = self.templates.render("change_batch", messages="\n".join(lines))
        for target in self.config.targets:
            if not self._accepting or generation != self._generation:
                return
            try:
                if await self.send(target, text) is False:
                    logger.warning("TeamSpeak notification target returned False: %s", target)
            except Exception:
                logger.exception("Failed to send TeamSpeak notification to %s", target)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.flush_due()
