# MaxShenBi 官方机器人：send_group.py 用法 + 收到的消息字段（2026-10-28 更新）

脚本：`python3 /workspace/qq-bot-persist/official-bot/send_group.py ...`
旧 CLI 的 --msg-id 会从本群匹配历史补全回复类型；找不到时保留 120 秒冷却。显式 --trigger 仍可指定自然闲聊、问句与 @。有来源 ID 时一律执行跨例程互斥。`--recent` 仅辅助观察，最终以发送器的事务校验为准。例程不得自动加 `--force`。

新增分层实现见 [ARCHITECTURE.md](ARCHITECTURE.md)，迁移见 [MIGRATION.md](MIGRATION.md)。
`--force` 允许人工重试同内容 unknown/failed 并绕过普通冷却；sent/partial 以及其他例程已占用的来源仍受保护。
重复业务调用可传 `--action-id <稳定操作标识>`，多段发送按 part 记录状态。
`--dry-run` 始终不上传或发送，包括与 `--upload-only` 组合时。

新格式、去重、斗图策略和可复制例程约束见 [REPLY_STYLE.md](REPLY_STYLE.md)。

普通对话新增 `--decision @独立JSON文件`：模型按语义决定 answer/clarify/ignore，并用 intent=request/casual 表达明确请求或闲聊。真实来源使用本轮 --msg-id 或 --event-id；选中有效待续问题才绕过普通话题冷却。默认编号文字；可选 --choice-buttons 在群已验证能力时显示按钮。完整字段、云端例程约定、上下文衔接及部署见 [CONVERSATION.md](CONVERSATION.md)。旧 --text 等入口不变，也不会仅因为文本含问号就建立追问状态。

新增参数：`--format auto|plain|markdown`、`--max-chars`、`--max-parts`、`--source-event-id`、`--trigger legacy|natural|at|question|command`、`--topic-text`、`--topic-key`、`--sticker`。默认 --text 仍为纯文本，但自动清洗表格、围栏、标题/加粗；--format auto 才按群验证能力升级类型。长消息有界分段、超限报错，不截断。

## 一、发送选项

| 选项 | 作用 |
|---|---|
| `--text "..."` | 纯文本（`msg_type=0`，**日常群聊普通文字回复默认推荐**，避免 QQ 客户端偶发静默吞掉 `msg_type=2` Markdown。**例外：文字里含 @（`<qqbot-at-user id="..." />` 或 `<@...>`）时自动改发 `msg_type=2` Markdown**，因为 QQ 群聊只在 Markdown 里渲染真 @，纯文本里的 `<@...>` 会原样显示成字） |
| `--markdown "..."` | Markdown（`msg_type=2`，适合挂 `--keyboard` 按钮或单条图文+真实 `@` 混排；`<@...>` 会自动转为 `<qqbot-at-user id="..." />`） |
| `--image PATH或URL` | 发图片（png/jpg；本地文件 ≤5MB，自动 base64 直传；http 链接由平台下载） |
| `--voice PATH或URL` | 发语音条（mp3/wav/silk；上传失败才自动转 silk） |
| `--video PATH或URL` | 发视频（mp4，本地 ≤5MB） |
| `--file PATH或URL` | 发文件（任意格式，本地 ≤5MB） |
| `--reply-to REFIDX_xxx` | 引用回复。值用收到消息的 `ref_idx` 字段（不是 msg_id） |
| `--keyboard JSON或@文件` | 在 markdown 下挂按钮，必须配合 `--markdown`。可以传 `{"rows":[...]}`（自定义）或 `{"id":"模板id"}` |
| `--msg-id ID` | 被动回复某条消息（5 分钟内有效，同一条最多回 5 次） |
| `--event-id ID` | 被动回复事件（按钮点击 INTERACTION、机器人进群 GROUP_ADD_ROBOT、GROUP_MSG_RECEIVE），用 payload 里的 `event_id` |
| `--recall MSG_ID` | 撤回自己 2 分钟内发的消息。id 在 sent-log.jsonl 的 `id` 字段，或发送结果 `results[].body.id` 里 |
| `--user-openid OPENID` | 发私聊（C2C），不发群；私聊不受 120 秒窗口限制 |
| `--upload-only` | 只上传媒体、打印结果，**不发消息**（测试用） |
| `--dry-run` | 只打印要发的请求内容，不上传也不发 |
| `--group X` | 目标群：别名（zwift）、群名、QQ 号或 group_openid。**不写则读取已配置的默认群**。旧参数 `--group-openid` 仍然可用 |
| `--feature F` | 该群 config 里 F 没开就拒发（exit 4），推送类例程用：`--feature daily_ai_news` |
| `--recent` / `--force` | 跟以前一样，但**按群分开**（每个群有自己的 sent-log 和锁） |

图片/媒体加说明文字：
- **本地图片 + 含 `@` 标签的说明文字**（`--image x.png --text "... <@openid> ..."` 或 `--markdown "... <qqbot-at-user ... />"`）：因 `msg_type=7` 在群聊中不解析真实 `@`，脚本会自动通过 `/v2/media-images` 上传图片获取 `qqbot-media://` 句柄，并合并为**单条** `msg_type=2` Markdown 消息发出（同时带图与真实艾特）。
- `--image x.png --text "说明"`（不含 `@`）：说明文字和图片在同一条 `msg_type=7` 消息里。
- 网络图片（http 链接）/语音/视频/文件 + 含 `@` 的说明：先发媒体，说明另起一条 Markdown 发（共 2 条），保证 @ 是真的。
- `--image x.png --markdown "说明"`（不含 `@`）：先发图，紧接着再发一条 markdown（共 2 条，第二条可以挂 `--keyboard`）。

例子：
```bash
# 生成的骑行排行卡片 + markdown 说明
python3 send_group.py --image /tmp/rank.png --markdown "【本周爬升榜】🏔️ 前三名见图"
# 引用某条消息回答（ref_idx 从 payload 里拿）
python3 send_group.py --markdown "这张图里功率曲线..." --reply-to "REFIDX_xxx" --msg-id "ROBOT1.0_xxx"
# 语音版资讯（先用 TTS 生成 mp3）
python3 send_group.py --voice /tmp/news.mp3
# 撤回刚发的重复回复
python3 send_group.py --recall "ROBOT1.0_xxx"
# 打卡按钮（指令按钮，点了在输入框插入 "@MaxShenBi /打卡 骑了"）
python3 send_group.py --markdown "## 🚴 今天骑了吗？" --keyboard @keyboards/checkin.json
```

sent-log.jsonl 每次发送记一行：`ts, at, scope(group/c2c), kind(text/markdown/image/voice/video/file/recall), content, id, ref_idx, keyboard`。

### 免申请的可点指令（markdown 里直接写）
`<qqbot-cmd-input text="/答 A" show="选A" />`：显示一个可点的标签，点了把 text 填进输入框，用户自己发出去。text/show 最多 100 字，含特殊字符时要 urlencode。群里**不支持** `<qqbot-cmd-enter>`（点了直接发送的那种）。
@ 人：QQ 群聊**只有 Markdown（`msg_type=2`）里的 `<qqbot-at-user id="member_openid" />` 会显示成真 @**（[官方文档·文本交互](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html)：群聊 @用户 仅支持 markdown 消息；`<@userid>` 是即将弃用的旧协议，群里纯文本会原样显示）。直接把 payload 里的 `at_tag` 写进 `--text` 或 `--markdown` 即可；`send_group.py` 发现 @ 会自动把这一段改成 Markdown 发送，并把 `<@id>` 统一换成 `<qqbot-at-user id="id" />`。id 必须是本机器人视角下对方的 `member_openid`（同一个人在不同机器人眼里 openid 不同）。含 @ 的 Markdown 被拒时不会自动降级成纯文本（降级只会得到假 @）。

### 按钮说明
- `action.type=2`（指令按钮）：点了插入「@机器人 data」，走普通消息事件。群里 `enter`（自动发送）不生效。
- `action.type=1`（回调按钮）：点了触发 INTERACTION_CREATE，bridge 将 ACK 放在独立通道尽快执行（网络故障仍可能导致失败），再转发成 `post_type=interaction`。同一 `group_id` 的按钮点了一个，其余变灰（适合答题、投票）。示例在 keyboards/quiz-callback.json。
- `action.type=0`：跳转链接。
- 每条最多 5 行 × 5 个；label 最多 10 字；style 0 灰框、1 蓝框、3 红字、4 蓝底；permission.type 0 指定用户、1 管理员、2 所有人。
- ⚠️ 群里自定义按钮还没有真机验证过（详见 docs-explore.md）。如果报 304xxx/305007/40034106 之类的错，就说明还需要在控制台开通，到时只用 `<qqbot-cmd-input>`。

## 二、webhook 收到的 payload 字段（bridge → 例程）

### 群消息 `post_type=message, message_type=group`（原有字段都保留）
- `raw_message`：**已经带上可读占位符**，按顺序是 `[引用: 谁: 内容]`、正文、`[图片: url]`、`[语音转文字: ...]`、`[视频: url]`、`[文件: 名]`、`[卡片(视频分享): 标题 url]`、`[表情:xx]`。纯图片、纯语音的消息也会转发。
- `text_only`：只有文字正文。
- `attachments[]`：`{kind: image/voice/video/file, url, content_type, filename, size, width, height, asr_refer_text?, voice_wav_url?}`。要看图就把 url 下载下来看（链接有时效）。
- `message[]`：文字段，加上每张图片的 `{"type":"image","data":{"url":...}}`。
- `quoted`：被引用的消息 `{source, text, name?, ref_idx?}`，没有引用就是 null。
- `card`：分享卡片 `{ark_type, ark_name, title, desc, url, source}`，没有就是 null。
- `ref_idx`：这条消息自己的 REFIDX，**用来 `--reply-to` 引用它**。
- `member_role`：owner/admin/member。
- `reply_context`：稳定 source_event_id/action_id、trigger、topic_text/topic_key 与发送约定。例程发送时必须透传这些字段。
- 原有：`at_me, from_bot, msg_id, event_id, member_openid, sender.nickname, ...`

### 多群字段（所有群事件都带，见 MULTIGROUP.md）
`group_openid, group_id(Zwift=<QQ群号>), group_name, group_alias, group_dir, group_enabled, features{...}`。
回复时用 `--group <group_openid>`；本群配置、笔记、知识库在 `group_dir` 下（config.json / notes.md / knowledge/，用 `kb.py --group <id> search` 查）。

### 群通知 `post_type=notice`
- `notice_type`：`member_join`（新人进群）、`member_leave`、`bot_added`、`bot_removed`、`proactive_off`、`proactive_on`。
- `member_openid`、`at_tag`（可直接写进 markdown 来 @ 他）、`event_id`、`raw`。
- 新人欢迎：主动发（不带 msg-id）markdown，用 `at_tag` @ 新人。bot_added 可以用 `--event-id` 被动回复。

### 按钮点击 `post_type=interaction`
- `button_data`、`button_id`、`member_openid`、`at_tag`、`scene`、`event_id`、`interaction_type`(11=按钮)。
- bridge 已经 ACK 过了。要回复就用 `--event-id <event_id>`（被动），或者主动发。

### 私聊 `post_type=message, message_type=private`
- `user_openid`、`raw_message`、`attachments`、`msg_id`。回复：`send_group.py --user-openid <user_openid> --msg-id <msg_id> --markdown "..."`。

图库与生成图默认不画英文署名页脚，来源/许可保存在索引与侧车。需画面署名时用 `make_sticker.py --embed-credit-footer`；旧素材包执行 `install_sticker_pack.py --group zwift` 可更新为无页脚版本（不删除旧文件或本土图库）。

## 三、做海报 / PPT（`ppt_poster` 插件，商汤日日新 SenseNova）

逻辑在 `core/sensenova.py`，命令行入口 `sensenova_make.py`（本地 `tools/sensenova_make.py` 只是兼容包装）。
密钥运行时读取 `official-bot/sensenova-key`（600 权限，不进 Git）或环境变量 `SENSENOVA_API_KEY`，从不打印。

```bash
python3 sensenova_make.py poster --prompt "周六夜骑团骑海报，Watopia 平路 40km" [--ref ref.png] [--no-image] [--name 标题]
python3 sensenova_make.py ppt --topic "乐扣低代码平台介绍" [--slides 6] [--images 2] [--extra "面向客户"] [--name 标题]
python3 sensenova_make.py rerender media/generated/<目录> [--name 新标题]   # 不调接口，只重渲染/改名
```

- 产物按业务标题命名：`<标题>.png`（海报）；`<标题>.pptx`、`<标题>.pdf`、`<标题>-长图.jpg` 和 `slideNN.png`（PPT）。
  标题优先级：`--name` → 模型给的 FILE_TITLE → 封面主标题 → 主题；deck/PPT/海报 等泛词会被丢弃。
- stdout 是一个 JSON（`ok`、`title`、`png`/`send_path` 或 `pptx`/`pdf`/`long_image`/`send_paths`、`fit`、`usage`），
  同时写入 `<目录>/result.json`。退出码：0 成功，2 失败，3 额度/限流（5 小时 12 万、每周 120 万积分），4 鉴权/缺密钥。
- 渲染用 Playwright + Chrome（`/opt/fetchurl-venv/bin/python`，没有则用当前解释器），字体 Noto Sans CJK SC；
  文字溢出会自动缩放，文字块重叠（交叠 >25% 较小块）会自动调用一次修复。发群用 `send_group.py --image <send_path>`，
  每次一张，超过 5MB 自动给 JPEG 副本。

群内命令（需 `features.ppt_poster=true` 且 `enabled_plugins` 含 `ppt_poster`，默认关闭）：
`@机器人 /海报 <需求>`、`@机器人 /PPT <主题> [N页]`。插件只返回 `media.poster` / `media.ppt` 动作，
`action_runner` 后台启动任务并在 `plugin_results` 里给出 `dir` 和 `result_file`；例程轮询 `result.json` 后自己发图。
同一条消息重投不会重复开任务；`plugin_bindings.ppt_poster.quota.max_actions_per_day` 可限制每天次数，
`config.default_slides` / `config.max_slides` 控制默认页数和上限。插件关闭时 `/海报`、`/PPT` 按普通 @ 消息交给例程。
