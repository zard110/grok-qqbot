# 升级、数据迁移与回退

## 更新前

1. 备份现有群目录、媒体、密钥及 SQLite 文件。运行中的 SQLite 应使用其备份 API，
   或先停止写入者再复制，不能仅复制主文件遗漏 WAL。
2. 停止旧版 `run-official-ws.sh`、`run-local-watchdog.sh` 的无限循环及其子进程。
   特别检查 `/root/forwarder` 下旧 wrapper；只更新源码而保留旧重启循环会持续重启旧进程。
3. 保留真实群配置、prompt、notes 和媒体。不要删除数据来解决迁移错误。

## 安装与启动

```bash
cd /workspace/qq-bot-persist
python3 -m pip install -r official-bot/requirements.txt
bash ensure-bots.sh
```

依赖安装现在是部署步骤，ensure/watchdog 不再运行 pip 或恢复退役 NapCat。
原命令入口继续有效：`official-ws-bridge.py`、`local-watchdog.py`、`send_group.py`、
`make_sticker.py`、`groups.py`、`kb.py`、`ensure-bots.sh`。

默认数据目录为 `official-bot/`。代码与数据分开时设置：

```bash
export QQBOT_DATA_DIR=/srv/qqbot-data
export QQBOT_CODE_DIR=/workspace/qq-bot-persist/official-bot
export QQBOT_FORWARDER_DIR=/root/forwarder
bash /workspace/qq-bot-persist/ensure-bots.sh
```

`QQBOT_REPO_DIR` 可指定持久化仓库根，`QQBOT_PYTHON` 可指定 shell wrapper 使用的解释器。
所有入口必须使用同一个 `QQBOT_DATA_DIR`。密钥文件位置仍为数据目录的 appid/appsecret，
Webhook 文件仍按既有约定恢复；权限设为仅部署用户可读写。

每种角色的 supervisor 持有整个生命周期锁，包括重启等待期间。重复 ensure 是幂等操作。
日志按大小轮转，supervisor 的运行状态与 QQ 连接健康分开报告。

## 配置和图库

首次读取仍兼容旧 features；可以逐群启用 `enabled_plugins`、`plugin_bindings` 和 quotas。
registry 自动维护，真实 registry 与群配置均不会提交到 Git。
配置损坏会关闭该群业务，修复 JSON 后重新加载；不要把配置错误当作新群重新初始化。

图库新调用推荐显式加 `--group <alias>`。原来的默认群与旧 Zwift 图库兼容逻辑保留，
多群调用不再隐式共用 Zwift 文件。旧素材不会自动公开给其他群。
没有默认群文件时，无 `--group` 的旧命令只会尝试已登记的唯一 `zwift` 别名；
如果尚未登记任何群，且唯一旧图库确为 `groups/zwift/stickers`，可继续使用该受管图库，
不需要设置额外环境变量。显式 `--group` 永远不会借用此兼容路径。
图库的“最近使用”保留旧语义：`--pick` 选中后即计入本群近期记录，生成模板在生成成功后计入；
它不是 QQ 发送成功统计。`--search` 仅查询候选，不改变近期记录。
生成产物使用任务独立路径；若调用者显式指定同一输出文件，调用者仍需避免并发覆盖。

## 记忆与知识迁移

首次上下文读取和相关入口会导入本群旧文件，并用稳定事件键防重复。
也可用 `kb.py --group <alias> import-legacy` 显式执行导入。
导入不会删除原 JSONL 和 notes，坏行记录为导入统计，不能覆盖数据库中的人工纠错。

知识索引从源文档重建。PDF 仍需系统的 `pdftotext`，语音转换仍需 ffmpeg 和可选 pilk；
没有安装这些可选组件时相关操作明确失败，其余功能可继续使用。
FTS5 不可用时自动使用普通检索；搜索仍保持群范围过滤。

## 回退原则

先停止新 supervisor 和后台，再备份新数据库、导出记忆及配置。旧 JSONL 仍保留用于兼容，
数据库中新创建或修改的事实必须先导出，旧程序不会直接读取这些数据库内容。
随后恢复旧源码及已确认兼容的配置快照，最后启动一套旧进程。

不得同时运行新旧两个发送者。`unknown` 表示发送结果无法确认；回退或重启不能自动清除
该状态重发。确认远端状态后再决定恢复任务。已发送的 QQ 消息不能通过数据库回退撤销。

## 管理后台

```bash
python3 official-bot/dashboard.py --port 8787
```

使用终端生成的一次性登录码。不要把登录码或密钥复制到 Git、群聊或截图。
后台只绑定本机回环地址；需要远程维护时先使用受控的本地端口转发。
