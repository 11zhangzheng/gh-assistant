# Docker 沙箱镜像

在使用 Docker 执行器前构建 Python Runner：

```powershell
docker build -t gh-assistant-python:3.12 docker
```

Harness 启动容器时会：

- 关闭网络。
- 使用只读容器根文件系统。
- 移除全部 Linux capabilities 并启用 `no-new-privileges`。
- 使用非 root 用户。
- 限制 CPU、内存、PID 和执行时间。
- 为 `/tmp` 提供受限的临时文件系统。
- 只将当前 run 的 worktree 挂载到 `/workspace`。

Python 命令使用独立的 pycache 前缀，避免快速返工后误用时间戳相同的旧 `.pyc`。模型 API Key
和 GitHub Token 不会传入容器。

Docker 不可用时，Harness 不会静默切换到宿主机执行，而是创建本次 run 的本地执行审批。非交互
模式会停留在 `waiting_approval`。
