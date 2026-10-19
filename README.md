# qq-bot-persist

QQ 官方机器人 MaxShenBi 的代码（多群、富媒体、按钮、知识库），运行在 `/workspace/qq-bot-persist`。

- `official-bot/`：bridge、发送脚本、多群注册表、知识库工具，以及文档（USAGE.md、MULTIGROUP.md）。
- `ensure-bots.sh`：开机自启脚本，负责拉起 bridge。
- 克隆后怎么恢复本地密钥：见 [official-bot/secrets.example.md](official-bot/secrets.example.md)。
- 提交前扫描：`tools/secret-scan.sh`，安装方法 `ln -sf ../../tools/secret-scan.sh .git/hooks/pre-commit`。

以下内容不进仓库：已停用的 NapCat（安装包、napcat-run.sh、qq-healer.sh、qr-watch.sh、forwarder/ 里的旧转发脚本、onebot 配置）、所有密钥、群配置和聊天记录、日志、媒体文件、备份文件。
