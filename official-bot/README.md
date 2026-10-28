# MaxShenBi QQ 官方机器人核心模块（`official-bot/`）

本目录存放 QQ 开放平台官方机器人的核心网关桥接、消息发送、多群管理、知识库检索、斗图表情包工坊以及本地零 Token 看门狗脚本。完整安装与 Grok Bot 例程配置指南请先阅读仓库根目录的 **[README.md](../README.md)**。

---

## 🔑 本地凭证与身份文件

- 机器人的 `AppID` 与 `AppSecret` 存放在当前目录下的 `appid` 和 `appsecret` 文件中（权限设为 `600`）。
- `group_openid.txt` 存放默认群的 `group_openid`（当调用 `send_group.py` 不传 `--group` 参数时默认发往该群）。
- 所有密钥与身份文件均被 `.gitignore` 忽略，不会进入 Git 仓库。克隆仓库后的恢复步骤详见 **[secrets.example.md](secrets.example.md)**。

## 🌐 IP 白名单注意事项

**请在 QQ 开放平台后台将「IP 白名单」保持留空**（若平台要求填写，请在容器出口 IP 变化时及时更新）。因为 Grok Bot 沙盒容器的出口 IP 会随容器轮换而改变，固定死旧 IP 会导致发送消息与获取 Token 失败。

---

## 🔌 WebSocket 网关桥接与本地零 Token 守护

### 1. 官方网关桥接（`official-ws-bridge.py`）

连接 `wss://api.sgroup.qq.com/websocket`，实时监听群消息（`GROUP_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE`）、单聊消息（`C2C_MESSAGE_CREATE`）、群成员变动与按钮点击互动（`INTERACTION_CREATE`），将事件写入对应群的 `last-events.jsonl`，并在通过群开关门控后以结构化 JSON POST 到 Grok Bot Webhook（读取 `/root/forwarder/url` 与 `/root/forwarder/key`）。

**核心高可用与表情包特性：**
- **断线 `RESUME` 补收事件**：网络闪断重连时优先发送 `op=6 RESUME`（携带 `session_id` 与 `last_seq`），自动补齐断线期间漏掉的群消息；仅当会话失效时自动回退到 `op=2 IDENTIFY`。
- **约 61 秒独立看门狗与 TCP Keepalive**：后台独立线程实时监测心跳 ACK 与收包时间，一旦发现静默死链（约 61 秒无响应）立即主动断开重连；遇到 HTTP 401 时自动清理缓存 Token 并换新。
- **异步工作线程转发**：Webhook 转发在独立队列线程中执行，即使云端响应慢也绝不阻塞 WebSocket 心跳。
- **群自定义表情包自动识别缓存**：收到群友发送的自定义表情包时，自动下载缓存到本群 `stickers/` 目录，并在 `raw_message` 中标注是否已收录进 `index.json`，方便 Grok Bot 例程一键归档入库。

### 2. 本地零 Token 看门狗（`local-watchdog.py`）

通过 `run-local-watchdog.sh` 在后台常驻，每 **120 秒**执行一次本地巡检，**全程消耗 0 LLM Token**：
- 自动检查 `/root/forwarder/url`、`/root/forwarder/key` 与 `websocket-client` 依赖，缺失时自动从持久化目录恢复或补装。
- 确保全局只有 **1 个** `official-ws-bridge.py` 实例运行（挂掉则拉起，多开则清理旧进程）。
- 检查 `official-ws-bridge.log` 中的活跃信号（`ack ok` / `READY` / `RESUMED` / `recv`）：
  - **沙盒休眠唤醒防误杀**：若检测到两次巡检间隔异常（`> 210s`，说明容器刚从平台休眠中唤醒），先给予网关 **25 秒宽限期**自动完成 `op=6 RESUME` 补收积压消息；唤醒后若仍超时，首次动作强制降级为 `SIGUSR1` 热重连而非 `SIGTERM` 强杀，确保会话与 `last_seq` 不丢失。
  - 超过 **8.5 分钟**无活跃信号：向网关进程发送 `SIGUSR1` 触发热重连（优先 `RESUME`）。
  - 超过 **11 分钟**仍未恢复：发送 `SIGTERM` 终止网关进程，由守护脚本干净重启。
  - 仅当连续故障超过 **15 分钟**仍无法自愈时，才向 Grok Bot Webhook 发送 `post_type="watchdog_alert"` 告警（每 30 分钟最多 1 次），唤醒云端修复并通知主人。

### 启动方式（已集成在根目录 `ensure-bots.sh` 中）

```bash
# 推荐直接运行根目录的一键自检启动脚本：
sudo bash /workspace/qq-bot-persist/ensure-bots.sh

# 或手动拉起后台循环守护：
setsid nohup bash /workspace/qq-bot-persist/official-bot/run-official-ws.sh >/dev/null 2>&1 < /dev/null &
setsid nohup bash /workspace/qq-bot-persist/official-bot/run-local-watchdog.sh >/dev/null 2>&1 < /dev/null &
```

运行日志位于：`official-ws-bridge.log`（看到 `READY` 或 `RESUMED` 即为正常）与 `local-watchdog.log`。

---

## 🎨 斗图表情包工坊（`make_sticker.py`）

支持基于 25 套经典表情包底图（二哈、柴犬、熊猫头、佩佩蛙、汤姆猫、章鱼哥等）动态排版加字、生成 GIF 动效（`shake` 震动 / `zoom` 冲屏 / `bounce` Q弹）、按关键词从本地图库检索现成表情包（内置最近 25 次防重复记忆），以及将群友新图 MD5 去重入库：

```bash
# 1. 随机或指定模板加字生成静态梗图（支持用 | 分隔上下两行文字）
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py \
  --template random --text "就这？|重构了个寂寞" --out /tmp/meme_out.jpg

# 2. 生成带动态特效的 GIF 表情包（shake / zoom / bounce）
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py \
  --template pepe_smug --animate shake --text "看破不说破" --out /tmp/meme_shake.gif

# 3. 从现成图库中按关键词/情绪挑选表情包（自动避开最近用过的 25 张）
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py --pick "二哈 鄙视" --prefer-gif

# 4. 全库语义检索最匹配的现成图与模板候选
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py --search "打工人 摸鱼"

# 5. 将群友发送的新表情包去重入库（自动计算 MD5 防重复）
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py \
  --add-sticker stickers/xxx.gif --text "图上文字与画面描述" --category panda --tags "标签1,标签2"
```

---

## 💬 发送群聊与私聊消息（`send_group.py`）

更多高级参数（引用回复、语音条、视频、互动按钮、消息撤回）详见 **[USAGE.md](USAGE.md)**。

```bash
# 1. 发送纯文本被动回复（日常群聊默认推荐 msg_type=0，防客户端静默吞消息，且自动将 <qqbot-at-user> 转为 <@openid>）
python3 /workspace/qq-bot-persist/official-bot/send_group.py \
  --text '你好！' --msg-id 'ROBOT1.0_...'

# 2. 发送本地图片 + 真实 @（自动走 /v2/media-images 上传 qqbot-media:// 并合成单条 msg_type=2 消息）
python3 /workspace/qq-bot-persist/official-bot/send_group.py \
  --image /tmp/cover.png --text '<@MEMBER_OPENID> 战报来啦！' --msg-id 'ROBOT1.0_...'

# 3. 发送图片或表情包（支持 jpg / png / gif）
python3 /workspace/qq-bot-persist/official-bot/send_group.py \
  --image /tmp/meme_shake.gif --msg-id 'ROBOT1.0_...'

# 4. 主动推送消息（不带 --msg-id，仅用于定时资讯等主动任务）
python3 /workspace/qq-bot-persist/official-bot/send_group.py \
  --markdown '早安！今日资讯如下...'

# 5. 仅校验 AppID / AppSecret 是否有效（不发消息）
python3 /workspace/qq-bot-persist/official-bot/send_group.py --dry-credentials
```

---

## 📚 本目录文档索引

- **[USAGE.md](USAGE.md)**：`send_group.py` 完整命令行参数表与 Webhook Payload JSON 字段说明。
- **[MULTIGROUP.md](MULTIGROUP.md)**：多群目录结构、`config.json` 功能开关、`prompt.md` 群人设与 `kb.py` 群知识库使用指南。
- **[secrets.example.md](secrets.example.md)**：克隆仓库后需在本地恢复的密钥与配置文件清单。
- **[docs-explore.md](docs-explore.md)**：QQ 官方机器人 API v2 接口探索与真机实测记录。
