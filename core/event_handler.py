"""Capture protocol events as immutable user and channel snapshots."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from uuid import UUID, uuid4

from astrbot.api import logger
from ts_async_api.server_query.exception import CmdException

from .types import (
    Change,
    Channel,
    Enter,
    Left,
    Move,
    OnlineClient,
    Ready,
    SessionIdentity,
    UidIdentity,
    User,
)

type Fields = Mapping[str, str | None]


def positive_id(fields: Fields, key: str) -> int:
    value = fields.get(key)
    if value is None or int(value) <= 0:
        raise ValueError(f"missing or invalid {key}")
    return int(value)


class EventHandler:
    def __init__(
        self,
        connection: UUID,
        notify: Callable[[Change], None],
        publish: Callable[[Ready], None],
    ) -> None:
        self.connection = connection
        self.notify = notify
        self.publish = publish
        self.clients: dict[int, OnlineClient] = {}
        self.channels: dict[int, Channel] = {}
        self.ready = False

    def channel(self, cid: int) -> Channel:
        return self.channels.get(cid, Channel(cid, None))

    def user(self, fields: Fields) -> User:
        uid = fields.get("client_unique_identifier")
        identity = (
            UidIdentity(self.connection, uid) if uid is not None else SessionIdentity(uuid4())
        )
        return User(
            identity,
            fields.get("client_nickname"),
            fields.get("connection_client_ip"),
            fields.get("client_version"),
        )

    def synchronize(self, channels: list[Fields], clients: list[Fields]) -> None:
        for fields in channels:
            cid = positive_id(fields, "cid")
            self.channels[cid] = Channel(cid, fields.get("channel_name"))
        for fields in clients:
            if fields.get("client_type") != "0":
                continue
            clid, cid = positive_id(fields, "clid"), positive_id(fields, "cid")
            self.clients[clid] = OnlineClient(self.user(fields), self.channel(cid))

    def snapshot(self) -> Ready:
        return Ready(tuple(self.clients[clid] for clid in sorted(self.clients)))

    async def handle(
        self,
        name: str,
        fields: Fields,
        query_info: Callable[[int], Awaitable[Fields | None]],
    ) -> None:
        """Update state before emitting a captured notification.

        Args:
            name: ServerQuery notification name.
            fields: Decoded fields preserved from the wire notification.
            query_info: Optional information lookup for an entering client.
        """
        try:
            change: Change | None = None
            if name == "notifycliententerview":
                clid, cid = positive_id(fields, "clid"), positive_id(fields, "ctid")
                if fields.get("client_type") != "0":
                    return
                data = dict(fields)
                uid = fields.get("client_unique_identifier")
                # Wire identity is authoritative: clientinfo may already describe a reused ID.
                if uid is not None:
                    try:
                        info = await query_info(clid)
                    except CmdException as exc:
                        if exc.res.id == 3329:
                            raise
                        logger.warning(
                            "Could not enrich TeamSpeak client %s: error %s", clid, exc.res.id
                        )
                        info = None
                    except (ConnectionError, TimeoutError):
                        raise
                    except Exception:
                        logger.exception(
                            "Ignoring invalid clientinfo for TeamSpeak client %s", clid
                        )
                        info = None
                    if info is not None and info.get("client_unique_identifier") == uid:
                        data = {**info, **fields}
                user = self.user(data)
                previous = self.clients.get(clid)
                channel = self.channel(cid)
                self.clients[clid] = OnlineClient(user, channel)
                if previous is None or previous.user.identity != user.identity:
                    change = Enter(user, channel)
            elif name == "notifyclientleftview":
                clid = positive_id(fields, "clid")
                previous = self.clients.get(clid)
                if previous is not None:
                    source = fields.get("cfid")
                    channel = (
                        self.channel(int(source))
                        if source is not None and int(source) > 0
                        else previous.channel
                    )
                    change = Left(previous.user, channel)
                    del self.clients[clid]
            elif name == "notifyclientmoved":
                clid, cid = positive_id(fields, "clid"), positive_id(fields, "ctid")
                previous = self.clients.get(clid)
                if previous is not None:
                    channel = self.channel(cid)
                    self.clients[clid] = OnlineClient(previous.user, channel)
                    if previous.channel.id != cid:
                        change = Move(previous.user, previous.channel, channel)
            elif name in {"notifychannelcreated", "notifychanneledited", "notifychanneldeleted"}:
                cid = positive_id(fields, "cid")
                if name == "notifychanneldeleted":
                    self.channels.pop(cid, None)
                elif "channel_name" in fields or name == "notifychannelcreated":
                    self.channels[cid] = Channel(cid, fields.get("channel_name"))
                for clid, client in self.clients.items():
                    if client.channel.id == cid:
                        self.clients[clid] = replace(client, channel=self.channel(cid))
            else:
                return
            if self.ready:
                self.publish(self.snapshot())
                if change is not None:
                    self.notify(change)
        except (ConnectionError, TimeoutError, CmdException):
            raise
        except Exception:
            logger.exception("Ignoring invalid TeamSpeak event %s", name)
