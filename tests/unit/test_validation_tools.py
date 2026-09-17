"""Unit tests for secret scanning and validation tools."""

import pytest

from sdlc_manager.tools.validation_tools import (
    apply_fixes,
    check_readiness,
    scan_content_for_secrets,
    scan_secrets,
    validate_directory,
    validate_file,
    validate_sql_syntax,
)


def test_scan_content_for_secrets_detects_api_key():
    content = "gcp_key = 'AIzaSyD-1234567890123456789012345678901'\n"
    findings = scan_content_for_secrets(content, "test.py")
    assert len(findings) == 1
    assert findings[0]["secret_type"] == "GCP API Key"  # noqa: S105


def test_scan_content_for_secrets_clean_code():
    content = "SELECT customer_id, sum(amount) FROM orders GROUP BY 1;\n"
    findings = scan_content_for_secrets(content, "query.sql")
    assert len(findings) == 0


@pytest.mark.asyncio
async def test_scan_secrets_clean_dir(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("print('hello world')", encoding="utf-8")
    res = await scan_secrets(str(tmp_path))
    assert res["status"] == "success"
    assert res["code"] == "clean"


@pytest.mark.asyncio
async def test_validate_sql_syntax_tool():
    valid_res = await validate_sql_syntax("SELECT 1;")
    assert valid_res["status"] == "valid"

    invalid_res = await validate_sql_syntax("SELECT FROM WHERE 1 = ;")
    assert invalid_res["status"] == "invalid"


@pytest.mark.asyncio
async def test_validate_file_and_apply_fixes(tmp_path):
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("DELETE FROM orders;", encoding="utf-8")

    val_res = await validate_file(
        working_directory=str(tmp_path), file_path="query.sql"
    )
    assert val_res["status"] == "review_required"
    assert val_res["total_proposals"] == 1
    fix_id = val_res["proposed_fixes"][0]["fix_id"]

    apply_res = await apply_fixes(
        working_directory=str(tmp_path),
        file_path="query.sql",
        accepted_fix_ids=[fix_id],
    )
    assert apply_res["status"] == "success"
    assert apply_res["applied_count"] == 1

    updated_content = sql_file.read_text(encoding="utf-8")
    assert "<MISSING_FILTER_REQUIRED>" in updated_content


@pytest.mark.asyncio
async def test_validate_directory_tool(tmp_path):
    f1 = tmp_path / "a.sql"
    f1.write_text("SELECT 1;", encoding="utf-8")
    f2 = tmp_path / "b.sql"
    f2.write_text("DELETE FROM users;", encoding="utf-8")

    res = await validate_directory(str(tmp_path))
    assert res["status"] == "success"
    assert res["scanned_files"] == 2
    assert res["total_proposals"] == 1


@pytest.mark.asyncio
async def test_check_readiness_tool(tmp_path):
    f = tmp_path / "main.py"
    f.write_text("print('safe')", encoding="utf-8")
    res = await check_readiness(str(tmp_path))
    assert res["status"] == "success"
    assert res["ready"] is True
