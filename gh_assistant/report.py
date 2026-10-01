"""Sanitized JSON and self-contained HTML run reports."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from gh_assistant.state import StateStore, redact_value


def generate_report(
    state: StateStore,
    run_id: str,
    *,
    output_dir: Path | None = None,
    include_content: bool = False,
) -> tuple[Path, Path]:
    run = state.get_run(run_id)
    events = state.events(run_id)
    approvals = state.list_approvals(run_id=run_id)
    tasks = state.tasks(run_id)
    payload = _build_payload(run, events, approvals, tasks, include_content)
    destination = output_dir or state.run_dir(run_id)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "report.json"
    html_path = destination / "report.html"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    html_path.write_text(_render_html(payload), encoding="utf-8")
    return json_path, html_path


def _build_payload(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    include_content: bool,
) -> dict[str, Any]:
    result = run.get("result", {})
    issue = result.get("issue", {})
    safe_result = {
        "issue": {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "body": issue.get("body") if include_content else "[hidden by default]",
        },
        "implementation": result.get("implementation", {}),
        "verification": _sanitize_verification(result.get("verification", {}), include_content),
        "review": result.get("review", {}),
        "pull_request": result.get("pull_request", {}),
        "needs_human_reason": result.get("needs_human_reason", ""),
        "commit_sha": result.get("commit_sha", ""),
        "outcome": result.get("outcome", ""),
        "outcome_reasons": result.get("outcome_reasons", []),
        "abstain": result.get("abstain", {}),
        "evidence": _sanitize_evidence(result.get("evidence", {}), include_content),
    }
    safe_events = []
    for event in events:
        value = dict(event)
        value["payload"] = _sanitize_event_payload(
            event.get("event_type", ""), event.get("payload", {}), include_content
        )
        safe_events.append(redact_value(value))
    usage = _aggregate_usage(safe_events)
    return redact_value(
        {
            "run": {
                key: run.get(key)
                for key in (
                    "id",
                    "repo",
                    "issue_number",
                    "branch",
                    "base_ref",
                    "base_sha",
                    "provider",
                    "model",
                    "phase",
                    "status",
                    "created_at",
                    "updated_at",
                )
            },
            "result": safe_result,
            "usage": usage,
            "tasks": [
                {
                    "id": task["id"],
                    "position": task["position"],
                    "description": task["description"],
                    "status": task["status"],
                    "evidence": task["evidence"],
                }
                for task in tasks
            ],
            "approvals": approvals,
            "events": safe_events,
            "content_included": include_content,
        }
    )


def _sanitize_verification(value: dict[str, Any], include_content: bool) -> dict[str, Any]:
    commands = []
    for command in value.get("commands", []):
        commands.append(
            {
                "argv": command.get("argv", []),
                "exit_code": command.get("exit_code"),
                "is_error": command.get("is_error"),
                "unverified": command.get("unverified", False),
                "status": command.get("status", "NOT_RUN" if command.get("unverified") else ("FAIL" if command.get("is_error") else "PASS")),
                "content": command.get("content", "") if include_content else "[output hidden]",
            }
        )
    return {
        "passed": value.get("passed"),
        "unverified": value.get("unverified", False),
        "commands": commands,
    }


def _sanitize_evidence(value: dict[str, Any], include_content: bool) -> dict[str, Any]:
    if not value:
        return {}
    evidence = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    for section in ("reproduction", "regression"):
        for key in ("before", "after"):
            check = evidence.get(section, {}).get(key)
            if isinstance(check, dict) and not include_content:
                check["output"] = "[output hidden]"
    for check in evidence.get("repository_checks", []):
        if not include_content:
            check["output"] = "[output hidden]"
    return evidence


def _sanitize_event_payload(
    event_type: str, payload: dict[str, Any], include_content: bool
) -> dict[str, Any]:
    value = dict(payload)
    if not include_content:
        value.pop("content", None)
        if "arguments" in value:
            arguments = dict(value["arguments"] or {})
            for key in ("old_text", "new_text", "patch", "body"):
                if key in arguments:
                    arguments[key] = f"[hidden; {len(str(arguments[key]))} chars]"
            if "env" in arguments:
                arguments["env"] = "[hidden]"
            value["arguments"] = arguments
        data = value.get("data")
        if isinstance(data, dict):
            value["data"] = {
                key: item
                for key, item in data.items()
                if key not in {"stdout", "stderr", "content"}
            }
    return value


def _aggregate_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_cost_usd": None,
        "tool_calls": 0,
        "tool_failures": 0,
        "retries": 0,
    }
    known_cost = 0.0
    has_cost = False
    for event in events:
        event_type = event["event_type"]
        if event_type == "model_completed":
            totals["model_calls"] += 1
            usage = event["payload"].get("usage", {})
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                totals[key] += int(usage.get(key, 0) or 0)
            if usage.get("estimated_cost_usd") is not None:
                known_cost += float(usage["estimated_cost_usd"])
                has_cost = True
        elif event_type == "tool_started":
            totals["tool_calls"] += 1
        elif event_type == "tool_failed":
            totals["tool_failures"] += 1
        elif event_type == "model_retry":
            totals["retries"] += 1
    totals["estimated_cost_usd"] = round(known_cost, 6) if has_cost else None
    return totals


def _render_html(payload: dict[str, Any]) -> str:
    run = payload["run"]
    result = payload["result"]
    usage = payload["usage"]
    verification = result.get("verification", {})
    review = result.get("review", {})
    evidence = result.get("evidence", {})
    outcome = result.get("outcome", "UNCLASSIFIED")
    events_json = json.dumps(payload["events"], ensure_ascii=False).replace("</", "<\\/")
    status_class = _status_class(str(run.get("status", "")))
    approval_rows = "".join(
        f"<tr><td>{_e(item['action'])}</td><td>{_e(item['status'])}</td>"
        f"<td><code>{_e(item['id'])}</code></td></tr>"
        for item in payload["approvals"]
    ) or '<tr><td colspan="3" class="muted">No approvals</td></tr>'
    task_rows = "".join(
        f"<tr><td>{item['position'] + 1}</td><td>{_e(item['description'])}</td>"
        f"<td>{_e(item['status'])}</td></tr>"
        for item in payload["tasks"]
    ) or '<tr><td colspan="3" class="muted">No submitted plan</td></tr>'
    verify_rows = "".join(
        f"<tr><td><code>{_e(' '.join(item.get('argv') or []) or 'not configured')}</code></td>"
        f"<td>{_e(str(item.get('exit_code')))}</td>"
        f"<td>{_e(item.get('status', 'NOT_RUN' if item.get('unverified') else ('FAIL' if item.get('is_error') else 'PASS')))}</td></tr>"
        for item in verification.get("commands", [])
    ) or '<tr><td colspan="3" class="muted">No verification evidence</td></tr>'
    findings = "".join(f"<li>{_e(item)}</li>" for item in review.get("findings", []))
    unverified = "".join(f"<li>{_e(item)}</li>" for item in evidence.get("unverified_claims", [])) or "<li>None recorded</li>"
    checks = "".join(
        f"<li>{_e(item.get('kind', 'configured'))}: {_e(item.get('status', 'NOT_RUN'))} "
        f"<code>{_e(' '.join(item.get('command', [])))}</code></li>"
        for item in evidence.get("repository_checks", [])
    ) or "<li>NOT_RUN</li>"
    regression = evidence.get("regression", {})
    reproduction = evidence.get("reproduction", {})
    scope = evidence.get("fix_scope", {})
    review_evidence = evidence.get("review", {})
    ci = evidence.get("ci", {})
    root_cause = evidence.get("root_cause", {})
    abstain = result.get("abstain", {})
    abstain_details = (
        f"<section><h2>Why the agent abstained</h2>"
        f"<p><b>Reason:</b> {_e(abstain.get('reason', 'Not recorded'))}</p>"
        f"<p><b>Confirmed:</b> {_e('; '.join(abstain.get('confirmed', [])) or 'None recorded')}</p>"
        f"<p><b>Missing:</b> {_e('; '.join(abstain.get('missing', [])) or 'None recorded')}</p>"
        f"<p><b>Next step:</b> {_e(abstain.get('next_step') or 'Ask a maintainer to inspect the evidence.')}</p></section>"
        if outcome == "ABSTAIN" else ""
    )
    pr = result.get("pull_request", {})
    implementation_summary = result.get("implementation", {}).get(
        "summary", "No implementation summary"
    )
    pr_link = (
        f'<a href="{_e(pr.get("url"))}">Draft PR #{_e(pr.get("number"))}</a>'
        if pr.get("url")
        else "Not published"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gh-assistant run {_e(run['id'])}</title>
<style>
:root {{ color-scheme: light; --ink:#171717; --muted:#666; --line:#d7d7d2; --paper:#fafafa;
--green:#147d4f; --amber:#9a6700; --red:#b42318; --blue:#1769aa; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--paper); color:var(--ink);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; letter-spacing:0; }}
header {{ border-bottom:1px solid var(--line); background:#fff; }}
.wrap {{ width:min(1180px,calc(100% - 32px)); margin:0 auto; }}
header .wrap {{ padding:24px 0 20px; }} h1 {{ margin:0; font-size:24px; letter-spacing:0; }}
.sub {{ color:var(--muted); margin-top:5px; }} .status {{ display:inline-block; padding:2px 7px;
border:1px solid currentColor; border-radius:4px; font-weight:650; }} .good {{ color:var(--green); }}
.warn {{ color:var(--amber); }} .bad {{ color:var(--red); }}
.stats {{ display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:1px; background:var(--line);
border-bottom:1px solid var(--line); }} .stat {{ background:#fff; padding:16px; min-width:0; }}
.stat b {{ display:block; font-size:20px; overflow-wrap:anywhere; }} .stat span {{ color:var(--muted); font-size:12px; }}
main {{ padding:24px 0 48px; }} section {{ margin:0 0 30px; }} h2 {{ font-size:16px; margin:0 0 10px; }}
.band {{ background:#fff; border-block:1px solid var(--line); padding:18px 0; margin-bottom:24px; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }} table {{ width:100%; border-collapse:collapse; background:#fff; }}
th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ font-size:12px; color:var(--muted); background:#f2f2ef; }} code {{ font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace; overflow-wrap:anywhere; }}
.controls {{ display:flex; gap:0; margin-bottom:10px; flex-wrap:wrap; }} .controls button {{ border:1px solid var(--line);
background:#fff; padding:6px 10px; color:var(--ink); cursor:pointer; }} .controls button+button {{ border-left:0; }}
.controls button.active {{ background:var(--ink); color:#fff; }} .event {{ display:grid; grid-template-columns:70px 150px 110px 1fr;
gap:10px; padding:9px 10px; border-bottom:1px solid var(--line); background:#fff; }}
.event .seq,.event .actor {{ color:var(--muted); }} details pre {{ white-space:pre-wrap; overflow-wrap:anywhere;
font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace; }} .muted {{ color:var(--muted); }}
a {{ color:var(--blue); }} ul {{ margin:8px 0; padding-left:20px; }}
@media (max-width:800px) {{ .stats {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .grid {{ grid-template-columns:1fr; }}
.event {{ grid-template-columns:55px 1fr; }} .event .actor,.event details {{ grid-column:2; }} }}
</style>
</head>
<body>
<header><div class="wrap"><h1>{_e(run['repo'])} #{_e(run['issue_number'])}</h1>
<div class="sub"><code>{_e(run['id'])}</code> · {_e(run['model'])} · <span class="status {status_class}">{_e(run['status'])}</span></div></div></header>
<div class="stats wrap"><div class="stat"><b>{usage['model_calls']}</b><span>Model calls</span></div>
<div class="stat"><b>{usage['tool_calls']}</b><span>Tool calls</span></div><div class="stat"><b>{usage['input_tokens']}</b><span>Input tokens</span></div>
<div class="stat"><b>{usage['output_tokens']}</b><span>Output tokens</span></div><div class="stat"><b>{usage['tool_failures']}</b><span>Tool failures</span></div>
<div class="stat"><b>{_e(usage['estimated_cost_usd'] if usage['estimated_cost_usd'] is not None else 'n/a')}</b><span>Estimated USD</span></div></div>
  <div class="band"><div class="wrap grid"><div><h2>Outcome: {_e(outcome)}</h2><p>{_e(implementation_summary)}</p>
<p>{'Human verification required before merge.' if outcome == 'CANDIDATE_FIX' else ('Stopped without publishing.' if outcome == 'ABSTAIN' else 'Local evidence verified; inspect CI before merge.')}</p>
<p><b>Verification:</b> {_e('passed' if verification.get('passed') else 'not passed')} · <b>Review:</b> {_e(review.get('verdict','not run'))}</p></div>
<div><h2>Publication</h2><p>{pr_link}</p><p class="muted">Branch <code>{_e(run['branch'])}</code></p></div></div></div>
<main class="wrap"><div class="grid"><section><h2>Plan</h2><table><thead><tr><th>#</th><th>Task</th><th>Status</th></tr></thead><tbody>{task_rows}</tbody></table></section>
<section><h2>Verification</h2><table><thead><tr><th>Command</th><th>Exit</th><th>Result</th></tr></thead><tbody>{verify_rows}</tbody></table></section></div>
  <div class="grid"><section><h2>Independent Review</h2><p><b>{_e(review.get('verdict','not run'))}</b> {_e(review.get('summary',''))}</p><ul>{findings}</ul></section>
  <section><h2>Approvals</h2><table><thead><tr><th>Action</th><th>Status</th><th>ID</th></tr></thead><tbody>{approval_rows}</tbody></table></section></div>
  <div class="grid"><section><h2>Evidence Contract</h2>
  <p><b>Expected Behavior:</b> {_e(evidence.get('expected_behavior') or 'Not documented')}</p>
  <p><b>Reproduction:</b> {_e(reproduction.get('status', 'NOT_RUN'))} <code>{_e(' '.join(reproduction.get('command', [])))}</code></p>
  <details><summary>Reproduction output</summary><pre>{_e(reproduction.get('before', {}).get('output') or 'No output recorded')}</pre></details>
  <p><b>Root Cause:</b> {_e(evidence.get('root_cause', {}).get('summary') or 'Not established')}</p>
  <p><b>Locations:</b> {_e(', '.join(root_cause.get('locations', [])) or 'Not recorded')}; <b>Basis:</b> {_e('; '.join(root_cause.get('basis', [])) or 'Not recorded')}; <b>Confidence:</b> {_e(root_cause.get('confidence') if root_cause.get('confidence') is not None else 'not recorded')}</p>
  <p><b>Fix Scope:</b> planned {_e(', '.join(scope.get('planned_files', [])) or 'unknown')}; changed {_e(', '.join(scope.get('actual_files', [])) or 'none')}; violation {_e(scope.get('violation', False))}</p>
  <p><b>Regression:</b> before {_e(regression.get('before', {}).get('status', 'NOT_RUN'))} → after {_e(regression.get('after', {}).get('status', 'NOT_RUN'))}</p>
  <details><summary>Regression before / after output</summary><pre>Before: {_e(regression.get('before', {}).get('output') or 'No output recorded')}\nAfter: {_e(regression.get('after', {}).get('output') or 'No output recorded')}</pre></details>
  <p><b>CI:</b> {_e(ci.get('status', 'NOT_RUN'))}</p></section>
  <section><h2>Decision Evidence</h2><p><b>Repository checks</b></p><ul>{checks}</ul>
  <p><b>Review blockers:</b> {_e(len(review_evidence.get('blockers', [])))}</p>
  <p><b>Review findings:</b> {_e('; '.join(review_evidence.get('blockers', []) + review_evidence.get('warnings', []) + review_evidence.get('info', [])) or 'None recorded')}</p>
  <p><b>Unverified claims</b></p><ul>{unverified}</ul></section></div>
{abstain_details}
<section><h2>Event Timeline</h2><div class="controls"><button class="active" data-filter="all">All</button><button data-filter="model">Model</button><button data-filter="tool">Tools</button><button data-filter="phase">Phases</button><button data-filter="approval">Approvals</button></div><div id="events"></div></section></main>
<script>const events={events_json}; const root=document.getElementById('events');
function group(t){{if(t.startsWith('model'))return'model';if(t.startsWith('tool')||t==='verification_command')return'tool';if(t==='phase_changed')return'phase';if(t.startsWith('approval'))return'approval';return'other';}}
function render(filter){{root.innerHTML='';events.filter(e=>filter==='all'||group(e.event_type)===filter).forEach(e=>{{const d=document.createElement('div');d.className='event';
d.innerHTML=`<span class="seq">#${{e.seq}}</span><b>${{escapeHtml(e.event_type)}}</b><span class="actor">${{escapeHtml(e.actor)}}</span><details><summary>Details</summary><pre>${{escapeHtml(JSON.stringify(e.payload,null,2))}}</pre></details>`;root.appendChild(d);}});}}
function escapeHtml(v){{return String(v).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{{document.querySelectorAll('[data-filter]').forEach(x=>x.classList.remove('active'));b.classList.add('active');render(b.dataset.filter);}}));render('all');</script>
</body></html>"""


def _status_class(status: str) -> str:
    if status in {"published", "completed_local", "succeeded"}:
        return "good"
    if status in {"failed", "needs_human"}:
        return "bad"
    return "warn"


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)
