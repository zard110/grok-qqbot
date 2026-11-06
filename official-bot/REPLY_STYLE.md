# QQ 回复格式、去重与斗图执行约定

适用：2026-10-28 现网兼容更新。旧 CLI 参数继续有效；仅传 --text / --msg-id 也能获得默认清洗和来源去重。

## 官方能力与保守边界

查阅腾讯官方 [Markdown 消息文档](https://q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html)，并核对腾讯维护的 [文档源码](https://github.com/tencent-connect/bot-docs/blob/645787a45937e5d9c4f0f61afefdffde0f38696e/docs/develop/api-v2/server-inter/message/type/markdown.md)。该快照明确列出标题、强调、链接、图片、列表、引用、分割线等，Markdown 还涉及单独开通权限；未明确列出表格、围栏代码块。不能据此断言所有 QQ 客户端都支持标准 Markdown。

[发送消息](https://github.com/tencent-connect/bot-docs/blob/645787a45937e5d9c4f0f61afefdffde0f38696e/docs/develop/api-v2/server-inter/message/send-receive/send.md) 定义纯文本、Markdown 和媒体类型；群被动回复有时效及次数限制，`msg_id + msg_seq` 用于重复识别。该页新公告与残留主动推送说明有冲突，现网能力以控制台和实际测试为准，不据旧段落新增主动权限。

[富媒体文档](https://github.com/tencent-connect/bot-docs/blob/645787a45937e5d9c4f0f61afefdffde0f38696e/docs/develop/api-v2/server-inter/message/send-receive/rich-media.md) 区分上传与发送、注明媒体凭据时效及群/私聊用途。仓库保留已上线的本地上传、图文 @ 路径；这些部署经验不是所有账号通用的官方能力保证。

| 场景 | 发送策略 |
| --- | --- |
| 日常问答、短列表、未经真机验证的账号 | `--text` 默认仍发送纯文本；检测到表格、围栏、标题/加粗就自动清洗，无需增加参数 |
| 标题、列表、引用 | 本群已验证时设置 `message_format.markdown_verified=true`，使用 `--format auto` |
| 比较表格 | 默认逐项“字段：值”；仅本群账号及目标客户端验证后额外开启 `tables_verified` |
| 代码 | 默认“代码（语言）：”加缩进；仅验证后开启 `fenced_code_verified`，长代码保留围栏配对分段 |
| 按钮、图文真实 @ | 保留显式 `--markdown` / 原图文上传路径；不做会丢失按钮或图片的自动降级 |
| 长文 | 默认每段最多 1000 字符、最多 3 段；用 `--max-chars 120..2000 --max-parts 1..5` 调整。超过上限报错，要求摘要，绝不静默截断 |

默认 --text 不会因为本群验证过 Markdown 就升级成 Markdown；普通无格式文字保持原样。唯一例外是 @：文字里含 `<qqbot-at-user id="..." />` 或 `<@...>` 时，该段自动改发 Markdown（QQ 群聊只在 Markdown 里渲染真 @，纯文本里的 `<@...>` 会原样显示），且这类消息被拒时不自动降级。显式 --markdown 保留旧行为，--format auto 才按验证能力选择消息类型。`--format` 只选择呈现，不授予平台权限。长表先转逐项说明再分段。媒体与 Markdown 说明分别计段；纯文字说明优先搭载媒体第一段。每段使用稳定且不同的 `msg_seq`；首次成功后再失败记为 partial，不重发已成功部分。默认 3 段给同一入站消息的其他平台操作留余量，不承诺其他调用已消耗的额度。

自动回退仅发生在明确的 HTTP 400/422 Markdown 不支持/未开通错误，且内容不含图片、按钮、可点命令。已核验错误码可填 `message_format.markdown_reject_codes`。超时、5xx、权限错误、限流和 HTTP 成功均不自动重发。HTTP 成功不证明客户端已显示；发生静默吞消息时先核实，再由人决定下一次格式或补发，不能循环发送同内容。

## 可直接放进云端例程的约束

> 先读取本群 config/prompt、payload.context、context.reply_guidance 与 payload.dialogue。信息足够先给结论，普通问答 1–3 句；缺少会改变结论的关键条件才问一个问题，能合理假设就说明假设并先回答，不重复问上下文已有条件。普通对话按 [澄清与衔接协议](CONVERSATION.md) 使用 --decision；语义判断由模型完成，非关键词规则。步骤用编号列表；比较默认用“方案—优点—限制”逐项列出。事实、建议、未知项分清，不自行打开 Markdown 验证开关。
>
> 使用 payload.reply_context 的 source_event_id、action_id、trigger、topic_text、topic_key。一次事件最多调用一次发送命令，图和说明合成同一调用，由发送器处理分段；重投沿用同一 action_id。不要用当前时间、随机数或模型输出充当 source_event_id；不要为通过冷却改 source/action/topic。
>
> 普通闲聊只有能补充新信息或自然接梗时才介入。同一话题短时间不反复点评；有明确问题或被 @ 时优先回答。exit 3/4 是策略跳过，安静结束；exit 5 是 unknown/partial，停止自动重试并保留结果。例程不得自动加 `--force`。
>
> `plugin_errors` 表示本地动作没有获准或没有完成；仅 `local_actions_completed=true` 的结果可视为已执行。不得把转发理解为本地记忆写入成功。
>
> 新图未入库提示仍要识图并执行其中的 `make_sticker.py --group ... --add-sticker ...`。群友上传不代表拥有商用许可；来源不明仅留在私有群库，不混入公开许可素材包。
>
> 斗图先 `--recommend`，结合上下文和上一张 mood。认真求助、痛苦或明确不要表情时不斗图。每次最多一张；图已表达情绪就不重复一段文字。改字用口语短句，最多两行，不把整段回答塞进图。发送加 `--sticker`，自然介入自动等待约 0.7–1.8 秒并检查群级间隔/额度；不要自行 sleep 很久或连续补图。

示例（尖括号值由 payload 填入，真实身份不写进仓库）：

```bash
python3 send_group.py --group "<group_alias>" \
  --text "结论：先降低强度。\n1. 今天轻松骑。\n2. 明天根据恢复情况调整。" --format auto \
  --msg-id "<msg_id>" --source-event-id "<reply_context.source_event_id>" \
  --action-id "<reply_context.action_id>" --trigger "<reply_context.trigger>" \
  --topic-text "<reply_context.topic_text>" --topic-key "<reply_context.topic_key>"

python3 make_sticker.py --group "<group_alias>" --recommend "笑死 哈哈" \
  --context "<最近对话>" --previous-tag laugh --seed "<source_event_id>"
python3 send_group.py --group "<group_alias>" --image "<推荐结果.path>" --sticker \
  --msg-id "<msg_id>" --source-event-id "<source_event_id>" \
  --action-id "<action_id>" --trigger natural --topic-text "<当前话题>"
```

调用进程应使用参数数组，勿把用户文本拼进 shell。Shell 示例中的多行文本应传真实换行，不依赖 shell 展开 `\n`。

## 去重所有权与阈值

待续对话的例外见 [CONVERSATION.md](CONVERSATION.md)：当前用户存在有效问题时，先将候选任务交给模型，不做 12 秒自然消息拦截；只有 --decision 明确选中且发送器验证通过的任务续答绕过普通话题冷却。同来源去重、插件权限和配额仍保留。模型用 intent=request/casual 表达语义判断，旧 CLI 继续使用关键词提示兼容。

只有 --decision --choice-buttons 自动生成的澄清按钮，因正文已经包含完整编号选项，支持在明确不支持格式时一次性退为纯文本；其他自定义键盘不改变既有规则。

1. 网关 inbox 以群/事件稳定身份持久去重，Webhook 重投不重复派发。所有入站消息仍写入有界上下文来源。
2. 自然消息使用每群 12 秒前沿防抖，只抑制相同/高重合词组的话题突发；@、命令、明确问句绕过。它不等待收集尾部消息，也不重放已转发请求。
3. 发送侧 `reply_claims + outbox` 在同一个 SQLite 写事务中校验并占位；多进程共享数据目录时，哪怕不同例程生成不同措辞/操作 ID，同一来源也只允许一个逻辑回复。多段属于同一个操作。
4. 自然回复默认 120 秒群冷却、300 秒规范化内容哈希去重、600 秒话题重复抑制；同来源、同内容和精确话题键查完整窗口；中文双字词/英文词 Jaccard 在最近 500 条候选中判断相近话题，只是可解释启发式，不是语义模型。无问号的“对比一下”“打个分”“推荐/解释/再来”等请求也归为 question，使用同话题 10 秒阈值，@/命令不套普通自然冷却；不同来源的正常问答可重复必要事实。
5. 表情默认间隔 5 秒、每小时最多 60 张；已确认发送记录参与选图避重。`sent-log` 是审计/旧 CLI 冷却兼容来源，不是新并发仲裁锁；SQLite 事务才是权威。
6. unknown/failed 不占普通 120 秒冷却；unknown 的同来源/同内容仍保留不确定性保护。人工 `--force` 可重试相同操作的 unknown/failed、绕过普通节奏限制，不能改写操作内容、抢占活跃来源或重发 sent/partial。操作锁内遇到遗留 pending/inflight 会先标记 unknown，核实后才能显式重试。无 msg_id 的主动发送无法保证远端 exactly-once，人工强制重试可能重复。

`reply_policy`、`message_format` 在群 config 中按请求读取；字段非法时关闭群处理，不沿用 enabled=true。只要存在来源 ID，legacy 也执行同源互斥；--msg-id 优先作为 QQ 入站来源，不能通过另传 --source-event-id 绕过。未显式传 --trigger 且未传 --topic-text 的 --msg-id 调用，会先查本群 last-forward.json，再查 last-events.jsonl 的有界尾部（最多 1 MiB / 1000 条）。严格匹配群和消息 ID，兼容原始 QQ 事件，恢复 trigger/topic_text/topic_key；旧 natural 提示按最新请求规则复判。显式参数不被覆盖，无法匹配时保持 legacy 120 秒冷却，仍有来源去重。私聊暂不自动恢复群历史。

来源检查优先于旧日志冷却，同操作重复调用返回既有结果，不产生新占位。两次不同 action_id 处理同一来源时返回 source_event_already_claimed，即使带 --force 也不绕过；这不会影响一个 action 内的长文分段。

## 固定收集窗口

`reply_policy.collection_window_seconds` 是按群设置的秒数，缺省 **0（关闭）**。合法值与其它节奏字段相同：有限数字，0 到 86400。它和 `inbound_debounce` 不是同一个开关。

关闭时（0、未写、群 `enabled` 为 false，或 `features.natural_reply` 为 false）：

- 普通消息仍走原来的同发送者约 3.5 秒 `claim_followup`。不同发送者会打断这次合并，新消息还会把截止时间改成「现在起最多 1.5 秒」，所以它不是固定窗口。
- `inbound_debounce` 仍是自然话题的前沿抑制：第一条相似自然消息马上转发，窗口内后面的相似自然消息被跳过。它不收集，也不重放。
- 只 @ 的群不会因为这个字段开始把普通聊天发给例程。

开启时（群启用、`natural_reply` 为 true、秒数大于 0）:

- 截止时间 = 该窗口第一条普通消息写入 inbox 的时间 + 秒数。后面的消息只加入，不后推。
- 窗口里可以有多个发送者，也包括没有 @ 的问句。
- 到点后只发 **一次** webhook。`text_only` / `raw_message` 是多行 `昵称: 正文`。`payload.collection.messages` 是逐条副本。
- `delivery_id` 和 `Idempotency-Key` 用第一条消息的 inbox id。`msg_id` 用窗口内最后一条，方便被动回复。
- `@机器人` 和以 `/` 开头的命令立刻单独转发，即使窗口还开着。调度器不会为了收集睡满整个窗口。
- 这次合并后的请求不再走 `inbound_debounce`，避免前沿抑制把整批丢掉。
- 消息先落在 durable inbox。进程重启后，未到点的窗口继续等到原截止时间；已经开始发送但没有终态回执的，用同一个 anchor id 再发一次。已记录 accepted/skipped/failed/unknown 的不重发。
- 例程把这一次请求当成一轮。不要按窗口里的每一行再各发一次处理。

例程侧要收到这批普通消息，该群必须本来就会转发非 @ 消息（`natural_reply: true`）。推荐只在现有 `reply_policy` 上增加字段，不要整段替换，以免把其它冷却改回代码默认值：

```json
"reply_policy": {
  "collection_window_seconds": 10
}
```

回滚：

1. 把该群 `collection_window_seconds` 设为 `0`。之后的新消息立刻回到旧路径。已经打开的窗口仍会在原来的截止时间发一批，然后停止。不需要为了这个字段重启，但进程里必须已经是带本功能的代码。
2. 若连这一批也不要发：先停 bridge，再在 `runtime.sqlite3` 执行下面的语句，然后启动。

```sql
DELETE FROM collection_members WHERE rowid IN (
  SELECT m.rowid FROM collection_members m
  JOIN collection_windows w ON w.scope = m.scope AND w.generation = m.generation
  WHERE w.status != 'closed');
UPDATE inbox SET status = 'pending', error = NULL WHERE status = 'collecting';
DELETE FROM collection_receipts WHERE status = 'inflight';
DELETE FROM collection_windows WHERE status != 'closed';
```

3. 若把代码退回到没有本功能的版本：必须先做第 2 步。否则 `collecting` 行不会被旧调度器取走，这些消息会一直停在库里。
4. 不要用回滚覆盖本机上比远端更新的 amap / luckin 改动；本功能不修改那两个插件。

## 同一批自然回复守卫

`reply_policy.natural_repeat_seconds` 默认 **0（关闭）**。不写这个字段的群保持原样，包括只 @ 的群和没有打开自然回复的群。它不是 `natural_cooldown`，也不是 `inbound_debounce`。

建议和 10 秒收集窗口一起，只加在需要的那一个群的现有 `reply_policy` 上：

```json
"collection_window_seconds": 10,
"natural_repeat_seconds": 180
```

打开之后：

- 一次自然转发，或一次收集批次，在 sqlite 里原子占住这批消息的 `msg_id`。截止时间是占住的那一刻加上秒数，后来的消息不把截止时间往后推。
- 同一批里的其它来源，以及窗口内同一话题的自然消息、纯图片、折叠后不超过 4 个字的附和，不再第二次转发。发送侧对仍在 pending/inflight/sent/partial 的自然回复返回 `topic_recently_answered`。并发的多次 `send_group.py` 只有第一次能占住。
- `@`、以 `/` 开头的命令、以及被现有规则判成新问题的消息照常转发，也可以回答。
- 有实质内容、而且下面说的话题规则对不上的另一件事，可以再占一轮。
- 转发没有真正发出去（HTTP 4xx、缺少 webhook）时放开这次占用，同一次收集批次仍可按原来的 anchor 重试。`unknown` 视为可能已经发出，占用保留。
- 进程重启后占用还在，到期前不会对同一批再答一次。

话题规则只做词面判断，不做语义嵌入：

- 至少 3 个字的连续中文，或至少 3 个字符的英文/数字词，一边包含另一边；或者沿用原来的 token 重叠（交集至少 2，且 Jaccard 不低于 0.65）。
- 没有共同片段的长改写，收集窗口会把 10 秒内的原文收成一次转发。窗口之外，这种改写仍可能再答一次。四句不同措辞的回复能收成一句，是因为它们对应同一批来源，不是因为比较了回复正文。
- 很短的附和在守卫打开期间不会单开一轮。一句同样很短、但确实是另一件事、又没有被问句规则认出的话，也会等到这次占用结束。

回滚这个守卫：把该群 `natural_repeat_seconds` 设为 `0`。新消息立刻不再占用。不必删除 `natural_leases`。代码退回旧版时，旧代码不读这张表；收集窗口若已有 `collecting` 行，仍要先做上一节的 SQL。

## 许可素材包与部署

本次收录 20 张 OpenMoji 原始 PNG，覆盖笑、围观、怀疑、认可、惊讶、感谢、庆祝、猫狗等常用反应，提供对应 20 个可改字模板。它们是开放许可反应图，不是来源不明的影视/角色截图。

来源： [OpenMoji](https://github.com/hfg-gmuend/openmoji)，固定版本 `aeb8bb3a59e2de39c754ac79180c8131c906acea`。图形按 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 授权，允许商用，需署名、注明修改，改编按相同许可分享；原始许可全文与每张来源及 SHA-256 在 `assets/openmoji-reactions/`。这些许可仅覆盖该素材包，不为旧群库补授版权。

```bash
python3 official-bot/install_sticker_pack.py --group zwift
```

安装器离线验证全部哈希，原图放本群 `stickers/templates/openmoji_*.png`，可发送图默认仅将透明背景合成白底，不再加画面页脚，以其 SHA-256 命名；写入 index、来源清单、许可全文及每图 .license.json。再次运行安装器按原始素材身份更新旧带页脚索引，保留本土图库和自定义文字/标签，不新增另一套重复条目，也不删除旧文件，避免已保存的路径失效。未注册任何真实群时允许在 `groups/zwift/stickers/` 先准备旧布局；注册环境解析 zwift 到已有群目录，不创建假 openid、不自动启用插件。准备的旧布局可用无 `--group` 的旧图库 CLI 访问；正式群仍显式指定群。

生成改字图/GIF 默认不绘制页脚，始终写 `.license.json`（作者、来源、许可、修改说明、输出 SHA）。需要画面署名时，安装器或 make_sticker.py 可显式加 --embed-credit-footer；函数参数为 embed_credit_footer=False。分享时仍需通过群公告、机器人资料或可访问的素材来源页等合理方式提供署名与许可入口；仅存在服务器侧车文件不会自动把署名传给收图者。源码包中的原始 PNG 可公开提交，实际群图库、配置和聊天记录继续被 Git 忽略。

部署顺序：更新源码 → 离线检查 → 安装素材 → 把上述约束接入云端例程 → 重启网关 → 在测试群确认客户端显示后才开格式验证选项。数据表采用追加建表，旧数据保留；回退代码也不要删除新账本，否则会失去防重复记录。现网共享目录继续遵循兼容补丁的 root/box 权限约定。

## 旧本土图库的选图兼容

`infer_mood()` 优先保留显式 mood，缺失时从 text/tags/category 推断：笑、质疑、围观、认可、鼓掌、震惊、破防、尴尬、猫狗等；先看情绪词，再以角色词兜底。推断只发生在读取时，不批量改写原始 159 张图库或补造来源许可。接龙使用推断后的 mood，结果也返回 mood 给下次衔接。

非 OpenMoji 图片必须先命中上下文或情绪接龙，才获得 +2 的本土偏好；无关图不会仅因 category 非 openmoji 被选中。严肃语境熔断、稳定选择种子、近期使用惩罚，以及发送侧 5 秒 / 每小时 60 张继续有效。
