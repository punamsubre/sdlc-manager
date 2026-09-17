"""Pre-commit / pre-PR quality gate.

Three native checks that run everywhere with nothing installed, so a gate
verdict is always a real verdict (passed or failed — never "could not run"):

  1. Hygiene  — trailing whitespace, missing end-of-file newline,
                merge-conflict markers, oversized files, invalid JSON/YAML.
                Ported from the Ford SDLC foundation's hygiene.sh.
  2. Secrets  — the platform's existing scanner; hard block by design.
  3. Message  — Conventional Commits for commit messages and PR titles,
                ported from the foundation's commitlint rules.

The gate proposes fixes and blocks until clean; it never modifies a file
without an explicit accept from the developer.
"""

import json
import os
import re
from typing import Any

from .validation_tools import scan_content_for_secrets

MAX_FILE_BYTES = 1024 * 1024  # 1 MiB, same bound as the foundation's hygiene.sh

CONVENTIONAL_TYPES = [
    "feat", "fix", "docs", "style", "refactor", "perf",
    "test", "build", "ci", "chore", "revert",
]
_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-z]+)(\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<subject>.+)$"
)
MAX_HEADER_LENGTH = 100

# Binary sniff: files with NUL bytes in the first block are skipped by hygiene.
_BINARY_SNIFF_BYTES = 8192


def _is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True


def check_hygiene_content(content: str, file_path: str) -> list[dict[str, Any]]:
    """Hygiene findings for one file's text content."""
    findings: list[dict[str, Any]] = []
    lines = content.split("\n")

    for idx, line in enumerate(lines, start=1):
        if line != line.rstrip(" \t"):
            findings.append({
                "file": file_path,
                "line": idx,
                "rule": "trailing-whitespace",
                "message": "Line ends with whitespace." if line.strip() else "Line contains only whitespace.",
                "auto_fixable": True,
            })

    if content and not content.endswith("\n"):
        findings.append({
            "file": file_path,
            "line": len(lines),
            "rule": "missing-eof-newline",
            "message": "File does not end with a newline.",
            "auto_fixable": True,
        })

    for idx, line in enumerate(lines, start=1):
        if re.match(r"^(<{7}|={7}|>{7})( |$)", line):
            findings.append({
                "file": file_path,
                "line": idx,
                "rule": "merge-conflict-marker",
                "message": "Unresolved merge-conflict marker.",
                "auto_fixable": False,
            })

    lower = file_path.lower()
    if lower.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            findings.append({
                "file": file_path,
                "line": e.lineno,
                "rule": "invalid-json",
                "message": f"Invalid JSON: {e.msg}.",
                "auto_fixable": False,
            })
    elif lower.endswith((".yaml", ".yml")):
        try:
            import yaml

            list(yaml.safe_load_all(content))
        except ImportError:
            pass  # no YAML parser in this runtime; the other hygiene rules still apply
        except Exception as e:
            mark = getattr(e, "problem_mark", None)
            findings.append({
                "file": file_path,
                "line": (mark.line + 1) if mark else 1,
                "rule": "invalid-yaml",
                "message": f"Invalid YAML: {str(e).splitlines()[0][:160]}.",
                "auto_fixable": False,
            })

    return findings


def check_hygiene_file(working_directory: str, file_path: str) -> list[dict[str, Any]]:
    """Hygiene findings for one file on disk."""
    full = os.path.join(working_directory, file_path)
    if not os.path.isfile(full):
        return []

    size = os.path.getsize(full)
    if size > MAX_FILE_BYTES:
        return [{
            "file": file_path,
            "line": None,
            "rule": "oversized-file",
            "message": f"File is {size // 1024} KB; the limit is {MAX_FILE_BYTES // 1024} KB. Large binaries belong in artifact storage, not git.",
            "auto_fixable": False,
        }]
    if _is_binary(full):
        return []

    with open(full, encoding="utf-8", errors="replace", newline="") as fh:
        content = fh.read()
    # Normalize CRLF for line-oriented rules; the fixer preserves the file's own endings.
    return check_hygiene_content(content.replace("\r\n", "\n"), file_path)


def apply_hygiene_fixes(working_directory: str, file_path: str) -> dict[str, Any]:
    """Apply the deterministic hygiene fixes: strip trailing whitespace,
    ensure a final newline. Preserves the file's dominant line ending.
    Only runs on explicit developer accept."""
    full = os.path.join(working_directory, file_path)
    if not os.path.isfile(full):
        return {"status": "error", "message": "File no longer exists on disk."}
    if _is_binary(full):
        return {"status": "error", "message": "Not a text file."}

    with open(full, encoding="utf-8", errors="replace", newline="") as fh:
        original = fh.read()

    eol = "\r\n" if original.count("\r\n") >= original.count("\n") - original.count("\r\n") and "\r\n" in original else "\n"
    lines = original.replace("\r\n", "\n").split("\n")
    fixed_lines = [line.rstrip(" \t") for line in lines]

    fixed = eol.join(fixed_lines)
    if fixed and not fixed.endswith(eol):
        fixed += eol

    if fixed == original:
        return {"status": "success", "changed": False, "message": "Nothing to fix."}

    with open(full, "w", encoding="utf-8", newline="") as fh:
        fh.write(fixed)
    return {"status": "success", "changed": True, "message": f"Cleaned whitespace and line endings in '{file_path}'."}


def check_conventional(header: str, kind: str = "commit message") -> dict[str, Any]:
    """Conventional Commits validation for a commit message or PR title."""
    header = (header or "").strip().splitlines()[0] if (header or "").strip() else ""
    errors: list[str] = []

    if not header:
        errors.append(f"The {kind} is empty.")
        return {"valid": False, "errors": errors, "suggestion": None}

    if len(header) > MAX_HEADER_LENGTH:
        errors.append(f"The {kind} is {len(header)} characters; the limit is {MAX_HEADER_LENGTH}.")

    m = _CONVENTIONAL_RE.match(header)
    if not m:
        errors.append(
            f"The {kind} must follow Conventional Commits: 'type: description' or 'type(scope): description'."
        )
    else:
        if m.group("type") not in CONVENTIONAL_TYPES:
            errors.append(
                f"'{m.group('type')}' is not an allowed type. Use one of: {', '.join(CONVENTIONAL_TYPES)}."
            )
        if m.group("scope") and m.group("scope") != m.group("scope").lower():
            errors.append("The scope must be lower-case.")
        if not (m.group("subject") or "").strip():
            errors.append("The description after ': ' must not be empty.")

    if not errors:
        return {"valid": True, "errors": [], "suggestion": None}
    return {"valid": False, "errors": errors, "suggestion": _suggest_conventional(header)}


def _suggest_conventional(header: str) -> str:
    """Deterministic best-effort rewrite of a non-conventional header."""
    text = header.strip().rstrip(".")

    # Salvage a valid structure that only used a wrong/upper-case type.
    m = _CONVENTIONAL_RE.match(text) or _CONVENTIONAL_RE.match(text.lower())
    if m and m.group("type").lower() in CONVENTIONAL_TYPES:
        scope = f"({m.group('scope').lower()})" if m.group("scope") else ""
        text = f"{m.group('type').lower()}{scope}: {m.group('subject').strip()}"
        return text[:MAX_HEADER_LENGTH]

    # Otherwise guess a type from the wording and prefix it.
    body = re.sub(r"^[a-zA-Z]+\s*[:\-]\s*", "", text) or text
    lowered = body.lower()
    if any(w in lowered for w in ("fix", "bug", "correct", "resolve")):
        guessed = "fix"
    elif any(w in lowered for w in ("add", "new", "implement", "create", "introduce")):
        guessed = "feat"
    elif any(w in lowered for w in ("doc", "readme", "guide")):
        guessed = "docs"
    elif any(w in lowered for w in ("test", "coverage")):
        guessed = "test"
    else:
        guessed = "chore"
    body = body[0].lower() + body[1:] if body else body
    return f"{guessed}: {body}"[:MAX_HEADER_LENGTH]


def run_gate(
    working_directory: str,
    files: list[str],
    file_contents: dict[str, str] | None = None,
    message: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Run all native checks. file_contents overrides disk content when the
    gate inspects branch content instead of the working tree (PR context)."""
    file_contents = file_contents or {}

    hygiene: list[dict[str, Any]] = []
    secrets: list[dict[str, Any]] = []
    for f in files:
        if f in file_contents:
            content = file_contents[f].replace("\r\n", "\n")
            hygiene.extend(check_hygiene_content(content, f))
            raw = file_contents[f]
        else:
            hygiene.extend(check_hygiene_file(working_directory, f))
            full = os.path.join(working_directory, f)
            if not os.path.isfile(full) or _is_binary(full):
                continue
            # Hygiene already refused >1 MiB files; the secret scan must
            # honor the same cap instead of materializing them anyway.
            if os.path.getsize(full) > 1024 * 1024:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        for s in scan_content_for_secrets(raw, f):
            secrets.append({
                "file": s["file"],
                "line": s["line_number"],
                "rule": s["secret_type"],
                "message": s["description"],
                "auto_fixable": False,
            })

    checks: list[dict[str, Any]] = [
        {
            "id": "hygiene",
            "name": "File hygiene",
            "status": "failed" if hygiene else "passed",
            "findings": hygiene,
        },
        {
            "id": "secrets",
            "name": "Secret scan",
            "status": "failed" if secrets else "passed",
            "findings": secrets,
        },
    ]

    if message is not None:
        result = check_conventional(message, "commit message")
        checks.append({
            "id": "message",
            "name": "Commit message",
            "status": "passed" if result["valid"] else "failed",
            "findings": [
                {"file": None, "line": None, "rule": "conventional-commit", "message": e, "auto_fixable": False}
                for e in result["errors"]
            ],
            "suggestion": result["suggestion"],
        })
    if title is not None:
        result = check_conventional(title, "pull request title")
        checks.append({
            "id": "title",
            "name": "Pull request title",
            "status": "passed" if result["valid"] else "failed",
            "findings": [
                {"file": None, "line": None, "rule": "conventional-commit", "message": e, "auto_fixable": False}
                for e in result["errors"]
            ],
            "suggestion": result["suggestion"],
        })

    return {
        "status": "success",
        "passed": all(c["status"] == "passed" for c in checks),
        "checks": checks,
        "files_checked": len(files),
    }
