from __future__ import annotations

from pathlib import Path

import pytest

from gh_assistant.executors import DockerExecutor


@pytest.mark.docker
def test_docker_runner_enforces_isolation_and_resource_limits(tmp_path: Path):
    available, reason = DockerExecutor.available()
    if not available:
        pytest.skip(f"Docker daemon unavailable: {reason}")
    executor = DockerExecutor(tmp_path)
    if not executor.image_available():
        pytest.skip(f"Docker image unavailable: {executor.image}")

    sentinel = tmp_path.parent / "host-only-sentinel.txt"
    sentinel.write_text("must not be mounted", encoding="utf-8")

    writable = executor.run(
        ["python", "-c", "from pathlib import Path; Path('container-write.txt').write_text('ok')"]
    )
    assert not writable.is_error
    assert (tmp_path / "container-write.txt").read_text(encoding="utf-8") == "ok"

    no_host_access = executor.run(
        [
            "python",
            "-c",
            f"from pathlib import Path; assert not Path('/workspace/../{sentinel.name}').exists()",
        ]
    )
    assert not no_host_access.is_error

    no_secrets = executor.run(
        [
            "python",
            "-c",
            "import os; bad=('TOKEN','SECRET','PASSWORD','API_KEY','AUTHORIZATION'); "
            "assert not [k for k in os.environ if any(x in k.upper() for x in bad)]",
        ]
    )
    assert not no_secrets.is_error

    network = executor.run(
        [
            "python",
            "-c",
            "import socket; socket.create_connection(('1.1.1.1', 53), timeout=1)",
        ],
        timeout_seconds=10,
    )
    assert network.is_error

    limits = executor.run(
        [
            "python",
            "-c",
            "from pathlib import Path; "
            "memory=Path('/sys/fs/cgroup/memory.max').read_text().strip(); "
            "pids=Path('/sys/fs/cgroup/pids.max').read_text().strip(); "
            "quota,period=Path('/sys/fs/cgroup/cpu.max').read_text().split(); "
            "assert memory!='max' and int(memory)<=2147483648; "
            "assert pids!='max' and int(pids)<=256; "
            "assert quota!='max' and int(quota)/int(period)<=2",
        ]
    )
    assert not limits.is_error, limits.content
