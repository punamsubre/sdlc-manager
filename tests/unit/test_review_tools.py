"""Unit tests for AI review tools and scorecards."""

import pytest

from sdlc_manager.tools.review_tools import (
    calculate_scorecard,
    get_quality_score,
    needs_ai_review,
)


def test_needs_ai_review_simple_query():
    sql = "SELECT id, name FROM users;"
    assert needs_ai_review(sql, "query.sql") is False


def test_needs_ai_review_complex_query():
    sql = """
    SELECT u.id, o.total, p.status
    FROM users u
    JOIN orders o ON u.id = o.user_id
    JOIN payments p ON o.id = p.order_id
    WHERE u.active = TRUE;
    """
    assert needs_ai_review(sql, "complex_query.sql") is True


def test_calculate_scorecard_clean():
    sql = "SELECT id FROM users WHERE id = 10;"
    res = calculate_scorecard(sql, "clean.sql")
    assert res["overall_score"] == 100
    assert res["security_score"] == 100


def test_calculate_scorecard_secret_detected():
    py_code = "key = 'AIzaSyD-1234567890123456789012345678901'\n"
    res = calculate_scorecard(py_code, "secret.py")
    assert res["security_score"] <= 50


@pytest.mark.asyncio
async def test_get_quality_score_tool(tmp_path):
    f = tmp_path / "test.sql"
    f.write_text("SELECT 1;", encoding="utf-8")
    res = await get_quality_score(str(tmp_path), "test.sql")
    assert res["status"] == "success"
    assert res["scorecard"]["overall_score"] == 100
