import pytest

from sdlc_manager.tools.review_tools import generate_pr_description, review_pr_diff
from sdlc_manager.tools.validation_tools import (
    _shannon_entropy,
    scan_content_for_secrets,
)


def test_shannon_entropy_calculation():
    # Low entropy (repetitive)
    low_ent = _shannon_entropy("aaaaaaaaaaaaaaaaaaaaaaaa")
    assert low_ent == 0.0

    # High entropy (random token)
    high_ent = _shannon_entropy("k9J2mP8xQ1vL5wZ0yT4bN7aR")
    assert high_ent > 4.0


def test_shannon_entropy_secret_detection():
    # Detect high-entropy random key
    code = "api_secret = 'k9J2mP8xQ1vL5wZ0yT4bN7aRc3D6'"
    findings = scan_content_for_secrets(code, "test.py")
    assert len(findings) > 0
    assert any(
        "High Entropy" in f["secret_type"] or "Secret" in f["secret_type"]
        for f in findings
    )


@pytest.mark.asyncio
async def test_review_pr_diff_no_changes(mocker):
    mocker.patch(
        "sdlc_manager.tools._executor.run_git_command",
        return_value={"status": "success", "output": ""},
    )
    res = await review_pr_diff(working_directory=".", base_branch="main")
    assert res["status"] == "success"
    assert "No changes detected" in res["message"]


@pytest.mark.asyncio
async def test_generate_pr_description_mock(mocker):
    mocker.patch(
        "sdlc_manager.tools._executor.run_git_command",
        side_effect=[
            {"status": "success", "output": "1 file changed, 2 insertions(+)"},
            {"status": "success", "output": "feat: test commit"},
            {"status": "success", "output": "+ new line"},
        ],
    )
    # Mock genai Client
    mock_client = mocker.MagicMock()
    mock_response = mocker.MagicMock()
    mock_response.text = "## What\nTest PR\n## Why\nTest"
    mock_client.models.generate_content.return_value = mock_response
    mocker.patch("google.genai.Client", return_value=mock_client)

    res = await generate_pr_description(working_directory=".", base_branch="main")
    assert res["status"] == "success"
    assert "## What" in res["pr_description"]
