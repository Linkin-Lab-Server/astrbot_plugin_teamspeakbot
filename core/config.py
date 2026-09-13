"""Strict configuration parsing without legacy configuration migration."""

from collections.abc import Mapping
from typing import Annotated

from astrbot.api.platform import MessageType
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PositiveInt = Annotated[int, Field(gt=0)]


class StrictConfig(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class ServerConfig(StrictConfig):
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 10011
    username: str
    password: str = Field(repr=False)
    server_id: PositiveInt = 1
    client_nickname: str = "AstrBot TS Monitor"
    log_level: str = "INFO"

    @field_validator("host", "username", "password", "client_nickname")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("log_level")
    @classmethod
    def valid_level(cls, value: str) -> str:
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("invalid log level")
        return value


class ConnectionPolicy(StrictConfig):
    reconnect_interval: PositiveInt = 5
    banned_retry_interval: PositiveInt = 120


class ChangeEvents(StrictConfig):
    enter: bool = True
    left: bool = True
    move: bool = True


class WindowConfig(StrictConfig):
    quiet_seconds: Annotated[int, Field(ge=0)] = 10
    max_seconds: PositiveInt = 60

    @model_validator(mode="after")
    def valid_window(self) -> "WindowConfig":
        if self.quiet_seconds >= self.max_seconds:
            raise ValueError("quiet_seconds must be less than max_seconds")
        return self


class NotificationConfig(StrictConfig):
    enabled: bool = True
    targets: tuple[str, ...] = ()
    window: WindowConfig = WindowConfig()
    events: ChangeEvents = ChangeEvents()

    @field_validator("targets", mode="before")
    @classmethod
    def valid_targets(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("must be a list of UMO strings")
        for index, target in enumerate(value):
            if not isinstance(target, str):
                raise ValueError(f"item {index} must be a UMO string")
            # Session IDs may contain colons, including platform-specific thread IDs.
            parts = target.split(":", 2)
            if len(parts) != 3 or any(not part or part != part.strip() for part in parts):
                raise ValueError(
                    f"item {index} must be platform_id:message_type:session_id "
                    "with nonblank parts and no surrounding whitespace"
                )
            try:
                MessageType(parts[1])
            except ValueError:
                raise ValueError(f"item {index} has an unsupported message type") from None
        return tuple(dict.fromkeys(value))


class PluginConfig(StrictConfig):
    teamspeak_server: ServerConfig
    connection_policy: ConnectionPolicy = ConnectionPolicy()
    notification: NotificationConfig = NotificationConfig()
    message_templates: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def parse(cls, config: Mapping) -> "PluginConfig":
        """Validate configuration without including secret input values in errors.

        Args:
            config: AstrBot-managed plugin configuration.

        Returns:
            Validated configuration.

        Raises:
            ValueError: A configuration path has an invalid value.
        """
        try:
            return cls.model_validate(dict(config))
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)
            )
            raise ValueError(errors) from None
