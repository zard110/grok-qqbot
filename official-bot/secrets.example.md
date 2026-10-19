# 本地密钥 / 身份文件（不进 git）

clone 之后，在本机按下面的清单手动放好这些文件（权限 600）。它们都被 `.gitignore` 排除了，**不要提交**。

| 路径（相对仓库根） | 内容 | 示例 |
|---|---|---|
| `official-bot/appid` | QQ 开放平台机器人 AppID | `<APPID>` |
| `official-bot/appsecret` | AppSecret（机器人密钥） | `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `official-bot/bot_uin.txt` | 机器人 QQ 号（payload 里的 bot_uin/self_id） | `<BOT_QQ>` |
| `official-bot/bot_name.txt` | （可选）机器人昵称，默认 MaxShenBi | `MaxShenBi` |
| `official-bot/bot_openid.txt` | 机器人在群里的 openid（bridge 也会从 @ 消息里自动学到） | 32 位十六进制 |
| `official-bot/group_openid.txt` | 默认群的 group_openid（send_group.py 不带 --group 时发往这个群） | 32 位十六进制 |
| `official-bot/intents.txt` | 已提交（117440512 = 1<<24 \| 1<<25 \| 1<<26），一般不用改 | |
| `official-bot/groups/<group_openid>/config.json` | 每个群的配置，从 `groups/config.example.json` 复制后填写 | |
| `official-bot/groups/<group_openid>/notes.md`、`knowledge/` | 群笔记、群知识库（knowledge 里的仓库可以用 `kb.py sync` 重新拉） | |
| `forwarder-url` → `/root/forwarder/url` | webhook 地址（ensure-bots.sh 开机时会复制过去） | `https://...` |
| `forwarder-key` → `/root/forwarder/key` | webhook 鉴权 key | |

恢复步骤：
1. `git clone` 到 `/workspace/qq-bot-persist`（路径写死在脚本里）。
2. 按上表放好文件：`chmod 600 official-bot/appsecret forwarder-key forwarder-url`。
3. 为每个群建 `official-bot/groups/<openid>/config.json`（参考 `groups/config.example.json`），也可以等机器人被拉进群后由 bridge 自动创建。
4. `bash ensure-bots.sh` 启动 bridge，在 `official-bot/official-ws-bridge.log` 里看到 `READY` 就算成功。
5. 运行时数据（last-events、sent-log、ref-cache、日志）会自动重新生成。
