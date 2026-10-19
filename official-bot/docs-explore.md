# QQ 官方机器人 API v2 文档探索（2026-10-19，MaxShenBi）

文档根：https://bot.q.qq.com/wiki/develop/api-v2/ （左侧目录 JS 渲染；本次用 curl 抓了约 98 页群聊/单聊相关文档，频道专属的没细看）
文档索引页：https://bot.q.qq.com/wiki/develop/api-v2/autogen/index.html

## 现状（读代码得出）
- bridge：intents=1<<25，只处理 GROUP_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE，**只转发文本**。图片、语音、引用、卡片分享（attachments / msg_elements / ark_data）都被丢掉了。
- send_group.py：只发 msg_type 0/2（文本、原生 markdown），没用 keyboard、media、message_reference，也没用撤回。
- 用的域名是 api.sgroup.qq.com。2026-08-10 的变更记录说接口域名已经统一成 **api.bot.qq.com**，旧域名目前还能用，但以后可能要迁。

## ⚠️ 重要坑
intents 里带上没权限的位，WebSocket 会**直接报错断开**（见 event-emit 页「权限」一节）。要试 1<<24 或 1<<26，必须先另起一个临时测试连接，不能直接改正在运行的 bridge。

---
## 排名（好玩 × 可行）

### 1. 群里发图片、语音、视频、文件（msg_type=7）✅ 立刻能试
- 流程：先 POST /v2/groups/{gid}/files 上传，拿到 file_info，再发 msg_type=7 + media.file_info。也可以加 srv_send_msg=true，上传完直接发出去（算一次主动消息）。
- 不需要公网地址：用 **file_data 二进制直传（≤5MB）**，或者走分片上传（upload_prepare → PUT → upload_part_finish → files）。我们的 box 没有公网，这一点很关键。
- 类型：1 图片 png/jpg；2 视频 mp4；3 语音 silk/mp3/wav；4 文件。软上限 20/30/20/200MB，硬上限 200MB。file_info 有 ttl。每天发文件有容量上限（错误码 40093002）。
- 玩法：
  - 骑行数据卡片：用 PIL/HTML 截图生成 FTP/W/kg 排行、本周爬升榜 PNG 发群里。
  - 8 点 AI 资讯出一个「语音版」：TTS 生成 mp3，作为语音条发。
  - 把三体动画录成 mp4 直接在群里播。
  - 生图功能改成真图片发出（不再只给链接）。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html
- https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html

### 2. 能「看图」「听语音」：接收附件 ✅ 立刻能做（只改 bridge）
- GROUP_MESSAGE_CREATE 里本来就带这些：
  - attachments[]：url、content_type（image/*、voice、video/mp4、file）、宽高。
  - **语音自带 asr_refer_text（语音转文字）和 voice_wav_url**。
  - message_type=103 是引用消息，内容在 msg_elements（含被引用的原文）。
  - ark_data 是分享卡片，ark_type 有 tuwen/feed/miniapp/video_share/picture 等，里面带 title、desc、jump_url。
- 玩法：
  - 群友发 Zwift 结算截图，机器人识图点评功率曲线。
  - 发语音提问，机器人直接用 ASR 文本回答。
  - 有人分享 B站/小程序卡片，机器人读出标题做摘要。
  - 有人引用某条消息问「这个对吗」，机器人能看到被引用的原文。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html

### 3. 指令按钮（keyboard，action.type=2）+ markdown 可点指令 🟡 先试，可能需要开通
- markdown 底部最多挂 5 行 × 5 个按钮。按钮文字 ≤10 字。style：0 灰框、1 蓝框、3 红字、4 蓝底。permission：0 指定用户、1 管理员、2 所有人。可选 modal 二次确认。
- 三种按钮：type=0 跳链接；type=2 指令按钮，点了会在输入框插入「@bot data」，用户再发出去，所以**走的还是普通消息事件，不需要 INTERACTION 权限**；type=1 回调按钮，见第 4 条。
- 权限说明有出入：msg-btn 页写「自定义按钮【内邀开通】、按钮模板【申请使用】」。但发群消息接口的文档里，示例直接就是自定义 keyboard，也没标需要申请。markdown 本身 2026-04-23 起已经对所有机器人开放。**结论：直接发一条带 keyboard 的消息试试，报 305007 或无权限，就需要群主（控制台管理员）去控制台申请。**
- 纯文本版替代（✅ 立刻能用）：在 markdown 里写 `<qqbot-cmd-input text="/答 A" show="A" />`，会显示成可点的标签，点一下把文字填进输入框（群里不支持 cmd-enter 那种点了直接发送的）。
- 玩法：
  - ARC/AI 知识竞答：ABCD 四个按钮，点了就是「@MaxShenBi /答 B」，机器人计分、出排行榜。
  - 每日骑行打卡：「🚴 打卡」「📊 排行」按钮。
  - 群投票：「今晚 Zwift 骑哪条路线？」
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html
- https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html

### 4. 回调按钮 + INTERACTION_CREATE（intent 1<<26）🟡 需先确认权限
- 点击后用户聊天框里不出消息，平台把事件推给机器人。事件里有 button_data、button_id、group_member_openid。
- 机器人必须在 **3 秒内** PUT /interactions/{id} 回 {"code":0}，否则客户端一直转圈。code 还能填 2 操作频繁、3 重复操作、4 没有权限。
- 按钮 group_id：同一分组里点了一个，其他按钮会变灰，天然适合单选投票或答题。
- 回调后可以用 event_id 做被动回复（发群消息接口支持传 INTERACTION_CREATE 的 event_id）。
- 玩法：静默投票，不刷屏，机器人隔一会儿汇总一次票数；竞答抢答，先点对的得分，其他按钮变灰。
- 风险：intent 1<<26 不确定有没有权限，必须先用测试连接试。还依赖第 3 条的 keyboard 能发出去。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html
- https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/interactions_interaction_id.put.html

### 5. 新人欢迎 / 退群提醒（GROUP_MEMBER_ADD/REMOVE，intent **1<<24 GROUP_MEMBER_EVENT**）🟡 先试
- 事件里只有 member_openid，没有昵称。欢迎时可以用 `<qqbot-at-user id="openid" />` @ 新人。
- 玩法：新人进群自动发 markdown 欢迎卡：群规、Zwift 入门、常用指令、本周组队骑行时间，再附一张欢迎图。
- 风险：不确定要不要申请，要先用测试连接验证 intents=(1<<25)|(1<<24) 会不会被断开。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html

### 6. 引用回复 message_reference ✅ 立刻能用
- 用户消息事件的 message_scene.ext 里有 `msg_idx=REFIDX_xxx`；机器人自己发的消息，发送响应里有 ext_info.ref_idx。发消息时带上 message_reference.message_id=REFIDX_xxx，就会以引用形式显示。
- 用途：群里同时聊好几件事时，精准回某一条，配合「需要时才引用」的群风格。

### 7. 撤回消息 ✅ 立刻能用
- DELETE /v2/groups/{gid}/messages/{message_id}。只能撤回 **2 分钟内**发的。普通成员身份只能撤自己的；机器人是群管理员时，也能撤普通成员的。
- 用途：
  - 防重复：发现自己重复回复了，2 分钟内自动撤掉后一条（send_group.py 需要把响应里的 id 记进 sent-log）。
  - 「阅后即焚」答案：先发谜面，2 分钟内撤掉提示。

### 8. C2C 私聊 + 流式 markdown（打字机效果）🟡 需群友先加机器人好友
- C2C_MESSAGE_CREATE 已经在 1<<25 里了，bridge 现在会把它当未知事件丢掉。
- 用户需要先把机器人加为好友。可以用 POST /v2/generate_url_link 生成带 callback_data 的分享链接，FRIEND_ADD 事件会带上来源（scene）。
- **流式消息只支持单聊**（群消息明确写了不支持流式）：POST /v2/users/{openid}/stream_messages，input_state=1 表示生成中、10 表示结束，可以做出逐字输出的效果。
- 限额：
  - 单聊被动回复 60 分钟内有效，每条最多回 4 次。
  - 主动消息：未认证 5qps、30qpm；每个用户 20qpm、每天 1000 条。
  - is_wakeup 召回消息：在当天、1–3 天、3–7 天、7–30 天四个周期里各能发 1 条。
- 单聊还能用自定义菜单（PUT /v2/menu，最多 10 项，支持开关、发消息、链接、子菜单）。
- 玩法：私聊做一对一 AI 教练（训练计划、FTP 测试解读）、私发竞答成绩，群里只公布排行榜。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_stream_messages.post.html
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/menu-panel/index.html

### 9. 指令面板 /v2/panels（scope=group，可指定只对本群生效）🟡 API 能试（2026-08 新功能）
- 最多 20 个面板，每个面板最多 20 项。每项可以是 command（点了把名称填进输入框）或 link，可设置仅管理员可用。
- 玩法：给群做一个固定面板，放「/今日AI资讯」「/信息差」「/生图」「/竞答」「/打卡」「/排行」，降低新人上手难度。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels.post.html

### 10. 群管理（大多需要**机器人是群管理员**，群主设置）🔴
- 禁言：POST restrict_chat_setting，一次最多 20 人，最长 30 天，不能禁群主、管理员、机器人。可以做成「输了竞答被禁言 1 分钟」的娱乐惩罚，需要谨慎。
- 入群申请：GROUP_JOIN_REQUEST 事件里有验证消息或问答；可以审批、设置自动审批策略。玩法：入群答一道 Zwift 题，自动审批。
- 群成员列表 / 成员信息 / 群信息 / bot_state：标着「内邀接入中」，报错 11253 表示白名单才能用，暂时不可用。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.post.html

### 11. 群里用不了的（跳过）
- ARK 卡片：群和单聊只能收，不能发。即便申请到 23/24/37 模板，也是频道场景用。
- Embed：仅频道。
- 表情表态 reaction：仅频道。
- 消息反馈（点赞/点踩）、清空会话、切换模型等 type 13–16：属于「智能体」形态，不是群机器人。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/overview.html

---
## 频控 / 时效速查
- 群被动回复：5 分钟内有效，每条最多 5 次。同一 msg_id 回复多次时，msg_seq 要递增。
- 群主动消息：机器人维度个人认证 60qpm、未认证 30qpm；每个群 20qpm、**每天 1000 条**。管理员在机器人资料页可以关闭主动消息，会触发 GROUP_MSG_REJECT 事件。
- 发群消息接口 100 QPS；上传 50 QPS；撤回 10 QPS；INTERACTION 回应 50 QPS、3 秒内必须回。
- 群事件里的 GROUP_ADD_ROBOT 和 GROUP_MSG_RECEIVE，可以用 event_id 做被动回复（例如机器人刚被拉进群时发一条自我介绍）。
- 文档：https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html

## 建议落地顺序
1. 立刻能做（不用申请）：
   - bridge 转发附件和语音 ASR，让机器人能看图、听语音。
   - send_group.py 加 --image/--voice（file_data 直传）和 --reply-ref，sent-log 记下消息 id 以便撤回。
   - markdown 里用 cmd-input 做可点答题。
2. 用临时测试连接验证 intents 1<<24、1<<26 有没有权限；试发一条带 keyboard 的消息，看会不会报错。
3. 根据测试结果，请群主/控制台管理员去控制台申请自定义按钮或 intents，或者把机器人设成群管理员。

---
## 实测结果（2026-10-19 13:45–13:52）
- intents 探测（临时第二连接，各约 1 秒，主连接没受影响）：(1<<25)|(1<<24) → READY ✅；(1<<25)|(1<<26) → READY ✅。对照组 (1<<25)|(1<<9) → close 4014 "disallowed intents"，说明探测方法有效。
- 线上 bridge 已改为 intents=117440512（1<<24|1<<25|1<<26），13:46:56 READY，心跳正常。
- 群上传（file_data base64，srv_send_msg=false，不发消息）：png ✅、mp3 ✅、wav ✅、mp4 ✅、file ✅，ttl=86400。msg_type=7 真发还没验证。
- 自定义按钮：服务端先校验 msg_id（假 id 返回 40034024，过期 id 返回 40034005），再看 keyboard。所以不真发一条到群里，就判断不了按钮能不能用。
