# 标准库
import asyncio
from collections import defaultdict
from typing import Optional

# 第三方库（ts_async_api）
from ts_async_api.server_query.client import Client, ServerStatus
from ts_async_api.server_query.event import (
    ClientEnterEvent,
    ClientLeftEventBase,
    ClientMovedEventBase,
    EventBase,
)
from ts_async_api.server_query.exception import CmdException
from ts_async_api.server_query.utils import init_logger

# 项目内部模块（astrbot）
from astrbot.api import (
    AstrBotConfig,
    logger,
)
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register


class ClientStatusChangeEventCtx:
    """
    客户端状态变化事件上下文类
    用于管理客户端进入和移动事件的合并报告，以及后台任务集合
    """

    # 事件映射：客户端ID -> 事件对象（ClientEnterEvent 或 ClientMovedEventBase）
    event_map: dict[int, ClientEnterEvent | ClientMovedEventBase]
    # 服务器当前状态，包括客户端和频道列表
    server_status: ServerStatus
    # 后台异步任务集合，用于跟踪报告任务
    background_tasks: set[asyncio.Task[None]]

    def __init__(self, server_status: ServerStatus) -> None:
        """
        初始化事件上下文
        :param server_status: 服务器状态对象
        """
        self.event_map = {}
        self.server_status = server_status
        self.background_tasks = set()
        self.plugin: Optional[TeamSpeakBotPlugin] = None

    async def report_event(
        self, event: ClientEnterEvent | ClientMovedEventBase
    ) -> None:
        """
        报告事件：延迟后输出日志通知
        该方法会在延迟EVENT_MERGE_TIME秒后执行实际的日志输出
        :param event: 要报告的事件（进入或移动）
        """
        # 延迟执行，以合并可能连续发生的事件
        if not self.plugin:
            raise RuntimeError("TeamSpeak plugin reference is None")
        await asyncio.sleep(self.plugin.event_merge_time)
        message_text: str = ""
        # 处理客户端进入服务器事件
        if isinstance(event, ClientEnterEvent):
            # 从服务器状态中获取客户端信息
            client_info = self.server_status.client_list.get(event.clid)
            if client_info is not None:
                # 获取客户端当前所在频道的名称
                channel_name = self.server_status.channel_list[
                    client_info.cid
                ].channel_name
                # 记录进入服务器的通知日志，包括昵称、IP、频道和客户端版本
                message_text = f"用户 {client_info.client_nickname} ({client_info.connection_client_ip}), 加入频道: {channel_name}, 客户端版本: {client_info.client_version}"
                await self.plugin.send_message(
                    message_text=message_text, no_ignore=self.plugin.ts_enter
                )
        # 处理客户端移动频道事件
        else:
            # 从服务器状态中获取客户端信息
            client_info = self.server_status.client_list.get(event.clid)
            if client_info is not None:
                # 获取原频道和当前频道的名称
                old_channel_name = self.server_status.channel_list[
                    event.cfid
                ].channel_name
                new_channel_name = self.server_status.channel_list[
                    client_info.cid
                ].channel_name
                # 记录频道切换的通知日志
                message_text = f"用户 {client_info.client_nickname} ({client_info.connection_client_ip}), 从频道 {old_channel_name} 切换到频道 {new_channel_name}"
                await self.plugin.send_message(
                    message_text=message_text, no_ignore=self.plugin.ts_move
                )
        if not self.plugin:
            raise RuntimeError("TeamSpeak plugin reference is None")

        # 从事件映射中移除该客户端的事件
        del self.event_map[event.clid]

    async def client_enter_server_callback(
        self, client: Client, event: EventBase
    ) -> bool:
        """
        客户端进入服务器事件的回调函数
        检查事件是否已存在于映射中，如果不存在则创建报告任务
        :param client: 客户端对象（未使用）
        :param event: 事件对象, 必须是ClientEnterEvent类型
        :return: False, 表示不阻止事件传播
        """
        if not isinstance(event, ClientEnterEvent):
            return True
        # 如果该客户端ID的事件尚未记录
        if event.clid not in self.event_map:
            self.event_map[event.clid] = event
            task = asyncio.create_task(self.report_event(event))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
        return False

    async def client_moved_callback(self, client: Client, event: EventBase) -> bool:
        """
        客户端移动频道事件的回调函数
        类似于进入回调，检查并创建报告任务
        :param client: 客户端对象（未使用）
        :param event: 事件对象, 必须是ClientMovedEventBase类型
        :return: False, 表示不阻止事件传播
        """
        if not isinstance(event, ClientMovedEventBase):
            return True
        # 如果该客户端ID的事件尚未记录
        if event.clid not in self.event_map:
            self.event_map[event.clid] = event
            task = asyncio.create_task(self.report_event(event))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
        return False

    async def client_left_server_callback(
        self, client: Client, event: EventBase
    ) -> bool:
        """
        客户端离开服务器事件的回调函数
        直接输出离开通知日志，不使用事件合并
        :param client: 客户端对象
        :param event: 事件对象, 必须是ClientLeftEventBase类型
        :return: False, 表示不阻止事件传播
        """
        # 检查事件类型
        if not isinstance(event, ClientLeftEventBase):
            return True
        client_info = client.server_status.client_list[event.clid]
        channel_name = client.server_status.channel_list[client_info.cid].channel_name
        message_text = f"用户 {client_info.client_nickname} ({client_info.connection_client_ip}), 从频道: {channel_name} 离开"
        logger.info(message_text)
        if not self.plugin:
            raise RuntimeError("TeamSpeak plugin reference is None")
        await self.plugin.send_message(
            message_text=message_text, no_ignore=self.plugin.ts_left
        )
        return False


@register(
    "astrbot_plugin_teamspeakbot",
    "Next-Page-Vi, plusls, Linkin-Lab-Server",
    "teamspeak 服务器变动通知插件",
    "1.1.0",
    "https://github.com/Linkin-Lab-Server/astrbot_plugin_teamspeakbot",
)
class TeamSpeakBotPlugin(Star):
    """插件主入口"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.ctx: Optional[ClientStatusChangeEventCtx] = None
        self.ts_task = None
        self.config = config
        self.host: str = self.config.get("ts_host") or ""
        self.port: int = self.config.get("ts_port") or 10011
        self.username: str = self.config.get("ts_username") or ""
        self.password: str = self.config.get("ts_password") or ""
        self.notification_umo_list: list[str] = (
            self.config.get("notification_umo_list") or []
        )
        self.event_merge_time: int = self.config.get("ts_event_merge_time") or 10
        self.client_nickname: str = self.config.get("ts_client_nickname") or "AstrBot"
        self.log_level = self.config.get("ts_log_level", "INFO")
        self.ts_enter = self.config["notification_level"].get("ts_enter", True)
        self.ts_left = self.config["notification_level"].get("ts_left", True)
        self.ts_move = self.config["notification_level"].get("ts_move", True)

    async def send_message(
        self, message_text: str, umo: Optional[list[str]] = None, no_ignore: bool = True
    ) -> None:
        """发送消息到指定的统一消息源"""
        if umo is not None:
            target_umo_list = umo
        else:
            target_umo_list = self.notification_umo_list
        message_chain = MessageChain(chain=[Plain(message_text)])
        for group_umo in target_umo_list:
            try:
                logger.info(f"Build message to group {group_umo}: {message_chain}")
                if not no_ignore:
                    logger.info("---Message ignored---")
                    continue
                await self.context.send_message(group_umo, message_chain)
            except Exception as e:
                logger.error(
                    f"Failed to send message to {group_umo}: {type(e).__name__}: {e}"
                )

    async def initialize(self) -> None:
        """插件初始化"""
        logger.info("Starting TeamSpeakBotPlugin...")
        if not all(bool(x) for x in [self.host, self.username, self.password]):
            logger.error(
                "Not fully configured, plugin will not connect until reloaded."
            )
            await self.terminate()
            return
        self.ts_task = asyncio.create_task(self.ts_connection_loop())

    async def ts_connection_loop(self) -> None:
        """连接循环, 支持重连"""
        while True:
            try:
                logger.info("开始连接到 teamspeak 服务器...")
                await self.connect_to_ts()
            except CmdException as e:
                if (
                    hasattr(e, "res")
                    and e.res.id == 3329
                    and "banned" in str(e).lower()
                ):
                    retry_seconds = 120  # 等待2分钟
                    logger.warning(f"由于封禁，等待 {retry_seconds} 秒后重试...")
                    await asyncio.sleep(retry_seconds)
                else:
                    logger.error(f"连接错误: {type(e).__name__}", exc_info=True)
                    await asyncio.sleep(5)  # 其他错误重试间隔5秒
            except Exception as e:
                logger.error(f"连接错误: {type(e).__name__}", exc_info=True)
                await asyncio.sleep(5)  # 其他错误重试间隔5秒

    async def connect_to_ts(self) -> None:
        """Ts server query client main"""
        init_logger(log_level=self.log_level)

        # 会等所有 task 结束后再销毁 client
        async with await Client.new(self.host, self.port) as client:
            version = await client.server_version()
            logger.info(
                f"Teamspeaker server version: {version.version}.{version.build}, platform: {version.platform}."
            )
            await client.login(self.username, self.password)
            await client.use(1, virtual=True, client_nickname=self.client_nickname)
            await asyncio.sleep(3)
            await client.listen_all_event()
            self.ctx = ClientStatusChangeEventCtx(server_status=client.server_status)
            self.ctx.plugin = self

            client.event_manager.register(
                ClientEnterEvent, self.ctx.client_enter_server_callback
            )
            client.event_manager.register(
                ClientLeftEventBase, self.ctx.client_left_server_callback
            )
            client.event_manager.register(
                ClientMovedEventBase, self.ctx.client_moved_callback
            )
            logger.info("Teamspeak connection initialized.")
            # Need to call wait() to ensure that exceptions in the inner tasks are propagated outward.
            await client.wait()

    async def get_ts_status(self) -> str:
        """查询TS服务器状态"""
        if not self.ctx or not self.ctx.server_status:
            await self.connect_to_ts()
            return "teamspeak 连接未初始化, 已开始重载, 请稍后再试..."
        client_list = self.ctx.server_status.client_list
        filtered_clients = [  # 排除自己
            info
            for clid, info in client_list.items()
            if info.client_nickname != self.client_nickname
        ]
        num_clients = len(filtered_clients)
        channel_to_clients = defaultdict(list)
        for info in filtered_clients:
            nickname = getattr(info, "client_nickname", "Unknown")
            ip = getattr(info, "connection_client_ip", "Unknown")
            channel_to_clients[info.cid].append((nickname, ip))

        status_text = f"当前在线客户端数: {num_clients}\n"
        for cid, clients in channel_to_clients.items():
            channel_name = self.ctx.server_status.channel_list[cid].channel_name
            if "AFK" in channel_name:
                status_text += f"😴 {channel_name}\n"
            else:
                status_text += f"📢 {channel_name}\n"
            for nickname, ip in clients:
                status_text += f"  - {nickname} ({ip})\n"
        return status_text

    @filter.command("ts")
    async def ts(self, event: AstrMessageEvent) -> None:
        status_text = await self.get_ts_status()
        umo = event.unified_msg_origin
        if umo not in self.notification_umo_list:
            logger.warning("Only Responding request on notification list.")
        else:
            await self.send_message(
                message_text=status_text, umo=[umo] if umo else None
            )
        event.call_llm = True
        # yield event.plain_result(status_text)

    @filter.llm_tool(name="query_teamspeak_status")
    async def get_ts_status_llm(self, event: AstrMessageEvent):
        """
        **工具名称**: query_teamspeak_status
        **功能描述**:
        通过调用 `query_teamspeak_status` 工具查询 TeamSpeak 语音服务器的在线用户列表及其所在频道，并根据用户问题生成简洁、自然的中文回答。工具返回的数据包含以下三种行：
        - **在线客户端总数**：格式为“当前在线客户端数: X”。
        - **频道信息**：以图标开头，"📢"表示活跃频道，"😴 AFK"表示空闲（离开）频道，频道名称紧随其后。
        - **用户信息**：格式为“- 昵称 (ID)”，ID 通常为 IP 地址，但格式可能不严格。
        你的任务是解析返回的 `status_text`，动态分析用户问题（`event.message`），提取相关信息，并生成符合中文语境的回答。特别注意用户昵称的模糊匹配需求，并支持可能的中文别名（如通过上下文推断），在有歧义时提示用户进一步确认。
        **关键要求**:
        1. **数据解析**:
           - 提取在线客户端总数、频道名称（区分 📢 和 😴 AFK）及用户列表（昵称和 ID）。
           - 使用正则表达式或字符串分割，确保解析准确无误。
        2. **模糊匹配用户昵称**:
           - 支持部分匹配（忽略大小写）。
           - 支持可能的中文别名，通过上下文推断。
           - 如果昵称有歧义（多个用户匹配），列出所有匹配用户的状态，并提示用户。
        """
        logger.info("LLM Tool: query_teamspeak_status called.")
        status_text = await self.get_ts_status()
        return status_text

    async def terminate(self) -> None:
        """插件终止"""
        if self.ts_task:
            self.ts_task.cancel()
            try:
                await self.ts_task
            except asyncio.CancelledError:
                pass
