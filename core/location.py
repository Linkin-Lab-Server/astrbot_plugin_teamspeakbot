"""Exact-address location lookups with an asynchronous persistent cache."""

import asyncio
import json
import math
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from email.utils import parsedate_to_datetime
from ipaddress import IPv6Address, ip_address
from pathlib import Path
from typing import Annotated, Literal

import aiofiles
import aiofiles.os
import httpx
from astrbot.api import logger
from pydantic import Field, field_validator

from .config import LocationProvider, StrictConfig

LOOKUP_TIMEOUT = 3
CACHE_LIMIT = 2048
FAILURE_SECONDS = 600
SAVE_DELAY = 1
IPYOU_INTERVAL = 1


class Located(StrictConfig):
    kind: Literal["located"] = "located"
    location: str

    @field_validator("location")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("location must not be blank")
        return value.strip()


class Unavailable(StrictConfig):
    kind: Literal["unavailable"] = "unavailable"
    retry_at: Annotated[float, Field(gt=0, allow_inf_nan=False)]


type CacheEntry = Located | Unavailable


def normalize_ip(value: str | None) -> str | None:
    """Normalize a public unicast address without grouping neighboring addresses.

    Args:
        value: Address reported by ServerQuery.

    Returns:
        Canonical address, or None for an invalid or nonpublic address.
    """
    if value is None or "%" in value:
        return None
    try:
        address = ip_address(value)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_global or address.is_multicast or address.is_reserved:
        return None
    return str(address)


class LocationResolver:
    def __init__(
        self,
        path: Path | None,
        *,
        provider: LocationProvider = "ipyou.net",
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.provider = provider
        self.http = http if http is not None else httpx.AsyncClient(timeout=LOOKUP_TIMEOUT)
        self.clock = clock
        self.entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self.retry_after_until: float | None = None
        self._slots = asyncio.Semaphore(1 if provider == "ipyou.net" else 4)
        self._next_request_at: float | None = None
        self._pending: dict[str, asyncio.Task[str | None]] = {}
        self._dirty = asyncio.Event()
        self._stopping = asyncio.Event()
        self._writer: asyncio.Task[None] | None = None

    async def clear(self) -> None:
        """Reset lookup state and overwrite the file before starting any workers."""
        self.entries.clear()
        self.retry_after_until = None
        self._next_request_at = None
        await self.save()

    def start(self) -> None:
        self._writer = asyncio.create_task(self.run(), name="teamspeak:location-cache")

    async def resolve(self, value: str | None) -> str | None:
        """Return an exact-address cache hit or await one bounded lookup.

        Args:
            value: Client IP address; absent addresses are represented by None.

        Returns:
            Region label, or None if the address cannot be resolved.
        """
        key = normalize_ip(value)
        if key is None or self._stopping.is_set():
            return None
        entry = self.entries.get(key)
        if entry is not None:
            if isinstance(entry, Located) or entry.retry_at > self.clock():
                self.entries.move_to_end(key)
                self._dirty.set()
                return entry.location if isinstance(entry, Located) else None
            del self.entries[key]
            self._dirty.set()
        if self.retry_after_until is not None and self.clock() < self.retry_after_until:
            return None
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._lookup(key), name="teamspeak:location-lookup")
            self._pending[key] = task
        try:
            return await task
        finally:
            if task.done() and self._pending.get(key) is task:
                del self._pending[key]

    async def resolve_many(self, values: Iterable[str | None]) -> dict[str, str | None]:
        """Resolve unique public addresses within one shared startup deadline.

        Args:
            values: Addresses from the initial voice-client snapshot.

        Returns:
            Labels for completed addresses; unfinished queries are cancelled.
        """
        keys = dict.fromkeys(key for value in values if (key := normalize_ip(value)) is not None)
        tasks = {key: asyncio.create_task(self.resolve(key)) for key in keys}
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT):
                await asyncio.gather(*tasks.values())
        except TimeoutError:
            pass
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {
            key: task.result()
            for key, task in tasks.items()
            if not task.cancelled() and task.exception() is None
        }

    async def _lookup(self, key: str) -> str | None:
        """Query the selected provider and capture a success or temporary failure.

        Args:
            key: Canonical public address.

        Returns:
            Formatted region label, or None on failure or service cooldown.
        """
        entry: CacheEntry = Unavailable(retry_at=self.clock() + FAILURE_SECONDS)
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT), self._slots:
                if self.retry_after_until is not None and self.clock() < self.retry_after_until:
                    return None
                if self.provider == "ipyou.net":
                    if self._next_request_at is not None:
                        await asyncio.sleep(max(0, self._next_request_at - time.monotonic()))
                    if self.retry_after_until is not None and self.clock() < self.retry_after_until:
                        return None
                    self._next_request_at = time.monotonic() + IPYOU_INTERVAL
                    response = await self.http.get(
                        f"https://ipyou.net/{key}", headers={"Accept": "application/json"}
                    )
                else:
                    response = await self.http.get(
                        f"https://ipwho.is/{key}",
                        params={
                            "lang": "zh-CN",
                            "fields": "success,country_code,country,region,city",
                        },
                    )
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After")
                    until = self.clock() + (60 if self.provider == "ipyou.net" else 86400)
                    if retry is not None:
                        try:
                            seconds = float(retry)
                            if not math.isfinite(seconds) or seconds < 0:
                                raise ValueError("invalid Retry-After")
                            until = self.clock() + seconds
                        except ValueError:
                            try:
                                timestamp = parsedate_to_datetime(retry).timestamp()
                                if timestamp > self.clock():
                                    until = timestamp
                            except (ValueError, TypeError, OverflowError):
                                pass
                    self.retry_after_until = until
                    self._dirty.set()
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("invalid location response")
                if self.provider == "ipyou.net":
                    address = data.get("ip")
                    if (
                        not isinstance(address, str)
                        or normalize_ip(address) != key
                        or not isinstance(data.get("geo"), dict)
                    ):
                        raise ValueError("unsuccessful ipyou response")
                    data = data["geo"]
                    region_field = "province"
                else:
                    if data.get("success") is not True:
                        raise ValueError("unsuccessful IPWhois response")
                    region_field = "region"
                country_code = data.get("country_code")
                if country_code is not None and not isinstance(country_code, str):
                    raise ValueError("invalid location country code")
                parts = []
                fields = (
                    (region_field, "city")
                    if country_code is not None and country_code.upper() == "CN"
                    else ("country", region_field, "city")
                )
                for field in fields:
                    value = data.get(field)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError("invalid location region field")
                        if (label := value.strip()) and label not in parts:
                            parts.append(label)
                if not parts:
                    raise ValueError("empty location region")
                entry = Located(location=" ".join(parts))
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError) as exc:
            logger.warning(
                "%s lookup failed (%s); temporarily caching failure",
                self.provider,
                type(exc).__name__,
            )
            entry = Unavailable(retry_at=self.clock() + FAILURE_SECONDS)
        self.entries[key] = entry
        self.entries.move_to_end(key)
        if len(self.entries) > CACHE_LIMIT:
            self.entries.popitem(last=False)
        self._dirty.set()
        return entry.location if isinstance(entry, Located) else None

    async def save(self) -> None:
        """Save a snapshot by atomically replacing a same-directory temporary file.

        The single writer owns all saves, including the final shutdown save.
        """
        self._dirty.clear()
        if self.path is None:
            return
        temporary = self.path.with_name(self.path.name + ".tmp")
        now = self.clock()
        payload = {
            "version": 1,
            "provider": self.provider,
            "entries": {
                key: entry.model_dump()
                for key, entry in self.entries.items()
                if isinstance(entry, Located) or entry.retry_at > now
            },
            "retry_after_until": self.retry_after_until
            if self.retry_after_until is not None and self.retry_after_until > now
            else None,
        }
        try:
            async with aiofiles.open(temporary, "w", encoding="utf-8") as file:
                await file.write(json.dumps(payload, ensure_ascii=False, allow_nan=False))
            await aiofiles.os.replace(temporary, self.path)
        except OSError:
            logger.warning("Could not save location cache; continuing with memory cache")

    async def run(self) -> None:
        while True:
            await self._dirty.wait()
            if not self._stopping.is_set():
                try:
                    await asyncio.wait_for(self._stopping.wait(), SAVE_DELAY)
                except TimeoutError:
                    pass
            await self.save()
            if self._stopping.is_set():
                return

    async def close(self) -> None:
        """Drain queries and the writer before closing the HTTP client.

        A cancelled owner still waits for the file writer to finish, preventing
        an old plugin instance from replacing a new instance's cache file.
        """
        self._stopping.set()
        tasks = tuple(self._pending.values())
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._dirty.set()
            try:
                if self._writer is None:
                    await self.save()
                else:
                    try:
                        await asyncio.shield(self._writer)
                    except asyncio.CancelledError:
                        await self._writer
                        raise
            finally:
                await self.http.aclose()
