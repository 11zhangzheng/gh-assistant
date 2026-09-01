# Sandbox image

Build the Python-first runner before using Docker execution:

```sh
docker build -t gh-assistant-python:3.12 docker
```

The harness starts this image with networking disabled, a read-only root filesystem,
dropped Linux capabilities, `no-new-privileges`, bounded CPU/memory/PIDs, and only the
run worktree mounted at `/workspace`.
