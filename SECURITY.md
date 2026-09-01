# Security model

## Trust boundaries

Repository files, GitHub issues, comments, labels, diffs, command output, and repository-provided
skills/configuration are untrusted data. They may influence model judgment but cannot grant
permissions, choose credentials, enable local execution, or authorize publication.

Trusted inputs are host CLI/configuration, packaged skills, compiled policy, and explicit approval
decisions. Secrets remain in host-side GitHub/Git transports.

## Permission lattice

Rules merge with `deny > ask > allow`. Unknown effects fail closed.

| Effect | Default |
| --- | --- |
| Read within worktree | Allow |
| Write within isolated worktree | Allow |
| Command in constrained Docker executor | Allow |
| Local host command | Ask once per run |
| GitHub label/comment, push, Draft PR | Ask |
| Merge, close issue, privileged host access | Deny |

Publish approval is scoped to an exact commit/diff subject hash. Editing code invalidates it.

## Filesystem and execution

- Every file path is resolved and checked relative to the worktree; symlink escapes are rejected.
- Commands are argv arrays. There is no shell parser, pipe, redirect, glob, or substitution.
- Environment variables with secret-like names are rejected from command tools.
- The base environment is allowlisted and excludes GitHub/model credentials.
- Git hooks are disabled for commits and pushes.
- Changed worktrees are preserved and never automatically deleted.

The Docker runner uses `--network none`, `--read-only`, `--cap-drop ALL`,
`no-new-privileges`, a non-root user, bounded pids/memory/CPU, a bounded tmpfs, and one worktree
bind mount. Docker daemon access remains a host-level trust assumption.

When Docker or its image is unavailable, the harness requests explicit run-scoped local execution
approval. Non-interactive operation remains `waiting_approval`.

## Secrets and reports

GitHub tokens are used only by the host-side REST client or a temporary askpass environment. The
askpass directory is removed after push. State and reports recursively redact named credential
fields and scan common bearer, GitHub, Anthropic, and OpenAI token patterns.

Reports hide source and test output bodies by default. `--include-content` is for local debugging;
secret scanning still applies. Redaction cannot guarantee detection of arbitrary encoded secrets,
so reports should still be reviewed before public sharing.

## Residual risks

- Malicious tests can consume resources within Docker bounds or modify the mounted worktree.
- Local executor approval permits repository commands on the host; use it only for trusted repos.
- Prompt injection can affect proposed code and tool choices. Policy limits effects but cannot
  guarantee code correctness.
- GitHub and model providers remain external trust dependencies.
- Windows symlink defense depends on the OS exposing symlink metadata; CI should exercise it where
  symlink creation is permitted.

Report vulnerabilities privately to the repository owner. Never include live credentials.
