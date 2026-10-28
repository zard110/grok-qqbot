# 合理追问与多轮衔接

模型决定何时回答、澄清或沉默；程序验证身份、插件权限、消息去重与短期任务状态。不新增关键词追问规则，不新增额外模型请求，不使用置信度百分比决定是否提问。实际模型在云端 Grok 例程运行，本仓库不伪装成本地模型服务。

## 模型约定

每次普通对话先读群人设、`payload.context`、`payload.context.reply_guidance` 和 `payload.dialogue`：

1. 已有足够信息或上下文已经给过条件，直接回答。
2. 非关键缺口：说明合理假设，先给有用答案，允许纠正。
3. 缺失条件会实质改变结论：一次问一个最重要的问题，可先回答不依赖它的部分。最多两轮；仍缺关键事实时说明限制，不编造、不强行给确定结论。
4. 未知客观事实先检索/核实，不把查资料变成反问用户。普通闲聊无价值则保持沉默。
5. `pending` 是同一个提问人的候选任务，不保证当前消息正在回答它。结合原需求、已知条件、问题、选项、当前文字与引用判断；相关才填写 `conversation_id`。序号也由模型结合选项理解。
6. 补充后直接继续原任务，不再问已知信息。群友明确换新任务且需要新追问时，可携带旧问题 ID 并 `restart:true`，以新消息为原需求。其他无关消息可直接回答或忽略，不自动覆盖旧任务。
7. 选项可省略；适合有限选项时提供 2–4 个易区分选项，始终接受自由文字。无需每次发卡片。用户不回答，不催问。

`intent=request` 表示模型从语义和对话关系判断这是明确请求；`casual` 表示普通闲聊。旧 `reply_context.trigger` 仍是兼容提示，不作为新协议的语义判断。来源是真实 @ /命令时保留原来源类别，普通请求使用 10 秒同话题防抖，闲聊使用原 120 秒冷却。经验证的待续任务单独放行，不需 `--force`。

## 发送接口（旧 CLI 不变）

模型将 decision 写入独立 UTF-8 JSON 临时文件，再用参数数组调用 CLI，避免把群聊内容拼成 shell。不要将 JSON 发给群友；并发例程各用独立文件，不共享一个 decision.json。

```json
{
  "action": "clarify",
  "intent": "request",
  "text": "这五个工具可以先按通用能力比较。要判断哪个适合你，还需要一个关键条件。",
  "question": "主要用于哪类界面？",
  "options": ["后台管理", "官网或落地页", "移动端"],
  "known": "比较当前上下文里的五个工具；尚未说明应用场景",
  "conversation_id": ""
}
```

```bash
python3 official-bot/send_group.py --group "<group_alias>" \
  --msg-id "<payload.msg_id>" --action-id "<payload.reply_context.action_id>" \
  --decision "@<独立JSON文件>"
```

用户回复“第一个，偏 Ant Design”后，模型生成：

```json
{
  "action": "answer",
  "intent": "request",
  "text": "按后台管理与 Ant Design 的要求，下面继续比较原来的五个工具……",
  "known": "后台管理，偏 Ant Design",
  "conversation_id": "<payload.dialogue.pending.id>"
}
```

使用本轮新的 `msg_id/action_id`，不要复用最初提问事件 ID。`ignore` 不发送、不消费问题，也不写入长期记忆。普通回答可省略 `conversation_id`；`answer` 也适用于明确说明假设后的答案。`clarify` 必须有 question，一条消息内展示完整问题与选项，过长报错不截断。`known` 只是短期任务摘要，不自动写入长期偏好。

新协议只接受网关近期记录的来源 ID（15 分钟内），不能凭空指定提问人。缺少来源或决策格式错误时停止执行，不自动换旧 CLI 绕过校验。原 `--text/--markdown/--image/...` 调用保持兼容；只发带问号的 `--text` 不会自动建立待续状态。

## 按钮与文字共用同一问题

默认发送编号文字。`--choice-buttons` 仅在群 `features.buttons=true` 且 `message_format.markdown_verified=true` 时生成 Markdown + 回调按钮，其余情况仍为文字。配置只表达已验证能力，不替代平台权限；上线前在测试群核对手机/桌面客户端。

按钮绑定不可变问题 ID 和选项序号。服务端核验群、提问人、有效期及问题状态，不依赖客户端按钮权限。其他群友不能替选，过期或已完成按钮安静忽略。按钮回调继续使用原独立快速 ACK；选项文字会进入有发言人信息的上下文，模型按本轮 `event_id` 回答。按钮和用户手动发“第二个”都交给同一流程。

仅对本模块生成的、正文已包含完整编号选项的按钮消息，允许在明确 HTTP 400/422 Markdown/keyboard 不支持时回退一次纯文本。普通自定义键盘仍保持旧回退规则。超时、5xx、权限/限流或 HTTP 成功不自动补发；成功不代表客户端一定显示。

平台参考：[腾讯官方消息按钮组件](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/pythonsdk/model/inline_keyboard.md)。SDK 文档部分描述频道场景，群回调能力以本机器人账号与客户端实测为准。

## 状态、并发与部署

- `runtime.sqlite3` 追加 `conversation_inputs` 与 `conversation_questions`，与 outbox 共用事务。记录来源/发言人绑定；同一群同一用户一次一个待续问题，15 分钟过期。不同群、不同用户互不覆盖。原需求、已知条件和有界 history（此前问题/选项/实际补充文字）一起保留，不仅依赖模型生成的摘要或最近 25 条群消息。
- 问题发送预占为 draft，确认发送才变 waiting。回答占位与 outbox 在同一个 `BEGIN IMMEDIATE` 中执行；不同入站消息同时回答同一问题也只允许一个发送者。成功后关闭旧问题，明确失败释放回答占位，未知/部分发送不自动重试。
- 原群开关、插件权限、入站配额、同来源去重继续生效。待续候选先于自然话题防抖交给模型；仅 presence 不授权绕过发送冷却，模型必须显式选中匹配的待续问题。并发中的问题不再提供为新的可回答候选。
- 输入绑定保留一天；问题过期一天后清理。业务不因到期主动发消息。重启保留 SQLite 状态，不恢复自动重发。
- `memory.sqlite3` 增量新增 actor/nickname/reply_to；保留旧消息并将未知作者留空。新消息、按钮选择、机器人回复保存归属；导出恢复兼容旧快照。长期事实不会被本模块自动修改。

部署：更新源码 → `python3 tools/check.py` 和 secret-scan → 重启网关 → 将本节模型约定与 `--decision` 调用接入实际云端例程。共享约定也会随每次 Webhook 放入 `dialogue.policy` 和 `context.reply_guidance`，不覆写各群私有人设；若现有例程硬编码只用 `--text`，仍需更新例程才会创建待续状态。仅修改默认 prompt 不会覆盖已存在的群 prompt。

自动回归覆盖来源/用户/群隔离、冷却放行、并发双回、按钮过期、明确失败与未知发送、两轮上限、换任务、旧数据库迁移及 CLI 兼容。上线后还需用真实多轮对话评估追问是否必要；单元测试不证明模型每次都理解正确。
