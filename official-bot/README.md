# MaxShenBi official QQ bot

AppID / AppSecret live in this directory (`appid`, `appsecret`, mode 600).
`group_openid.txt` = 默认群（Zwift练功房）的 group_openid。这些文件都不进 git，见 secrets.example.md。

## IP whitelist

**Keep IP whitelist EMPTY** on the open platform. This box’s egress IP rotates; a fixed allowlist will break sends.

## Gateway bridge & Local Watchdog

`official-ws-bridge.py` connects to `wss://api.sgroup.qq.com/websocket`, listens for
`GROUP_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` / C2C events, and POSTs OneBot-shaped
JSON (plus `official` / `msg_id` / `group_openid` / `at_me`) to the webhook (`/root/forwarder/url` + `key`).

Key reliability & sticker features in `official-ws-bridge.py`:
- **Session RESUME**: Reconnects with `op=6 RESUME` when `session_id` and `last_seq` are available so missed group messages are replayed, falling back to `op=2 IDENTIFY` on invalid session.
- **Independent ~61s watchdog & TCP keepalive**: Detects silent TCP drops within ~61s and forces a clean reconnect; refreshes cached token automatically on HTTP 401.
- **Custom sticker auto-cache**: Automatically identifies and caches incoming group custom stickers into the group's `stickers/` folder and tags whether they are already indexed in `index.json`.

`local-watchdog.py` (started via `run-local-watchdog.sh`) runs locally with **zero LLM token cost** every 120s:
- Ensures `/root/forwarder/{url,key}` and `websocket-client` are present.
- Ensures a single `official-ws-bridge.py` instance is running and receiving `ack ok` / `READY` / `RESUMED` / `recv` signals.
- Sends `SIGUSR1` (>8.5 min stale) or `SIGTERM` (>11 min stale) for local self-healing, and only sends a `watchdog_alert` webhook payload if unrecovered for >15 minutes.

Start (also wired in `ensure-bots.sh`):

```bash
setsid nohup bash /workspace/qq-bot-persist/official-bot/run-official-ws.sh >/dev/null 2>&1 < /dev/null &
setsid nohup bash /workspace/qq-bot-persist/official-bot/run-local-watchdog.sh >/dev/null 2>&1 < /dev/null &
```

Log: `official-ws-bridge.log` (look for `READY` / `RESUMED`) and `local-watchdog.log`.

## Sticker Workshop (`make_sticker.py`)

Generate custom text memes, animated GIF stickers (`shake` / `zoom` / `bounce`), pick/search from the local sticker index with anti-repeat memory, or add newly collected group stickers with MD5 deduplication:

```bash
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py --template random --text "就这？|重构了个寂寞" --out /workspace/napcat/media/meme_out.jpg
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py --template pepe_smug --animate shake --text "看破不说破" --out /workspace/napcat/media/meme_shake.gif
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py --pick "二哈 鄙视"
python3 /workspace/qq-bot-persist/official-bot/make_sticker.py --add-sticker xxx.gif --text "图上文字与描述" --category panda --tags "标签1,标签2"
```

## Send group message

Passive reply (preferred, uses 5‑min `msg_id` window):

```bash
python3 /workspace/qq-bot-persist/official-bot/send_group.py \
  --markdown $'**标题**\n正文' \
  --msg-id 'ROBOT1.0_...'
```

Plain text:

```bash
python3 /workspace/qq-bot-persist/official-bot/send_group.py --text 'hi' --msg-id 'ROBOT1.0_...'
```

Proactive (no `msg_id`; quota is tight — avoid for routine replies):

```bash
python3 /workspace/qq-bot-persist/official-bot/send_group.py --markdown '主动推送'
```

Credential check only:

```bash
python3 /workspace/qq-bot-persist/official-bot/send_group.py --dry-credentials
```

## Git 备份 / 克隆后恢复本地密钥

仓库根目录在 `/workspace/qq-bot-persist`，`.gitignore` 用**白名单**写法：只提交代码和文档，密钥、群配置、聊天记录、日志、媒体、备份文件一律不提交。
clone 之后按 [secrets.example.md](secrets.example.md) 放回本地文件。
提交前 pre-commit 钩子（`tools/secret-scan.sh`，安装命令：`ln -sf ../../tools/secret-scan.sh .git/hooks/pre-commit`）会扫描暂存内容，发现密钥、openid、QQ 号、webhook 就拦下这次提交。
