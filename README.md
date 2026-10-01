# gh-assistant

面向中小型 Python 开源仓库维护者的 Bug Issue 修复助手：尝试复现问题、修改代码，并给出支持或拒绝修复的证据。

目标是把 Issue 推进到**可决策状态**，降低维护者判断修复是否值得继续处理的成本。系统将结果区分为 `VERIFIED_FIX`（本地证据齐全）、`CANDIDATE_FIX`（有补丁但仍需人工验证）和 `ABSTAIN`（证据或修复条件不足，主动停止）。目前尚无真实维护者效率提升数据。

[架构设计](DESIGN.md) | [安全模型](SECURITY.md) | [评测体系](EVALUATION.md) | [Docker 执行器](docker/README.md)

## 它具体能做什么

给定一个 GitHub Issue 和本地仓库路径，`gh-assistant` 会：

1. 获取 Issue、评论和仓库信息，并将这些内容视为不可信输入。
2. 为本次任务创建独立 branch 和 Git worktree，不修改用户当前 checkout。
3. 让主 Agent 阅读代码并提交计划；有复现命令时，由 Harness 在修改前记录实际失败及预期失败特征。
4. Agent 修改代码后，Harness 运行 targeted regression 和仓库验证命令，保存前后结果。
5. 测试失败时把真实证据交回主 Agent，最多进行两轮修复。
6. 测试通过后启动干净上下文、只读权限的 Reviewer 独立检查 Issue、diff 和测试证据。
7. Reviewer 驳回时允许一轮返工；Evidence Gate 结合复现、回归、仓库检查、修改范围和 Review 决定 Outcome。
8. `ABSTAIN` 不发布；`CANDIDATE_FIX` 可在审批后生成显著标注需人工验证的 Draft PR。JSON/HTML 报告列出未验证结论。

```text
GitHub Issue
     |
     v
隔离 Worktree -> 分析与复现 -> 修改代码 -> Harness 验证
                                      |          |
                                      |          +-- 失败 -> 修复
                                      v
                                独立 Reviewer
                                      |
                               通过 / 一轮返工
                                      |
                              Evidence Outcome Gate
                               /       |        \
                          VERIFIED  CANDIDATE  ABSTAIN
                               \       /        (停止)
                                人工审批
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
- **证据门禁**：`gh_assistant/evidence.py` 统一判断 Outcome；没有验证命令、缺少 patch 前失败/patch 后通过证据、缺少 Review 或越出计划范围，都不能成为 `VERIFIED_FIX`。默认允许“本地已验证”，但报告始终明确 CI 尚未运行；设置 `GHA_ALLOW_LOCAL_VERIFIED=false` 可要求 CI PASS 才给予 VERIFIED 结果（当前尚无 CI 反馈闭环，因而会降为 Candidate）。

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
| `gha triage OWNER/REPO [--dry-run]` | 分类开放 Issue；可只预览 label/comment 而不写入 GitHub |
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

测试结果以当前机器实际运行输出为准；Docker 不可用或 symlink 权限不足的测试会显式 skip。

`benchmarks/manifest.yaml` 保留 8 个 synthetic fixture，用于检验 Harness 回归行为：

```powershell
gha eval benchmarks/manifest.yaml --profile baseline --output .gha/eval
gha eval benchmarks/manifest.yaml --profile full --output .gha/eval
```

`benchmarks/real_issues/` 提供真实 Issue 的离线 task schema 和两个明确标记为**高真实性示例、并非真实 GitHub Issue**的任务：

```powershell
gha eval benchmarks/real_issues --profile full --output .gha/eval-real
```

真实 Issue 任务需要公开 Issue URL、固定 base commit 与本地 Git 快照；格式见 [任务模板](benchmarks/real_issues/TEMPLATE.md)。`solve_rate` 只统计拥有独立 hidden judge 的任务。评测还记录三态 Outcome、证据覆盖、耗时、调用和可用的成本数据；维护者审查分钟数等人工指标需要手工标注，不会自动编造。`baseline` 关闭 Skills、Memory 和 Reviewer，因此不会满足 `VERIFIED_FIX` 的 Review 条件，仍可用于比较 hidden judge 通过率。

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
benchmarks/          8 个 synthetic 任务和真实 Issue 离线任务格式/高真实性示例
tests/               单元、集成、恢复、安全、报告与评测测试
```

仓库只保留 `gh_assistant/` 这一套正式实现，CLI、测试和文档共享同一事实来源，避免教学原型与产品
代码并行演化。
