"""The actual documentation guard must reject contradictory current statuses."""

import json

from tools import check_workflow as guard


STATUS = """<!-- platform-status:start -->
| Windows | PASS | accepted revision synthetic |
| macOS | PENDING | user native acceptance pending |
<!-- platform-status:end -->
"""


def test_guard_runs_status_validation_on_document_files(tmp_path, monkeypatch, capsys):
    (tmp_path / "src/hoyo_roster").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    for name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"):
        (tmp_path / name).write_text("synthetic docs", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[status](docs/release-validation.md#当前平台状态)", encoding="utf-8"
    )
    (tmp_path / "docs/platform-support.md").write_text(
        "[status](release-validation.md#当前平台状态)", encoding="utf-8"
    )
    status = tmp_path / "docs/release-validation.md"
    status.write_text(STATUS, encoding="utf-8")
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    monkeypatch.setattr(guard, "PACKAGE", tmp_path / "src/hoyo_roster")
    assert guard.check() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
    status.write_text(STATUS + "\n当前 Windows 完整官网工作流仍为 **未通过**。", encoding="utf-8")
    assert guard.check() == 1
    assert "CONFLICTING_CURRENT_PLATFORM_CLAIM" in json.loads(capsys.readouterr().out)["problems"]


def test_status_requires_exactly_one_table_and_both_platforms():
    assert guard.validation_status_problems(STATUS) == []
    assert guard.validation_status_problems(STATUS + STATUS) == ["PLATFORM_STATUS_BLOCK_INVALID"]
    assert guard.validation_status_problems(
        STATUS.replace("| macOS | PENDING", "| Windows | PASS")
    ) == ["PLATFORM_STATUS_ROWS_INVALID"]
