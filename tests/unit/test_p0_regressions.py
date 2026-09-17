"""Regression tests for the production-hardening audit fixes (2026-08-28).

Each test pins a specific defect found by the architecture audit so it can
never silently return: result-key contract, path confinement, git argument
injection, the approval gate's binding rules, shell-metacharacter refusal,
and the secret-scanner exclusions.
"""

import asyncio
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "sdlc_manager"


# --- 1. review tools must read the executor's actual result keys -----------


def test_review_tools_use_stdout_key():
    """run_git_command returns 'stdout'; reading 'output' made review_pr_diff
    and generate_pr_description silently operate on empty diffs."""
    source = (SRC / "tools" / "review_tools.py").read_text(encoding="utf-8")
    assert '.get("output"' not in source
    assert '.get("stdout"' in source


def test_review_pr_diff_reads_real_diff(monkeypatch):
    from sdlc_manager.tools import review_tools

    captured = {}

    async def fake_git(args, cwd=None, **kwargs):
        return {"status": "success", "stdout": "+++ b/x.py\n+changed line", "stderr": ""}

    async def fake_generate(model, prompt, config):
        captured["prompt"] = prompt

        class R:
            text = "- looks fine"

        return R()

    monkeypatch.setattr("sdlc_manager.tools._executor.run_git_command", fake_git)
    monkeypatch.setattr(review_tools, "_generate", fake_generate)
    result = asyncio.run(review_tools.review_pr_diff(".", "main"))
    assert result["status"] == "success"
    assert "changed line" in captured["prompt"], "diff must reach the model"


# --- 2. path confinement ----------------------------------------------------


def test_apply_fixes_refuses_escape(tmp_path):
    from sdlc_manager.tools.validation_tools import apply_fixes

    result = asyncio.run(
        apply_fixes(str(tmp_path), "../outside.sql", accepted_fix_ids=["x"])
    )
    assert result["status"] == "error"
    assert "outside" in result["message"].lower() or "Refused" in result["message"]


def test_routes_have_no_unguarded_request_path_joins():
    source = (SRC / "dashboard" / "routes.py").read_text(encoding="utf-8")
    unguarded = re.findall(
        r"os\.path\.join\((?:req\.)?(?:working_directory|cwd), (?:req\.)?(?:file_path|fname)\)",
        source,
    )
    assert unguarded == [], f"request paths must go through _resolve_inside: {unguarded}"


# --- 3. git argument injection ----------------------------------------------


def test_safe_git_value_rejects_option_injection():
    from sdlc_manager.tools._executor import safe_git_value

    assert safe_git_value("feature/x") == "feature/x"
    for bad in ("--force", "-D", "", "  -rf"):
        with pytest.raises(ValueError):
            safe_git_value(bad)


def test_git_clone_refuses_option_url(monkeypatch):
    from sdlc_manager.tools import write_tools

    async def must_not_run(*a, **k):  # pragma: no cover - guard
        raise AssertionError("git must not be invoked for a refused URL")

    monkeypatch.setattr(write_tools, "run_git_command", must_not_run)
    result = asyncio.run(
        write_tools.git_clone(None, "--upload-pack=evil", "dest")
    )
    assert result["status"] == "error"


def test_git_reset_mode_allowlist(monkeypatch):
    from sdlc_manager.tools import write_tools

    async def must_not_run(*a, **k):  # pragma: no cover - guard
        raise AssertionError("git must not run with an invalid mode")

    monkeypatch.setattr(write_tools, "run_git_command", must_not_run)
    result = asyncio.run(
        write_tools.git_reset(None, ".", mode="hard --force", target="HEAD")
    )
    assert result["status"] == "error"


# --- 4. approval gate binding ------------------------------------------------


class _Tool:
    name = "git_push"


class _Ctx:
    def __init__(self, state, user_text):
        self.state = state

        class _Part:
            text = user_text

        class _Content:
            parts = [_Part()]

        self.user_content = _Content() if user_text is not None else None


def test_approval_bound_to_exact_args():
    from sdlc_manager.callbacks import require_user_approval

    state = {}
    first = require_user_approval(_Tool(), {"branch": "demo"}, _Ctx(state, "push it"))
    assert first["status"] == "pending_approval"
    # changed args after the user saw the request -> fresh approval, not execution
    second = require_user_approval(
        _Tool(), {"branch": "main", "force": True}, _Ctx(state, "yes")
    )
    assert second is not None and second["status"] == "pending_approval"


def test_approval_denial_blocks():
    from sdlc_manager.callbacks import require_user_approval

    state = {}
    require_user_approval(_Tool(), {"branch": "demo"}, _Ctx(state, "x"))
    denied = require_user_approval(_Tool(), {"branch": "demo"}, _Ctx(state, "no, cancel"))
    assert denied["status"] == "blocked"


def test_approval_grants_on_consent():
    from sdlc_manager.callbacks import require_user_approval

    state = {}
    require_user_approval(_Tool(), {"branch": "demo"}, _Ctx(state, "x"))
    granted = require_user_approval(_Tool(), {"branch": "demo"}, _Ctx(state, "yes"))
    assert granted is None  # None = proceed


# --- 5. lefthook bridge safety ------------------------------------------------


def test_bridge_quote_refuses_shell_metacharacters():
    from sdlc_manager.tools.lefthook_bridge import _quote

    assert _quote("plain.tf") == "plain.tf"
    assert _quote("has space.tf") == '"has space.tf"'
    for bad in ("a;b.sh", "x`id`.py", "y$(rm).tf", 'z".sql', "p|q.yml"):
        with pytest.raises(ValueError):
            _quote(bad)


def test_bridge_fails_closed_on_unsafe_filename(tmp_path):
    (tmp_path / "lefthook.yml").write_text(
        "pre-commit:\n  commands:\n    secrets:\n      run: gitleaks git --staged {staged_files}\n",
        encoding="utf-8",
    )
    from sdlc_manager.tools.lefthook_bridge import run_repo_checks

    result = asyncio.run(run_repo_checks(str(tmp_path), "commit", ["ok.py", "evil;rm.sh"]))
    assert result["passed"] is False
    assert result["checks"][0]["id"] == "safety"


# --- 6. secret scanner behavior -----------------------------------------------


def test_scan_skips_lockfiles_but_flags_secrets(tmp_path):
    from sdlc_manager.tools.validation_tools import scan_secrets

    (tmp_path / "package-lock.json").write_text(
        '{"integrity": "sha512-XN2B7dO2NsLLDMi9nMOL3QCyPfNYCEfaLypVL8T77mJdCUJKUZzKfhAFXVKAsSTItQENpcyTfBxfCg7Hvir22Q=="}',
        encoding="utf-8",
    )
    (tmp_path / "config.py").write_text(
        'api_key = "AIzaSyD9X8kQ7wL2mN4pR6tY1uZ3vB5cE8fG0hJ"\n', encoding="utf-8"
    )
    result = asyncio.run(scan_secrets(str(tmp_path)))
    assert result["status"] == "blocked"
    files_flagged = {f["file"] for f in result["findings"]}
    assert "config.py" in files_flagged
    assert not any("package-lock" in f for f in files_flagged)


# --- 7. PROD-audit round 2 fixes ----------------------------------------------


def test_linter_checks_sql_with_comparison_operators():
    """'<' + '>' in SQL used to skip syntax checking entirely."""
    from sdlc_manager.tools._linter import quick_validate_sql_syntax

    assert quick_validate_sql_syntax("SELECT 1 WHERE 1 < 2 AND 3 > 2") == ""
    assert quick_validate_sql_syntax("SELEC x FROM t WHERE a < b AND c > d") != ""
    assert quick_validate_sql_syntax("SELECT * FROM <table_name>") == ""  # template


def test_linter_where_in_comment_does_not_satisfy_rule():
    from sdlc_manager.tools._linter import lint_sql_content

    fixes = lint_sql_content("DELETE FROM t -- WHERE id = 1\n;")
    assert any("missing-filter" in f.rule_reference for f in fixes)
    fixes = lint_sql_content("DELETE FROM t WHERE id = 1;")
    assert not any("missing-filter" in f.rule_reference for f in fixes)


def test_linter_last_update_is_not_an_update_statement():
    from sdlc_manager.tools._linter import lint_sql_content

    fixes = lint_sql_content("SELECT LAST_UPDATE FROM t;")
    assert not any("missing-filter" in f.rule_reference for f in fixes)


def test_bridge_refuses_windows_cmd_metacharacters():
    from sdlc_manager.tools.lefthook_bridge import _quote

    for bad in ("%PATH%.tf", "x!y.sql"):
        with pytest.raises(ValueError):
            _quote(bad)


def test_rebase_interactive_refused(monkeypatch):
    from sdlc_manager.tools import write_tools

    async def must_not_run(*a, **k):  # pragma: no cover - guard
        raise AssertionError("interactive rebase must never reach git")

    monkeypatch.setattr(write_tools, "run_git_command", must_not_run)
    result = asyncio.run(
        write_tools.git_rebase(None, ".", onto_branch="main", interactive=True)
    )
    assert result["status"] == "error"


def test_review_code_refuses_path_escape(tmp_path):
    from sdlc_manager.tools.review_tools import review_code

    result = asyncio.run(review_code(str(tmp_path), "../../outside.py"))
    assert result["status"] == "error"
    assert "outside" in result["message"].lower() or "Refused" in result["message"]


def test_apply_fixes_line_anchored_replacement(tmp_path, monkeypatch):
    """Duplicated text must be patched at the recorded line, not first match."""
    from sdlc_manager.tools import validation_tools as vt

    target = tmp_path / "q.sql"
    target.write_text("SELECT a FROM t;\nSELECT a FROM t;\n", encoding="utf-8")

    async def fake_validate(**kwargs):
        return {
            "proposed_fixes": [{
                "fix_id": "fix-1",
                "line_number": 2,
                "original_code": "SELECT a FROM t;",
                "proposed_code": "SELECT a, b FROM t;",
            }]
        }

    monkeypatch.setattr(vt, "validate_file", fake_validate)
    result = asyncio.run(vt.apply_fixes(str(tmp_path), "q.sql", ["fix-1"]))
    assert result.get("applied_count", result.get("applied", 0)) or "success" in str(result.get("status"))
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "SELECT a FROM t;", "line 1 must be untouched"
    assert lines[1] == "SELECT a, b FROM t;", "line 2 must carry the fix"


def test_executor_output_is_capped():
    from sdlc_manager.tools._executor import MAX_CAPTURE_BYTES, _drain_capped

    class FakeStream:
        def __init__(self, total: int):
            self.remaining = total

        async def read(self, n: int) -> bytes:
            if self.remaining <= 0:
                return b""
            chunk = min(n, self.remaining)
            self.remaining -= chunk
            return b"x" * chunk

    data = asyncio.run(_drain_capped(FakeStream(MAX_CAPTURE_BYTES * 3)))
    assert len(data) < MAX_CAPTURE_BYTES + 200
    assert b"bytes dropped" in data


# --- 8. STRUCTURE-audit fixes ---------------------------------------------------


def test_parse_findings_actionlint_not_treated_as_tflint():
    from sdlc_manager.tools.lefthook_bridge import _parse_findings

    tflint_style = "Warning: something (rule_x)\n  on main.tf line 3:"
    # actionlint output must NOT be fed through the tflint parser
    assert _parse_findings("actionlint .github/workflows/ci.yml", tflint_style) == []
    # real tflint invocations still parse
    assert _parse_findings("tflint --chdir=modules/x", tflint_style) != []
    assert _parse_findings("pwsh -NoProfile -File ./make.ps1 lint", tflint_style) != []


def test_fmt_adaptation_only_rewrites_bare_target():
    from sdlc_manager.tools.lefthook_bridge import _adapt_command

    cmd, note = _adapt_command("pwsh -NoProfile -File ./make.ps1 fmt", ["a.tf"])
    assert cmd.endswith("fmt-check") and note
    cmd, note = _adapt_command("make fmt", ["a.tf"])
    assert cmd == "make fmt-check"
    # an explicit check-only invocation must pass through untouched
    cmd, note = _adapt_command("terraform fmt -check", ["a.tf"])
    assert cmd == "terraform fmt -check" and note is None


def test_scorecard_uses_linter_semantics():
    """The old scorecard missed a WHERE-less DELETE if any WHERE appeared
    anywhere in the file; the linter checks per statement."""
    from sdlc_manager.tools.review_tools import calculate_scorecard

    sql = "SELECT * FROM a WHERE x = 1;\nDELETE FROM b;\n"
    result = calculate_scorecard(sql, "two.sql")
    assert result["deductions"]["security"] >= 30, "per-statement rule must fire"
    clean = calculate_scorecard("DELETE FROM b WHERE id = 1;", "ok.sql")
    assert clean["deductions"]["security"] == 0


# --- 9. auth hardening ----------------------------------------------------------


def test_oauth_state_is_purpose_and_nonce_bound():
    from sdlc_manager.dashboard import auth

    state = auth.create_state_token("a@b.com", "nonce123")
    assert auth.verify_state_token(state, "nonce123")
    assert not auth.verify_state_token(state, "wrong-nonce")
    assert not auth.verify_state_token(state, None)
    # a plain session JWT must never pass as OAuth state (login-CSRF vector)
    session = auth.create_access_token("a@b.com")
    assert not auth.verify_state_token(session, "nonce123")
    # and a state token must never pass as a session
    assert auth.decode_access_token(state) is None


def test_github_token_encrypted_in_cookie():
    import base64
    import json as _json

    from sdlc_manager.dashboard import auth

    jwt_token = auth.create_github_session_token("ghp_SUPERSECRET", "octocat")
    payload = _json.loads(base64.urlsafe_b64decode(jwt_token.split(".")[1] + "=="))
    assert "ghp_SUPERSECRET" not in _json.dumps(payload), "token must be encrypted"

    class _Req:
        cookies = {"gh_session": jwt_token}

    token, login = auth.read_github_session(_Req())
    assert token == "ghp_SUPERSECRET" and login == "octocat"


def test_logout_revokes_session_token():
    from sdlc_manager.dashboard import auth

    token = auth.create_access_token("x@y.com")
    assert auth.decode_access_token(token) is not None
    auth.revoke_token(token)
    assert auth.decode_access_token(token) is None


def test_workspace_root_confinement(tmp_path, monkeypatch):
    from sdlc_manager.tools._executor import run_git_command

    inside = tmp_path / "repo"
    inside.mkdir()
    monkeypatch.setenv("SDLC_WORKSPACE_ROOT", str(tmp_path))
    ok = asyncio.run(run_git_command(["--version"], cwd=str(inside)))
    assert ok["status"] == "success"
    refused = asyncio.run(run_git_command(["--version"], cwd="C:\Windows"))
    assert refused["status"] == "error" and refused["code"] == "workspace_confined"


# --- 10. agents-layer PROD fixes -------------------------------------------------


def test_consent_natural_language_edges():
    from sdlc_manager.callbacks import _user_consents

    assert _user_consents("no problem, go ahead") is True
    assert _user_consents("sure, but what does this do?") is None
    assert _user_consents("no, cancel that") is False
    assert _user_consents("wait, first show me the diff") is None
    assert _user_consents("approved") is True


def test_config_errors_are_catchable(monkeypatch):
    from pydantic import BaseModel, Field

    from sdlc_manager.config import ConfigurationError, initialize_environment

    class Strict(BaseModel):
        must_exist: str = Field(alias="THIS_VAR_DOES_NOT_EXIST_XYZ")

    with pytest.raises(ConfigurationError):
        initialize_environment(Strict, print_config=False)


# --- 11. import-shadowing crash class (gate/run 500) -----------------------------


def test_no_use_before_local_import_shadowing():
    """A function-local `import asyncio` after an `asyncio.X` use makes the
    module-level name a local and raises UnboundLocalError at runtime — this
    took down POST /api/v1/gate/run in production testing."""
    import ast
    from pathlib import Path

    offenders = []
    for f in Path("src").rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            first_import: dict[str, int] = {}
            for node in ast.walk(fn):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        name = (a.asname or a.name).split(".")[0]
                        first_import[name] = min(
                            first_import.get(name, node.lineno), node.lineno
                        )
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in first_import
                    and node.lineno < first_import[node.id]
                ):
                    offenders.append(f"{f}:{node.lineno} {fn.name}() uses {node.id}")
    assert not offenders, offenders


def test_sse_endpoint_receives_request_object():
    """The SSE generator calls request.is_disconnected(); the endpoint must
    actually declare `request: Request` or every agent chat turn NameErrors."""
    import ast
    from pathlib import Path

    src = Path("src/sdlc_manager/dashboard/routes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_agent_run"
    )
    assert "request" in [a.arg for a in fn.args.args], (
        "api_agent_run must take `request: Request` for disconnect detection"
    )


# --- 12. empty GCP project must not produce projects//... BigQuery URLs ---------


def test_bq_client_rejects_empty_project(monkeypatch):
    """A set-but-empty GOOGLE_CLOUD_PROJECT survives google.auth resolution and
    malformed every BigQuery URL (404 projects//datasets) — the client must
    report 'unavailable' cleanly instead."""
    import google.cloud.bigquery as bq

    from sdlc_manager.tools import schema_validation as sv

    class _EmptyProjectClient:
        project = ""

        def __init__(self, project=None):
            pass

    monkeypatch.setattr(bq, "Client", _EmptyProjectClient)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    client, reason = sv._construct_bq_client()
    assert client is None
    assert "GOOGLE_CLOUD_PROJECT" in (reason or "")


# --- 13. governed-but-unwired repos must not commit unchecked --------------------


def test_governance_state_detection(tmp_path):
    from sdlc_manager.tools.lefthook_bridge import governance_state

    assert governance_state(str(tmp_path)) == "ungoverned"
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n", encoding="utf-8")
    assert governance_state(str(tmp_path)) == "unwired"
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("# lefthook\n", encoding="utf-8")
    assert governance_state(str(tmp_path)) == "wired"


def test_commit_and_push_blocked_when_hooks_not_wired(tmp_path):
    """A lefthook-governed repo whose hooks were never installed would commit
    with zero checks while looking governed — the tools must refuse."""
    from sdlc_manager.tools.write_tools import git_commit, git_push

    (tmp_path / "lefthook.yml").write_text("pre-commit:\n", encoding="utf-8")
    commit = asyncio.run(git_commit(None, str(tmp_path), message="feat: x"))
    assert commit["status"] == "blocked" and commit["code"] == "hooks_not_wired"
    push = asyncio.run(git_push(None, str(tmp_path)))
    assert push["status"] == "blocked" and push["code"] == "hooks_not_wired"


# --- 14. CLI hook report shows verdicts without timing noise ---------------------


def test_hook_report_strips_durations(capsys):
    from sdlc_manager.cli import _print_hook_report

    _print_hook_report({
        "stdout": (
            "lefthook v2.1.11\n"
            "summary: (done in 20.66 seconds)\n"
            "✔️ secrets (2.35 seconds)\n"
            "🥊 conventional-commit (10.64 seconds)\n"
        ),
    })
    out = capsys.readouterr().out
    assert "✔️ secrets" in out and "🥊 conventional-commit" in out
    assert "seconds" not in out and "summary:" not in out


# --- 15. syntactically broken SQL must not validate as clean ---------------------


def test_validate_file_flags_sql_syntax_error(tmp_path):
    """lint_sql_content returns no findings for unparseable SQL, so without a
    syntax-first check a broken file reads as 'passed all quality checks'."""
    from sdlc_manager.tools.validation_tools import validate_file

    bad = tmp_path / "q.sql"
    bad.write_text("SELECT title ,budget FRO movies_data;\n", encoding="utf-8")
    result = asyncio.run(validate_file(str(tmp_path), "q.sql"))
    assert result["status"] == "invalid" and result["code"] == "syntax_error"
    assert "Line 1" in result["message"]
