"""AstrBot lifecycle and passive TeamSpeak status command."""

import asyncio
from dataclasses import dataclass

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

from .core.client import TeamSpeakClient
from .core.config import PluginConfig
from .core.message_manager import MessageManager
from .core.templates import Templates
from .core.types import Ready


@dataclass(frozen=True)
class Runtime:
    settings: PluginConfig
    templates: Templates
    messages: MessageManager
    client: TeamSpeakClient
    tasks: tuple[asyncio.Task[None], asyncio.Task[None]]


class TeamSpeakBotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.raw_config = config
        self.runtime: Runtime | None = None

    async def initialize(self) -> None:
        await self.terminate()
        try:
            settings = PluginConfig.parse(self.raw_config)
            templates = Templates(settings.message_templates)
        except ValueError as exc:
            logger.error(
                "TeamSpeak configuration rejected: %s; update it in WebUI and reload the plugin.",
                exc,
            )
            return
        messages = MessageManager(settings.notification, templates, self.send_notification)
        client = TeamSpeakClient(settings.teamspeak_server, settings.connection_policy, messages)
        self.runtime = Runtime(
            settings,
            templates,
            messages,
            client,
            (
                asyncio.create_task(client.run(), name="teamspeak:connection"),
                asyncio.create_task(messages.run(), name="teamspeak:notifications"),
            ),
        )

    async def send_notification(self, target: str, text: str) -> bool:
        return await self.context.send_message(target, MessageChain(chain=[Plain(text)]))

    @filter.command("ts")
    async def ts(self, event: AstrMessageEvent):
        runtime = self.runtime
        if runtime is None or event.unified_msg_origin not in runtime.settings.notification.targets:
            text = "当前会话无权查询 TeamSpeak 状态。"
        elif not isinstance(runtime.client.state, Ready):
            text = "TeamSpeak 尚未连接，请稍后再试。"
        else:
            try:
                text = runtime.templates.status(runtime.client.state)
            except Exception:
                logger.exception("Failed to render TeamSpeak status")
                text = "查询 TeamSpeak 状态失败，请稍后再试。"
        event.stop_event()
        yield event.plain_result(text)

    async def terminate(self) -> None:
        runtime = self.runtime
        if runtime is None:
            return
        self.runtime = None
        runtime.client.stop()
        runtime.messages.stop()
        for task in runtime.tasks:
            task.cancel()
        await asyncio.gather(*runtime.tasks, return_exceptions=True)
