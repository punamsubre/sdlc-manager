"""Layered file validation: syntax checks and BigQuery schema verification.

Schema verification is strictly read-only metadata retrieval (table + column
names and datatypes). It NEVER executes queries against data, caches only
schema metadata with a TTL, and fails OPEN: if metadata cannot be retrieved,
the check reports "could not verify" instead of failing the file.
"""

import asyncio
import difflib
import json
import logging
import os
import re
import time
from typing import Any

from ._executor import run_cli_command
from ._linter import quick_validate_sql_syntax
from ._schema import (
    get_cached_schema,
    normalize_datatype,
    parse_dataset_map,
    store_cached_schema,
)

logger = logging.getLogger(__name__)

try:
    import sqlglot
    from sqlglot import exp

    _HAS_SQLGLOT = True
except Exception:  # pragma: no cover
    _HAS_SQLGLOT = False


def _cache_ttl() -> int:
    try:
        return int(os.getenv("SCHEMA_CACHE_TTL", "900"))
    except ValueError:
        return 900


# Tracks when each table's schema was fetched (for UI freshness display).
_CACHE_FETCHED_AT: dict[str, float] = {}

# Lazily-created BigQuery client (or a cached failure reason).
_BQ_CLIENT: Any = None
_BQ_UNAVAILABLE_REASON: str | None = None
_BQ_CHECKED = False


import threading

_BQ_LOCK = threading.Lock()


def _get_bq_client() -> tuple[Any, str | None]:
    """Return (client, None) or (None, human-readable reason it's unavailable).

    Called from the event loop AND from to_thread workers — the lock stops
    two threads from constructing two clients or reading a stale None.
    """
    global _BQ_CLIENT, _BQ_UNAVAILABLE_REASON, _BQ_CHECKED
    if _BQ_CHECKED:
        return _BQ_CLIENT, _BQ_UNAVAILABLE_REASON
    with _BQ_LOCK:
        if _BQ_CHECKED:
            return _BQ_CLIENT, _BQ_UNAVAILABLE_REASON
        _BQ_CHECKED = True
        return _construct_bq_client()


def _construct_bq_client() -> tuple[Any, str | None]:
    global _BQ_CLIENT, _BQ_UNAVAILABLE_REASON
    try:
        from google.cloud import bigquery

        project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip() or None
        _BQ_CLIENT = bigquery.Client(project=project)
        # A set-but-empty project survives google.auth resolution and makes
        # every API URL malformed (projects//...): report cleanly instead.
        if not (_BQ_CLIENT.project or "").strip():
            _BQ_CLIENT = None
            _BQ_UNAVAILABLE_REASON = (
                "No GCP project is configured - set GOOGLE_CLOUD_PROJECT "
                "to enable schema verification."
            )
            return None, _BQ_UNAVAILABLE_REASON
        return _BQ_CLIENT, None
    except Exception as e:  # missing credentials, no project, import failure...
        _BQ_UNAVAILABLE_REASON = f"BigQuery connection unavailable: {str(e)[:180]}"
        logger.info("BigQuery unavailable, schema checks will report 'could not verify': %s", e)
        return None, _BQ_UNAVAILABLE_REASON


def clear_schema_cache() -> int:
    """Drop all cached schema metadata; returns number of entries removed."""
    global _BQ_CHECKED, _BQ_CLIENT, _BQ_UNAVAILABLE_REASON
    from . import _schema

    count = len(_schema._SCHEMA_CACHE)
    _schema._SCHEMA_CACHE.clear()
    _CACHE_FETCHED_AT.clear()
    _UNQUALIFIED_CACHE.clear()
    # Also allow a fresh connection attempt (credentials may have been fixed).
    _BQ_CHECKED = False
    _BQ_CLIENT = None
    _BQ_UNAVAILABLE_REASON = None
    return count


def schema_cache_status() -> dict[str, Any]:
    """Report schema-source availability and cache freshness for the UI.

    Called from async routes: client construction (credential I/O) happens
    at most once per process; after that this is a pure cache read.
    """
    client, reason = _get_bq_client()
    now = time.time()
    entries = [
        {"table": table, "age_seconds": int(now - fetched)}
        for table, fetched in sorted(_CACHE_FETCHED_AT.items())
    ]
    newest = min((e["age_seconds"] for e in entries), default=None)
    return {
        "available": client is not None,
        "reason": reason,
        "ttl_seconds": _cache_ttl(),
        "cached_tables": len(entries),
        "entries": entries[:50],
        "newest_age_seconds": newest,
    }


def _fetch_table_schema_sync(table_id: str) -> dict[str, Any]:
    """Fetch table schema metadata (names + types only). Runs in a worker thread."""
    client, reason = _get_bq_client()
    if client is None:
        return {"status": "unavailable", "reason": reason}
    try:
        from google.api_core import exceptions as gexc
    except Exception:
        gexc = None
    try:
        tbl = client.get_table(table_id)
        schema = {field.name.lower(): field.field_type for field in tbl.schema}
        return {"status": "ok", "schema": schema}
    except Exception as e:
        if gexc is not None and isinstance(e, gexc.NotFound):
            return {"status": "missing"}
        return {"status": "unavailable", "reason": str(e)[:200]}


_INFLIGHT: dict[str, asyncio.Task] = {}


async def get_table_schema(table_id: str) -> dict[str, Any]:
    """TTL-cached, lazy schema lookup for one table."""
    cached = get_cached_schema(table_id)
    if cached is not None:
        return {"status": "ok", "schema": cached, "from_cache": True}
    # Concurrent misses on the same table share one fetch instead of
    # herding N identical network calls.
    task = _INFLIGHT.get(table_id)
    if task is None:
        task = asyncio.ensure_future(
            asyncio.to_thread(_fetch_table_schema_sync, table_id)
        )
        _INFLIGHT[table_id] = task
        task.add_done_callback(lambda _t: _INFLIGHT.pop(table_id, None))
    result = await task
    if result.get("status") == "ok":
        store_cached_schema(table_id, result["schema"], ttl_seconds=_cache_ttl())
        _CACHE_FETCHED_AT[table_id] = time.time()
    return result


# Cache for unqualified-table-name resolution: name -> (result, expiry)
_UNQUALIFIED_CACHE: dict[str, tuple[dict[str, Any], float]] = {}


async def _resolve_unqualified_table(name: str) -> dict[str, Any]:
    """Find which dataset an unqualified table name belongs to.

    Scans the project's datasets (metadata only) and caches the outcome.
    Set BQ_DEFAULT_DATASET to check a preferred dataset first.
    """
    now = time.time()
    hit = _UNQUALIFIED_CACHE.get(name.lower())
    if hit and now < hit[1]:
        return hit[0]

    # Client construction does credential file I/O / metadata HTTP on
    # first use — it must not run on the event loop.
    client, reason = await asyncio.to_thread(_get_bq_client)
    if client is None:
        return {"status": "unavailable", "reason": reason}

    def _scan() -> dict[str, Any]:
        try:
            from google.api_core import exceptions as gexc

            project = client.project
            preferred = os.getenv("BQ_DEFAULT_DATASET", "").strip()
            dataset_ids: list[str] = []
            if preferred:
                dataset_ids.append(preferred)
            for ds in client.list_datasets(max_results=50):
                if ds.dataset_id not in dataset_ids:
                    dataset_ids.append(ds.dataset_id)

            # Probe datasets concurrently: 50 serial get_table round
            # trips was the worst-case cost of one unqualified name.
            from concurrent.futures import ThreadPoolExecutor

            def _probe(ds: str) -> str | None:
                try:
                    client.get_table(f"{project}.{ds}.{name}")
                    return f"{project}.{ds}.{name}"
                except gexc.NotFound:
                    return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                matches = [m for m in pool.map(_probe, dataset_ids) if m]
            matches = matches[:2]
            if len(matches) == 1:
                return {"status": "ok", "matches": matches, "project": project}
            if len(matches) > 1:
                return {"status": "ambiguous", "matches": matches, "project": project}
            return {"status": "missing", "matches": [], "project": project}
        except Exception as e:
            return {"status": "unavailable", "reason": str(e)[:200]}

    result = await asyncio.to_thread(_scan)
    if result.get("status") in ("ok", "ambiguous", "missing"):
        _UNQUALIFIED_CACHE[name.lower()] = (result, now + _cache_ttl())
    return result


def _find_line(content: str, needle: str) -> int | None:
    """Best-effort line number for the first occurrence of a name in the file."""
    if not needle:
        return None
    lowered = content.lower()
    idx = lowered.find(needle.lower())
    if idx < 0:
        return None
    return content.count("\n", 0, idx) + 1


def _line_text(content: str, line_no: int | None) -> str | None:
    if not line_no:
        return None
    lines = content.splitlines()
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1]
    return None


def _column_rename_fix(content: str, line_no: int | None, wrong: str, right: str) -> dict[str, str] | None:
    """Propose replacing a misspelled column with the closest real one, on its line."""
    line = _line_text(content, line_no)
    if not line:
        return None
    new_line, n = re.subn(rf"\b{re.escape(wrong)}\b", right, line, count=1, flags=re.IGNORECASE)
    if n == 0 or new_line == line:
        return None
    return {
        "original_code": line,
        "proposed_code": new_line,
        "explanation": f"'{wrong}' does not exist on the table — '{right}' does, and is the closest match.",
    }


def _quote_literal_fix(content: str, line_no: int | None, column: str) -> dict[str, str] | None:
    """Propose quoting an unquoted value compared against a text column."""
    line = _line_text(content, line_no)
    if not line:
        return None
    m = re.search(rf"(\b{re.escape(column)}\b\s*(?:=|!=|<>|>=|<=|>|<)\s*)(\d+(?:\.\d+)?)\b", line, re.IGNORECASE)
    if not m:
        return None
    new_line = line[: m.start(2)] + f"'{m.group(2)}'" + line[m.end(2):]
    return {
        "original_code": line,
        "proposed_code": new_line,
        "explanation": "This column holds text, so the value must be in quotes to match at run time.",
    }


def _unquote_literal_fix(content: str, line_no: int | None, column: str) -> dict[str, str] | None:
    """Propose removing quotes from a numeric value compared against a numeric column."""
    line = _line_text(content, line_no)
    if not line:
        return None
    m = re.search(
        rf"(\b{re.escape(column)}\b\s*(?:=|!=|<>|>=|<=|>|<)\s*)(['\"])(\d+(?:\.\d+)?)\2",
        line,
        re.IGNORECASE,
    )
    if not m:
        return None
    new_line = line[: m.start(2)] + m.group(3) + line[m.end(2) + len(m.group(3)) + 1 :]
    return {
        "original_code": line,
        "proposed_code": new_line,
        "explanation": "This column is numeric, so the value should not be quoted.",
    }


def _literal_category(node: Any) -> str | None:
    """Categorize a sqlglot literal for datatype comparison."""
    if isinstance(node, exp.Literal):
        return "STRING" if node.is_string else "NUMERIC"
    if isinstance(node, (exp.Boolean,)):
        return "BOOLEAN"
    return None


async def validate_sql_schema(
    content: str,
    dataset_map: str | None = None,
) -> dict[str, Any]:
    """Verify SQL table/column references against real BigQuery metadata.

    Returns a layered result:
      status: "passed" | "failed" | "unverified" | "partial" | "not_applicable"
      issues: confirmed problems (missing table/column, datatype mismatch)
      unverified: references that could not be checked (metadata unavailable)
    """
    if not _HAS_SQLGLOT:
        return {
            "status": "unverified",
            "issues": [],
            "unverified": [{"table": "*", "reason": "SQL parser is not installed."}],
            "tables_checked": [],
        }

    # Substitute known template variables so parsing works on templated SQL.
    probe = content
    for var, real in parse_dataset_map(dataset_map or os.getenv("SQL_DATASET_MAP")).items():
        probe = probe.replace("${" + var + "}", real)
    if "${" in probe:
        return {
            "status": "unverified",
            "issues": [],
            "unverified": [{"table": "*", "reason": "File contains unresolved template variables — schema references cannot be checked until they are resolved."}],
            "tables_checked": [],
        }

    try:
        statements = sqlglot.parse(probe, read="bigquery")
    except Exception:
        # Syntax layer reports parse problems; schema layer stays silent here.
        return {"status": "not_applicable", "issues": [], "unverified": [], "tables_checked": []}

    default_project = os.getenv("GOOGLE_CLOUD_PROJECT", "")

    # Prefetch: fire every fully-qualified table lookup concurrently so the
    # per-statement loop below hits warm cache instead of paying N serial
    # network round trips. Unqualified names keep the resolution path.
    prefetch_ids: set[str] = set()
    for _stmt in statements:
        if _stmt is None:
            continue
        for _tbl in _stmt.find_all(exp.Table):
            _db = _tbl.text("db")
            if _tbl.name and _db:
                _project = _tbl.text("catalog") or default_project
                prefetch_ids.add(
                    f"{_project}.{_db}.{_tbl.name}" if _project else f"{_db}.{_tbl.name}"
                )
    if len(prefetch_ids) > 1:
        await asyncio.gather(
            *(get_table_schema(t) for t in prefetch_ids), return_exceptions=True
        )

    issues: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    tables_checked: list[str] = []
    resolved_schemas: dict[str, dict[str, str]] = {}  # alias/full -> schema
    alias_to_full: dict[str, str] = {}

    for stmt in statements:
        if stmt is None:
            continue
        cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
        subquery_aliases = {
            s.alias_or_name.lower()
            for s in stmt.find_all(exp.Subquery)
            if s.alias_or_name
        }

        # --- Resolve real table references (skip CTEs / derived tables) ---
        for tbl in stmt.find_all(exp.Table):
            name = tbl.name
            db = tbl.text("db")
            catalog = tbl.text("catalog")
            if not name:
                continue
            if not db and name.lower() in cte_names:
                continue  # CTE reference, not a real table
            alias = (tbl.alias_or_name or name).lower()
            if alias in subquery_aliases:
                continue

            if db:
                project = catalog or default_project
                full = f"{project}.{db}.{name}" if project else f"{db}.{name}"
            else:
                # Unqualified reference — try to discover the dataset automatically.
                resolution = await _resolve_unqualified_table(name)
                if resolution.get("status") == "ok":
                    full = resolution["matches"][0]
                elif resolution.get("status") == "ambiguous":
                    unverified.append({
                        "table": name,
                        "reason": (
                            f"'{name}' exists in more than one dataset ({', '.join(resolution['matches'])}) — "
                            "qualify the reference to pick one."
                        ),
                        "line": _find_line(content, name),
                    })
                    continue
                elif resolution.get("status") == "missing":
                    issues.append({
                        "type": "missing_table",
                        "table": name,
                        "line": _find_line(content, name),
                        "message": (
                            f"Table '{name}' was not found in any dataset of project "
                            f"'{resolution.get('project', default_project)}'. Qualify the reference if it lives elsewhere."
                        ),
                    })
                    continue
                else:
                    unverified.append({
                        "table": name,
                        "reason": resolution.get(
                            "reason",
                            f"'{name}' is not qualified with a dataset and automatic lookup was unavailable.",
                        ),
                        "line": _find_line(content, name),
                    })
                    continue

            if full in tables_checked:
                alias_to_full[alias] = full
                continue
            tables_checked.append(full)
            alias_to_full[alias] = full

            lookup = await get_table_schema(full)
            if lookup.get("status") == "ok":
                resolved_schemas[full] = lookup["schema"]
            elif lookup.get("status") == "missing":
                # A table BigQuery doesn't have yet may simply not be deployed
                # (new table, other environment) — note it, don't block, and
                # skip column checks that have no schema to check against.
                unverified.append({
                    "table": full,
                    "reason": "not in BigQuery",
                    "line": _find_line(content, name),
                })
            else:
                unverified.append({
                    "table": full,
                    "reason": lookup.get("reason", "Schema metadata could not be retrieved."),
                    "line": _find_line(content, name),
                })

        # --- Check column references against resolved schemas ---
        known_missing: set[str] = set()
        for col in stmt.find_all(exp.Column):
            cname = (col.name or "").lower()
            if not cname or cname == "*":
                continue
            qualifier = (col.table or "").lower()

            if qualifier:
                full = alias_to_full.get(qualifier)
                if not full or full not in resolved_schemas:
                    continue  # CTE/subquery column or unverifiable table
                schema = resolved_schemas[full]
                if cname not in schema:
                    key = f"{full}.{cname}"
                    if key in known_missing:
                        continue
                    known_missing.add(key)
                    close = difflib.get_close_matches(cname, list(schema.keys()), n=1, cutoff=0.6)
                    line_no = _find_line(content, col.name)
                    issues.append({
                        "type": "missing_column",
                        "table": full,
                        "column": col.name,
                        "line": line_no,
                        "message": f"Column '{col.name}' does not exist on '{full}'.",
                        "suggestion": close[0] if close else None,
                        "fix": _column_rename_fix(content, line_no, col.name, close[0]) if close else None,
                    })
            else:
                # Unqualified: only flag when we resolved at least one schema
                # and the column exists on none of them (safe, no false alarms).
                if resolved_schemas and cte_names == set() and subquery_aliases == set():
                    if all(cname not in s for s in resolved_schemas.values()):
                        key = f"*.{cname}"
                        if key in known_missing:
                            continue
                        known_missing.add(key)
                        all_cols: list[str] = []
                        for s in resolved_schemas.values():
                            all_cols.extend(s.keys())
                        close = difflib.get_close_matches(cname, all_cols, n=1, cutoff=0.6)
                        line_no = _find_line(content, col.name)
                        issues.append({
                            "type": "missing_column",
                            "table": ", ".join(resolved_schemas.keys()),
                            "column": col.name,
                            "line": line_no,
                            "message": f"Column '{col.name}' was not found on any referenced table.",
                            "suggestion": close[0] if close else None,
                            "fix": _column_rename_fix(content, line_no, col.name, close[0]) if close else None,
                        })

        # --- Obvious datatype mismatches in comparisons ---
        for cmp_node in stmt.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
            sides = [cmp_node.left, cmp_node.right]
            col_node = next((s for s in sides if isinstance(s, exp.Column)), None)
            lit_node = next((s for s in sides if _literal_category(s)), None)
            if col_node is None or lit_node is None:
                continue
            qualifier = (col_node.table or "").lower()
            cname = (col_node.name or "").lower()
            schema = None
            if qualifier and alias_to_full.get(qualifier) in resolved_schemas:
                schema = resolved_schemas[alias_to_full[qualifier]]
            elif not qualifier and len(resolved_schemas) == 1:
                schema = next(iter(resolved_schemas.values()))
            if not schema or cname not in schema:
                continue
            col_cat = normalize_datatype(schema[cname])
            lit_cat = _literal_category(lit_node)
            if col_cat == "STRING" and lit_cat == "NUMERIC":
                line_no = _find_line(content, col_node.name)
                issues.append({
                    "type": "type_mismatch",
                    "table": alias_to_full.get(qualifier, ""),
                    "column": col_node.name,
                    "line": line_no,
                    "message": (
                        f"Column '{col_node.name}' is text but is compared to an unquoted number — "
                        "the value should probably be in quotes."
                    ),
                    "fix": _quote_literal_fix(content, line_no, col_node.name),
                })
            elif col_cat in ("NUMERIC", "BOOLEAN") and lit_cat == "STRING":
                line_no = _find_line(content, col_node.name)
                issues.append({
                    "type": "type_mismatch",
                    "table": alias_to_full.get(qualifier, ""),
                    "column": col_node.name,
                    "line": line_no,
                    "message": (
                        f"Column '{col_node.name}' is {col_cat.lower()} but is compared to a quoted text value."
                    ),
                    "fix": _unquote_literal_fix(content, line_no, col_node.name),
                })

    if issues:
        status = "partial" if unverified else "failed"
    elif unverified:
        status = "unverified"
    elif tables_checked:
        status = "passed"
    else:
        status = "not_applicable"  # no table references in this file

    return {
        "status": status,
        "issues": issues,
        "unverified": unverified,
        "tables_checked": tables_checked,
    }


async def validate_syntax(file_path: str, content: str) -> dict[str, Any]:
    """Per-type syntax validation with file/line/plain-language errors."""
    fn = file_path.lower()
    errors: list[dict[str, Any]] = []

    if fn.endswith(".sql"):
        template_vars = parse_dataset_map(os.getenv("SQL_DATASET_MAP"))
        err = quick_validate_sql_syntax(content, template_vars or None)
        if err:
            line = None
            m = re.search(r"[Ll]ine (\d+)", err)
            if m:
                line = int(m.group(1))
            errors.append({"line": line, "message": f"SQL could not be parsed: {err}"})
        return {"status": "failed" if errors else "passed", "errors": errors}

    if fn.endswith(".py"):
        import ast as _ast

        try:
            _ast.parse(content)
        except SyntaxError as e:
            errors.append({
                "line": e.lineno,
                "message": f"Python syntax error: {e.msg} (near '{(e.text or '').strip()[:60]}')",
            })
        return {"status": "failed" if errors else "passed", "errors": errors}

    if fn.endswith((".sh", ".ksh", ".bash")):
        import tempfile

        tmp = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8")
        try:
            tmp.write(content)
            tmp.close()
            res = await run_cli_command("bash", ["-n", tmp.name])
            if res.get("code") == "command_not_found":
                return {
                    "status": "unverified",
                    "errors": [],
                    "note": "No shell parser is available on this machine — shell syntax could not be verified.",
                }
            if res.get("return_code", 0) != 0:
                stderr = (res.get("stderr") or "").strip()
                m = re.search(r"line (\d+):(.*)", stderr)
                errors.append({
                    "line": int(m.group(1)) if m else None,
                    "message": f"Shell syntax error: {(m.group(2).strip() if m else stderr)[:200]}",
                })
            return {"status": "failed" if errors else "passed", "errors": errors}
        finally:
            os.unlink(tmp.name)

    if fn.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            errors.append({"line": e.lineno, "message": f"Invalid JSON: {e.msg}"})
        return {"status": "failed" if errors else "passed", "errors": errors}

    if fn.endswith((".yaml", ".yml")):
        try:
            import yaml

            yaml.safe_load(content)
            return {"status": "passed", "errors": []}
        except ImportError:
            return {"status": "unverified", "errors": [], "note": "No YAML parser installed — syntax could not be verified."}
        except Exception as e:
            line = getattr(getattr(e, "problem_mark", None), "line", None)
            errors.append({
                "line": (line + 1) if line is not None else None,
                "message": f"Invalid YAML: {str(e)[:200]}",
            })
            return {"status": "failed", "errors": errors}

    return {"status": "not_applicable", "errors": []}
