---
name: python-testing
description: Inspect and verify Python changes using repository-native test conventions.
---

# Python Verification

- Read `pyproject.toml`, `pytest.ini`, `tox.ini`, or `noxfile.py` before choosing commands.
- Start with the narrowest relevant pytest target, then run the configured harness verification.
- Cover the reported failure and at least one boundary case without overfitting to implementation details.
- Do not install packages or enable network access unless the harness explicitly authorizes it.

