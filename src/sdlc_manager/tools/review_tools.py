"""AI Architectural Review tools powered by Google GenAI."""

import logging
import os
from pathlib import Path
from typing import Any

from google import genai
from google.adk.tools import ToolContext
from google.genai import types

from ._linter import quick_validate_sql_syntax
from ._rules import get_rules_for_file
from .validation_tools import scan_content_for_secrets

logger = logging.getLogger(__name__)

_CLIENT: "genai.Client | None" = None


def _client() -> "genai.Client":
    """One client per process: construction resolves credentials (I/O)."""
    global _CLIENT
    if _CLIENT is None:
        # 120s cap: a hung model call must fail, not hold a worker forever.
        _CLIENT = genai.Client(http_options=types.HttpOptions(timeout=120_000))
    return _CLIENT


async def _generate(model: str, prompt: str, config: "types.GenerateContentConfig"):
    """The sync SDK call, off the event loop — one blocked thread, not a
    blocked server."""
    import asyncio

    return await asyncio.to_thread(
        _client().models.generate_content, model=model, contents=prompt, config=config
    )


def needs_ai_review(content: str, filename: str) -> bool:
    """Heuristic cost gate for AI review."""
    fn = filename.lower()
    if fn.endswith(".sql"):
        if quick_validate_sql_syntax(content):
            return True
        upper = content.upper()
        join_count = upper.count(" JOIN ")
        if join_count >= 2 or "WITH " in upper or "OVER (" in upper:
            return True
    elif fn.endswith(".py"):
        if (
            "DAG(" in content
            or "Operator(" in content
            or len(content.splitlines()) > 50
        ):
            return True
    elif fn.endswith((".sh", ".ksh", ".bash")) and len(content.splitlines()) > 30:
        return True
    return False


def calculate_scorecard(content: str, filename: str) -> dict[str, Any]:
    """Calculate 100-point architecture quality scorecard."""
    perf_deduction = 0
    finops_deduction = 0
    security_deduction = 0
    compliance_deduction = 0

    secrets = scan_content_for_secrets(content, filename)
    if secrets:
        security_deduction += 50

    fn = filename.lower()

    # The linter is the single source of truth for these rules — the
    # scorecard only maps its findings to deductions. A weaker parallel
    # implementation here had already drifted (whole-file WHERE checks).
    _RULE_DEDUCTIONS = {
        "sql-missing-filter": (("security", 30),),
        "sql-missing-join-condition": (("perf", 20),),
        "sql-unpartitioned-window": (("perf", 15), ("finops", 15)),
        "airflow-dag-catchup": (("compliance", 15),),
        "python-missing-import": (("compliance", 10),),
    }
    from ._linter import lint_python_dag_content, lint_sql_content

    findings = []
    if fn.endswith(".sql"):
        findings = lint_sql_content(content, filename)
        if "${" not in content and any(
            k in content for k in ("DB_AEDW", "DB_STG", "DB_SRC")
        ):
            compliance_deduction += 10
    elif fn.endswith(".py"):
        findings = lint_python_dag_content(content, filename)
    seen_rules: set[str] = set()
    for f in findings:
        rule = f.rule_reference
        if rule in seen_rules:
            continue  # deduct once per rule, however often it fires
        seen_rules.add(rule)
        for bucket, points in _RULE_DEDUCTIONS.get(rule, ()):  # noqa: B007
            if bucket == "perf":
                perf_deduction += points
            elif bucket == "finops":
                finops_deduction += points
            elif bucket == "security":
                security_deduction += points
            elif bucket == "compliance":
                compliance_deduction += points

    perf_score = max(0, 100 - perf_deduction)
    finops_score = max(0, 100 - finops_deduction)
    sec_score = max(0, 100 - security_deduction)
    comp_score = max(0, 100 - compliance_deduction)
    overall = int((perf_score + finops_score + sec_score + comp_score) / 4)

    return {
        "overall_score": overall,
        "performance_score": perf_score,
        "finops_score": finops_score,
        "security_score": sec_score,
        "compliance_score": comp_score,
        "deductions": {
            "performance": perf_deduction,
            "finops": finops_deduction,
            "security": security_deduction,
            "compliance": compliance_deduction,
        },
    }


async def review_code(
    working_directory: str,
    file_path: str,
    model_override: str | None = None,
    advisory_only: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Perform AI code review using Gemini Pro."""
    base = Path(working_directory).resolve()
    target = (
        (base / file_path) if not Path(file_path).is_absolute() else Path(file_path)
    ).resolve()
    # Reads feed the model: an escape would exfiltrate arbitrary host files
    # into a prompt. Same confinement rule as every other file endpoint.
    if base != target and base not in target.parents:
        return {
            "status": "error",
            "message": f"Refused: '{file_path}' resolves outside the working directory.",
        }

    if not target.is_file():
        return {"status": "error", "message": f"File '{file_path}' not found."}

    content = target.read_text(encoding="utf-8", errors="replace")
    rules = get_rules_for_file(file_path)
    scorecard = calculate_scorecard(content, file_path)

    if not needs_ai_review(content, file_path) and not advisory_only:
        return {
            "status": "success",
            "code": "ai_skipped",
            "message": "Simple file with 0 bottlenecks. AI skipped to save cost.",
            "recommendations": ["No advanced architectural bottlenecks detected."],
            "scorecard": scorecard,
        }

    numbered_lines = [
        f"{idx + 1:03d} | {line}" for idx, line in enumerate(content.splitlines())
    ]
    numbered_code = "\n".join(numbered_lines[:500])

    model_name = str(
        model_override or os.getenv("REVIEW_AGENT_MODEL", "gemini-3.1-pro-preview")
    )

    prompt = f"""You are a code reviewer. Surface ONLY architectural enhancements.
Be terse: at most 4 short bullet points citing line numbers.
If clean, output: "No advanced architectural bottlenecks detected."

Rules to enforce:
{rules}

File: {file_path}
Code:
```
{numbered_code}
```
"""

    try:
        response = await _generate(
            model_name,
            prompt,
            types.GenerateContentConfig(temperature=0.2, max_output_tokens=2048),
        )
        review_text = response.text or "No advanced architectural bottlenecks detected."
        recommendations = [
            line.strip()
            for line in review_text.splitlines()
            if line.strip().startswith(("-", "*", "+", "1.", "2.", "3.", "4."))
        ]
        if not recommendations:
            recommendations = [review_text.strip()]

        return {
            "status": "success",
            "file_path": file_path,
            "model_used": model_name,
            "recommendations": recommendations[:4],
            "raw_review": review_text,
            "scorecard": scorecard,
        }
    except Exception as e:
        logger.exception("AI review error: %s", e)
        return {
            "status": "partial",
            "message": "AI review unavailable - see server logs.",
            "recommendations": [
                "Automated rule checks completed. Deep AI review skipped."
            ],
            "scorecard": scorecard,
        }


async def get_quality_score(
    working_directory: str,
    file_path: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Calculate and return quality score without calling LLM."""
    base = Path(working_directory)
    target = base / file_path if not Path(file_path).is_absolute() else Path(file_path)
    if not target.is_file():
        return {"status": "error", "message": f"File '{file_path}' not found."}
    content = target.read_text(encoding="utf-8", errors="replace")
    return {
        "status": "success",
        "file_path": file_path,
        "scorecard": calculate_scorecard(content, file_path),
    }


async def review_pr_diff(
    working_directory: str,
    base_branch: str = "main",
    model_override: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Review only the PR diff (changed lines) between current branch and base."""
    from ._executor import run_git_command

    diff_result = await run_git_command(
        ["diff", f"{base_branch}...HEAD", "--unified=3"],
        cwd=working_directory,
    )

    if diff_result.get("status") == "error":
        return {
            "status": "error",
            "message": f"Could not generate diff against {base_branch}.",
            "detail": diff_result.get("message", ""),
        }

    diff_text = diff_result.get("stdout", "")
    if not diff_text.strip():
        return {
            "status": "success",
            "message": f"No changes detected against {base_branch}.",
            "recommendations": [],
        }

    # Truncate diff to first 500 lines for token budget
    diff_lines = diff_text.splitlines()[:500]
    truncated_diff = "\n".join(diff_lines)

    model_name = str(
        model_override or os.getenv("REVIEW_AGENT_MODEL", "gemini-3.1-pro-preview")
    )

    prompt = f"""You are a senior code reviewer analyzing a pull request diff.
Focus ONLY on the changed lines (+ and - prefixed). Surface high-impact issues:
- Security vulnerabilities introduced
- Performance regressions
- Breaking API changes
- Missing error handling

Be terse: at most 4 bullet points with file/line references.
If the diff looks clean, respond: "No issues found in the changed lines."

Diff (against {base_branch}):
```diff
{truncated_diff}
```
"""

    try:
        response = await _generate(
            model_name,
            prompt,
            types.GenerateContentConfig(temperature=0.2, max_output_tokens=2048),
        )
        review_text = response.text or "No issues found in the changed lines."
        recommendations = [
            line.strip()
            for line in review_text.splitlines()
            if line.strip().startswith(("-", "*", "+", "1.", "2.", "3.", "4."))
        ]
        if not recommendations:
            recommendations = [review_text.strip()]

        return {
            "status": "success",
            "base_branch": base_branch,
            "model_used": model_name,
            "diff_lines_reviewed": len(diff_lines),
            "recommendations": recommendations[:4],
            "raw_review": review_text,
        }
    except Exception as e:
        logger.exception("PR diff review error: %s", e)
        return {
            "status": "partial",
            "message": "AI PR review unavailable - see server logs.",
            "recommendations": ["PR diff review could not be completed."],
        }


async def generate_pr_description(
    working_directory: str,
    base_branch: str = "main",
    pr_title: str | None = None,
    model_override: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Auto-generate a markdown PR description from the current branch diff."""
    import asyncio

    from ._executor import run_git_command

    # Three independent reads — one round of subprocess wall time, not three.
    stat_result, log_result, diff_result = await asyncio.gather(
        run_git_command(
            ["diff", f"{base_branch}...HEAD", "--stat"], cwd=working_directory
        ),
        run_git_command(
            ["log", f"{base_branch}..HEAD", "--oneline", "--no-decorate"],
            cwd=working_directory,
        ),
        run_git_command(
            ["diff", f"{base_branch}...HEAD", "--unified=2"], cwd=working_directory
        ),
    )

    stat_text = stat_result.get("stdout", "No stats available")
    log_text = log_result.get("stdout", "No commits")
    diff_text = diff_result.get("stdout", "")

    # Truncate diff for token budget
    diff_lines = diff_text.splitlines()[:300]
    truncated_diff = "\n".join(diff_lines)

    model_name = str(model_override or os.getenv("AI_REVIEW_MODEL", "gemini-3.7-flash"))

    prompt = f"""Generate a professional GitHub Pull Request description in markdown.

Title: {pr_title or "Auto-generated"}

Include these sections:
## What
Brief summary of changes (2-3 sentences).

## Why
Business/technical motivation.

## Changes
Bullet list of key changes derived from the diff.

## Testing
Suggest what should be tested.

Commit log:
{log_text}

File stats:
{stat_text}

Diff (truncated):
```diff
{truncated_diff}
```
"""

    try:
        response = await _generate(
            model_name,
            prompt,
            types.GenerateContentConfig(temperature=0.3, max_output_tokens=2048),
        )
        description = response.text or "No description generated."

        return {
            "status": "success",
            "model_used": model_name,
            "pr_description": description,
            "stats": stat_text,
            "commits": log_text,
        }
    except Exception as e:
        logger.exception("PR description generation error: %s", e)
        return {
            "status": "partial",
            "message": f"AI PR description generation failed: {e}",
            "pr_description": f"## Changes\n\n{stat_text}\n\n## Commits\n\n{log_text}",
        }
