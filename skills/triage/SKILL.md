---
name: triage
description: Classify open issues (bug/feature/question) and write triage labels + comments.
when_to_use: Whenever triaging or classifying GitHub issues.
---

# Triage

Classify the repo's open issues and leave a short triage note on each.
(Once this skill is loaded, do not call load_skill again — follow it.)

## Procedure
1. Read before you write: `repo_info`, then `list_issues`, then `list_labels`.
2. For each open issue, call `get_issue` to read the full body + comments.
3. Classify each issue: bug / feature (enhancement) / question / invalid.
4. If a label is clearly missing, propose adding it with `add_labels`.
5. When confident, write a short triage comment with `comment_on_issue`.

## Rules
- Never close an issue without explicit user approval.
- Only use labels that already exist in the repo — don't invent new ones.
- Junk/probe issues (e.g. body is "temp", no real content): label `invalid`,
  flag for closing, and do NOT close them yourself.
- End with a summary table: per issue, the classification and what you did.
