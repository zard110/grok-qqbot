# 分层运行时与管理后台

## 职责与数据所有权

现有脚本名称和参数保留。新功能使用 Python 标准库实现；网关继续依赖
websocket-client，图片渲染继续使用 Pillow。生产网关及 shell 守护运行在原来的
Linux 环境；配置、记忆、后台和离线测试也支持 Windows。

| 模块 | 职责 |
| --- | --- |
| `core/settings.py` | 统一代码与数据目录，支持 `QQBOT_DATA_DIR` |
| `core/files.py` | 原子发布、线程/进程锁、受管目录边界 |
| `groups.py` | 有效配置、revision、registry、插件绑定与权限 |
| `core/plugins.py` | 可信内置插件、生命周期、稳定顺序、群级配额与动作路由 |
| `core/sensenova.py` | `ppt_poster` 插件的商汤日日新海报/PPT 生成（CLI：`sensenova_make.py`） |
| `core/luckin.py` | `luckin_coffee` 插件：瑞幸 MCP 客户端、比价/下单会话、支付轮询（CLI/worker：`luckin_coffee.py`） |
| `core/amap.py` | `amap` 插件：高德 REST（附近找店、骑行路线、天气骑行建议、中点聚餐）、静态地图、缓存/QPS/月度配额（CLI/worker：`amap_tool.py`） |
| `core/qq_client.py` | QQ HTTP、凭据和 Token 生命周期 |
| `core/media.py` | 附件来源校验、限额及安全缓存 |
| `core/delivery.py` | 入站去重、投递状态、发送动作与分段进度 |
| `core/reply_policy.py` / `core/message_format.py` | 回复身份与节奏规则、保守格式选择及分段 |
| `core/sticker_policy.py` / `core/sticker_pack.py` | 上下文选图、素材校验/许可与离线入库 |
| `core/memory.py` | 消息窗口、摘要、结构化事实、版本化知识索引 |
| `core/supervisor.py` | 唯一重启拥有者、退避、退出和日志轮转 |
| `dashboard.py` | 本机会话认证、管理 API 与可操作的内嵌网页 |

`runtime.sqlite3` 属于投递层，`memory.sqlite3` 属于记忆层。数据库放本机磁盘，
不放网络共享目录。配置 JSON 仍是配置权威来源，带 revision 和跨进程锁；
`groups/registry.json` 是可重建登记信息。媒体保留在群目录中。

## 请求流程

QQ 事件先以稳定标识持久化，然后进入有界的分群执行通道。交互 ACK 有独立通道。
业务事件先校验群配置，再进行可选媒体处理和插件路由。命令插件优先于普通回复；
本地知识/记忆/图库动作的结果与有界上下文进入 Webhook payload。
Grok 例程继续负责模型推理及最终回复，仍可调用旧 CLI。

开启了 `reply_policy.collection_window_seconds` 的群，普通非 @ 消息先按固定截止时间收进同一窗口，到点后合成一次 webhook；`@` 和以 `/` 开头的命令不进入窗口，分组调度器也不会为了收集去睡眠。字段为 0、群被关闭或未开自然回复时，仍走原来的同发送者短等待。`inbound_debounce` 只抑制单条自然消息，不会丢掉已经收好的窗口。另有 opt-in 的 `natural_repeat_seconds`（默认 0）：同一批来源只放行一次自然回复，图片和很短的附和并入该批；@ 和新问题仍可回答。未打开的群不走这条守卫。

Webhook 的 `accepted` 仅表示 HTTP 接收成功，不代表外部 Routine 已完成。
发送先保存意图，再执行上传/各消息分段，记录成功、失败或结果不确定。
没有远端幂等确认时，网络超时不会被假定为“肯定未发送”。

`send_group.py --action-id` 可给重试使用同一操作身份。定时推送应包含计划时刻，
例如 `daily-news:<date>:<slot>`，下一期使用新身份。`--force` 允许人工重试同内容的 unknown/failed；不能改变身份绑定内容或重发 sent/partial。重复调用须检查结构化输出。详见 [回复策略](REPLY_STYLE.md)。

## 群、插件与权限

真实群 ID 仍作为群目录键，alias 仅用于查找。私聊按用户建立独立目录和上下文。
配置缺少新字段时使用兼容默认；损坏、非法或曾登记但缺失的配置关闭业务处理，
不会恢复为默认启用。后台保存使用 revision；并发编辑返回冲突。

内置插件为 `command`、`kb`、`sticker`、`ppt_poster`、`luckin_coffee`、`amap`、`digest`、`reply`、`natural_reply`。
可配置 `enabled_plugins` 和 `plugin_bindings`，同时保留旧 `features` 门控。
插件配置包括 `enabled`、`config`、`permissions`、`quota`。群 `quotas` 和插件
`quota` 可配置 `max_actions_per_day`，UTC 日界切换。配额为本地业务动作额度，
不替代 QQ 平台限流或模型供应商计费控制。

命令路由示例：`/记忆`、`/记忆 查看`、`/记忆 <内容>`、`/记忆 忘记 <记录ID>`、
`/知识 <问题>`、`/表情 <关键词>`、`@机器人 /海报 <需求>`、`@机器人 /PPT <主题> [N页]`。修改类动作核对 `admins` 或对应插件能力。
`digest` 消费带 `feature` 的 schedule 事件；外部 Cron 负责触发和内容生产，
此版本没有额外部署自动资讯抓取服务。

插件返回结构化动作，不直接发送 QQ。配置变更在下一次 dispatch 应用，执行中的
调用使用其快照。代码替换应停止接收并 drain 后重新注册或重启。
这是可信 Python 插件接口，不是任意第三方代码的安全沙箱。

## 记忆与知识

短期记忆包括入站事件与成功发出的消息；上下文同时限定条数和总字符预算。
中期摘要记录来源范围与方法，支持人工摘要与确定性摘录。确定性摘录不被称为
模型语义压缩。长期记忆是可编辑、带来源与 revision 的事实记录。

旧 `last-events.jsonl`、`sent-log.jsonl` 和 `notes.md` 按来源哈希/事件键导入，
重复导入不会重复添加。旧共享私聊日志不会未经归属确认就导入某个用户。
数据库中的人工修改不由再次导入旧笔记覆盖。JSON 导出可用于备份和恢复。

知识文档维护内容版本、chunk 和索引。FTS5 可用时使用全文排序及中文 n-gram；
不可用时使用受作用域限制的普通检索。搜索查询按文本处理，不直接拼接正则或
FTS 表达式。文档删除立即取消检索资格，旧 PDF 缓存不再单独成为知识来源。
此版本没有向量数据库依赖。

## 管理后台

从仓库根运行 `python official-bot/dashboard.py --port 8787`。
打开终端显示的本机地址，在页面输入一次性登录码；URL 不包含登录码。
后台绑定回环地址，使用 HttpOnly/SameSite Cookie、CSRF、Origin 和 Host 校验。
登录码使用后失效；丢失会话时重启后台取得新码。

页面支持群开关、插件配置、人设编辑、事实记忆编辑、知识搜索与刷新、图库预览和
健康状态。长操作返回后台任务状态。配置错误、CAS 冲突及索引失败会显示错误，
不会假装保存成功。图库接口只读取当前群的受管图片，不接受任意本机路径。

后台没有自动调用 shell 或替用户发送 QQ 的通用接口。运行健康分别显示 supervisor、
watchdog、网关及投递状态；进程存在不等于 QQ 已连接，也不等于模型回复成功。
后台为本机单管理员设计，远程多人部署需要另行增加 HTTPS 和用户身份系统。

## 验证

仓库根运行 `python tools/check.py`：语法解析、旧 CLI 参数集合检查、离线 unittest。
测试只使用临时数据、替代 HTTP 客户端及回环 HTTP 服务。
`official-bot/tests/intent_probe.py` 是真实 QQ 联网诊断，不在自动测试发现范围内。
提交前执行 `bash tools/secret-scan.sh`；全索引扫描使用 `--tracked`。
扫描器报告位置和规则，不输出命中的秘密内容。
