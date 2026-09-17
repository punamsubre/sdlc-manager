"""Async subprocess executor for Git and Terraform CLI commands."""

import asyncio
import contextlib
import logging
import os
import shutil
from typing import Any

logger = logging.getLogger(__name__)

SUCCESS_STATUS = "success"
ERROR_STATUS = "error"
TIMEOUT_CODE = "timeout"
EXECUTION_ERROR_CODE = "execution_error"
CMD_NOT_FOUND_CODE = "command_not_found"
SUCCESS_CODE = "command_completed"

# Overridable per deployment via the GIT_COMMAND_TIMEOUT env knob.
DEFAULT_TIMEOUT = int(os.getenv("GIT_COMMAND_TIMEOUT", "30"))

# Binary locations don't move mid-process; a PATH scan per call is waste.
_BINARY_CACHE: dict[str, str | None] = {}


def safe_git_value(value: str, kind: str = "value") -> str:
    """Refuse git argument values that would be parsed as options.

    Exec-array calls are shell-safe, but a value beginning with '-' becomes an
    option to git itself (--upload-pack=..., --force, ...) — the classic
    argument-injection vector for ref/branch/path parameters.
    """
    v = str(value).strip()
    if not v or v.startswith("-"):
        raise ValueError(f"invalid git {kind}: {value!r}")
    return v
CLONE_TIMEOUT = 300
MAX_OUTPUT_LENGTH = 50_000
# Hard byte cap while draining a pipe: the child is read to completion so
# it never blocks, but only this much is kept in memory.
MAX_CAPTURE_BYTES = 5_000_000


async def _drain_capped(stream, cap: int = MAX_CAPTURE_BYTES) -> bytes:
    kept = bytearray()
    dropped = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if len(kept) < cap:
            kept.extend(chunk[: cap - len(kept)])
            dropped += max(0, len(chunk) - (cap - len(kept)))
        else:
            dropped += len(chunk)
    if dropped:
        kept.extend(f"\n... [{dropped} bytes dropped]".encode())
    return bytes(kept)


def _truncate_output(output: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """Truncate output to prevent excessively large responses."""
    if len(output) <= max_length:
        return output
    return output[:max_length] + f"\n... [truncated, {len(output)} total chars]"


async def run_cli_command(
    executable: str,
    args: list[str],
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    quiet_fail: bool = False,
) -> dict[str, Any]:
    """Execute a CLI tool (git, terraform) asynchronously with structured output."""
    if executable not in _BINARY_CACHE:
        _BINARY_CACHE[executable] = shutil.which(executable)
    exe_path = _BINARY_CACHE[executable]
    if exe_path is None:
        return {
            "status": ERROR_STATUS,
            "code": CMD_NOT_FOUND_CODE,
            "message": f"Executable '{executable}' not found on system PATH.",
            "command": f"{executable} {' '.join(args)}",
        }

    full_command = [exe_path, *args]
    command_str = f"{executable} {' '.join(args)}"

    # Hosted hardening (opt-in): confine subprocess cwd to the workspace
    # root when SDLC_WORKSPACE_ROOT is set. Local/helper runs leave it
    # unset — pointing at any local repo is the product there.
    root = os.getenv("SDLC_WORKSPACE_ROOT", "").strip()
    if root and cwd:
        from pathlib import Path as _P

        _root, _cwd = _P(root).resolve(), _P(cwd).resolve()
        if _cwd != _root and _root not in _cwd.parents:
            return {
                "status": ERROR_STATUS,
                "code": "workspace_confined",
                "message": "Refused: cwd is outside this deployment's workspace root.",
                "command": command_str,
            }

    logger.info("Executing: %s (cwd=%s, timeout=%ds)", command_str, cwd, timeout)

    try:
        process = await asyncio.create_subprocess_exec(
            *full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        async def _capped_communicate() -> tuple[bytes, bytes]:
            # communicate() would buffer a multi-GB diff/plan whole; capped
            # draining keeps memory bounded while still reaping the child.
            out_b, err_b = await asyncio.gather(
                _drain_capped(process.stdout), _drain_capped(process.stderr)
            )
            await process.wait()
            return out_b, err_b

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            _capped_communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("Command timed out after %ds: %s", timeout, command_str)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        # Reap the killed child — abandoning it leaks pipe transports and
        # leaves zombie handles until GC gets around to them.
        with contextlib.suppress(Exception):
            await process.wait()
        return {
            "status": ERROR_STATUS,
            "code": TIMEOUT_CODE,
            "message": f"Command timed out after {timeout} seconds: {command_str}",
            "command": command_str,
        }
    except OSError as e:
        logger.exception("Failed to execute: %s", command_str)
        return {
            "status": ERROR_STATUS,
            "code": EXECUTION_ERROR_CODE,
            "message": f"Failed to execute command: {e}",
            "command": command_str,
        }

    stdout = _truncate_output(stdout_bytes.decode("utf-8", errors="replace").strip())
    stderr = _truncate_output(stderr_bytes.decode("utf-8", errors="replace").strip())

    if process.returncode == 0:
        logger.info("Command succeeded: %s", command_str)
        return {
            "status": SUCCESS_STATUS,
            "code": SUCCESS_CODE,
            "message": f"Successfully executed: {command_str}",
            "stdout": stdout,
            "stderr": stderr,
            "command": command_str,
            "return_code": 0,
        }

    # quiet_fail: expected-to-fail probes (e.g. "does this branch exist?")
    # log at debug so routine resolution doesn't look like an error.
    log = logger.debug if quiet_fail else logger.warning
    log(
        "Command failed (rc=%d): %s   -   %s", process.returncode, command_str, stderr
    )
    return {
        "status": ERROR_STATUS,
        "code": EXECUTION_ERROR_CODE,
        "message": f"Command failed with exit code {process.returncode}",
        "stdout": stdout,
        "stderr": stderr,
        "command": command_str,
        "return_code": process.returncode,
    }


async def run_git_command(
    args: list[str],
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    quiet_fail: bool = False,
) -> dict[str, Any]:
    """Execute a git command asynchronously."""
    return await run_cli_command(
        "git", args, cwd=cwd, timeout=timeout, quiet_fail=quiet_fail
    )


async def run_terraform_command(
    args: list[str],
    cwd: str | None = None,
    timeout: int = int(os.getenv("TERRAFORM_COMMAND_TIMEOUT", "120")),
) -> dict[str, Any]:
    """Execute a terraform command asynchronously."""
    return await run_cli_command("terraform", args, cwd=cwd, timeout=timeout)
