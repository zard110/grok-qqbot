# 本地密钥与身份文件恢复清单（不进 Git）

克隆本仓库到 `/workspace/qq-bot-persist` 后，请按下方表格在本机创建并填写这些密钥与身份文件（建议权限设为 `600`）。这些路径均已被 `.gitignore` 严格排除，且受 `tools/secret-scan.sh` 保护，**切勿强行提交到 Git 仓库**。

## 一、文件清单

| 路径（相对于仓库根目录） | 用途说明 | 内容格式示例 |
|---|---|---|
| `official-bot/appid` | QQ 开放平台机器人的 `AppID` | `<APPID>` |
| `official-bot/appsecret` | QQ 开放平台机器人的 `AppSecret`（机器人密钥） | `<APPSECRET>` |
| `official-bot/sensenova-key` | （可选）商汤日日新 SenseNova API Key，`ppt_poster` 插件 / `sensenova_make.py` 做海报和 PPT 用 | `<SENSENOVA_API_KEY>` |
| `official-bot/luckin-token` | （可选）瑞幸官方 MCP 令牌（open.lkcoffee.com/mcp 生成），`luckin_coffee` 插件点单用，订单记在生成令牌的账号上 | `<LUCKIN_MCP_TOKEN>` |
| `official-bot/amap-key` | （可选）高德 Web 服务 Key，`luckin_coffee` 把地址转经纬度用；没有则用 OpenStreetMap | `<AMAP_KEY>` |
| `official-bot/bot_uin.txt` | 机器人自身的 QQ 号（用于写入转发 Payload 的 `bot_uin` / `self_id`） | `<BOT_QQ>` |
| `official-bot/bot_name.txt` | （可选）机器人昵称，未配置时默认为 `MaxShenBi` | `MaxShenBi` |
| `official-bot/bot_openid.txt` | （可选）机器人在群内的 `member_openid`，网关收到 `@` 消息时也会自动学习记录 | `<32位十六进制OPENID>` |
| `official-bot/group_openid.txt` | 默认群的 `group_openid`（当 `send_group.py` 不传 `--group` 时默认发往该群） | `<32位十六进制GROUP_OPENID>` |
| `official-bot/intents.txt` | 已包含在仓库中（默认 `117440512` = `1<<24 \| 1<<25 \| 1<<26`），一般无需修改 | `117440512` |
| `forwarder-url`（同步至 `/root/forwarder/url`） | Grok Bot 中「QQ群自然介入」例程的 Webhook 触发地址（`ensure-bots.sh` 与本地看门狗会自动将其恢复到 `/root/forwarder/url`） | `https://...` |
| `forwarder-key`（同步至 `/root/forwarder/key`） | Grok Bot Webhook 鉴权 Bearer Key（自动同步到 `/root/forwarder/key`） | `<WEBHOOK_KEY>` |
| `official-bot/groups/<group_openid>/config.json` | 每个群独立的功能开关与别名配置（可参考 `groups/config.example.json`） | JSON 文件 |
| `official-bot/groups/<group_openid>/prompt.md` | 每个群专属的 AI 人设与回复风格说明（新群会自动从 `groups/_default/prompt.md` 复制） | Markdown 文件 |
| `official-bot/groups/<group_openid>/notes.md` | 每个群的长期记忆笔记（记录群友信息、群梗与约定） | Markdown 文件 |

---

## 二、快速恢复步骤

```bash
# 1. 克隆仓库到固定目录 /workspace/qq-bot-persist
git clone https://github.com/zard110/maxshenbi-qqbot.git /workspace/qq-bot-persist
cd /workspace/qq-bot-persist

# 2. 写入 QQ 开放平台凭证与 Grok Bot Webhook 密钥
echo -n "你的AppID" > official-bot/appid
echo -n "你的AppSecret" > official-bot/appsecret
echo -n "机器人QQ号" > official-bot/bot_uin.txt
echo -n "你的Webhook_URL" > forwarder-url
echo -n "你的Webhook_Key" > forwarder-key
chmod 600 official-bot/appid official-bot/appsecret forwarder-url forwarder-key

# 3. 验证凭证是否可用
python3 official-bot/send_group.py --dry-credentials

# 4. 一键同步配置并启动 WebSocket 网关与本地零 Token 看门狗
sudo bash /workspace/qq-bot-persist/ensure-bots.sh

# 5. 查看网关日志，出现 READY 即代表连接成功
tail -n 20 official-bot/official-ws-bridge.log
```

> 💡 **提示**：首次启动后无需手动创建群文件夹，只需在 QQ 群里 `@机器人` 发一条消息，网关就会自动根据 `official-bot/groups/_default/` 模板生成该群的 `config.json`、`prompt.md` 与 `notes.md`，随后按需修改即可立即生效。
