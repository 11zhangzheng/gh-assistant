# gh-assistant

一个跑在 GitHub 仓库上的 agent 助手：自动分类 issue、打标签、起草修复、review PR。
每个模块都从 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的一个 harness 机制学来，
再改造成产品级。**目标不是抄教学代码，是把 s01-s20 每个机制重新推导一遍。**

> **一个夜晚的狗粮化循环**（完整版形态）：
> cron 定时启动 → 扫 open issues 变成任务（s12）→ 记忆/技能按仓库加载（s07/s09）→
> triager 子 agent 分类（s06）→ fixer 队友自动认领（s17）→ 在独立 worktree 里修（s18）→
> 开 PR 前过 plan 审批协议（s16）→ 每轮结束提取记忆（s09）→ 压缩兜底（s08）

---

## 快速开始

```sh
pip install -r requirements.txt
cp .env.example .env      # 填入 ANTHROPIC_API_KEY + GITHUB_TOKEN
python cli.py ping yourname/yourrepo       # 先验证连通性
python cli.py triage yourname/yourrepo     # agent 分类 open issues
python cli.py interactive yourname/yourrepo   # REPL
```

`triage` 是当前唯一可运行的纵向切片：模型读 repo 元数据 + issue 列表，逐个分类（bug/feature/question）、
建议标签，需要写仓库时（打标签/评论）走 s03 权限管线等你确认。

---

## 架构：每个 harness 机制 → 一个模块

| 模块 | 章节 | 机制 | 状态 |
|------|------|------|------|
| `core/loop.py` | s01 | Agent Loop | ✅ 骨架 |
| `core/tools.py` | s02 | 工具注册表 / dispatch | ✅ 骨架 |
| `core/permissions.py` | s03 | 三门权限管线 | ✅ 骨架 |
| `github/api.py` + `tools.py` | s02 | GitHub 工具集 | ✅ triage slice |
| `cli.py` | — | 入口 | ✅ triage slice |
| `context/skills.py` | s07 | 按仓库加载技能（构建/测试命令） | ⬜ 待做 |
| `context/compact.py` | s08 | 四层压缩管线 | ⬜ 待做 |
| `context/system_prompt.py` | s10 | 运行时组装 prompt | ⬜ 待做 |
| `memory/` | s09 | 每仓库记忆 + 提取 + 整理 | ⬜ 待做 |
| `tasks/` | s12 | issue→task，blockedBy 依赖 | ⬜ 待做 |
| `team/` | s15-s18 | 消息总线 / 协议 / 自治 / worktree | ⬜ 待做 |
| `scheduler/cron.py` | s14 | 每晚定时 sweep | ⬜ 待做 |

---

## 设计要点

- **issues = tasks**：GitHub issue 天然映射到 s12 的任务（状态、owner、blockedBy），
  这是多 agent 协作的地基。
- **GitHub API = 工具**：`github/api.py` 是这产品的"bash"——读写 issue 就是工具的输入输出。
- **权限是安全关键路径**：这个 agent 会往真实仓库写东西。`core/permissions.py` 的默认策略是
  **读免费、写需确认、高风险默认拒**。开 PR / merge / close 属于"对外有影响"的操作。
- **每仓库一套记忆/技能**：不是全局一套。repo 的 CI 命令、代码约定按需加载。

---

## 路线图

- [x] **里程碑 0（本月目标）**：`gha triage` 闭环跑通（读→分类→打标签→评论，权限门控）
- [ ] 里程碑 1：memory + skills + compact，长会话不断、跨会话记得
- [ ] 里程碑 2：task system + fixer 自动修 issue、worktree 隔离、开 PR
- [ ] 里程碑 3：多 agent（triager/fixer/reviewer）+ 每晚 cron 无人值守
- [ ] 里程碑 4（证明物）：15-20 个真实 issue benchmark + DESIGN.md + 演示视频

## 学习路径对照

每个待做模块开工前，先回读对应章节的 README，把 `<details>` 里的
"教学版 vs 真实 CC"差距清单当作升级任务。比如 `memory/` 开工前读 s09，把
"文件数阈值整理"升级成"四层门控"。

---

**Agency 来自模型。gh-assistant 负责给模型一个 GitHub 仓库的世界。**
