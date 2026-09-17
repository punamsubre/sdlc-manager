"""Unit tests for AST and Rule linter engine."""

from sdlc_manager.tools._linter import (
    lint_python_dag_content,
    lint_sql_content,
    quick_validate_sql_syntax,
    split_sql_statements,
)


def test_split_sql_statements():
    sql = "SELECT 1; SELECT 'hello;world'; SELECT 3"
    stmts = split_sql_statements(sql)
    assert len(stmts) == 3
    assert stmts[0] == "SELECT 1"
    assert stmts[1] == "SELECT 'hello;world'"
    assert stmts[2] == "SELECT 3"


def test_quick_validate_sql_syntax_valid():
    sql = "SELECT id, name FROM users WHERE active = TRUE;"
    err = quick_validate_sql_syntax(sql)
    assert err == ""


def test_quick_validate_sql_syntax_invalid():
    sql = "SELECT id, name FORM users"
    err = quick_validate_sql_syntax(sql)
    assert err != ""


def test_lint_sql_dataset_parameterization():
    sql = "SELECT * FROM DB_AEDWD2.customer_table WHERE id = 10;"
    fixes = lint_sql_content(
        sql,
        file_path="test.sql",
        dataset_param_map={"DB_AEDWD2": "${AEDW_DB}"},
    )
    assert len(fixes) == 1
    assert fixes[0].fix_id == "fix-sql-1"
    assert fixes[0].category == "compliance"
    assert "${AEDW_DB}" in fixes[0].proposed_code


def test_lint_sql_missing_where_filter():
    sql = "DELETE FROM users;"
    fixes = lint_sql_content(sql, file_path="cleanup.sql")
    assert len(fixes) == 1
    assert fixes[0].category == "security"
    assert fixes[0].severity == "critical"
    assert "<MISSING_FILTER_REQUIRED>" in fixes[0].proposed_code


def test_lint_sql_cartesian_join():
    sql = "SELECT a.id, b.name FROM table_a a JOIN table_b b;"
    fixes = lint_sql_content(sql, file_path="join.sql")
    assert len(fixes) == 1
    assert fixes[0].category == "optimization"
    assert "<JOIN_CONDITION_REQUIRED>" in fixes[0].proposed_code


def test_lint_python_dag_catchup():
    py_code = """
from airflow import DAG
dag = DAG('my_dag', schedule_interval='@daily')
"""
    fixes = lint_python_dag_content(py_code, file_path="my_dag.py")
    assert len(fixes) == 1
    assert fixes[0].fix_id == "fix-py-1"
    assert "catchup=False" in fixes[0].proposed_code


def test_lint_python_dag_missing_timedelta():
    py_code = """
from datetime import datetime
execution_timeout = timedelta(hours=2)
"""
    fixes = lint_python_dag_content(py_code, file_path="timeout_dag.py")
    assert len(fixes) == 1
    assert "import datetime, timedelta" in fixes[0].proposed_code
