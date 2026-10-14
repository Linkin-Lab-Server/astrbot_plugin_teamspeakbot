# TeamSpeak Bot for AstrBot

通过 TeamSpeak ServerQuery 维护在线客户端和频道快照，在 AstrBot 会话中查询状态，并合并推送用户进入、离开和切换频道的动态。

- `/ts`：按频道列出在线语音客户端、昵称和地域，不计入 ServerQuery 客户端。
- IP 地域：使用 IPWhois 异步查询，按完整 IPv4 / IPv6 地址缓存并持久化到插件数据目录。
- 动态通知：合并连续变化，只发送窗口起点到终点的净变化。
- 访问控制：指定哪些会话可以查询状态、接收通知。
- 自动重连：断线立即作废快照，重新同步成功后恢复查询和通知。
- 自定义文本：支持事件、通知外层、在线状态和频道列表模板。

## 查询与访问控制

| 操作或状态 | 行为 |
| --- | --- |
| 允许会话发送 `/ts`，连接已同步 | 返回在线客户端数及有人的频道列表 |
| 允许会话发送 `/ts`，尚未连接或正在重连 | 回复“TeamSpeak 尚未连接，请稍后再试。” |
| 未允许的会话发送 `/ts` | 回复“当前会话无权查询 TeamSpeak 状态。” |
| 配置校验失败，插件运行实例未启动 | `/ts` 同样回复无权查询；具体错误见日志 |
| `notification.enabled = false` | 停止记录和发送动态通知，允许会话仍可查询 |
| `notification.targets = []` | 所有会话均不能查询，也不发送通知 |

`/ts` 读取本地维护的快照，不会临时发起 ServerQuery 或地域查询。空服务器显示在线客户端数为 `0`；空频道不展示。昵称、地域、IP、客户端版本缺失时显示“未知”，频道名缺失时显示频道 ID。当前没有单独处理客户端改名事件，昵称通常在重新进入或重新同步后更新。

文中命令使用默认 `/` 前缀；如果 AstrBot 修改了唤醒前缀或命令配置，请使用对应触发方式。AstrBot 自身的白名单、插件启用范围和平台权限也会影响命令是否到达本插件。

## 配置参考

在 WebUI 编辑配置，保存并重载插件后生效。

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

### IP 地域与持久化缓存

地域查询固定使用 [IPWhois](https://ipwhois.io/documentation) 的 `https://ipwho.is/{ip}` 接口，通过 HTTPX 异步请求中文结果，不提供自定义 API 配置。查询会将客户端公网 IP 发送给该服务；缺失、非法及非公网地址不会发起请求。

国内显示省、市，国外补充国家，重复名称去重；名称沿用接口结果，不追加运营商。服务推断的是 IP 归属地域，不保证与用户实际所在地一致。查询失败或没有有效地域时显示“未知”，不会自动展示 IP。

首次同步按唯一 IP 批量查询，最多同时请求 4 个地址，整体等待最多 3 秒。实时进入事件在取得 IP 后，地域查询最多等待 3 秒。超时取消未完成请求，已成功取得的结果保留；移动和离开沿用捕获的地域。查询失败不会导致 TeamSpeak 重连。

缓存通过 `aiofiles` 异步保存于 AstrBot 实际数据目录下的 `plugin_data/astrbot_plugin_teamspeakbot/location_cache.json`，目录通过 `StarTools.get_data_dir()` 获取，符合 [AstrBot 插件存储规范](https://docs.astrbot.app/dev/star/guides/storage.html)。缓存不放在插件源码目录，重连、重载及重启后可复用。

- IPv4 和 IPv6 均按完整地址缓存，不按网段推断其他地址的地域；IPv6 等价写法使用同一规范化键。
- 成功结果没有时间有效期；临时失败缓存 10 分钟，再次遇到该 IP 时可重试。最多保留 2,048 项，超过容量按最近使用顺序淘汰。
- HTTP 429 按有效的 `Retry-After` 暂停新查询，缺失或无效时暂停 24 小时；暂停状态也会保存。
- 缓存变更合并后通常在 1 秒内异步落盘，文件写入耗时另计；使用同目录临时文件原子替换，停用和重载时完成最后一次保存。
- 文件损坏或读写失败会记录警告，当前实例继续使用内存缓存，后续变更重新尝试保存。

要强制刷新地域，请先关闭插件，删除上述缓存文件，再打开插件。缓存由插件运行时管理，无需手动编辑。Docker 部署需持久化 AstrBot 的数据目录，才能在容器重建后保留缓存。

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
| `enter`、`left` | `nickname`、`ip`、`location`、`client_version`、`channel` |
| `move` | `nickname`、`ip`、`location`、`client_version`、`from_channel`、`to_channel` |
| `change_batch` | `messages` |
| `status` | `online_count`、`channels` |
| `status_channel` | `channel_icon`、`channel`、`clients` |
| `status_client` | `nickname`、`ip`、`location` |

例如，使用下面的模板可自定义地域展示：

```json
{
  "message_templates": {
    "enter": "{nickname}（{location}）加入了 {channel}",
    "left": "{nickname}（{location}）离开了 {channel}",
    "move": "{nickname}（{location}）：{from_channel} → {to_channel}",
    "change_batch": "TeamSpeak 动态：\n{messages}",
    "status_client": "  - {nickname}（{location}）"
  }
}
```

`enter.channel` 为最终频道，`left.channel` 为窗口起始频道；移动使用起始和最终频道。`channel_icon` 在频道名包含大写 `AFK` 时为 `😴`，其他情况为 `📢`。

WebUI 多行文本框直接输入换行，JSON 中使用 `\n`。用 `{{` 和 `}}` 表示字面花括号；客户端昵称中的花括号不会被再次解析。

不支持属性访问、索引、转换及非空格式说明符，例如 `{nickname.name}`、`{nickname[0]}`、`{nickname!r}`、`{nickname:>10}`。`{nickname:}` 与 `{nickname}` 等价。空白文本、未知变量或语法错误会使该项回退默认模板并记录警告，其他有效模板继续生效；模板层的回退不改写已保存的配置。

模板值必须为字符串。注意 AstrBot 会先整理配置：`null` 可能被替换成 Schema 默认值，未知模板键可能在进入插件前被移除；只有实际传到插件的非法类型才会导致校验失败。关闭通知请使用开关。

默认模板使用 `{location}` 展示地域；已保存的有效模板按原样渲染，即使与旧版默认值相同也不会自动替换 `{ip}`。需要展示地域时，请将对应模板中的 `{ip}` 改为 `{location}`。“客户端行”只用于 `/ts` 查询，不影响进入、离开和移动事件模板。

## 开发检查

```sh
uv sync --locked
uv run ruff format .
uv run ruff check .
uv run ty check
uv run pytest -q
```

类型检查使用 ty，覆盖插件源码和测试。根据 [ty 配置文档](https://docs.astral.sh/ty/reference/configuration/)，仅允许独立开发环境中无法解析的 `astrbot.api` 宿主接口；这些接口由 AstrBot 运行环境提供，测试中使用替身。其余导入与类型问题仍会报错。
