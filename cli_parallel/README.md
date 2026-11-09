# Codex CLI 多任务并行参考包

供任意领域项目复制的轻量架构：一个主任务负责有限范围、依赖和正式合并；多个独立 CLI 任务完成互斥工作包；成果一旦就绪即可交给下一环节，不等待整批。专业质量规则由目标项目自己定义，不预设文学、工程或科研领域。

这是一份从实际项目提炼的参考实现，不是通用工作流平台，也不是原项目整库备份。只复制有用部分，不必照搬全部角色。阅读顺序：本页 → OPERATING-GUIDE.md → templates/。需要执行时再看 cli_parallel.py 与 VALIDATION.md。

## 包含什么

- cli_parallel.py：标准库启动器，默认只打印执行计划；独立输出目录、身份记录、超时、失败保留、逐任务完成通知。
- OPERATING-GUIDE.md：任务拆分、并发配比、上下文减负、质量链、暂停恢复、跨领域迁移。
- templates/：项目规则、主任务入口、工作包、审查与交接模板。每份都是按需填写的起点，不是必填表单。
- examples/：两个完全合成的无外部依赖任务，用于目标机器首次小规模试运行。
- tests/：离线契约测试，不调用模型、不登录、不启动真实 Codex。
- PROVENANCE.md、VALIDATION.md、SHA256SUMS.txt：提炼来源、实际验证边界、包内校验值。

不含原项目正文、业务成果、实际任务 ID、日志、凭据、账户配置或旧管理体系。不含自动派工、定时器、邮箱、仪表板、全局限流服务和自动验收器。

## 核心关系

主任务 → 已就绪工作包 → 并行生产 → 按需独立核查／裁决 → 唯一合并出口 → 项目交付

核查发现问题只返给受影响工作包；没有变更的有效结论不重新研究。可自动检查的路径、字段、哈希、重复键、测试结果交给程序；语义、科学解释、设计取舍和质量判断交给具备相应能力的任务。

独立性、验收深度、是否需要第三方裁决，取决于目标项目风险。三角色链不是通用强制要求。独立任务也不保证统计独立的错误，需要有效测试和原始证据。

## 先离线，再两路试运行

依赖：Python 3.10+、目标机器已有并获准使用的 Codex CLI 和 ChatGPT 登录。参考适配器不安装软件、不创建密钥，也不切换账户。实际核验环境见 VALIDATION.md。

在解压目录运行：

~~~powershell
python -B -m unittest discover -s tests -v
python -B cli_parallel.py --help
~~~

下面的模型、effort、真实协调者 ID 和可执行文件路径，由目标项目主任务根据本机能力填写；不要原样使用占位符。Windows 如果 PATH 只给出 codex.cmd，需找到对应真实可执行文件，不要为迁就包装器改成 shell=True。

~~~powershell
$cliPath = '<本机 codex 可执行文件绝对路径>'
$modelName = '<已验证可用的模型>'
$effortName = '<该模型支持的 effort>'
$coordinatorId = '<真实协调者任务 ID 或 CI run ID>'
python -B cli_parallel.py examples/task-a.txt examples/task-b.txt --exe $cliPath --out .\runs\smoke-01 --policy templates\project-policy.txt --owner $coordinatorId --model $modelName --effort $effortName --workers 2 --timeout 900
~~~

以上是 dry-run，不登录、不调用模型、不创建 runs/smoke-01。确认任务范围、模型、权限与费用授权后，原命令末尾增加 --execute 才真正执行。第一次只跑两个合成任务，确认身份、结果和错误处理，再接真实项目。参考包导出时没有执行这一步。

同一批次使用同一模型配置。不同角色可分别调用启动器，主任务负责所有批次加上其他辅助的总并发。默认 2 路不是建议的最终规模；16 路也不是架构固有限制。启动器不替你施加账户级全局上限。

## 权限和费用

默认 workspace-write，工作目录为各 job；网络检索默认 disabled。只读分析可选择 read-only，但此时辅助可能不能写 artifacts；最终 result.md 由 CLI 宿主输出。研究任务经授权可选 --web-search live。工程任务需要修改代码时，优先向 job 提供独立源码副本／受控工作树，而不是允许所有任务同时写主仓库。

danger-full-access 仅在另有明确授权、且理解风险时使用，并要求显式 --allow-full-access。该开关本身不是授权。独立目录不等于完整沙箱，工作区沙箱也不等于隐藏全机文件；隔离强度必须结合本机 CLI、系统、插件与外部容器验证。参考启动器不会自动增加可写目录、安装依赖或降低权限。

仅要求 ChatGPT 登录，并清除子进程继承的 OPENAI_API_KEY 与 CODEX_API_KEY。这不保证只消耗周额度、不保证不使用 credits，也不能计算每份任务真实账单。账户计费规则和用量由当前产品决定，禁止把 token 数或进程退出码当作账单证明。

官方说明支持 codex exec 的非交互执行、JSONL 输出及单独的 resume 子命令：[Codex developer commands](https://learn.chatgpt.com/docs/developer-commands#codex-exec)。本参考选择 --ephemeral，恢复靠磁盘交接材料启动新任务，不依赖持久会话恢复。具体参数以目标机器 --help 为准。

## 首次迁移完成标准

两份任务在各自目录留下结果和不同的真实身份；至少一次故障不会污染其他任务；主任务可从准确交接继续；一个领域内最小成果完成“生产—必要核查—正式交付”。不是把槽位跑满，也不是只证明多个进程能同时启动。
