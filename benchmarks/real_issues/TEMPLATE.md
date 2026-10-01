# Offline Python Bug Task Schema

Each `<task-id>/` contains `task.yaml`, `issue.md`, and `repository/`. Run the directory with `gha eval benchmarks/real_issues --profile full --output .gha/eval-real`. Evaluation never publishes a PR.

`metadata.source` is either `high_realism_fixture` (invented local example, `issue_url` and `base_commit` must be null) or `github_issue` (real public Issue URL, full 40-character base commit, and a local Git snapshot at `repository/`). Do not label an invented example as a real Issue. The runner clones a real local snapshot and checks the base commit without downloading a repository.

```yaml
id: my-bug
repo: owner/repository
issue_number: 123
issue_url: https://github.com/owner/repository/issues/123
base_commit: 0123456789abcdef0123456789abcdef01234567
language: python
issue_type: bug
issue_title: Concise bug title
reproduction:
  available: true
  command: [python, -m, pytest, -q, tests/test_bug.py]
verification:
  targeted: [python, -m, pytest, -q, tests/test_bug.py]
  repository:
    - [python, -m, pytest, -q]
  hidden: [python, -m, pytest, -q, tests/test_hidden.py] # optional independent judge
metadata:
  source: github_issue
  difficulty: medium
human: {} # optional manual review time, interventions, revisions, accepted, incorrect_fix
```

`issue.md` is the issue text shown to the Agent. `hidden` is run only after a locally completed patch and is never included in the Agent's issue prompt. Without a hidden judge, `solve_rate` is unavailable rather than assumed to be zero. The two included examples are high-realism fixtures, not real GitHub Issues or evidence of user productivity gains.
