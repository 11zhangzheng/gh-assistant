# gh-assistant

一个能够**自动分析 GitHub Issue、修改代码、运行测试、独立审查，并在审批后创建 Draft PR**
的可审计 SWE Agent。

它不是一个简单的「LLM + 工具调用」Demo，而是一套面向真实软件仓库的 Agent Harness：模型负责
探索、推理和实现，Harness 负责上下文、工具、安全隔离、权限审批、状态持久化、崩溃恢复、评测
与可观测性。

[架构设计](DESIGN.md) | [安全模型](SECURITY.md) | [评测体系](EVALUATION.md) | [Docker 执行器](docker/README.md)

## 它具体能做什么

给定一个 GitHub Issue 和本地仓库路径，`gh-assistant` 会：

1. 获取 Issue、评论和仓库信息，并将这些内容视为不可信输入。
2. 为本次任务创建独立 branch 和 Git worktree，不修改用户当前 checkout。
3. 让主 Agent 自主阅读代码、搜索调用关系、制定计划并提交结构化补丁。
4. 由 Harness 自动执行项目测试，而不是相信模型声称「测试已通过」。
5. 测试失败时把真实证据交回主 Agent，最多进行两轮修复。
6. 测试通过后启动干净上下文、只读权限的 Reviewer 独立检查 Issue、diff 和测试证据。
7. Reviewer 驳回时允许一轮返工；通过后生成本地结果和可审计报告。
8. 在用户批准与当前 commit/diff 绑定的发布请求后，push 分支并幂等创建 Draft PR。

```text
GitHub Issue
     |
     v
隔离 Worktree -> 分析与计划 -> 修改代码 -> Harness 测试
                                      |          |
                                      |          +-- 失败 -> 修复
                                      v
                                独立 Reviewer
                                      |
                               通过 / 一轮返工
                                      |
                               哈希绑定的审批
                                      |
                               Push + Draft PR
```

## 核心设计

- **统一模型协议**：内部只使用 `Message / TextPart / ToolCall / ToolResult`，支持 Anthropic 和
  OpenAI-compatible 后端，Provider SDK 对象不会进入核心循环。
- **真实 Tool Call 驱动**：循环依据响应中的实际工具调用继续，不依赖 Provider 的 stop reason。
- **每次调用都有结果**：成功、失败、拒绝、未知工具和恢复结果都会形成结构化 `ToolResult`。
- **固定生命周期**：`intake -> planning -> implementation -> verification -> review ->
  repair/publish -> done`，控制器只维护状态约束，不替模型做具体判断。
- **可恢复执行**：SQLite 保存 run、event、checkpoint、task、approval、tool call 和 memory；崩溃后
  可以从主 Agent 或 Reviewer 的独立 checkpoint 恢复。
- **默认安全**：Docker 关闭网络、移除 capabilities、限制 CPU/内存/PID/时间，只挂载 worktree。
- **显式降级**：Docker 不可用时不会静默在宿主机运行；必须审批本次 run 的本地执行器。
- **最小多 Agent 拓扑**：只保留真正需要上下文隔离的独立 Reviewer，不常驻三 Agent 团队。
- **发布幂等**：审批绑定 commit/diff 哈希；代码变化后旧审批失效；PR 通过 branch 和隐藏 run marker
  去重。
- **可展示报告**：输出脱敏 JSON 与单文件 HTML，记录阶段、工具、测试、审查、审批、重试、token
  和可配置成本。

## 快速开始

要求 Python 3.11+、Git，以及可选的 Docker。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env

gha doctor
gha solve OWNER/REPO 123 --path D:\code\repo --no-publish
gha runs
gha report RUN_ID
```

使用 Docker 执行测试前先构建内置 Python runner：

```powershell
docker build -t gh-assistant-python:3.12 docker
```

没有安装 console script 时也可以直接运行：

```powershell
python -m gh_assistant --state-dir .gha runs
```

## CLI

| 命令 | 作用 |
| --- | --- |
| `gha doctor [--repo OWNER/REPO]` | 检查 Python、Git、Docker、模型、GitHub 和状态库 |
| `gha triage OWNER/REPO` | 读取并分类开放 Issue，写操作必须审批 |
| `gha solve OWNER/REPO ISSUE --path PATH` | 启动完整的隔离修复流程 |
| `gha resume RUN_ID` | 从 checkpoint 或待审批状态恢复 |
| `gha runs` | 查看持久化的运行记录 |
| `gha approvals [approve\|deny]` | 查看或处理审批 |
| `gha report RUN_ID` | 生成脱敏 JSON 和单文件 HTML 报告 |
| `gha eval MANIFEST --profile baseline\|full` | 运行确定性 benchmark |

默认预算为 30 个主 Agent 回合、120 次工具调用、2 次测试修复和 1 次 Reviewer 返工。预算耗尽
时进入 `needs_human`，并保留 worktree、checkpoint 和事件现场。

## 项目配置

宿主机配置来自环境变量。仓库内的 `gh-assistant.yaml` 属于不可信数据，只允许提供验证命令和
skill/include/exclude 提示：

```yaml
version: 1
verify:
  - ["python", "-m", "pytest", "-q"]
skills: ["python-testing"]
```

仓库配置不能扩大权限、选择本地执行器、提供凭据或开启发布。其他语言可以通过宿主机可信配置
指定 Docker 镜像与 argv 验证命令进行扩展。

## 权限与安全

权限规则按 `deny > ask > allow` 合并：

| 操作 | 默认策略 |
| --- | --- |
| worktree 内读取和结构化修改 | 允许 |
| 受限 Docker 中运行命令 | 允许 |
| 宿主机本地执行 | 询问 |
| GitHub label、comment、push、Draft PR | 询问 |
| merge、关闭 Issue、越权路径、未知能力 | 拒绝 |

GitHub token 只会进入宿主侧 REST 客户端或临时 askpass 环境，不会进入模型上下文、Docker、
审批载荷或报告。更多细节见 [SECURITY.md](SECURITY.md)。

## 测试与评测

测试使用 scripted fake model、临时 Git 仓库和 mock GitHub transport，不消耗模型额度，也不会
写入真实 GitHub：

```powershell
python -m coverage run --branch -m pytest -q
python -m coverage report --fail-under=85
```

当前本地结果：**58 passed、2 skipped、86% branch coverage**。两个 skip 分别是 Docker daemon
未启动，以及 Windows 当前权限无法创建 symlink；两项都不会伪装成通过。

项目包含 8 个 Python fixture issue：

```powershell
gha eval benchmarks/manifest.yaml --profile baseline --output .gha/eval
gha eval benchmarks/manifest.yaml --profile full --output .gha/eval
```

`baseline` 与 `full` 使用相同模型、worktree、验证和安全层；baseline 关闭 skills、memory 与
Reviewer，用于衡量 Harness 机制本身带来的收益。

## 为什么 MVP 不加入更多功能

- **不自动 merge 或关闭 Issue**：这是不可逆的仓库决策，不应交给 MVP 默认执行。
- **不删除有改动的 worktree**：中断后的恢复现场比自动清理更重要。
- **不运行 cron**：无人值守调度会扩大权限范围，却不能证明核心 Harness 更可靠。
- **不维护常驻三 Agent 团队**：消息路由和所有权状态会显著增加复杂度，Reviewer 已覆盖最有价值
  的独立审查需求。
- **不强依赖 MCP**：typed Tool 是稳定核心，未来可以把 MCP 作为新的工具适配层。

## 目录结构

```text
gh_assistant/        正式 Agent Harness 实现
gh_assistant/skills  内置可信 Skills
docker/              受限 Python Runner
benchmarks/          8 个确定性修复任务
tests/               单元、集成、恢复、安全、报告与评测测试
```

早期学习代码仍保留在 `core/`、`context/`、`memory/`、`skills/` 和 `github/` 中，正式产品实现位于
`gh_assistant/`。
