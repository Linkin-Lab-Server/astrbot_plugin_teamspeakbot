"""Single-pass, allowlisted text templates."""

from collections import defaultdict
from collections.abc import Mapping
from string import Formatter

from astrbot.api import logger

from .types import Change, Enter, Left, Move, OnlineClient, Ready, User

DEFAULT_TEMPLATES = {
    "enter": "{nickname} ({ip}) 加入频道 {channel}，客户端版本：{client_version}。",
    "left": "{nickname} ({ip}) 从频道 {channel} 离开了。",
    "move": "{nickname} ({ip}) 从频道 {from_channel} 切换到频道 {to_channel}。",
    "change_batch": "{messages}",
    "status": "当前在线客户端数：{online_count}\n{channels}",
    "status_channel": "{channel_icon} {channel}\n{clients}",
    "status_client": "  - {nickname} ({ip})",
}
USER_VARIABLES = {"nickname", "ip", "client_version"}
VARIABLES = {
    "enter": USER_VARIABLES | {"channel"},
    "left": USER_VARIABLES | {"channel"},
    "move": USER_VARIABLES | {"from_channel", "to_channel"},
    "change_batch": {"messages"},
    "status": {"online_count", "channels"},
    "status_channel": {"channel_icon", "channel", "clients"},
    "status_client": {"nickname", "ip"},
}


class Templates:
    def __init__(self, custom: Mapping[str, str]) -> None:
        self._parts: dict[str, tuple[tuple[str, str | None], ...]] = {}
        for key in custom.keys() - DEFAULT_TEMPLATES.keys():
            logger.warning("message_templates.%s: unknown template; ignoring it", key)
        for key, default in DEFAULT_TEMPLATES.items():
            value = custom.get(key, default)
            try:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("template must not be blank")
                parts = []
                for literal, variable, spec, conversion in Formatter().parse(value):
                    if variable is not None:
                        if variable not in VARIABLES[key]:
                            raise ValueError(f"unsupported variable: {variable}")
                        if spec or conversion is not None:
                            raise ValueError(
                                "nonempty format specifications and conversions are forbidden"
                            )
                    parts.append((literal, variable))
                self._parts[key] = tuple(parts)
            except ValueError as exc:
                logger.warning("message_templates.%s: %s; using the default template", key, exc)
                self._parts[key] = tuple(
                    (literal, variable) for literal, variable, _, _ in Formatter().parse(default)
                )

    def render(self, key: str, **values: str | int | None) -> str:
        return "".join(
            literal + (self._display(values[variable]) if variable is not None else "")
            for literal, variable in self._parts[key]
        )

    @staticmethod
    def _display(value: str | int | None) -> str:
        return "未知" if value is None else str(value)

    @staticmethod
    def _user_values(user: User) -> dict[str, str | None]:
        return {"nickname": user.nickname, "ip": user.ip, "client_version": user.client_version}

    def event(self, event: Change) -> str:
        values = self._user_values(event.user)
        match event:
            case Enter(channel=channel):
                return self.render("enter", **values, channel=channel.label)
            case Left(channel=channel):
                return self.render("left", **values, channel=channel.label)
            case Move(from_channel=origin, to_channel=destination):
                return self.render(
                    "move", **values, from_channel=origin.label, to_channel=destination.label
                )

    def status(self, snapshot: Ready) -> str:
        grouped: dict[int, list[OnlineClient]] = defaultdict(list)
        for client in snapshot.clients:
            grouped[client.channel.id].append(client)
        channels = []
        for cid in sorted(grouped):
            clients = grouped[cid]
            channel = clients[0].channel
            lines = [
                self.render("status_client", nickname=client.user.nickname, ip=client.user.ip)
                for client in clients
            ]
            channels.append(
                self.render(
                    "status_channel",
                    channel_icon="😴"
                    if channel.name is not None and "AFK" in channel.name
                    else "📢",
                    channel=channel.label,
                    clients="\n".join(lines),
                )
            )
        return self.render(
            "status", online_count=len(snapshot.clients), channels="\n".join(channels)
        )
