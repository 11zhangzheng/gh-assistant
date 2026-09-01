# Evaluation

## Goals

Evaluation separates model capability from harness contribution and checks:

1. Correctness: hidden verification passes.
2. Regression safety: repository verification passes before review/publication.
3. Efficiency: turns, tool calls, tokens, wall time, and optional estimated cost.
4. Control quality: unrelated diff and policy violations remain low or zero.

## Deterministic suite

`benchmarks/manifest.yaml` contains eight small Python issue fixtures. Each defines visible files
and verification plus a hidden argv check that runs only after the workflow completes. Repositories
and worktrees are created per case; benchmarks never publish.

```bash
gha eval benchmarks/manifest.yaml --profile baseline --output .gha/eval
gha eval benchmarks/manifest.yaml --profile full --output .gha/eval
```

Outputs include machine-readable JSON and standalone HTML. A case setup or run error is recorded as
that case's failure and does not abort the remaining suite.

## Profiles and ablations

`baseline` and `full` share model, budgets, worktree, verification, executor, policy, and reporting.
Baseline disables skills, durable memory, and independent review. Full enables them. This isolates
harness contribution better than comparing different models.

One-factor ablations can disable skills, reviewer, or memory. Use fixed model versions, provider
settings, fixture revisions, and multiple seeds when the backend is stochastic.

## Metrics

| Metric | Definition |
| --- | --- |
| Solve rate | Hidden-passing cases / selected cases |
| Regression pass rate | Runs passing repository verification |
| Unrelated diff | Changed lines/files outside expected scope |
| Tool calls | Completed, failed, denied, and recovered calls per run |
| Tokens/cost | Normalized usage and configured price estimate |
| Recovery correctness | Resume converges without duplicate external actions |
| Policy violations | Escaped paths, secret env attempts, unauthorized writes |

## CI and live evaluation

CI uses scripted backends, fake GitHub transports, and temporary Git repositories. It requires at
least 85% branch-aware package coverage and spends no model/GitHub quota. Docker tests detect the
daemon and skip explicitly when unavailable.

Live benchmarks and real-repository demonstrations are separate. Run them only after confirming
model cost, repository scope, local-executor risk, and GitHub write approval. Record model/provider,
commit SHA, manifest hash, executor image digest, and report artifacts for reproducibility.
