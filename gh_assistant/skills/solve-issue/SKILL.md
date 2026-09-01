---
name: solve-issue
description: Produce a focused, evidence-driven fix for one GitHub issue.
---

# Solve An Issue

1. Restate the observable failure and success condition from the issue.
2. Trace the behavior through code and tests; distinguish evidence from assumptions.
3. Prefer the smallest change that addresses the root cause and preserves public contracts.
4. Add or update a regression test when the repository has a test suite.
5. Run focused checks while iterating, inspect the final diff, then call `finish_task`.
6. Do not claim final verification passed; the harness owns that gate.

