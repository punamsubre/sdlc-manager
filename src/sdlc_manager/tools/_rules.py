"""Embedded canonical validation rules for SDLC Manager."""

SQL_RULES: str = """
Specific Refactoring Tasks:
- Target Columns: Explicitly define target column list for INSERTs.
- Filter Requirement: Ensure UPDATE/DELETE statements have a WHERE clause.
- Join Condition: Treat JOIN missing ON/USING as accidental Cartesian.
- Window Functions: Flag OVER(ORDER BY) with no PARTITION BY.
- Parameterization: Parameterize dataset names (e.g. DB_AEDW -> ${AEDW_DB}).
- Semicolons: Ensure statements are separated by semicolons.
- Datatype Alignment: Quote string literals per column datatype.
"""

PYTHON_DAG_RULES: str = """
Specific Refactoring Tasks:
- DAG Config: Ensure `catchup=False` and `max_active_runs=1` in DAGs.
- Import Cleanliness: Ensure `timedelta` and `datetime` are imported.
- Execution Safety: Ensure task operators use parameterized connection IDs.
"""

# Prompt text only: shell scripts get AI review, not a deterministic
# linter — there is deliberately no shell branch in _linter.py.
SHELL_RULES: str = """
Specific Refactoring Tasks:
- Error Handling: Ensure `set -euo pipefail` is present in scripts.
- Quoting: Ensure variable expansions are quoted (e.g., "$FILE").
"""


def get_rules_for_file(filename: str) -> str:
    """Return applicable rule set string based on file extension."""
    fn = filename.lower()
    if fn.endswith(".sql"):
        return SQL_RULES
    if fn.endswith(".py"):
        return PYTHON_DAG_RULES
    if fn.endswith((".sh", ".ksh", ".bash")):
        return SHELL_RULES
    return "Review for correctness, security, and maintainability."
