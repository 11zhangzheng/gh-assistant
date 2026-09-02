# 评测体系

## 评测目标

评测需要区分模型能力与 Harness 机制贡献，并同时衡量四个维度：

1. **修复正确性**：隐藏验证是否通过。
2. **回归安全性**：仓库公开验证是否在审查和发布前通过。
3. **执行效率**：回合、工具调用、token、耗时和可配置成本。
4. **控制质量**：无关 diff 与策略违规是否足够低。

## 确定性 Fixture Suite

`benchmarks/manifest.yaml` 包含 8 个小型 Python Issue。每个 case 定义：

- 模型可见的 Issue 标题和正文。
- 初始仓库文件和公开测试。
- Harness 使用的验证命令。
- 只在修复流程结束后执行的隐藏 argv 验证。

每个 case 都在独立临时仓库和 worktree 中运行，benchmark 永远不会发布到 GitHub：

```powershell
gha eval benchmarks/manifest.yaml --profile baseline --output .gha/eval
gha eval benchmarks/manifest.yaml --profile full --output .gha/eval
```

结果同时输出机器可读 JSON 和单文件 HTML。单个 fixture 初始化或执行失败只会记录为该 case
失败，不会中断整批评测。

## Baseline 与 Full

两个 profile 使用完全相同的：

- 模型与 Provider 配置
- token 和工具预算
- Git worktree
- 验证命令与执行器
- 权限策略
- 报告与指标逻辑

`baseline` 关闭 Skills、持久 Memory 和独立 Reviewer；`full` 启用它们。这样比较的是 Harness
机制增益，而不是不同模型或执行环境的差异。

后续还可以分别关闭 Skills、Reviewer、Memory 做单因素消融。使用随机模型后，应固定模型版本、
Provider 参数、fixture revision，并运行多个 seed。

## 指标

| 指标 | 定义 |
| --- | --- |
| Solve Rate | 隐藏测试通过数 / 选中 case 数 |
| Regression Pass Rate | 通过仓库验证的 run 比例 |
| Unrelated Diff | 预期修复范围之外的修改文件与行数 |
| Tool Calls | 成功、失败、拒绝和恢复调用数量 |
| Token / Cost | Provider 统一后的 usage 与配置价格估算 |
| Recovery Correctness | Resume 后是否收敛到相同结果且不重复外部操作 |
| Policy Violations | 越界路径、秘密环境变量和未授权外部写入 |

## CI

CI 使用 scripted backend、fake GitHub transport 和临时 Git 仓库，不消耗模型或 GitHub 配额。
CI 会构建 Docker Runner、运行隔离测试，并要求整个 `gh_assistant` 包达到至少 85% 的 branch-aware
coverage。

```powershell
python -m coverage run --branch -m pytest -q
python -m coverage report --fail-under=85
```

Docker daemon 或镜像不可用时，Docker 测试必须显式 skip，不能伪装成通过。

## 真实模型评测

真实模型 benchmark 和真实仓库案例与 CI 分离。运行前必须确认：

- 模型调用成本与上限。
- GitHub 仓库和 token 权限范围。
- Docker 不可用时执行本地代码的风险。
- 是否允许 push、comment 和创建 Draft PR。

报告中应记录模型 ID、Provider endpoint、代码 commit SHA、manifest hash、执行器镜像 digest 和
所有 run artifacts，保证结果可追溯和可复现。
