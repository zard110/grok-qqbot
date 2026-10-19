# MaxShenBi official QQ bot

AppID / AppSecret live in this directory (`appid`, `appsecret`, mode 600).
`group_openid.txt` = 默认群（Zwift练功房）的 group_openid。这些文件都不进 git，见 secrets.example.md。

## IP whitelist

**Keep IP whitelist EMPTY** on the open platform. This box’s egress IP rotates; a fixed allowlist will break sends.

## Gateway bridge

`official-ws-bridge.py` connects to `wss://api.sgroup.qq.com/websocket`, listens for
`GROUP_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` on our group, and POSTs OneBot-shaped
JSON (plus `official` / `msg_id` / `group_openid` / `at_me`) to the same webhook as NapCat
forwarder (`/root/forwarder/url` + `key`).

Start (also wired in `ensure-bots.sh`):

```bash
setsid nohup bash /workspace/qq-bot-persist/official-bot/run-official-ws.sh >/dev/null 2>&1 < /dev/null &
```

Log: `official-ws-bridge.log` — look for `READY`.

While official bridge is primary, **do not** start NapCat `ws-bridge.py` (double replies).
Keep `forwarder.py` :8080 and NapCat itself for emergency.

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
