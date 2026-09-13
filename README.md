# TeamSpeak Bot for AstrBot

通过 TeamSpeak ServerQuery 维护在线客户端和频道快照，在 AstrBot 会话中查询状态，并合并推送用户进入、离开和切换频道的动态。

- `/ts`：按频道列出在线语音客户端、昵称和 IP，不计入 ServerQuery 客户端。
- 动态通知：合并连续变化，只发送窗口起点到终点的净变化。
- 访问控制：指定哪些会话可以查询状态、接收通知。
- 自动重连：断线立即作废快照，重新同步成功后恢复查询和通知。
- 自定义文本：支持事件、通知外层、在线状态和频道列表模板。

插件不依赖 LLM，不提供语音接入、聊天转发或服务器管理功能。

## 安装与首次使用

### 运行条件

- **Python 3.12 或更高版本**：插件及固定版本的 `ts-async-api` 均有此要求；请检查 AstrBot 实际使用的解释器。
- AstrBot，建议使用最新稳定版。当前未在 `metadata.yaml` 声明 AstrBot 版本范围，也未验证所有历史版本。
- 可访问的 TeamSpeak **TCP ServerQuery** 服务及账号。默认端口为 `10011`，不是语音端口；当前连接实现不支持 SSH、TLS 或 HTTP Query。
- 要接收动态通知，所用 AstrBot 平台适配器必须支持主动消息发送。支持命令回复不代表支持后台推送。

### 安装插件

在 AstrBot WebUI 的插件管理中，选择通过仓库地址安装：

```text
https://github.com/Linkin-Lab-Server/astrbot_plugin_teamspeakbot
```

AstrBot 通过插件根目录的 `requirements.txt` 安装运行依赖。依赖固定到 Git 提交，安装环境需要 Git，并能访问 GitHub。插件的 `pyproject.toml` 和 `uv.lock` 用于本仓库开发环境，不替代 AstrBot 的依赖安装入口。

手动部署时，将仓库放入 `AstrBot/data/plugins/astrbot_plugin_teamspeakbot`，把 `requirements.txt` 安装到 **AstrBot 实际运行的 Python 环境**，然后重载插件或重启 AstrBot。例如，AstrBot 使用根目录下的 `.venv` 时，在 AstrBot 根目录执行：

```sh
uv pip install --python .venv/bin/python -r data/plugins/astrbot_plugin_teamspeakbot/requirements.txt
```

Windows 应改用实际的解释器路径，例如 `.venv\Scripts\python.exe`。Docker 部署需在 AstrBot 容器内安装；宿主机上的 Python 环境不会自动供容器使用。

### 完成配置

1. 打开插件配置页，填写 TeamSpeak 主机、ServerQuery 账号和密码；按需修改端口与虚拟服务器 ID。
2. 在目标群聊或私聊发送 AstrBot 内置命令 `/sid`，复制返回的 **UMO**，加入 `notification.targets`。不要填写 UID、单独的群号或 UMO 展示别名。
3. 保存配置并重载插件。首次安装时账号密码默认为 `null`，填写前不会启动 TeamSpeak 连接。
4. 等待日志出现 `TeamSpeak initial synchronization completed`，在允许的会话发送 `/ts`。
5. 让一个语音客户端加入或移动频道，等待通知窗口到期，验证主动推送。

下列 JSON 展示必需配置，其余字段使用默认值。请替换示例账号和 UMO：

```json
{
  "teamspeak_server": {
    "host": "ts.example.com",
    "username": "query-monitor",
    "password": "替换为 ServerQuery 密码"
  },
  "notification": {
    "targets": ["my-bot:GroupMessage:123456789"]
  }
}
```

UMO 的结构是 `平台实例 ID:消息类型:会话 ID`，以 `/sid` 的实际输出为准。插件按完整字符串匹配；开启会话隔离或变更平台实例后，应重新获取 UMO。加载时会检查三部分均非空、没有首尾空白，且消息类型是 AstrBot 支持的 `GroupMessage`、`FriendMessage` 或 `OtherMessage`。会话 ID 可以包含冒号。格式错误会阻止连接启动，日志会标明 `notification.targets` 和从 0 开始的列表索引；平台实例是否存在及目标能否投递仍需实际验证。

ServerQuery 账号需能执行 `login`、`use`、`servernotifyregister`、`channellist`、`clientlist`、`clientinfo` 和 `version`，并订阅服务器及全部频道事件。IP 是否可见取决于账号权限。Docker 内的 `127.0.0.1` 指向容器自身，请填写容器实际能访问的 TeamSpeak 地址。

## 查询与访问控制

| 操作或状态 | 行为 |
| --- | --- |
| 允许会话发送 `/ts`，连接已同步 | 返回在线客户端数及有人的频道列表 |
| 允许会话发送 `/ts`，尚未连接或正在重连 | 回复“TeamSpeak 尚未连接，请稍后再试。” |
| 未允许的会话发送 `/ts` | 回复“当前会话无权查询 TeamSpeak 状态。” |
| 配置校验失败，插件运行实例未启动 | `/ts` 同样回复无权查询；具体错误见日志 |
| `notification.enabled = false` | 停止记录和发送动态通知，允许会话仍可查询 |
| `notification.targets = []` | 所有会话均不能查询，也不发送通知 |

`/ts` 读取本地维护的快照，不会临时发起 ServerQuery 查询。空服务器显示在线客户端数为 `0`；空频道不展示。昵称、IP、客户端版本缺失时显示“未知”，频道名缺失时显示频道 ID。当前没有单独处理客户端改名事件，昵称通常在重新进入或重新同步后更新。

文中命令使用默认 `/` 前缀；如果 AstrBot 修改了唤醒前缀或命令配置，请使用对应触发方式。AstrBot 自身的白名单、插件启用范围和平台权限也会影响命令是否到达本插件。

## 配置参考

在 WebUI 编辑配置，保存并重载插件后生效。不要修改 `_conf_schema.json` 来填写账号；它定义配置界面和默认值。AstrBot 将实际配置保存在 `data/config/<plugin_name>_config.json`。

### 连接

| 配置项 | 默认值 | 约束或用途 |
| --- | --- | --- |
| `teamspeak_server.host` | `127.0.0.1` | 非空白主机地址 |
| `teamspeak_server.port` | `10011` | 整数，范围 1–65535 |
| `teamspeak_server.username` | `null` | 必填，非空白字符串 |
| `teamspeak_server.password` | `null` | 必填，非空白字符串；WebUI 使用密码框 |
| `teamspeak_server.server_id` | `1` | 虚拟服务器 ID，正整数 |
| `teamspeak_server.client_nickname` | `AstrBot TS Monitor` | ServerQuery 客户端昵称，非空白字符串 |
| `teamspeak_server.log_level` | `INFO` | 协议命令日志级别：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` |
| `connection_policy.reconnect_interval` | `5` | 普通重连间隔，正整数秒 |
| `connection_policy.banned_retry_interval` | `120` | 错误码 `3329` 的重试间隔，正整数秒 |

`secret: true` 只遮罩 WebUI 中的密码显示，不加密配置文件。默认状态和事件模板会展示用户 IP；不需要时可从模板中移除 `{ip}`。

### 通知

| 配置项 | 默认值 | 约束或用途 |
| --- | --- | --- |
| `notification.targets` | `[]` | 通知目标及 `/ts` 允许名单，重复项自动去重 |
| `notification.enabled` | `true` | 动态通知总开关 |
| `notification.events.enter` | `true` | 进入通知 |
| `notification.events.left` | `true` | 离开通知 |
| `notification.events.move` | `true` | 移动通知 |
| `notification.window.quiet_seconds` | `10` | 最后一个事件后的安静时长，非负整数秒 |
| `notification.window.max_seconds` | `60` | 从首个事件起算的最大窗口时长，正整数秒 |

窗口必须满足 `0 <= quiet_seconds < max_seconds`。`quiet_seconds = 0` 表示在下一次窗口检查时发送，并非同步立即发送。数值字段不接受字符串或布尔值。

## 通知如何合并

所有用户共享一个窗口。任意用户的新事件都会重新计算安静时长，但不会延后窗口的最大截止时间；同一用户只保留起点到终点的净变化。

| 窗口内发生的变化 | 最终通知 |
| --- | --- |
| 加入 A → 移动到 B | 加入 B |
| A → B → C | A → C |
| A → B → A | 无 |
| 加入 A → 离开 | 无 |
| A → B → 离开 | 离开 A |
| 离开 A → 加入 A | 无 |
| 离开 A → 加入 B | A → B |

先归并，再应用进入、离开和移动开关。例如，只启用移动通知时，“离开 A → 加入 B”仍会产生移动通知。

- 优先用 UID 识别用户；缺少 UID 时只在同一次客户端会话内合并。不同 ServerQuery 连接之间不合并。
- 启动和重连时的初始同步不发送进入通知。
- 每批对每个目标最多调用一次发送接口，多个用户的动态以换行连接。
- 窗口通常每秒检查一次，发送耗时另计；慢速平台可能延迟后续目标和下一批消息。
- 发送异常或接口返回 `false` 时记录日志，不自动补发。接口返回 `true` 只表示找到平台并完成适配器调用，不代表收件人已收到消息。
- 断线、停用或重载会丢弃待发变化，并停止尝试旧批次中剩余的目标；已进入平台发送调用的消息可能仍然送达。

## 自定义消息模板

在 WebUI 的 `message_templates` 中修改对应文本，保存并重载。模板支持换行和下表中的命名占位符：

| 模板键 | 可用变量 |
| --- | --- |
| `enter`、`left` | `nickname`、`ip`、`client_version`、`channel` |
| `move` | `nickname`、`ip`、`client_version`、`from_channel`、`to_channel` |
| `change_batch` | `messages` |
| `status` | `online_count`、`channels` |
| `status_channel` | `channel_icon`、`channel`、`clients` |
| `status_client` | `nickname`、`ip` |

例如，使用下面的模板可省略用户 IP：

```json
{
  "message_templates": {
    "enter": "{nickname} 加入了 {channel}",
    "left": "{nickname} 离开了 {channel}",
    "move": "{nickname}：{from_channel} → {to_channel}",
    "change_batch": "TeamSpeak 动态：\n{messages}",
    "status_client": "  - {nickname}"
  }
}
```

`enter.channel` 为最终频道，`left.channel` 为窗口起始频道；移动使用起始和最终频道。`channel_icon` 在频道名包含大写 `AFK` 时为 `😴`，其他情况为 `📢`。

WebUI 多行文本框直接输入换行，JSON 中使用 `\n`。用 `{{` 和 `}}` 表示字面花括号；客户端昵称中的花括号不会被再次解析。

不支持属性访问、索引、转换及非空格式说明符，例如 `{nickname.name}`、`{nickname[0]}`、`{nickname!r}`、`{nickname:>10}`。`{nickname:}` 与 `{nickname}` 等价。空白文本、未知变量或语法错误会使该项回退默认模板并记录警告，其他有效模板继续生效；模板层的回退不改写已保存的配置。

模板值必须为字符串。注意 AstrBot 会先整理配置：`null` 可能被替换成 Schema 默认值，未知模板键可能在进入插件前被移除；只有实际传到插件的非法类型才会导致校验失败。关闭通知请使用开关。

## 从旧版升级到 2.0

2.0 使用新的配置结构，不提供语义迁移。升级前备份原配置，升级后在 WebUI 重新核对账号、目标会话、窗口、事件开关和模板。

旧窗口字段、`change`、`visit` 及访问模板不再使用；当前只注册 `/ts` 命令，不再提供旧版的 LLM 查询工具。AstrBot 会根据新 Schema 删除不存在的字段、补齐默认值，因此旧配置可能被清理后以新默认值启动，不能依赖“旧字段必然报错”来判断迁移是否完成。直接调用插件配置解析器时，残留的未知配置字段则会被拒绝。

## 排查问题

| 现象 | 检查方式 |
| --- | --- |
| 安装失败或缺少模块 | 确认 Python 3.12+、Git 可用、依赖安装在 AstrBot 的运行环境中；检查 GitHub 访问和安装日志 |
| 插件显示已加载，但无连接日志 | 查找 `TeamSpeak configuration rejected`；修正日志给出的配置路径，保存并重载 |
| `/ts` 没有回复 | 检查 AstrBot 前缀、白名单、插件启用范围和命令冲突 |
| `/ts` 提示无权查询 | 核对完整 UMO 是否在 `notification.targets`，并检查配置是否通过校验 |
| 一直提示尚未连接 | 检查 TCP ServerQuery 地址、端口、账号、虚拟服务器 ID 和权限；`3329` 表示封禁，按封禁间隔重试 |
| 查询正常，但没有通知 | 检查通知及事件开关、窗口归并结果、UMO 格式和平台主动消息能力；查看发送异常或 `returned False` 日志 |
| 短暂进入或来回移动没有通知 | 同一窗口内没有净变化时会抵消，属于预期行为 |
| IP 或客户端版本显示“未知” | 检查地址查看权限；客户端已离开或补充查询失败时也可能拿不到信息 |

## 开发与兼容性核对

在本仓库目录使用 uv：

```sh
uv sync --locked
uv run ruff format .
uv run ruff check .
uv run pytest -q
```

`main.py` 负责 AstrBot 生命周期、命令和主动消息边界；`core/client.py` 管理 ServerQuery 连接与传输任务；`core/event_handler.py` 维护快照；`core/message_manager.py` 负责通知窗口；配置、模板和事件类型位于其余 `core` 模块中。

测试覆盖配置、模板、允许名单、生命周期、事件归并以及模拟 ServerQuery 服务。连接集成测试需要绑定 `127.0.0.1` 临时端口。AstrBot 接口在现有单元测试中被替换为测试对象，测试通过不等同于完成真实平台投递验证；部署后仍需检查连接、命令、推送和重载。

2026-09-13 对照官方文档及 [AstrBot 上游源码 `bd046ed`](https://github.com/AstrBotDevs/AstrBot/tree/bd046ed29914ee559e9bf47676ccb71a84f747ba) 核对：继承 `Star` 自动注册、`initialize()` / `terminate()` 生命周期、`@filter.command`、`event.plain_result()`、`Context.send_message()` 和当前配置 Schema 均仍受支持，无需补回已废弃的 `@register` 装饰器。

插件及核心模块统一通过 `astrbot.api.logger` 输出日志，在支持独立插件日志的 AstrBot 版本中遵循本插件的日志级别设置。查看协议命令调试日志时，需要同时将 `teamspeak_server.log_level` 和 AstrBot 中本插件的日志级别设为 `DEBUG`；协议配置的其他级别均不输出命令调试日志，不影响连接错误日志。命令日志只记录命令名，不记录账号密码等参数。

核对依据：

- [插件开发入口与依赖管理](https://docs.astrbot.app/dev/star/plugin-new.html)
- [最小插件实例](https://docs.astrbot.app/dev/star/guides/simple.html)
- [消息事件与命令](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [主动与被动消息发送](https://docs.astrbot.app/dev/star/guides/send-message.html)
- [插件配置与 Schema 更新](https://docs.astrbot.app/dev/star/guides/plugin-config.html)

## 依赖、致谢与许可

连接层使用 [ts-async-api](https://github.com/Next-Page-Vi/ts-async-api) 的协议编解码，固定到提交 `c30e24a7c26ab26aaff74a4307594c596190550c`；插件自行管理连接任务和顺序事件处理。通知窗口设计参考 [Minecraft QueQiao Lite](https://github.com/Next-Page-Vi/astrbot_plugin_queqiao_lite)。

项目采用 [GNU AGPLv3](LICENSE) 许可。
