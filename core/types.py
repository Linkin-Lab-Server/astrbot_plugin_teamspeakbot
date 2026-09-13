"""Immutable connection-scoped identities, snapshots and notification events."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class UidIdentity:
    connection: UUID
    uid: str


@dataclass(frozen=True)
class SessionIdentity:
    session: UUID


type Identity = UidIdentity | SessionIdentity


@dataclass(frozen=True)
class User:
    identity: Identity
    nickname: str | None
    ip: str | None
    client_version: str | None


@dataclass(frozen=True)
class Channel:
    id: int
    name: str | None

    @property
    def label(self) -> str:
        return self.name if self.name is not None else str(self.id)


@dataclass(frozen=True)
class OnlineClient:
    user: User
    channel: Channel


@dataclass(frozen=True)
class Ready:
    clients: tuple[OnlineClient, ...]


@dataclass(frozen=True)
class Disconnected:
    pass


type ConnectionState = Ready | Disconnected


@dataclass(frozen=True)
class Enter:
    user: User
    channel: Channel


@dataclass(frozen=True)
class Left:
    user: User
    channel: Channel


@dataclass(frozen=True)
class Move:
    user: User
    from_channel: Channel
    to_channel: Channel


type Change = Enter | Left | Move
