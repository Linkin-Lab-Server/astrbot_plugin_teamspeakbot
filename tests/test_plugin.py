import asyncio
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import logger


@pytest.fixture
def plugin_class(monkeypatch):
    # Only the AstrBot-facing boundary is stubbed; the plugin services are real.
    class Star:
        def __init__(self, context):
            self.context = context

    class Plain:
        def __init__(self, text):
            self.text = text

    api = ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logger
    events = ModuleType("astrbot.api.event")
    events.AstrMessageEvent = object
    events.MessageChain = lambda **kwargs: SimpleNamespace(**kwargs)
    events.filter = SimpleNamespace(command=lambda name: lambda function: function)
    stars = ModuleType("astrbot.api.star")
    stars.Context = object
    stars.Star = Star
    components = ModuleType("astrbot.api.message_components")
    components.Plain = Plain
    package = ModuleType("test_teamspeak_plugin")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    for name, module in {
        "astrbot": ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": events,
        "astrbot.api.star": stars,
        "astrbot.api.message_components": components,
        "test_teamspeak_plugin": package,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("test_teamspeak_plugin.main", Path("main.py"))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class Event:
    def __init__(self, origin):
        self.unified_msg_origin = origin
        self.stopped = False

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


def config(targets=None):
    return {
        "teamspeak_server": {"username": "admin", "password": "test"},
        "notification": {"targets": ["bot:GroupMessage:allowed"] if targets is None else targets},
    }


@pytest.fixture
async def running_plugin(plugin_class, monkeypatch):
    async def block(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(plugin_class.TeamSpeakClient, "run", block)
    plugin = plugin_class.TeamSpeakBotPlugin(SimpleNamespace(send_message=AsyncMock()), config())
    await plugin.initialize()
    try:
        yield plugin
    finally:
        await plugin.terminate()


async def test_command_permissions_before_state_access(running_plugin):
    plugin = running_plugin

    class UnreadableClient:
        @property
        def state(self):
            raise AssertionError("Unauthorized query read state")

    runtime = plugin.runtime
    plugin.runtime = replace(runtime, client=UnreadableClient())
    try:
        event = Event("bot:GroupMessage:denied")
        assert [result async for result in plugin.ts(event)] == [
            "当前会话无权查询 TeamSpeak 状态。"
        ]
        assert event.stopped
    finally:
        plugin.runtime = runtime


async def test_disconnected_query_returns_without_connecting(running_plugin):
    plugin = running_plugin
    tasks = asyncio.all_tasks()
    event = Event("bot:GroupMessage:allowed")
    async with asyncio.timeout(0.1):
        assert [result async for result in plugin.ts(event)] == ["TeamSpeak 尚未连接，请稍后再试。"]
    assert event.stopped
    assert asyncio.all_tasks() == tasks


async def test_online_passive_reply_and_empty_allowlist(plugin_class, running_plugin):
    module = plugin_class
    plugin = running_plugin
    plugin.runtime = replace(
        plugin.runtime, templates=module.Templates({"status": "在线 {online_count}"})
    )
    plugin.runtime.client.state = module.Ready(())
    event = Event("bot:GroupMessage:allowed")
    assert [result async for result in plugin.ts(event)] == ["在线 0"]
    assert event.stopped
    plugin.context.send_message.assert_not_awaited()
    plugin.runtime = replace(plugin.runtime, settings=module.PluginConfig.parse(config([])))
    assert [result async for result in plugin.ts(event)] == ["当前会话无权查询 TeamSpeak 状态。"]


@pytest.mark.parametrize(
    "invalid,path",
    [
        ({"teamspeak_server": {"username": None, "password": None}}, "teamspeak_server"),
        ({"notification": {"window": {"quiet_seconds": 60}}}, "notification.window"),
        ({"message_templates": {"enter": None}}, "message_templates.enter"),
        ({"notification": {"targets": ["invalid-umo"]}}, "notification.targets"),
    ],
)
async def test_invalid_configuration_remains_editable(plugin_class, invalid, path, caplog):
    module = plugin_class
    data = config() | invalid
    plugin = module.TeamSpeakBotPlugin(SimpleNamespace(), data)
    await plugin.initialize()
    assert plugin.runtime is None
    assert plugin.raw_config is data
    assert path in caplog.text and "WebUI" in caplog.text
    assert "test-password" not in caplog.text
    event = Event("bot:GroupMessage:allowed")
    assert [result async for result in plugin.ts(event)] == ["当前会话无权查询 TeamSpeak 状态。"]
    await plugin.terminate()
    await plugin.terminate()


async def test_initialize_does_not_hide_programming_errors(plugin_class, monkeypatch):
    module = plugin_class

    def fail(config):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(module, "Templates", fail)
    plugin = module.TeamSpeakBotPlugin(SimpleNamespace(), config())
    with pytest.raises(RuntimeError, match="unexpected failure"):
        await plugin.initialize()
    assert plugin.runtime is None


@pytest.mark.parametrize("enabled", [True, False])
async def test_invalid_notification_template_keeps_status_available(
    plugin_class, running_plugin, enabled, caplog
):
    plugin = running_plugin
    plugin.raw_config["notification"]["enabled"] = enabled
    plugin.raw_config["message_templates"] = {"enter": "{bad}", "status": "Online: {online_count}"}
    await plugin.initialize()
    assert plugin.runtime is not None
    plugin.runtime.client.state = plugin_class.Ready(())
    event = Event("bot:GroupMessage:allowed")
    assert [result async for result in plugin.ts(event)] == ["Online: 0"]
    assert event.stopped
    assert "message_templates.enter" in caplog.text
    assert "using the default template" in caplog.text


async def test_initialize_reload_and_repeated_cleanup(plugin_class, monkeypatch):
    module = plugin_class
    started = asyncio.Event()

    async def block(self):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module.TeamSpeakClient, "run", block)
    plugin = module.TeamSpeakBotPlugin(SimpleNamespace(send_message=AsyncMock()), config())
    # An invalid initial configuration can be repaired on the retained plugin instance.
    plugin.raw_config["teamspeak_server"]["username"] = None
    await plugin.initialize()
    assert plugin.runtime is None
    plugin.raw_config["teamspeak_server"]["username"] = "admin"
    await plugin.initialize()
    await started.wait()
    old_runtime = plugin.runtime
    await plugin.initialize()
    assert all(task.done() for task in old_runtime.tasks)
    assert old_runtime.messages is not plugin.runtime.messages
    old_runtime = plugin.runtime
    plugin.raw_config["teamspeak_server"]["username"] = None
    await plugin.initialize()
    assert plugin.runtime is None
    assert all(task.done() for task in old_runtime.tasks)
    plugin.raw_config["teamspeak_server"]["username"] = "admin"
    await plugin.initialize()
    await plugin.terminate()
    await plugin.terminate()
    assert plugin.runtime is None
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("teamspeak:")]


async def test_cancelled_shutdown_clears_runtime_and_propagates(plugin_class, monkeypatch):
    started, cleaning = asyncio.Event(), asyncio.Event()

    async def block(self):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(plugin_class.TeamSpeakClient, "run", block)
    plugin = plugin_class.TeamSpeakBotPlugin(SimpleNamespace(send_message=AsyncMock()), config())
    await plugin.initialize()
    runtime = plugin.runtime
    await started.wait()
    closing = asyncio.create_task(plugin.terminate())
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        assert plugin.runtime is None
        event = Event("bot:GroupMessage:allowed")
        assert [result async for result in plugin.ts(event)] == [
            "当前会话无权查询 TeamSpeak 状态。"
        ]
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert all(task.done() for task in runtime.tasks)
        await plugin.terminate()
    finally:
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
