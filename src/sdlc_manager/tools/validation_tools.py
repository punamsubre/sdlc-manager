"""Validation tools for code quality, AST linting, and fix proposals."""

import asyncio
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext

from ._linter import (
    ProposedFix,
    lint_python_dag_content,
    lint_sql_content,
    quick_validate_sql_syntax,
)
from ._schema import parse_dataset_map

logger = logging.getLogger(__name__)

SECRET_PATTERNS: list[tuple[str, str, str]] = [
    (
        "GCP API Key",
        r"AIza[0-9A-Za-z\-_]{35}",
        "Critical security risk: Exposed Google Cloud API key.",
    ),
    (
        "AWS Access Key ID",
        r"AKIA[0-9A-Z]{16}",
        "Critical security risk: Exposed AWS Access Key ID.",
    ),
    (
        "Generic Secret Assignment",
        r"""(?i)(?:password|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*=\s*['"][a-zA-Z0-9_\-.~!@#$%^&*()+=]{8,}['"]""",
        "High security risk: Hardcoded password, token or key string assignment.",
    ),
    (
        "Private Key Header",
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
        "Critical security risk: Exposed cryptographic private key.",
    ),
]


# Patterns compile once at import; matching runs per line per file.
_COMPILED_SECRET_PATTERNS = [
    (name, re.compile(pattern), desc) for name, pattern, desc in SECRET_PATTERNS
]
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-/.+]{20,}")

# (path -> ((mtime_ns, size), findings)) — identity-keyed scan results.
_SCAN_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}


def _shannon_entropy(data: str) -> float:
    """Calculate Shannon entropy of a string to detect high-randomness secrets."""
    if not data:
        return 0.0
    freq: dict[str, int] = {}
    for ch in data:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(data)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def scan_content_for_secrets(
    content: str, filename: str = "unknown"
) -> list[dict[str, Any]]:
    """Scan string content for hardcoded secrets and credentials."""
    findings: list[dict[str, Any]] = []
    lines = content.splitlines()
    # Comment lines are scanned too: a credential in a comment is just as
    # leaked as one in code once it reaches git history.
    for line_idx, line in enumerate(lines, start=1):
        for name, pattern, desc in _COMPILED_SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    {
                        "file": filename,
                        "line_number": line_idx,
                        "secret_type": name,
                        "description": desc,
                        "snippet": line[:80],
                    }
                )
    # Shannon entropy scan: detect high-randomness tokens (API keys, passwords)
    for line_idx, line in enumerate(lines, start=1):
        # Look for long random-looking tokens (>20 chars, high entropy)
        tokens = _TOKEN_RE.findall(line)
        for token in tokens:
            entropy = _shannon_entropy(token)
            is_common = any(
                token.startswith(p)
                for p in ("http", "import", "from_", "class_", "def_")
            )
            if entropy > 4.5 and len(token) >= 24 and not is_common:
                desc = (
                    f"High-entropy string (entropy={entropy:.2f})"
                    " may be an API key or secret."
                )
                findings.append(
                    {
                        "file": filename,
                        "line_number": line_idx,
                        "secret_type": "High Entropy Token",
                        "description": desc,
                        "snippet": line[:80],
                    }
                )

    return findings


async def scan_secrets(
    working_directory: str,
    file_path: str | None = None,
    tool_context: ToolContext | None = None,
    exclude_dirs: list[str] | None = None,
) -> dict[str, Any]:
    """Scan files in working directory for hardcoded secrets.

    exclude_dirs: top-level folders to skip (e.g. ["tests"] so deliberate
    test fixtures don't count as real findings). Skipped files are counted
    and reported so the exclusion is transparent, never silent.
    """
    base = Path(working_directory)
    if not base.is_dir():
        return {
            "status": "error",
            "message": f"Directory '{working_directory}' does not exist.",
        }

    excluded = {
        d.strip().replace("\\", "/").strip("/")
        for d in (exclude_dirs or [])
        if d.strip()
    }

    return await asyncio.to_thread(
        _scan_secrets_sync, base, file_path, excluded
    )


def _scan_secrets_sync(
    base: Path, file_path: str | None, excluded: set[str]
) -> dict[str, Any]:
    """The filesystem walk and reads, in a worker thread — a repo-wide
    scan must not stall the event loop."""
    all_findings: list[dict[str, Any]] = []
    targets: list[Path] = []

    if file_path:
        target = (
            base / file_path if not Path(file_path).is_absolute() else Path(file_path)
        )
        if target.is_file():
            targets.append(target)
    else:
        # One pruned walk instead of eight rglob passes: rglob descends into
        # node_modules/.git/.venv on every pass and filters afterwards, which
        # dominates scan time on any repo with installed dependencies.
        wanted = {".sql", ".py", ".ksh", ".sh", ".tf", ".json", ".yaml", ".yml"}
        skip_dirs = {".git", "node_modules", ".venv", "__pycache__", ".terraform"}
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                if Path(fname).suffix.lower() in wanted:
                    targets.append(Path(dirpath) / fname)

    # Dependency lockfiles are catalogues of integrity checksums — public by
    # design, but statistically indistinguishable from secrets, so the entropy
    # scan would flood on them. Gitleaks ships the same default exclusion.
    lockfiles = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
                 "poetry.lock", "Cargo.lock", "composer.lock", "Gemfile.lock",
                 ".terraform.lock.hcl"}

    scanned = 0
    excluded_count = 0
    for p in targets:
        if ".git" in p.parts or "node_modules" in p.parts or ".venv" in p.parts:
            continue
        if p.name in lockfiles:
            excluded_count += 1
            continue
        try:
            rel = str(p.relative_to(base)).replace("\\", "/")
        except ValueError:
            rel = str(p)
        if excluded and any(rel == d or rel.startswith(d + "/") for d in excluded):
            excluded_count += 1
            continue
        try:
            # An unchanged file scans to the same result — cache by identity
            # (path, mtime, size) so dashboard refreshes and repeat gate runs
            # only pay for files that actually changed.
            stat = p.stat()
            cache_key = str(p)
            cached = _SCAN_CACHE.get(cache_key)
            if cached and cached[0] == (stat.st_mtime_ns, stat.st_size):
                findings = cached[1]
            else:
                content = p.read_text(encoding="utf-8", errors="replace")
                findings = scan_content_for_secrets(content, rel)
                # Bounded: drop the oldest half rather than grow forever
                # across every repo this process ever scans.
                if len(_SCAN_CACHE) > 20_000:
                    for stale in list(_SCAN_CACHE)[:10_000]:
                        _SCAN_CACHE.pop(stale, None)
                _SCAN_CACHE[cache_key] = ((stat.st_mtime_ns, stat.st_size), findings)
            all_findings.extend(findings)
            scanned += 1
        except OSError as e:
            logger.debug("Failed to read file %s: %s", p, e)
            continue

    if all_findings:
        return {
            "status": "blocked",
            "code": "secrets_detected",
            "message": (
                f"Found {len(all_findings)} secret(s). Commits are BLOCKED."
            ),
            "findings": all_findings,
            "scanned_files_count": scanned,
            "excluded_files_count": excluded_count,
        }

    return {
        "status": "success",
        "code": "clean",
        "message": f"Secret scan passed: 0 secrets in {scanned} files.",
        "scanned_files_count": scanned,
        "excluded_files_count": excluded_count,
    }


async def validate_sql_syntax(
    sql_code: str,
    template_vars: dict[str, str] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Validate SQL syntax using fast local AST parsing (sqlglot)."""
    error = quick_validate_sql_syntax(sql_code, template_vars)
    if error:
        return {
            "status": "invalid",
            "code": "syntax_error",
            "message": f"SQL Syntax Error: {error}",
            "error_detail": error,
        }
    return {
        "status": "valid",
        "code": "syntax_ok",
        "message": "SQL syntax is valid.",
    }


async def check_bigquery_schema(
    working_directory: str,
    file_path: str,
    dataset_map: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Verify a SQL file's table and column references against live BigQuery
    metadata.

    Returns a layered result: confirmed mismatches (missing columns, wrong
    datatypes) are issues; tables that cannot be found are non-blocking
    'unverified' notes — they may be created downstream.
    """
    base = Path(working_directory)
    full_path = (
        base / file_path if not Path(file_path).is_absolute() else Path(file_path)
    )
    if not full_path.is_file():
        return {
            "status": "error",
            "message": f"File '{file_path}' does not exist in '{working_directory}'.",
        }
    if full_path.suffix.lower() != ".sql":
        return {
            "status": "not_applicable",
            "message": "Schema verification applies to .sql files only.",
        }
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {
            "status": "error",
            "message": f"Could not read file '{file_path}': {e}",
        }
    from .schema_validation import validate_sql_schema

    return await validate_sql_schema(
        content, dataset_map or os.getenv("SQL_DATASET_MAP")
    )


async def validate_file(
    working_directory: str,
    file_path: str,
    dataset_map: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Run deterministic validation on a file and generate PROPOSED fixes."""
    base = Path(working_directory)
    full_path = (
        base / file_path if not Path(file_path).is_absolute() else Path(file_path)
    )

    if not full_path.is_file():
        return {
            "status": "error",
            "message": f"File '{file_path}' does not exist in '{working_directory}'.",
        }

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {
            "status": "error",
            "message": f"Could not read file '{file_path}': {e}",
        }

    # Step 1: Secret scan (hard block)
    secret_findings = scan_content_for_secrets(content, file_path)
    if secret_findings:
        return {
            "status": "blocked",
            "code": "secrets_detected",
            "message": f"Secrets found in '{file_path}'. This file is blocked.",
            "findings": secret_findings,
            "proposed_fixes": [],
        }

    # Step 2: AST / Rule Linting
    proposed_fixes: list[ProposedFix] = []
    fn = file_path.lower()

    if fn.endswith(".sql"):
        raw_map = parse_dataset_map(dataset_map)
        # Broken SQL parses to nothing, so the rule linter would silently
        # report a syntactically invalid file as clean — syntax comes first.
        syntax_error = quick_validate_sql_syntax(content, raw_map or None)
        if syntax_error:
            return {
                "status": "invalid",
                "code": "syntax_error",
                "file_path": file_path,
                "total_proposals": 0,
                "proposed_fixes": [],
                "message": f"SQL could not be parsed: {syntax_error}",
            }
        param_map = (
            {real: f"${{{var}}}" for var, real in raw_map.items()} if raw_map else None
        )
        proposed_fixes = lint_sql_content(
            content=content,
            file_path=file_path,
            dataset_param_map=param_map,
        )
    elif fn.endswith(".py"):
        proposed_fixes = lint_python_dag_content(
            content=content,
            file_path=file_path,
        )

    return {
        "status": "review_required" if proposed_fixes else "clean",
        "file_path": file_path,
        "total_proposals": len(proposed_fixes),
        "proposed_fixes": [f.to_dict() for f in proposed_fixes],
        "message": (
            f"Found {len(proposed_fixes)} proposal(s) for '{file_path}'."
            if proposed_fixes
            else f"'{file_path}' passed all quality checks."
        ),
    }


_APPLY_LOCK: "asyncio.Lock | None" = None


def _apply_lock() -> "asyncio.Lock":
    global _APPLY_LOCK
    if _APPLY_LOCK is None:
        _APPLY_LOCK = asyncio.Lock()
    return _APPLY_LOCK


async def apply_fixes(
    working_directory: str,
    file_path: str,
    accepted_fix_ids: list[str],
    dataset_map: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Apply accepted fix proposals to a file on disk."""
    base = Path(working_directory).resolve()
    full_path = (
        (base / file_path) if not Path(file_path).is_absolute() else Path(file_path)
    ).resolve()
    # This function WRITES — the target must stay inside the workspace.
    if base != full_path and base not in full_path.parents:
        return {
            "status": "error",
            "message": (
                f"Refused: '{file_path}' resolves outside the working directory."
            ),
        }

    if not full_path.is_file():
        return {
            "status": "error",
            "message": f"File '{file_path}' does not exist.",
        }

    # Same dataset_map as the original validation — otherwise the
    # parameterization fix IDs generated earlier can never match here.
    val_res = await validate_file(
        working_directory=working_directory,
        file_path=file_path,
        dataset_map=dataset_map,
        tool_context=tool_context,
    )
    proposals = val_res.get("proposed_fixes", [])

    if not proposals:
        return {
            "status": "success",
            "message": "No pending proposals for this file.",
            "applied_count": 0,
        }

    # One writer at a time: revalidate/read/write must not interleave
    # with another apply on the same process.
    async with _apply_lock():
        content = full_path.read_text(encoding="utf-8", errors="replace")
        applied = 0

        for prop in proposals:
            if prop["fix_id"] not in accepted_fix_ids:
                continue
            orig = prop["original_code"]
            replacement = prop["proposed_code"]
            # Anchor at the recorded line: first-occurrence patching edits
            # the wrong copy when the same text appears twice in a file.
            line_no = prop.get("line_number")
            replaced = False
            if line_no:
                lines = content.splitlines(keepends=True)
                offset = sum(len(ln) for ln in lines[: line_no - 1])
                idx = content.find(orig, max(0, offset - 1))
                if idx != -1:
                    content = content[:idx] + replacement + content[idx + len(orig):]
                    replaced = True
            if not replaced and orig in content:
                content = content.replace(orig, replacement, 1)
                replaced = True
            if replaced:
                applied += 1

        if applied > 0:
            full_path.write_text(content, encoding="utf-8")

    return {
        "status": "success",
        "code": "fixes_applied",
        "message": f"Applied {applied} accepted fix(es) to '{file_path}'.",
        "applied_count": applied,
    }


async def validate_directory(
    working_directory: str,
    file_extensions: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Batch validate all matching files in a directory."""
    base = Path(working_directory)
    if not base.is_dir():
        return {
            "status": "error",
            "message": f"Directory '{working_directory}' does not exist.",
        }

    exts = tuple(file_extensions) if file_extensions else (".sql", ".py", ".tf")
    all_results: list[dict[str, Any]] = []
    total_proposals = 0

    # Pruned walk (same rationale as scan_secrets) + bounded concurrency:
    # wall time is the slowest few files, not the sum of all of them.
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__", ".terraform"}
    rel_paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            if Path(fname).suffix.lower() in exts:
                rel_paths.append(str((Path(dirpath) / fname).relative_to(base)))

    semaphore = asyncio.Semaphore(4)

    async def _validate_one(rel_path: str) -> dict[str, Any]:
        async with semaphore:
            return await validate_file(
                working_directory=working_directory,
                file_path=rel_path,
                tool_context=tool_context,
            )

    all_results = list(await asyncio.gather(*(_validate_one(r) for r in rel_paths)))
    total_proposals = sum(len(r.get("proposed_fixes", [])) for r in all_results)

    return {
        "status": "success",
        "scanned_files": len(all_results),
        "total_proposals": total_proposals,
        "results": all_results,
    }


async def check_readiness(
    working_directory: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Pre-commit readiness check: combines secret scan and basic quality checks."""
    secrets_res = await scan_secrets(
        working_directory=working_directory, tool_context=tool_context
    )
    if secrets_res.get("status") == "blocked":
        return {
            "status": "blocked",
            "ready": False,
            "message": "Pre-commit check failed: secrets found in the repository.",
            "secrets": secrets_res,
        }

    return {
        "status": "success",
        "ready": True,
        "message": "Readiness check passed. Repository is clean.",
    }
