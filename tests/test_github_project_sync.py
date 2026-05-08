from __future__ import annotations

from pathlib import Path

from cascade import github as github_module


def test_read_project_config_returns_dict(tmp_path: Path) -> None:
    config_path = tmp_path / ".github" / "mandates"
    config_path.mkdir(parents=True, exist_ok=True)
    (config_path / ".project-config.json").write_text(
        '{"project_number": 1, "status_field_id": "field"}\n',
        encoding="utf-8",
    )

    payload = github_module.read_project_config(tmp_path)
    assert payload is not None
    assert payload["project_number"] == 1


def test_get_project_item_for_issue_returns_none_on_missing_data(monkeypatch) -> None:
    monkeypatch.setattr(github_module, "_gh_graphql", lambda query, token=None: None)
    item = github_module.get_project_item_for_issue(
        owner="pinestraw",
        repo="jungle",
        project_number=1,
        issue_number=12,
    )
    assert item is None


def test_update_project_status_false_on_graphql_failure(monkeypatch) -> None:
    monkeypatch.setattr(github_module, "_gh_graphql", lambda query, token=None: None)
    ok = github_module.update_project_v2_item_status(
        project_id="p",
        item_id="i",
        field_id="f",
        option_id="o",
    )
    assert ok is False


def test_update_project_text_false_on_graphql_failure(monkeypatch) -> None:
    monkeypatch.setattr(github_module, "_gh_graphql", lambda query, token=None: None)
    ok = github_module.update_project_v2_text_field(
        project_id="p",
        item_id="i",
        field_id="f",
        value="JUNG-01012026-001",
    )
    assert ok is False
