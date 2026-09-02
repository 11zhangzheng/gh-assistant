# 安全模型

## 信任边界

以下内容全部视为不可信数据：仓库文件、Issue、评论、标签、diff、命令输出、仓库配置和仓库提供
的 Skills。它们可以影响模型的技术判断，但不能授予权限、选择凭据、启用本地执行或批准发布。

可信输入只有宿主机 CLI/配置、随包发布的 Skills、编译进代码的安全策略和用户的显式审批。

## 权限模型

多条规则按照 `deny > ask > allow` 合并，未知 effect 默认拒绝。

| Effect | 默认决策 |
| --- | --- |
| worktree 内读取 | Allow |
| 隔离 worktree 内结构化写入 | Allow |
| 受限 Docker 中执行命令 | Allow |
| 宿主机本地执行命令 | Ask，每个 run 单独审批 |
| GitHub label/comment、push、Draft PR | Ask |
| merge、关闭 Issue、宿主机越权访问 | Deny |

发布审批只对精确的 commit/diff subject hash 有效，任何代码变化都会使旧审批失效。

## 文件系统与命令执行

- 所有文件路径先解析再检查是否位于 worktree 内，symlink 越界同样会被拒绝。
- 命令只能使用 argv 数组，不经过 shell，不支持管道、重定向、glob 或命令替换。
- 命令环境变量中出现 token、secret、password、api key 等名称时直接拒绝。
- 基础环境采用 allowlist，不包含 GitHub 或模型凭据。
- commit 和 push 显式禁用仓库 Git hooks。
- 有修改的 worktree 永远不会自动删除。

Docker Runner 使用以下约束：

- `--network none`
- `--read-only`
- `--cap-drop ALL`
- `no-new-privileges`
- 非 root 用户
- CPU、内存、PID 与执行时间限制
- 有限大小的 `/tmp` tmpfs
- 只挂载本次 run 的 worktree

Docker daemon 本身仍属于宿主机信任边界。Docker 或指定镜像不可用时，Harness 必须请求一次
run-scoped 本地执行审批；非交互模式保持 `waiting_approval`，不会静默降级。

## 凭据与报告

GitHub token 只由宿主侧 REST Client 或临时 askpass 环境使用，push 完成后 askpass 目录会被
删除。状态库与报告会递归脱敏敏感字段，并扫描常见 Bearer、GitHub、Anthropic 和 OpenAI token
模式。

报告默认隐藏源码和测试输出正文。`--include-content` 只适合本地调试，即使启用仍会进行 secret
扫描。任何正则扫描都无法保证识别任意编码后的秘密，因此公开报告前仍应人工检查。

## 剩余风险

- 恶意测试仍可以在 Docker 资源上限内消耗资源或修改挂载的 worktree。
- 批准 Local Executor 等价于允许可信仓库代码在宿主机运行。
- Prompt injection 可能影响模型提出的代码与工具选择；确定性策略只能限制影响范围，不能保证
  代码正确。
- GitHub 和模型 Provider 仍是外部信任依赖。
- Windows symlink 防护依赖操作系统是否允许创建并暴露 symlink 元数据。

发现安全问题时请私下联系仓库所有者，不要在公开 Issue 中附带真实凭据。
