from __future__ import annotations

from pathlib import Path

from gh_assistant.contracts import Message
from gh_assistant.evidence import empty_evidence
from gh_assistant.report import generate_report
from gh_assistant.state import StateStore, redact_text, redact_value


def test_state_checkpoint_approval_and_tool_idempotency(tmp_path: Path):
    state = StateStore(tmp_path / "state")
    run = state.create_run(
        repo="owner/repo",
        issue_number=7,
        repo_path=tmp_path,
        provider="scripted",
        model="test",
        config={"kind": "solve"},
    )
    state.save_checkpoint(run["id"], 2, [Message.text("user", "hello")], {"x": 1}, actor="main")
    state.save_checkpoint(run["id"], 1, [Message.text("user", "review")], {}, actor="reviewer")
    assert state.load_checkpoint(run["id"], actor="main")["turn"] == 2
    assert state.load_checkpoint(run["id"], actor="reviewer")["turn"] == 1
    approval1 = state.create_approval(run["id"], "publish", "hash", {"token": "secret"})
    approval2 = state.create_approval(run["id"], "publish", "hash", {})
    assert approval1["id"] == approval2["id"]
    assert approval1["payload"]["token"] == "[REDACTED]"
    assert state.decide_approval(approval1["id"], "approved")["status"] == "approved"
    state.begin_tool_call(run["id"], "call1", "echo")
    state.finish_tool_call(run["id"], "call1", {"content": "ok", "is_error": False})
    assert state.completed_tool_call(run["id"], "call1")["content"] == "ok"


def test_report_is_sanitized_and_self_contained(tmp_path: Path):
    state = StateStore(tmp_path / "state")
    run = state.create_run(
        repo="owner/repo",
        issue_number=1,
        repo_path=tmp_path,
        provider="scripted",
        model="model",
        config={},
    )
    secret = "ghp_abcdefghijk123456"
    result = {
        "issue": {"number": 1, "title": "Bug", "body": f"private {secret}"},
        "implementation": {"summary": "Fixed it", "risks": []},
        "verification": {
            "passed": True,
            "unverified": False,
            "commands": [
                {"argv": ["python", "-m", "pytest"], "exit_code": 0, "is_error": False, "content": secret}
            ],
        },
        "review": {"verdict": "approve", "summary": "ok", "findings": []},
    }
    state.update_run(run["id"], result_json=result, status="completed_local", phase="done")
    state.append_event(
        run["id"],
        "tool_completed",
        "main",
        {"content": f"output {secret}", "arguments": {"new_text": f"code {secret}"}},
    )
    json_path, html_path = generate_report(state, run["id"])
    json_text = json_path.read_text(encoding="utf-8")
    html_text = html_path.read_text(encoding="utf-8")
    assert secret not in json_text
    assert secret not in html_text
    assert "[hidden by default]" in json_text
    assert "<!doctype html>" in html_text
    assert "Event Timeline" in html_text


def test_redact_text_handles_named_and_bearer_secrets():
    value = redact_text("api_key=abc123 Authorization: Bearer token.value")
    assert "abc123" not in value
    assert "token.value" not in value


def test_redact_value_preserves_token_counters_but_hides_credentials():
    value = redact_value(
        {"input_tokens": 12, "max_tokens": 100, "github_token": "secret-value"}
    )
    assert value["input_tokens"] == 12
    assert value["max_tokens"] == 100
    assert value["github_token"] == "[REDACTED]"


def test_evidence_report_exposes_decision_without_leaking_command_output(tmp_path: Path):
    state = StateStore(tmp_path / "state")
    run = state.create_run(repo="owner/repo", issue_number=3, repo_path=tmp_path, provider="scripted", model="test", config={})
    evidence = empty_evidence()
    evidence["expected_behavior"] = "Empty input returns an empty list"
    evidence["reproduction"].update(status="PASS", command=["python", "-m", "pytest"], before={"status": "FAIL", "output": "ghp_abcdefghijk123456", "exit_code": 1})
    evidence["regression"]["before"] = {"status": "FAIL", "output": "private traceback", "exit_code": 1}
    evidence["unverified_claims"] = ["Windows behavior"]
    state.update_run(run["id"], result_json={"outcome": "CANDIDATE_FIX", "outcome_reasons": ["Missing checks"], "evidence": evidence}, status="completed_local")
    json_path, html_path = generate_report(state, run["id"])
    json_text = json_path.read_text(encoding="utf-8")
    html_text = html_path.read_text(encoding="utf-8")
    assert '"outcome": "CANDIDATE_FIX"' in json_text
    assert "Windows behavior" in json_text
    assert "private traceback" not in json_text
    assert "ghp_abcdefghijk123456" not in json_text
    assert "Human verification required" in html_text
    assert "Expected Behavior" in html_text
    _, detailed_html = generate_report(state, run["id"], output_dir=tmp_path / "detailed", include_content=True)
    assert "private traceback" in detailed_html.read_text(encoding="utf-8")
