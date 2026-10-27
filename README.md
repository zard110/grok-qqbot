# qq-bot-persist

QQ 官方机器人 MaxShenBi 的代码（多群、富媒体、按钮、知识库、斗图工坊、本地守护），运行在 `/workspace/qq-bot-persist`。

- `official-bot/`：
  - `official-ws-bridge.py`：QQ 官方 WebSocket 网关桥接（支持断线 `RESUME` 补收事件、~61s 独立看门狗死链重连、401 自动换新 Token、群聊自定义表情包自动识别缓存与入库提示）。
  - `make_sticker.py`：斗图表情包工坊（支持 25 套模板加字、GIF 动效 `shake`/`zoom`/`bounce`、全库关键词检索 `--pick`/`--search`、新图 MD5 去重入库 `--add-sticker`）。
  - `local-watchdog.py` & `run-local-watchdog.sh`：本地零 Token 守护进程（每 120 秒自检进程、转发配置与 ACK 活性，自动发 `SIGUSR1`/`SIGTERM` 自愈，连续故障超 15 分钟才通过 Webhook 发 `watchdog_alert` 唤醒云端）。
  - `send_group.py`、`groups.py`、`kb.py`：消息/富媒体发送、多群注册表、知识库工具，以及文档（USAGE.md、MULTIGROUP.md）。
- `ensure-bots.sh`：开机/容器重置自启脚本，自动检查补装 `websocket-client` 并拉起 `official-ws-bridge.py` 与 `local-watchdog.py`。
- 克隆后怎么恢复本地密钥：见 [official-bot/secrets.example.md](official-bot/secrets.example.md)。
- 提交前扫描：`tools/secret-scan.sh`，安装方法 `ln -sf ../../tools/secret-scan.sh .git/hooks/pre-commit`。

以下内容不进仓库：已停用的 NapCat（安装包、napcat-run.sh、qq-healer.sh、qr-watch.sh、forwarder/ 里的旧转发脚本、onebot 配置）、所有密钥、群配置和聊天记录、表情包图库文件、日志、媒体文件、备份文件。
