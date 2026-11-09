# 来源与适配说明

提炼日期：2026-10-12。来自一个实际运行的 Codex CLI 多任务项目，经用户授权提炼并上传，供不限领域的其他项目参考。不是对原项目领域成果的交付或公开发布。

原启动器相对路径：scripts/research/translation-preparation/cli_parallel.py。
提炼时原文件 SHA256：
A504EECFE34053E9259F17F6439525FA580DAA393837A8E6374B6825C071CB34

本包 cli_parallel.py 是通用化改写，不是该哈希对应的原文件。保留：有限批次、线程池启动独立CLI、stdin短输入、每job目录、原子状态文件、真实thread.started身份、逐任务结果通知、失败保留、无自动重试、dry-run默认、协调者统一计并发。

移除：原项目名、绝对路径、实际任务ID、文学阶段、素材规模、数据结构与验收专属条款；不带原项目prompt、日志、源文本或业务成果。
调整：工作区写入为默认、全权限必须另行授权；模型和effort显式传入、不写死候选列表；不固定8/16路；网络检索默认关闭；短项目规则由目标项目提供并冻结；外部输入锁定由协调者负责。
保持边界：ChatGPT登录适配，不自动继承API密钥；不承诺费用桶；不创建账户配置、邮箱、服务或自动化；不自动合并、不自动裁决。

本机只读核验 CLI 版本：codex-cli 0.162.0-alpha.17.2。
本机核验了 --no-daemon、--ask-for-approval、exec --ignore-user-config、--ephemeral、--skip-git-repo-check、--sandbox、--model、-c、--cd、--output-last-message、--json、--color、stdin提示接口。该版本是本机实际结果，不声称最新稳定版本或跨版本兼容。

agents.enabled、web_search 和 model_reasoning_effort 配置键继承了原实现用法；本次未请求模型验证它们的服务端效果。目标环境首轮小规模试运行需确认实际行为。--ignore-user-config 只是不加载用户config.toml，并不保证所有项目规则、插件、环境都被隔离；不忽略安全规则或自动修改目标项目配置。

官方参考（访问日期2026-10-12）：
https://learn.chatgpt.com/docs/developer-commands#codex-exec
用途仅为核对非交互exec、JSONL输出及resume接口；其余具体参数以本机 --help 只读结果为依据。
