from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import override_settings


# MIGRATION_MODULES={} clears any per-test override so the loader sees every app's real migrations, which is what the baseline command reads.
@pytest.mark.django_db
@override_settings(MIGRATION_MODULES={})
def test_baseline_names_the_apps_leaf_and_does_not_overwrite_an_existing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "deferred_migrations_baseline.txt"
    monkeypatch.setattr("deferred_migrations.management.commands.deferred_migrations_baseline.baseline_path", lambda app_label: path)

    call_command("deferred_migrations_baseline", "--app", "deferred_migrations", stdout=StringIO())
    assert path.read_text() == "0002_modelrename\n"

    path.write_text("0002_already_chosen\n")
    call_command("deferred_migrations_baseline", "--app", "deferred_migrations", stdout=StringIO())
    assert path.read_text() == "0002_already_chosen\n"
