"""Bridge to a repository's own lefthook-governed quality gates.

When the served repository carries a lefthook.yml, that file — not this
platform — defines what a valid change is. This module reads it, selects the
commands that apply on this machine's OS, and executes them one by one so
every check gets its own verdict and output. The rules are never copied here:
if the repository changes its gates, this bridge follows automatically.

Three commands need context adaptation, because the gate runs before anything
is staged and must never modify the developer's files or git index:

  - a `fmt` target (lefthook runs it with stage_fixed, i.e. it rewrites files)
    is swapped for its check-only `fmt-check` twin;
  - `gitleaks protect --staged` (reads the git index) becomes a per-file scan
    of exactly the files the gate was asked about;
  - `security-check` (derives Checkov targets from `git diff --cached`) becomes
    a direct Checkov run over the module directories those files belong to.

Everything else runs verbatim.
"""

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext

LEFTHOOK_FILE = "lefthook.yml"
STAGE_HOOKS = {"commit": ["pre-commit"], "pr": ["pre-push"]}

# Repo-wide validate/test targets initialise and test real modules; they are
# slow by nature and get a longer leash than the fast pre-commit checks.
CHECK_TIMEOUT_SECONDS = {"pre-commit": 300, "pre-push": 900, "commit-msg": 60}
_MAX_OUTPUT_CHARS = 20_000


def _is_windows() -> bool:
    return os.name == "nt"


_ENV_CACHE: tuple[float, dict[str, str]] | None = None
_ENV_CACHE_TTL = 5.0


def _effective_env() -> dict[str, str]:
    """The process environment with PATH refreshed from the Windows registry.

    Installers (winget, pip, npm) extend PATH via the registry; a long-running
    server keeps the PATH it was born with, and IDE-spawned terminals inherit
    the IDE's frozen copy. Reading the live machine+user PATH directly makes
    tool detection and check execution independent of how stale the parent
    process is — otherwise 'run setup' installs tools this process can't see."""
    global _ENV_CACHE
    import time

    if _ENV_CACHE and time.monotonic() - _ENV_CACHE[0] < _ENV_CACHE_TTL:
        return dict(_ENV_CACHE[1])
    env = dict(os.environ)
    if not _is_windows():
        return env
    try:
        import winreg

        registry_paths = []
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    value, _ = winreg.QueryValueEx(k, "Path")
                    registry_paths.append(os.path.expandvars(value))
            except OSError:
                continue

        seen: set[str] = set()
        merged: list[str] = []
        for part in ";".join([env.get("PATH", ""), *registry_paths]).split(";"):
            cleaned = part.strip()
            key_form = cleaned.rstrip("\\").lower()
            if cleaned and key_form not in seen:
                seen.add(key_form)
                merged.append(cleaned)
        env["PATH"] = ";".join(merged)

        # Git's bin directory must lead: otherwise a bare `bash` in any child
        # process (repo Makefile targets, hook scripts) resolves to System32's
        # WSL stub — a Linux environment where the Windows toolchain vanishes.
        import shutil

        git = shutil.which("git", path=env["PATH"])
        if git:
            bash_dir = str(Path(git).parent.parent / "bin")
            if Path(bash_dir).is_dir() and not env["PATH"].lower().startswith(bash_dir.lower()):
                env["PATH"] = bash_dir + ";" + env["PATH"]
    except Exception:
        pass  # a registry read failure falls back to the inherited PATH
    _ENV_CACHE = (time.monotonic(), dict(env))
    return env


def discover_lefthook(working_directory: str) -> dict[str, Any] | None:
    """Parsed lefthook.yml of the served repo, or None when absent/unreadable."""
    path = Path(working_directory) / LEFTHOOK_FILE
    if not path.is_file():
        return None
    try:
        import yaml

        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return config if isinstance(config, dict) else None


# Executable skip/only conditions are evaluated once per unique command —
# the same condition typically guards every check pair in a config.
_CONDITION_CACHE: dict[str, bool] = {}


def _condition_true(cond: dict[str, Any]) -> bool:
    """Evaluate one lefthook skip/only condition on this machine: the
    declarative `os:` form directly, the executable `run:` form by running it
    exactly as lefthook does (exit 0 = condition true, under sh/Git Bash)."""
    if "os" in cond:
        import platform

        current = "windows" if _is_windows() else platform.system().lower()
        return str(cond["os"]).strip().lower() == current

    if "run" in cond:
        command = str(cond["run"])
        if command not in _CONDITION_CACHE:
            import subprocess

            env = _effective_env()
            try:
                # Executing the repo-declared condition IS the feature — this
                # is exactly what lefthook does with a `run:` condition.
                bash = _find_bash(env.get("PATH"))
                if bash:
                    result = subprocess.run(  # noqa: S603
                        [bash, "-c", command], capture_output=True, timeout=15, env=env
                    )
                else:
                    result = subprocess.run(  # noqa: S602
                        command, shell=True, capture_output=True, timeout=15, env=env
                    )
                _CONDITION_CACHE[command] = result.returncode == 0
            except Exception:
                _CONDITION_CACHE[command] = False
        return _CONDITION_CACHE[command]

    return False


def _find_bash(path_env: str | None) -> str | None:
    """Locate Git Bash. Git for Windows exposes git\\cmd on PATH, not its bin
    directory, so bash is derived from git's location. System32's bash.EXE is
    the WSL stub — a Linux environment that would answer 'uname' wrongly for
    hook conditions — so a PATH hit inside the Windows directory is ignored."""
    import shutil

    git = shutil.which("git", path=path_env)
    if git:
        candidate = Path(git).parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    bash = shutil.which("bash", path=path_env)
    if bash and "system32" not in bash.lower():
        return bash
    return None


def _os_allows(entry: dict[str, Any]) -> bool:
    """Apply lefthook's skip/only conditions for this machine."""
    if entry.get("skip") is True:
        return False

    def conds(value: Any) -> list[dict[str, Any]]:
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    if any(_condition_true(c) for c in conds(entry.get("skip"))):
        return False
    only = conds(entry.get("only"))
    return not only or any(_condition_true(c) for c in only)


def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]+)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[: m.start()], pattern[m.end() :]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(head + alt + tail))
    return out


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif ch == "*":
            parts.append("[^/]*")
            i += 1
        elif ch == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _glob_matches(pattern: str, files: list[str]) -> list[str]:
    """Files matching a lefthook glob. A pattern without '/' matches basenames,
    which is how lefthook treats patterns like '*.sh'."""
    matched: list[str] = []
    for pat in _expand_braces(pattern):
        regex = _glob_to_regex(pat)
        for f in files:
            norm = f.replace("\\", "/")
            candidate = norm.rsplit("/", 1)[-1] if "/" not in pat else norm
            if regex.match(candidate) and f not in matched:
                matched.append(f)
    return matched


# File names get substituted into shell commands ({staged_files}); anything
# that could terminate or chain a command is refused outright — the gate then
# fails closed naming the file, instead of executing a crafted path.
# %VAR% expansion and ! (delayed expansion) are cmd.exe's injection
# vectors — commands run through create_subprocess_shell on Windows.
_UNSAFE_PATH = re.compile(r"[;&|`$<>^%!\n\r\"']")


def _quote(path: str) -> str:
    if _UNSAFE_PATH.search(path):
        raise ValueError(f"unsafe characters in file path: {path!r}")
    return f'"{path}"' if " " in path else path


def _adapt_command(
    command: str, files: list[str]
) -> tuple[str | list[str], str | None]:
    """Return the command(s) to actually run, plus a note when the gate runs an
    equivalent form of the hook's command rather than the command itself."""
    # fmt rewrites files in place; the gate must only ever report.
    # Swap only the bare make-target form (command ENDS with 'fmt');
    # 'terraform fmt -check' must not become the invalid 'fmt-check -check'.
    fmt_swap = re.sub(r"\bfmt$", "fmt-check", command)
    if fmt_swap != command:
        return fmt_swap, "ran the check-only variant: the gate never rewrites files"

    if "gitleaks" in command and "--staged" in command:
        cmds = [f"gitleaks dir {_quote(f)} --no-banner --redact" for f in files]
        return (cmds or command), "scanned the files in this change instead of the git index"

    if "security-check" in command:
        modules = sorted(
            {
                m.group(1)
                for f in files
                if (m := re.match(r"^(modules/[^/]+)/.+\.(tf|hcl)$", f.replace("\\", "/")))
            }
        )
        if modules:
            cmds = [
                f"checkov --directory {_quote(m)} --framework terraform --output cli --compact --quiet"
                for m in modules
            ]
            return cmds, "scanned the modules this change touches instead of the git index"

    return command, None


def plan_checks(
    config: dict[str, Any], stage: str, files: list[str]
) -> list[dict[str, Any]]:
    """The checks the repo's lefthook.yml declares for this stage, on this OS,
    filtered to this change's files."""
    checks: list[dict[str, Any]] = []
    for hook in STAGE_HOOKS.get(stage, []):
        commands = (config.get(hook) or {}).get("commands") or {}
        for name, entry in commands.items():
            if not isinstance(entry, dict) or not entry.get("run"):
                continue
            if not _os_allows(entry):
                continue

            command = str(entry["run"])
            scoped_files = files
            if entry.get("glob"):
                scoped_files = _glob_matches(str(entry["glob"]), files)
                if not scoped_files:
                    continue  # lefthook skips a glob'd command with no matching files

            if "{staged_files}" in command:
                command = command.replace(
                    "{staged_files}", " ".join(_quote(f) for f in scoped_files)
                )

            run, note = _adapt_command(command, scoped_files)
            checks.append(
                {
                    "id": name,
                    "name": name.removesuffix("-windows").replace("-", " ").capitalize(),
                    "hook": hook,
                    "commands": run if isinstance(run, list) else [run],
                    "note": note,
                }
            )
    return checks


async def _run_shell(command: str, cwd: str, timeout: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=_effective_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        # Drain with a byte cap: a runaway check's output stays bounded in
        # memory while the child is still read to completion.
        from ._executor import _drain_capped

        async def _capped() -> bytes:
            data = await _drain_capped(proc.stdout)
            await proc.wait()
            return data

        out_bytes = await asyncio.wait_for(_capped(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        # Reap the killed child so pipes/handles are released deterministically.
        import contextlib

        with contextlib.suppress(Exception):
            await proc.wait()
        return 124, f"timed out after {timeout}s: {command}"
    output = out_bytes.decode("utf-8", errors="replace")
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[-_MAX_OUTPUT_CHARS:]
    return proc.returncode or 0, output


def _tail(output: str, lines: int = 12) -> str:
    kept = [ln for ln in output.strip().splitlines() if ln.strip()]
    return "\n".join(kept[-lines:])


# --- per-tool output parsers -------------------------------------------------
# Each turns a tool's own report into file/line findings so the UI can point
# at code and offer fixes. All are best-effort: when nothing parses, the
# caller falls back to showing the raw failing output, so a format change in
# a tool degrades the display, never the verdict.

_HYGIENE_RULES = {
    "trailing whitespace": ("trailing-whitespace", True),
    "missing end-of-file newline": ("missing-eof-newline", True),
    "merge conflict marker": ("merge-conflict-marker", False),
}


def _parse_hygiene(output: str) -> list[dict[str, Any]]:
    findings = []
    for line in output.splitlines():
        m = re.match(r"^error: (trailing whitespace|missing end-of-file newline|merge conflict marker): (.+)$", line.strip())
        if m:
            rule, fixable = _HYGIENE_RULES[m.group(1)]
            findings.append({"file": m.group(2).strip(), "line": None, "rule": rule,
                             "message": f"{m.group(1).capitalize()}.", "auto_fixable": fixable})
            continue
        m = re.match(r"^error: file larger than 1 MiB \((\d+) bytes\): (.+)$", line.strip())
        if m:
            findings.append({"file": m.group(2).strip(), "line": None, "rule": "oversized-file",
                             "message": f"File is {int(m.group(1)) // 1024} KB; the limit is 1024 KB.", "auto_fixable": False})
            continue
        m = re.match(r"^error: invalid (JSON|YAML): (.+)$", line.strip())
        if m:
            findings.append({"file": m.group(2).strip(), "line": None, "rule": f"invalid-{m.group(1).lower()}",
                             "message": f"Invalid {m.group(1)}.", "auto_fixable": False})
    return findings


def _parse_gitleaks(output: str) -> list[dict[str, Any]]:
    findings = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        m = re.match(r"^(RuleID|File|Line|Finding):\s*(.+)$", line.strip())
        if not m:
            continue
        current[m.group(1)] = m.group(2).strip()
        if m.group(1) == "Line" and current.get("RuleID"):
            findings.append({
                "file": current.get("File"),
                "line": int(current["Line"]) if current["Line"].isdigit() else None,
                "rule": current["RuleID"],
                "message": f"Possible secret ({current['RuleID']}) — remove it and rotate the credential.",
                "auto_fixable": False,
            })
            current = {}
    return findings


def _parse_checkov(output: str, command: str) -> list[dict[str, Any]]:
    base = ""
    m = re.search(r"--directory\s+\"?([^\s\"]+)\"?", command)
    if m:
        base = m.group(1).rstrip("/")
    findings = []
    for block in re.finditer(
        r"Check: (CKV\S+): \"([^\"]+)\"\s*\n\s*FAILED for resource: (\S+)\s*\n\s*File: /?([^:\n]+):(\d+)",
        output,
    ):
        check_id, desc, resource, path, line = block.groups()
        findings.append({
            "file": f"{base}/{path.lstrip('/')}" if base else path,
            "line": int(line),
            "rule": check_id,
            "message": f"{desc} (resource: {resource})",
            "auto_fixable": False,
        })
    return findings


def _parse_shellcheck(output: str) -> list[dict[str, Any]]:
    findings = []
    current_file, current_line = None, None
    for line in output.splitlines():
        m = re.match(r"^In (.+) line (\d+):$", line.strip())
        if m:
            current_file, current_line = m.group(1), int(m.group(2))
            continue
        m = re.match(r"^.*?(SC\d+)(?: \((\w+)\))?: (.+)$", line.strip())
        if m and current_file:
            findings.append({"file": current_file, "line": current_line, "rule": m.group(1),
                             "message": m.group(3), "auto_fixable": False})
    return findings


def _parse_fmt(output: str) -> list[dict[str, Any]]:
    files = []
    for line in output.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"[\w./\\-]+\.(tf|hcl)", stripped) and stripped not in files:
            files.append(stripped)
    return [
        {"file": f, "line": None, "rule": "needs-formatting",
         "message": "File is not formatted to the repository standard.", "auto_fixable": False}
        for f in files
    ]


def _parse_tflint(output: str) -> list[dict[str, Any]]:
    findings = []
    pending = None
    for line in output.splitlines():
        m = re.match(r"^(Error|Warning|Notice): (.+?) \(([\w.-]+)\)$", line.strip())
        if m:
            pending = (m.group(3), m.group(2))
            continue
        m = re.match(r"^on (.+?) line (\d+)", line.strip())
        if m and pending:
            findings.append({"file": m.group(1), "line": int(m.group(2)), "rule": pending[0],
                             "message": pending[1], "auto_fixable": False})
            pending = None
    return findings


def _parse_findings(command: str, output: str) -> list[dict[str, Any]]:
    if "hygiene" in command:
        return _parse_hygiene(output)
    if "gitleaks" in command:
        return _parse_gitleaks(output)
    if "checkov" in command:
        return _parse_checkov(output, command)
    if "shellcheck" in command:
        return _parse_shellcheck(output)
    if "fmt-check" in command:
        return _parse_fmt(output)
    if "actionlint" in command:
        return []  # no structured parser yet — raw output is shown instead
    if re.search(r"\btflint\b|\bmake(\.ps1)?['\"]?\s+lint\b", command):
        return _parse_tflint(output)
    return []


async def _run_check(check: dict[str, Any], cwd: str) -> dict[str, Any]:
    timeout = CHECK_TIMEOUT_SECONDS.get(check["hook"], 300)
    # The adapted per-file commands (gitleaks dir per file) are independent;
    # run them concurrently with a small cap. Output order is preserved.
    semaphore = asyncio.Semaphore(4)

    async def _one(command: str) -> tuple[int, str]:
        async with semaphore:
            return await _run_shell(command, cwd, timeout)

    results = await asyncio.gather(*(_one(c) for c in check["commands"]))
    outputs = [
        f"$ {command}\n{output.strip()}"
        for command, (_code, output) in zip(check["commands"], results)
    ]
    failed = any(code != 0 for code, _output in results)

    combined = "\n\n".join(outputs)
    findings = []
    if failed:
        findings = _parse_findings(" ".join(check["commands"]), combined)
        if not findings:
            message = _tail(combined)
            if "is not recognized as an internal or external command" in combined or "command not found" in combined:
                message = (
                    "A tool this check needs is not installed on this machine. "
                    "Run the repository's setup script (scripts/setup.ps1 on Windows, "
                    "scripts/setup.sh otherwise), then try again.\n\n" + message
                )
            findings.append(
                {
                    "file": None,
                    "line": None,
                    "rule": check["id"],
                    "message": message,
                    "auto_fixable": False,
                }
            )
    return {
        "id": check["id"],
        "name": check["name"],
        "status": "failed" if failed else "passed",
        "findings": findings,
        "output": combined,
        "note": check.get("note"),
    }


def _native_message_check(text: str, kind: str) -> dict[str, Any]:
    """The platform's Conventional Commit check, in gate-check shape — used
    when the repo's lefthook.yml defines no commit-msg hook of its own."""
    from .quality_gate import check_conventional

    res = check_conventional(text, kind)
    check_id = "message" if kind == "commit message" else "title"
    return {
        "id": check_id,
        "name": "Commit message" if check_id == "message" else "Pull request title",
        "status": "passed" if res["valid"] else "failed",
        "findings": [
            {
                "file": None,
                "line": None,
                "rule": "conventional-commit",
                "message": e,
                "auto_fixable": False,
            }
            for e in res["errors"]
        ],
        "suggestion": res.get("suggestion"),
        "output": None,
        "note": "this repository's lefthook.yml defines no commit-msg hook; the platform standard applies",
    }


async def _run_commitlint(
    config: dict[str, Any], text: str, kind: str, cwd: str
) -> dict[str, Any] | None:
    """The repo's own commit-msg hook (commitlint), applied to a commit message
    or a PR title — squash merges make the title the commit subject."""
    commands = (config.get("commit-msg") or {}).get("commands") or {}
    template = next(
        (str(e["run"]) for e in commands.values() if isinstance(e, dict) and e.get("run")),
        None,
    )
    if not template:
        return None

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(text.strip() + "\n")
        tmp.close()
        command = template.replace("{1}", _quote(tmp.name))
        code, output = await _run_shell(command, cwd, CHECK_TIMEOUT_SECONDS["commit-msg"])
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    check_id = "message" if kind == "commit message" else "title"
    findings = []
    suggestion = None
    if code != 0:
        # Commitlint prints one ✖ line per broken rule; keep those, drop the tally.
        rule_lines = [
            ln.strip().lstrip("✖×xX ").strip()
            for ln in output.splitlines()
            if ln.strip().startswith(("✖", "×")) and "problems" not in ln
        ]
        for text_line in rule_lines or [_tail(output)]:
            findings.append(
                {
                    "file": None,
                    "line": None,
                    "rule": "conventional-commit",
                    "message": text_line,
                    "auto_fixable": False,
                }
            )
        # The repo's rules are the Conventional Commits standard, so the native
        # deterministic rewrite is a valid suggestion here too.
        from .quality_gate import _suggest_conventional

        suggestion = _suggest_conventional(text)
    return {
        "id": check_id,
        "name": "Commit message" if check_id == "message" else "Pull request title",
        "status": "failed" if code != 0 else "passed",
        "findings": findings,
        "suggestion": suggestion,
        "output": f"$ {command}\n{output.strip()}",
        "note": None,
    }


# The repository contract, as defined by the governed-repo maintainers:
# lefthook.yml declares how to execute the checks; the setup script carries
# the logic to install their prerequisites. Everything else a hook command
# references (Makefiles, package.json, ...) is repo-internal detail.
_KNOWN_TOOLS = [
    "pwsh", "make", "gitleaks", "shellcheck", "actionlint", "checkov",
    "npx", "tflint", "tofu", "terragrunt", "python",
]

# The repo may ship one setup script per OS. The platform detects the OS and
# picks the matching one: setup.ps1 on Windows, setup.sh elsewhere — falling
# back to setup.sh via Git Bash when a repo ships no Windows variant.
_SETUP_BY_OS = {
    "windows": ("scripts/setup.ps1", "setup.ps1", "scripts/setup.sh", "setup.sh"),
    "posix": ("scripts/setup.sh", "setup.sh"),
}


def _find_setup_script(root: Path) -> str | None:
    candidates = _SETUP_BY_OS["windows" if _is_windows() else "posix"]
    return next((c for c in candidates if (root / c).is_file()), None)


def _hooks_wired(root: Path) -> bool:
    """Whether 'lefthook install' has run here — i.e. terminal commits are
    covered by the repo's hooks, not only platform commits by the gate."""
    try:
        return "lefthook" in (root / ".git" / "hooks" / "pre-commit").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False


def governance_state(working_directory: str) -> str:
    """'ungoverned' (no lefthook.yml), 'wired', or 'unwired' — governed but
    'lefthook install' never ran, so git would commit with zero checks."""
    root = Path(working_directory).resolve()
    if not (root / LEFTHOOK_FILE).is_file():
        return "ungoverned"
    return "wired" if _hooks_wired(root) else "unwired"


async def wire_repo_hooks(
    tool_context: ToolContext,
    working_directory: str,
) -> dict[str, Any]:
    """Install the repository's own git hooks (runs 'lefthook install').

    Args:
        working_directory: Absolute path to the Git repository.

    Returns:
        Structured result; success only when the hooks are verified wired.
    """
    import shutil

    root = Path(working_directory).resolve()
    if not (root / LEFTHOOK_FILE).is_file():
        return {
            "status": "error",
            "code": "not_governed",
            "message": "This repository has no lefthook.yml — nothing to wire.",
        }
    env = _effective_env()
    if not shutil.which("lefthook", path=env.get("PATH")):
        return {
            "status": "error",
            "code": "missing_tool",
            "message": (
                "The 'lefthook' binary is not installed on this machine. "
                "Run the repository setup first."
            ),
        }
    code, out = await _run_shell("lefthook install", str(root), 120)
    if code == 0 and _hooks_wired(root):
        return {
            "status": "success",
            "code": "hooks_wired",
            "message": (
                "Repository hooks installed — commits and pushes now run "
                "the repository's own checks."
            ),
            "stdout": out[-2000:],
        }
    return {
        "status": "error",
        "code": "wire_failed",
        "message": f"'lefthook install' exited with code {code}.",
        "stdout": out[-2000:],
    }


def repo_readiness(working_directory: str) -> dict[str, Any]:
    """Is this repo + machine ready for the lefthook-governed flow?

    Everything is derived from the repo's own lefthook.yml — which tools the
    declared checks invoke, and whether their per-repo dependencies exist —
    so a developer learns what is missing before a gate fails on it."""
    import shutil

    root = Path(working_directory)
    config = discover_lefthook(working_directory)
    if not config:
        return {"status": "success", "lefthook": False}

    hook_text = ""
    for hook in ("pre-commit", "pre-push", "commit-msg"):
        for entry in ((config.get(hook) or {}).get("commands") or {}).values():
            if isinstance(entry, dict) and entry.get("run") and _os_allows(entry):
                hook_text += " " + str(entry["run"])

    setup_script = _find_setup_script(root)
    files = [
        {
            "path": "lefthook.yml",
            "purpose": "defines the checks",
            "present": True,
            "required": True,
        },
        {
            "path": setup_script or "scripts/setup.*",
            "purpose": "installs the checks' prerequisites",
            "present": setup_script is not None,
            "required": False,
        },
    ]

    # Tools the hooks invoke — including what make entry points fan out to,
    # read from the repo's own files and only when the hooks call them.
    referenced_text = hook_text
    if "make.ps1" in hook_text and (root / "make.ps1").is_file():
        referenced_text += " " + (root / "make.ps1").read_text(
            encoding="utf-8", errors="replace"
        )
    if re.search(r"\bmake\s", hook_text) and (root / "Makefile").is_file():
        referenced_text += " " + (root / "Makefile").read_text(
            encoding="utf-8", errors="replace"
        )

    live_path = _effective_env().get("PATH")
    tools = []
    for tool in _KNOWN_TOOLS:
        if re.search(rf"\b{tool}\b", referenced_text):
            tools.append({"name": tool, "present": shutil.which(tool, path=live_path) is not None})

    # Files the governance transitively references (entry points, helper
    # scripts, config files). Setup installs TOOLS; it cannot create missing
    # FILES — those must come from the governance source repo, so the two
    # kinds of gap get different guidance in the UI.
    scan_text = referenced_text
    if setup_script:
        scan_text += " " + (root / setup_script).read_text(encoding="utf-8", errors="replace")
    referenced_paths: set[str] = set()
    if "make.ps1" in hook_text:
        referenced_paths.add("make.ps1")
    if re.search(r"\bmake\s", hook_text):
        referenced_paths.add("Makefile")
    referenced_paths.update(re.findall(r"scripts/[\w./-]+\.(?:ps1|sh|py)", scan_text))
    for known in (
        ".tflint.hcl",
        "PSScriptAnalyzerSettings.psd1",
        ".checkov.yml",
        ".commitlintrc.yml",
        ".opentofu-version",
        ".terragrunt-version",
        ".github/tool-versions",
    ):
        if known in scan_text:
            referenced_paths.add(known)
    if "_lib.ps1" in scan_text:
        referenced_paths.add("scripts/_lib.ps1")
    missing_files = sorted(p for p in referenced_paths if not (root / p).is_file())

    # commitlint runs from the repo's own node_modules — only relevant when
    # the hooks actually use it.
    needs_node = bool(re.search(r"\b(npx|commitlint)\b", hook_text))
    if needs_node:
        for pkg in ("package.json", "package-lock.json"):
            if not (root / pkg).is_file():
                missing_files.append(pkg)
    commitlint_installed = (root / "node_modules" / ".bin").is_dir() if needs_node else True

    missing_tools = [t["name"] for t in tools if not t["present"]]
    hooks_wired = _hooks_wired(root)
    return {
        "status": "success",
        "lefthook": True,
        # Wired hooks are part of ready: the setup script's 'lefthook install'
        # is what covers commits made outside the platform.
        "ready": (
            not missing_files and not missing_tools and commitlint_installed and hooks_wired
        ),
        "files": files,
        "commitlint_installed": commitlint_installed,
        "hooks_wired": hooks_wired,
        "missing_files": sorted(missing_files),
        "missing_tools": missing_tools,
        "has_setup_script": setup_script is not None,
        "setup_hint": (
            f"Run {setup_script} in the repository once, then reconnect."
            if setup_script
            else "This repository ships no setup script — install the missing tools by hand."
        ),
    }


async def run_repo_setup(working_directory: str) -> dict[str, Any]:
    """Run the repository's own setup script — explicit accept only, and only
    when something is missing. The platform detects the OS, picks the repo's
    matching script (setup.ps1 on Windows, setup.sh elsewhere), and invokes
    it; the script owns the install logic (tools, per-repo dependencies,
    'lefthook install')."""
    import platform
    import shutil

    root = Path(working_directory)
    steps: list[dict[str, Any]] = []

    # The setup script exists to install missing prerequisites — when the
    # environment already has everything the checks need, it must not run.
    current = repo_readiness(working_directory)
    if current.get("lefthook") and current.get("ready"):
        return {
            "status": "success",
            "steps": steps,
            "readiness": current,
            "message": (
                "Nothing to install — this machine already has everything "
                "the repository's checks need."
            ),
        }
    # Setup installs tools; it cannot create repository files.
    if current.get("lefthook") and current.get("missing_files"):
        return {
            "status": "error",
            "steps": steps,
            "readiness": current,
            "message": "Setup cannot create missing repository files: "
            + ", ".join(current["missing_files"])
            + ". Copy them from the governance source repository first, then run setup.",
        }

    def record(name: str, code: int, output: str) -> None:
        steps.append({"step": name, "ok": code == 0, "output": _tail(output, 20)})

    script = _find_setup_script(root)
    if not script:
        return {
            "status": "error",
            "message": "This repository ships no setup script for this OS "
            "(scripts/setup.ps1 on Windows, scripts/setup.sh elsewhere).",
        }
    os_name = platform.system() or ("Windows" if _is_windows() else "POSIX")
    record(f"detect environment: {os_name} -> {script}", 0, "")

    live_path = _effective_env().get("PATH")
    if script.endswith(".ps1"):
        pwsh = shutil.which("pwsh", path=live_path)
        if not pwsh:
            # The .ps1 needs PowerShell 7; without it, prefer the repo's .sh
            # twin over failing outright.
            sh_twin = next((c for c in ("scripts/setup.sh", "setup.sh") if (root / c).is_file()), None)
            sh_bash = _find_bash(live_path)
            if sh_twin and sh_bash:
                script = sh_twin
                record(f"PowerShell 7 not found -> using {script} via Git Bash", 0, "")
                command = f'"{sh_bash}" {script}'
            else:
                return {
                    "status": "error",
                    "steps": steps,
                    "message": "PowerShell 7 is required for this repository's setup.ps1 — "
                    "install it (winget install --id Microsoft.PowerShell), then run setup again.",
                }
        else:
            command = f'"{pwsh}" -NoProfile -File {script}'
    else:
        run_bash = _find_bash(live_path)
        if not run_bash:
            return {
                "status": "error",
                "steps": steps,
                "message": (
                    "bash was not found — setup.sh needs Git Bash, "
                    "which installs with Git."
                ),
            }
        command = f'"{run_bash}" {script}'

    code, out = await _run_shell(command, working_directory, 1800)
    record("repository setup script", code, out)

    # Setup installs tools and changes PATH: every cached probe result is
    # now suspect. Drop them so readiness reflects the new reality at once.
    global _ENV_CACHE
    _ENV_CACHE = None
    _CONDITION_CACHE.clear()
    from ._executor import _BINARY_CACHE

    _BINARY_CACHE.clear()

    readiness = repo_readiness(working_directory)
    still_missing = (readiness.get("missing_tools") or []) if readiness.get("lefthook") else []
    note = None
    if code == 0 and still_missing:
        # Installers extend PATH via the registry; a running process keeps its
        # old PATH, so freshly installed tools stay invisible until restart.
        note = (
            "Setup finished, but some tools are not visible to the running helper yet: "
            + ", ".join(still_missing)
            + ". Restart the helper from a fresh terminal to pick them up."
        )

    return {
        "status": "success" if code == 0 else "error",
        "steps": steps,
        "readiness": readiness,
        "message": note
        or (
            "Repository setup completed — governance checks are ready."
            if code == 0
            else "Setup reported errors — see the output for what is still missing."
        ),
    }


async def run_repo_format(working_directory: str) -> dict[str, Any]:
    """Run the repository's own formatter (the real fmt target, which rewrites
    files) — invoked only from an explicit 'fix formatting' accept."""
    config = discover_lefthook(working_directory)
    if not config:
        return {"status": "error", "message": "This repository has no lefthook.yml."}

    commands = (config.get("pre-commit") or {}).get("commands") or {}
    for entry in commands.values():
        if not isinstance(entry, dict) or not entry.get("run"):
            continue
        if not _os_allows(entry):
            continue
        command = str(entry["run"])
        if re.search(r"\bfmt(\s|$)", command):
            code, output = await _run_shell(command, working_directory, 300)
            if code == 0:
                return {"status": "success", "changed": True,
                        "message": "The repository's formatter ran and rewrote the unformatted files."}
            return {"status": "error", "message": _tail(output)}
    return {"status": "error", "message": "This repository's lefthook.yml defines no formatter command."}


async def run_repo_checks(
    working_directory: str,
    stage: str,
    files: list[str],
    message: str | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    """Run the repository's own lefthook gates for a stage. None when the repo
    has no lefthook.yml — the caller then falls back to the native gate."""
    config = discover_lefthook(working_directory)
    if not config:
        return None

    try:
        planned = await asyncio.to_thread(plan_checks, config, stage, files)
    except ValueError as e:
        # A crafted file name must never reach a shell — fail closed, named.
        return {
            "status": "success",
            "source": "lefthook",
            "passed": False,
            "checks": [{
                "id": "safety",
                "name": "Path safety",
                "status": "failed",
                "findings": [{
                    "file": None,
                    "line": None,
                    "rule": "unsafe-path",
                    "message": str(e),
                    "auto_fixable": False,
                }],
                "output": None,
                "note": None,
            }],
            "files_checked": len(files),
        }

    # Run checks the way the repo's own config says lefthook should: hooks
    # marked `parallel: true` run concurrently (wall time = slowest check,
    # not the sum), anything else keeps declaration order.
    checks: list[dict[str, Any]] = []
    by_hook: dict[str, list[dict[str, Any]]] = {}
    for check in planned:
        by_hook.setdefault(check["hook"], []).append(check)
    for hook, group in by_hook.items():
        if (config.get(hook) or {}).get("parallel") is True and len(group) > 1:
            checks.extend(await asyncio.gather(*(_run_check(c, working_directory) for c in group)))
        else:
            for check in group:
                checks.append(await _run_check(check, working_directory))

    # Message/title: the repo's own commit-msg hook when it defines one,
    # otherwise the native Conventional Commit check — a repo opting out of
    # commitlint doesn't opt out of readable history.
    if message is not None:
        result = await _run_commitlint(config, message, "commit message", working_directory)
        checks.append(result or _native_message_check(message, "commit message"))
    if title is not None:
        result = await _run_commitlint(config, title, "pull request title", working_directory)
        checks.append(result or _native_message_check(title, "pull request title"))

    return {
        "status": "success",
        "source": "lefthook",
        # Vacuously true on purpose: a stage with no applicable checks (e.g. a
        # doc-only change against glob'd hooks) has nothing to fail.
        "passed": all(c["status"] == "passed" for c in checks),
        "checks": checks,
        "files_checked": len(files),
    }
