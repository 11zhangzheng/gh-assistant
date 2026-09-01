# gh-assistant

`gh-assistant` 是一个可审计、可恢复的 GitHub SWE Agent harness。它接收 issue，在独立
Git worktree 中让模型探索与修复，由 harness 自动执行验证，再让干净上下文、只读权限的
Reviewer 检查 issue、diff 与测试证据；只有审批绑定的代码哈希仍然有效时，才会 push 并创建
Draft PR。

这个项目的重点不是堆叠 Agent 数量，而是展示 Agent/AI Infra 岗位真正关心的控制面：模型负责
判断与行动，harness 负责生命周期、上下文、工具、安全、预算、持久化、恢复、评测与可观测性。

[English README](README.md) | [架构](DESIGN.md) | [安全](SECURITY.md) | [评测](EVALUATION.md)

## 核心机制

- 统一内部消息协议，Anthropic/OpenAI-compatible SDK 类型不会进入核心循环。
- 固定并持久化的阶段：`intake -> planning -> implementation -> verification -> review ->
  repair/publish -> done`。
- 每个 issue 独占 run、branch 和 worktree，不修改用户当前 checkout。
- Docker 默认关闭网络、只挂载 worktree、限制 CPU/内存/进程/时间并移除 capabilities。
- Docker 不可用时必须审批本次 run 的本地执行；非交互模式停在 `waiting_approval`。
- `finish_task` 只表示模型交回控制权，测试是否通过只能由 harness 自动验证决定。
- Reviewer 使用全新上下文和只读工具，最多触发一轮返工。
- SQLite 保存 run、event、checkpoint、task、approval、tool call 和来源可追踪的 memory。
- publish 审批绑定 commit/diff 哈希；代码变化后旧审批自动失效。
- 报告默认隐藏源码正文并扫描凭据模式，生成脱敏 JSON 与可独立打开的 HTML。

## 快速开始

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

需要 Docker 执行时先构建内置 Python runner：

```powershell
docker build -t gh-assistant-python:3.12 docker
```

## 权限模型

规则按 `deny > ask > allow` 合并：worktree 内读取、写入和 Docker 命令默认允许；本地执行、
push、评论和 Draft PR 需要审批；merge、关闭 issue、删除有改动的 worktree永久拒绝。仓库内容、
issue、评论和 repo skill 都是不可信数据，不能修改权限或向执行环境注入凭据。

## 测试与评测

```powershell
python -m coverage run --branch -m pytest -q
python -m coverage report --fail-under=85
gha eval benchmarks/manifest.yaml --profile baseline --executor local
gha eval benchmarks/manifest.yaml --profile full --executor local
```

当前本地结果为 **58 passed、2 skipped、86% branch coverage**。两个 skip 分别是 Docker daemon
未启动，以及 Windows 当前权限无法创建 symlink；它们都不会伪装成通过。

`baseline` 与 `full` 使用同一模型、worktree、验证和安全层，baseline 关闭 skills、memory 与
Reviewer。这样比较的是 harness 机制贡献，而不是模型或执行环境变化。

## 为什么 MVP 不做更多 Agent

常驻 triager/fixer/reviewer 团队会引入消息路由、所有权和一致性状态，但不会自动提高修复质量。
当前设计只在有明确独立性价值时引入 Reviewer，并把主要复杂度投入到恢复、权限、幂等和评测。
同理，MVP 不自动 merge/close issue、不运行 cron、不删除保存中的 worktree，也不把 MCP 作为核心
依赖。这些都可以后续通过现有 typed Tool 边界扩展。
