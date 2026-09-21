import pytest

from deferred_migrations.safety.walker import check_installed_project
from tests.conftest import isolated_test_app_settings


def findings_for_test_app(monkeypatch: pytest.MonkeyPatch, baseline: str | None, applied: set[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    monkeypatch.setattr("deferred_migrations.safety.walker.read_baseline", lambda app_label: baseline if app_label == "deferred_migrations_testapp" else None)

    with isolated_test_app_settings("tests.testapp.unsafe_migrations"):
        return sorted((finding.migration_name, finding.rule_id) for finding in check_installed_project(applied) if finding.app_label == "deferred_migrations_testapp")


def test_without_a_baseline_every_first_party_migration_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    assert findings_for_test_app(monkeypatch, baseline=None) == [("0003_remove_child_note", "E001")]


@pytest.mark.parametrize(("baseline", "expected"), [("0002_remove_child_legacy", [("0003_remove_child_note", "E001")]), ("0003_remove_child_note", [])])
def test_migrations_at_or_before_the_baseline_are_skipped(monkeypatch: pytest.MonkeyPatch, baseline: str, expected: list[tuple[str, str]]) -> None:
    assert findings_for_test_app(monkeypatch, baseline=baseline) == expected


def test_a_baseline_naming_a_missing_migration_is_e010(monkeypatch: pytest.MonkeyPatch) -> None:
    assert findings_for_test_app(monkeypatch, baseline="0099_missing") == [("0099_missing", "E010")]


def test_unapplied_only_skips_applied_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    assert findings_for_test_app(monkeypatch, baseline=None, applied={("deferred_migrations_testapp", "0003_remove_child_note")}) == []


@pytest.mark.parametrize(("applied", "expected"), [(None, False), (set(), True)])
def test_model_renames_are_tracked_only_when_applied_migrations_are_known(monkeypatch: pytest.MonkeyPatch, applied: set[tuple[str, str]] | None, expected: bool) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("deferred_migrations.safety.walker.check_graph", lambda graph, checked, connection, baseline_findings, track_renamed_in_batch=False: calls.append(track_renamed_in_batch) or [])
    findings_for_test_app(monkeypatch, baseline=None, applied=applied)

    assert calls == [expected]
