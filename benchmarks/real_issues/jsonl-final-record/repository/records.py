import json
from pathlib import Path


def read_records(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.endswith("\n"):
                continue
            if line.strip():
                records.append(json.loads(line))
    return records
