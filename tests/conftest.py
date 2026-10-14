"""Stub public AstrBot APIs without starting the host application's services."""

import logging
import sys
from enum import Enum
from types import ModuleType


class MessageType(Enum):
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"
    OTHER_MESSAGE = "OtherMessage"


api = ModuleType("astrbot.api")
api.__dict__["logger"] = logging.getLogger("astrbot.plugin.astrbot_plugin_teamspeakbot")
platform = ModuleType("astrbot.api.platform")
platform.__dict__["MessageType"] = MessageType
sys.modules["astrbot"] = ModuleType("astrbot")
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.platform"] = platform
