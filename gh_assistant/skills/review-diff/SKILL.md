---
name: review-diff
description: Independently review a proposed patch for correctness, regressions, and unsafe scope.
---

# Independent Diff Review

1. Derive acceptance criteria from the issue, not from the primary agent's summary.
2. Inspect every changed file and relevant unchanged contract.
3. Check boundary cases, error behavior, backwards compatibility, and test adequacy.
4. Reject only blocking correctness, safety, regression, or scope problems.
5. Return concrete findings with file and behavior references through `submit_review`.

