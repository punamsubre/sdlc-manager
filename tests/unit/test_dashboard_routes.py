"""Unit tests for dashboard endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from sdlc_manager.server import app


@pytest.mark.asyncio
async def test_health_endpoint():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        res = await ac.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_dashboard_summary_endpoint():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        res = await ac.get("/api/v1/dashboard/summary?working_directory=.")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert "quality_score" in data


@pytest.mark.asyncio
async def test_git_branches_endpoint():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        res = await ac.get("/api/v1/git/branches?working_directory=.")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert "branches" in data
        assert "current_branch" in data


@pytest.mark.asyncio
async def test_git_compare_endpoint():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        res = await ac.post(
            "/api/v1/git/compare",
            json={"working_directory": ".", "base_branch": "main", "compare_branch": "main"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["is_identical"] is True


@pytest.mark.asyncio
async def test_git_generate_pr_description_endpoint():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        res = await ac.post(
            "/api/v1/git/generate-pr-description",
            json={
                "working_directory": ".",
                "base_branch": "main",
                "compare_branch": "feat/test",
                "commits": [{"hash": "abc1234", "author": "dev", "message": "feat: test"}],
                "files": [{"file": "test.py", "additions": 10, "deletions": 2}],
                "diff_summary": "+ test diff line",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert "title" in data
        assert "description" in data


@pytest.mark.asyncio
async def test_git_create_pr_endpoint(mocker):
    mocker.patch(
        "sdlc_manager.tools.github_tools.github_create_pr",
        return_value={
            "status": "success",
            "code": "pr_created",
            "message": "PR #1 created",
            "pr_number": 1,
            "pr_url": "https://github.com/owner/repo/pull/1",
            "pr_title": "feat: test pr",
            "pr_state": "open",
        },
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        res = await ac.post(
            "/api/v1/git/create-pr",
            json={
                "working_directory": ".",
                "base_branch": "main",
                "compare_branch": "feat/test",
                "title": "feat: test pr",
                "description": "Test description",
                "draft": False,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert "pr_title" in data
