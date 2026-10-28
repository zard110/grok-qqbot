# MaxShenBi 多群支持（2026-10-19 起）

一个官方机器人服务多个 QQ 群，每个群有自己的开关、风格、笔记和知识库。Zwift练功房 的行为保持不变。

## 分层升级补充

- 配置支持 `revision`、`enabled_plugins`、`plugin_bindings` 和 `quotas`。
- `groups/registry.json` 自动维护登记和别名；该文件属于本机运行数据。
- 无效配置会关闭业务处理；后台编辑使用 revision 防止覆盖并发更新。
- `make_sticker.py --group <alias>` 明确选择本群图库，私聊按用户分目录。
- 数据库与旧 JSONL 的迁移、回退见 [MIGRATION.md](MIGRATION.md)。
- 完整角色/配额/插件说明见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 目录结构
```
official-bot/
  groups.py                  # 群注册表（bridge / send_group / kb 共用）
  kb.py                      # 知识库：search / sync / list / add-doc
  groups/
    _default/config.json     # 新群模板
    zwift -> <GROUP_OPENID>     # 别名软链（只是为了方便，程序按 config 里的 aliases 解析）
    <GROUP_OPENID>/   # Zwift练功房
      config.json            # 名称、别名、开关、风格、管理员、知识库配置
      notes.md               # 本群笔记（事实、约定、梗）
      knowledge/             # 文档（md/txt/pdf，可放子目录）
        repos/threebody/     # kb.py sync 拉下来的仓库
      last-events.jsonl      # 本群原始事件（上下文）
      sent-log.jsonl, .send.lock   # 本群兼容记录；幂等与状态在 runtime.sqlite3
      last-forward.json      # 最近一次转给例程的 payload（排查用）
  c2c/<USER_OPENID>/         # 每位私聊用户独立日志和状态
  _system/last-events.jsonl  # READY 等系统事件
  last-events.jsonl, sent-log.jsonl, .send.lock  -> 软链到 Zwift 群文件夹（兼容旧例程和命令）
```

## config.json 字段
```json
{
  "name": "群名", "aliases": ["短名"], "group_openid": "...", "qq_group_id": 123456,
  "enabled": true,
  "features": {
    "natural_reply": false,   // 不 @ 也自然插话（关掉时：非 @ 消息只记上下文，不转给例程）
    "at_reply": true,         // 被 @ 时回复
    "daily_ai_news": false,   // 8 点 AI 资讯
    "x_info_push": false,     // X 信息差推送
    "image_gen": false,       // 生图
    "buttons": false,         // 按钮回调（INTERACTION）是否转发
    "welcome": false,         // 新人进群/退群通知是否转发
    "media_understanding": true, // 看图/听语音
    "knowledge": true         // 允许 kb.py 用本群知识库
  },
  "style": "本群回复风格（例程照着写）",
  "admins": ["member_openid", "..."],
  "knowledge": {"docs_dir": "knowledge/", "repos": [{"url": "https://github.com/x/y", "branch": "main", "local_path": "knowledge/repos/y"}]}
}
```
没写的 feature 用 `_default` 的值。改 config.json **立即生效**，bridge 每条消息都会重新读，不用重启。

## bridge 怎么处理
- 不再只认一个群。所有群消息、通知、按钮点击都会转发，带上 `group_openid, group_id, group_name, group_alias, group_dir, group_enabled, features`。
  - `group_id`：Zwift 是 <QQ群号>；其他群用 config 里的 `qq_group_id`，没填就用由 openid 算出的稳定整数。
- **门控**（在 bridge 里做，省掉例程运行）：
  - `enabled=false` → 什么都不转发（事件仍记到本群 last-events）。
  - @ 消息看 `at_reply`；非 @ 消息看 `natural_reply`；进群/退群看 `welcome`；按钮回调看 `buttons`（回调总会先 ACK）。
  - 被丢掉的会在 log 里记一条 `gated ...`。
- **未知群**（机器人被拉进新群、或新群第一条消息）：
  - 自动用 `_default` 建文件夹，调 `/v2/groups/{id}/info` 取群名（失败就叫「未命名群-xxxxxx」）。
  - 默认只开 `at_reply`，被 @ 才礼貌回答；log 里记 `NEW GROUP auto-created`。
  - GROUP_ADD_ROBOT 会把拉机器人的人记到 `added_by`。
- 私聊照旧转发（`message_type=private`, `group_openid=""`, `c2c_dir`）。

## 新增一个群
1. 把机器人拉进群。bridge 会自动建好 `groups/<openid>/`（看 log 里的 `NEW GROUP`）。
2. 编辑 `groups/<openid>/config.json`：填 `name`、`aliases`（比如 `["ai-lab"]`）、`qq_group_id`、`style`，按需打开 features。
3. 写 `notes.md`；往 `knowledge/` 放文档，或者在 `knowledge.repos` 里加仓库后执行 `python3 kb.py --group ai-lab sync`。
4. 发消息：`python3 send_group.py --group ai-lab --markdown "..."`。
5. 想先手动建（机器人还没进群）：`python3 -c "import sys;sys.path.insert(0,'.');import groups as G;G.ensure_group('<openid>','群名')"`。

## 开关功能
- 直接改 JSON，比如关掉某群插话：`"natural_reply": false`；整个群停用：`"enabled": false`（send_group.py 也会拒发，exit 4）。
- 推送类例程加 `--feature daily_ai_news` 或 `--feature x_info_push`：开关没开就 exit 4，不会发出去。

## 知识库
```bash
python3 kb.py --group zwift search "FTP 测试"      # 关键词；多个词时整句和每个词都会搜
python3 kb.py --group zwift search "canvas" --json  # 给程序用
python3 kb.py --group zwift sync                    # clone/pull config 里的仓库（只支持公开仓库）
python3 kb.py --group zwift list
python3 kb.py --group zwift add-doc /path/to/file.pdf
```
- 只读取指定群自己的文件夹，路径越界会被拒绝。PDF 自动转成文本缓存在 `knowledge/.pdftext/`。
- 搜到返回 0，没搜到返回 1。
- **严禁**用 A 群的知识或笔记回答 B 群。

## 回滚
备份文件后缀 `.bak-1405`。回滚步骤：
1. 把 official-ws-bridge.py、send_group.py 恢复成 .bak-1405。
2. 删掉 last-events.jsonl、sent-log.jsonl、.send.lock 三个软链，把 groups/9F23…/ 里对应的文件移回原位置。
3. `sudo cp official-ws-bridge.py /root/forwarder/ && pkill -f 'official-bot/official-ws-bridge.py'`

## 群人设 prompt.md
- 位置：groups/<group_openid>/prompt.md。webhook 例程每次回复前先读，优先级高于 config.style 和默认风格，但突破不了安全规则（防重复、不泄密、不串群、功能开关）。
- 结构建议：你是谁 / 群主题 / 说话方式 / 群梗与玩法 / 推送口味（daily_ai_news、x_info_push 读这一节）/ 禁忌。
- config.style 只写一句「详见 prompt.md」，避免两处重复。群里有谁、链接、ID 等事实放 notes.md。
- 新群自动复制 groups/_default/prompt.md。人设只由机器人主人决定，机器人自己不改。
- 群人设含群内信息，被 .gitignore 忽略，只有 _default/prompt.md 进仓库。
