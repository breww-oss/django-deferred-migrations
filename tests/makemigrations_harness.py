import importlib
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.core.management import call_command
from django.db import models
from django.db.migrations.migration import Migration

TEST_APP = "deferred_migrations_testapp"

WIDGET_INITIAL = """from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Widget",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50)),
                ("flag", models.BooleanField(default=False)),
                ("note", models.TextField(null=True)),
                ("code", models.CharField(db_index=True, max_length=20)),
                ("count", models.IntegerField()),
            ],
        ),
    ]
"""


@dataclass
class GeneratedMigrations:
    package: str
    directory: Path

    def names(self) -> list[str]:
        return sorted(path.stem for path in self.directory.glob("0*.py"))

    def load(self, name: str) -> Migration:
        return importlib.import_module(f"{self.package}.{name}").Migration(name, TEST_APP)

    def max_migration(self) -> str:
        return (self.directory / "max_migration.txt").read_text().strip()


# Fresh Field instances every call: Django binds a field to the model it is declared on.
def widget_fields(**overrides: models.Field | None) -> dict[str, models.Field]:
    fields: dict[str, models.Field | None] = {
        "name": models.CharField(max_length=50),
        "flag": models.BooleanField(default=False),
        "note": models.TextField(null=True),
        "code": models.CharField(max_length=20, db_index=True),
        "count": models.IntegerField(),
        **overrides,
    }
    return {name: field for name, field in fields.items() if field is not None}


def forget_test_models() -> None:
    apps.all_models[TEST_APP].clear()
    apps.clear_cache()


def run_makemigrations(*args: str, interactive: bool = True, answer: str = "y") -> str:
    stdout = StringIO()

    with mock.patch("builtins.input", return_value=answer):
        call_command("makemigrations", TEST_APP, *args, interactive=interactive, stdout=stdout, stderr=StringIO())

    return stdout.getvalue()
