import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.config import NotificationConfig, PluginConfig
from core.templates import DEFAULT_TEMPLATES, Templates
from core.types import Channel, Enter, OnlineClient, Ready, UidIdentity, User

USER = User(UidIdentity(uuid4(), "uid"), "{ip}", None, None)


def configuration():
    return {"teamspeak_server": {"username": "serveradmin", "password": "secret"}}


@pytest.mark.parametrize(
    "value",
    [
        " ",
        "",
        "{unknown}",
        "{nickname.x}",
        "{nickname[0]}",
        "{nickname!r}",
        "{nickname:>8}",
        "{nickname",
        "nickname}",
        "{}",
        "{0}",
        "{nickname:{ip}}",
    ],
)
def test_invalid_template_falls_back_without_replacing_valid_templates(value, caplog):
    templates = Templates({"enter": value, "status": "Online: {online_count}"})
    event = Enter(USER, Channel(1, "A"))
    assert templates.event(event) == Templates({}).event(event)
    assert templates.status(Ready(())) == "Online: 0"
    assert "message_templates.enter" in caplog.text
    assert "using the default template" in caplog.text
    assert all(
        record.name == "astrbot.plugin.astrbot_plugin_teamspeakbot" for record in caplog.records
    )


@pytest.mark.parametrize("key", ["invalid", "visit", "visit_batch"])
def test_unknown_template_key_is_ignored(key, caplog):
    templates = Templates({key: "text", "status": "Online: {online_count}"})
    assert templates.status(Ready(())) == "Online: 0"
    assert f"message_templates.{key}: unknown template" in caplog.text


def test_empty_format_specification_is_equivalent(caplog):
    templates = Templates({"enter": "{{{nickname:}}}\n{ip:}|{channel:}"})
    assert templates.event(Enter(USER, Channel(7, None))) == "{{ip}}\n未知|7"
    assert not caplog.records


def test_one_pass_multiline_and_missing_values():
    templates = Templates({"enter": "{{{nickname}}}\n{ip}|{channel}|{client_version}"})
    channel = Channel(7, None)
    assert templates.event(Enter(USER, channel)) == "{{ip}}\n未知|7|未知"


def test_online_templates_and_empty_server():
    templates = Templates({"status": "{online_count}\n{channels}"})
    assert templates.status(Ready(())) == "0\n"
    snapshot = Ready((OnlineClient(USER, Channel(1, "AFK")), OnlineClient(USER, Channel(2, None))))
    assert templates.status(snapshot) == "2\n😴 AFK\n  - {ip} (未知)\n📢 2\n  - {ip} (未知)"


@pytest.mark.parametrize("minimum,maximum", [(0, 60), (10, 60)])
def test_valid_windows(minimum, maximum):
    config = NotificationConfig(window={"quiet_seconds": minimum, "max_seconds": maximum})
    assert (config.window.quiet_seconds, config.window.max_seconds) == (minimum, maximum)


@pytest.mark.parametrize(
    "minimum,maximum",
    [(-1, 60), (10, 10), (60, 10), (0, 0), (True, 60), (1.5, 60), (None, 60), ("0", 60)],
)
def test_invalid_windows_rejected(minimum, maximum):
    with pytest.raises(ValueError):
        NotificationConfig(window={"quiet_seconds": minimum, "max_seconds": maximum})


@pytest.mark.parametrize(
    "config",
    [
        {"min_merge_window": 10},
        {"max_merge_window": 60},
        {"change": {"enabled": True}},
        {"visit": {"enabled": True}},
    ],
)
def test_old_notification_config_rejected(config):
    with pytest.raises(ValueError):
        NotificationConfig.model_validate(config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", None),
        ("host", " "),
        ("username", ""),
        ("password", None),
        ("password", ""),
        ("port", 0),
        ("port", 65536),
        ("port", True),
        ("port", "10011"),
        ("server_id", 0),
        ("server_id", 1.5),
        ("client_nickname", " "),
        ("log_level", "invalid"),
    ],
)
def test_strict_connection_config(field, value):
    config = configuration()
    config["teamspeak_server"][field] = value
    with pytest.raises(ValueError, match=f"teamspeak_server.{field}"):
        PluginConfig.parse(config)


def test_password_not_exposed_in_errors():
    config = configuration()
    config["teamspeak_server"]["port"] = "invalid"
    with pytest.raises(ValueError) as exc:
        PluginConfig.parse(config)
    assert "secret" not in str(exc.value)
    assert "secret" not in repr(PluginConfig.parse(configuration()))


@pytest.mark.parametrize("value", [0, -1, None, True, "5", 1.5])
def test_strict_reconnect_interval(value):
    config = configuration() | {"connection_policy": {"reconnect_interval": value}}
    with pytest.raises(ValueError, match="connection_policy.reconnect_interval"):
        PluginConfig.parse(config)


def test_schema_defaults_match_runtime():
    schema = json.loads(Path("_conf_schema.json").read_text())

    def defaults(items):
        return {
            key: defaults(value["items"]) if value["type"] == "object" else value["default"]
            for key, value in items.items()
        }

    data = defaults(schema)
    assert data["teamspeak_server"]["password"] is None
    assert schema["teamspeak_server"]["items"]["password"]["secret"] is True
    data["teamspeak_server"].update(username="admin", password="password")
    parsed = PluginConfig.parse(data)
    assert parsed.message_templates == DEFAULT_TEMPLATES
    assert Templates(parsed.message_templates)
    assert all(entry["type"] == "text" for entry in schema["message_templates"]["items"].values())


@pytest.mark.parametrize(
    "value",
    [
        None,
        "bot:GroupMessage:123",
        {"bot:GroupMessage:123"},
        [None],
        [False],
        [123],
        [""],
        [" "],
        ["123"],
        ["bot:GroupMessage"],
        [":GroupMessage:123"],
        ["bot::123"],
        ["bot:GroupMessage:"],
        ["bot:GroupMessage: "],
        ["bot:groupmessage:123"],
        ["bot:UnknownMessage:123"],
        [" bot:GroupMessage:123"],
        ["bot :GroupMessage:123"],
        ["bot: GroupMessage:123"],
        ["bot:GroupMessage: 123"],
        ["bot:GroupMessage:123\n"],
    ],
)
def test_targets_validation(value):
    with pytest.raises(ValueError):
        NotificationConfig(targets=value)


def test_targets_are_deduplicated():
    first, second = "bot:GroupMessage:a", "bot:FriendMessage:b"
    assert NotificationConfig(targets=[first, first, second]).targets == (first, second)


@pytest.mark.parametrize(
    "target",
    [
        "bot:GroupMessage:123",
        "bot:FriendMessage:123",
        "bot:OtherMessage:system",
        "bot:GroupMessage:group:thread:user",
        "bot:GroupMessage:group_user",
        "平台实例:GroupMessage:会话",
    ],
)
def test_valid_targets_preserve_exact_session_identity(target):
    assert NotificationConfig(targets=[target]).targets == (target,)
    assert NotificationConfig(targets=(target,)).targets == (target,)


def test_invalid_target_reports_configuration_path_and_item_without_echoing_input():
    data = configuration() | {
        "notification": {"targets": ["bot:GroupMessage:valid", "accidentally-pasted-secret"]}
    }
    with pytest.raises(ValueError, match="notification.targets:.*item 1") as exc:
        PluginConfig.parse(data)
    assert "accidentally-pasted-secret" not in str(exc.value)
