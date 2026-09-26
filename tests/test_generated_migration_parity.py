import importlib
import sys
import zlib
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock
from uuid import UUID
from uuid import uuid4

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.commands.makemigrations import Command as DjangoMakemigrations
from django.db import connections
from django.db import models
from django.db import transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import F
from django.test import override_settings

from deferred_migrations.models import DeferredOperation
from tests.conftest import isolated_test_app_settings
from tests.makemigrations_harness import TEST_APP
from tests.makemigrations_harness import forget_test_models
from tests.migration_helpers import define_test_model
from tests.schema_snapshot import drop_relations
from tests.schema_snapshot import schema_snapshot

# Each arm has a database of its own, so each keeps its own django_migrations history for the same app label and the same table names.
NATIVE = "native"
DEFERRED = "deferred"
SEED_ROWS = 6

ModelDefinition = tuple[str, dict[str, models.Field | None], dict[str, object]]
Version = Callable[[], list[ModelDefinition]]


# Fresh Field instances on every call: Django binds a field to the model it is declared on, and each version is defined more than once.
def owner() -> ModelDefinition:
    return "Owner", {"name": models.CharField(max_length=50)}, {}


def gadget(name: str = "Gadget") -> ModelDefinition:
    return name, {"label": models.CharField(max_length=30), "owner": models.ForeignKey(f"{TEST_APP}.Owner", models.CASCADE)}, {}


def widget(options: dict[str, object] | None = None, **overrides: models.Field | None) -> ModelDefinition:
    fields: dict[str, models.Field | None] = {
        "name": models.CharField(max_length=50),
        "flag": models.BooleanField(default=False),
        "note": models.TextField(null=True),
        "code": models.CharField(max_length=20, db_index=True),
        "count": models.IntegerField(),
        "legacy_ref": models.CharField(max_length=20, null=True, unique=True),
        # Nullable and unconstrained, for the hand-written recipes to tighten and reshape.
        "score": models.IntegerField(null=True),
        "owner": models.ForeignKey(f"{TEST_APP}.Owner", models.CASCADE),
        "tags": models.ManyToManyField(f"{TEST_APP}.Owner", related_name="tagged_widgets"),
        **overrides,
    }
    return "Widget", fields, options or {}


# One column per rule DeferredRemoveField chooses between when it relaxes a column (a constant default, a callable default, a db_default, a generated column, a unique column), in types other than Widget's.
def ledger(**overrides: models.Field | None) -> ModelDefinition:
    fields: dict[str, models.Field | None] = {
        "amount": models.DecimalField(max_digits=12, decimal_places=2, default=Decimal(0)),
        "fee": models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1.50")),
        "day": models.DateField(null=True),
        "stamp": models.DateTimeField(null=True),
        "token": models.UUIDField(null=True, unique=True),
        "payload": models.JSONField(default=dict),
        "status": models.CharField(max_length=10, db_default="new"),
        "total": models.GeneratedField(expression=F("amount") * 2, output_field=models.DecimalField(max_digits=14, decimal_places=2), db_persist=True),
        **overrides,
    }
    return "Ledger", fields, {}


def base() -> list[ModelDefinition]:
    return [owner(), gadget(), widget(), ledger()]


def define_version(version: Version) -> list[type[models.Model]]:
    forget_test_models()
    return [define_test_model(name, {field_name: field for field_name, field in fields.items() if field is not None}, options) for name, fields, options in version()]


# Every seeded foreign key points at row 1 of its target, which is always written first because models are listed with their targets ahead of them.
def sample_value(field: models.Field, row: int, tag: str) -> object:
    if field.null and (row % 3 == 0 or field.related_model is field.model):
        return None

    if isinstance(field, models.ForeignKey):
        return 1

    match field.get_internal_type():
        case "CharField" | "TextField":
            return f"{tag}-{field.name}-{row}-ü"[: field.max_length]
        case "BooleanField":
            return row % 2 == 0
        case "IntegerField" | "BigIntegerField":
            return row * 7 + 1
        case "DecimalField":
            return Decimal(row) * Decimal("2.25")
        case "DateField":
            return date(2026, 1, 1) + timedelta(days=row)
        case "DateTimeField":
            return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=row)
        case "UUIDField":
            return UUID(int=zlib.crc32(tag.encode()) * 1000 + row)
        case "JSONField":
            return {"tag": tag, "row": row, "nested": [row, None]}

    raise NotImplementedError(f"No sample value for {field!r}; add one for {field.get_internal_type()}.")


# explicit=True sets every column, as an INSERT from old code does; explicit=False leaves out whatever has a default or allows NULL, so the database's own defaults decide those columns.
def write_rows(model_classes: list[type[models.Model]], using: str, tag: str, count: int, explicit: bool = True) -> None:
    for model in model_classes:
        for row in range(count):
            fields = [field for field in model._meta.concrete_fields if not field.primary_key and not field.generated and (explicit or not (field.null or field.has_default() or field.has_db_default()))]
            instance = model.objects.using(using).create(**{field.attname: sample_value(field, row, tag) for field in fields})

            for relation in model._meta.local_many_to_many:
                getattr(instance, relation.name).set([1, row % count + 1])


# Old code still serving during the rollout does everything an ORM does to a table, not only insert: it reads every column, inserts, updates, upserts, takes a row lock and deletes.
# For a renamed model all of it goes through the compatibility view under the old name; for a column being synced or filled, all of it goes through the triggers.
def old_code_activity(model_classes: list[type[models.Model]], using: str) -> None:
    write_rows(model_classes, using, "rollout", 1)

    for model in model_classes:
        writable = [field for field in model._meta.concrete_fields if not field.primary_key and not field.generated]
        manager = model.objects.using(using)
        assert len(list(manager.all())) > 0
        manager.filter(pk=2).update(**{field.attname: sample_value(field, 2, "updated") for field in writable})
        manager.bulk_create([model(pk=1, **{field.attname: sample_value(field, 1, "upserted") for field in writable})], update_conflicts=True, unique_fields=[model._meta.pk.name], update_fields=[field.name for field in writable])

        with transaction.atomic(using=using):
            assert len(list(manager.select_for_update().filter(pk=1))) == 1

    # Children before the models they point at, as an application deletes them.
    for model in reversed(model_classes):
        model.objects.using(using).filter(pk=3).delete()


def app_relations(using: str) -> dict[str, str]:
    with connections[using].cursor() as cursor:
        cursor.execute(
            "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'v', 'm', 'p', 'f') AND c.relname LIKE %s ORDER BY 1",
            [TEST_APP.replace("_", "\\_") + "\\_%"],
        )
        return dict(cursor.fetchall())


# Columns are selected by name, sorted, so physical column order never shows up as a difference; rows are sorted on every column, so neither does insertion order.
def table_rows(table: str, using: str) -> list[dict[str, object]]:
    connection = connections[using]

    with connection.cursor() as cursor:
        names = sorted(column.name for column in connection.introspection.get_table_description(cursor, table))
        cursor.execute(f"SELECT {', '.join(connection.ops.quote_name(name) for name in names)} FROM {connection.ops.quote_name(table)} ORDER BY {', '.join(str(position) for position in range(1, len(names) + 1))}")
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


# Model renames and deletions both touch django_content_type, the package's through its rename handling and Django's through its injected RenameContentType.
def content_types(using: str) -> list[tuple[str, str]]:
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT app_label, model FROM django_content_type WHERE app_label = %s ORDER BY 1, 2", [TEST_APP])
        return cursor.fetchall()


@dataclass
class DatabaseState:
    relations: dict[str, str]
    schema: dict[str, object]
    rows: dict[str, list[dict[str, object]]]
    content_types: list[tuple[str, str]]


def database_state(using: str) -> DatabaseState:
    relations = app_relations(using)
    tables = [name for name, kind in relations.items() if kind == "r"]
    return DatabaseState(relations, schema_snapshot(tables, using), {table: table_rows(table, using) for table in tables}, content_types(using))


# Commands run quietly, but a failure carries their output, so a refused pre-deploy shows the findings that refused it.
class CommandFailed(AssertionError):
    pass


def run_command(command: object, *args: str, **options: object) -> None:
    stdout, stderr = StringIO(), StringIO()

    try:
        call_command(command, *args, stdout=stdout, stderr=stderr, **options)
    except Exception as error:
        raise CommandFailed(f"{command} {' '.join(args)} failed: {error}\n{stdout.getvalue()}{stderr.getvalue()}") from error


# Answers yes to every rename question. Django's other questions take a number or a default, and would loop forever on "y", so an unexpected one fails the test instead of hanging it.
@contextmanager
def answer_yes_to_renames() -> Iterator[None]:
    answers = iter(["y"] * 10)

    with mock.patch("builtins.input", side_effect=lambda *args: next(answers)):
        yield


HAND_WRITTEN_MIGRATION = """from django.db import migrations
from django.db import models

from deferred_migrations import operations as deferred


class Migration(migrations.Migration):
    atomic = {atomic}

    dependencies = {dependencies!r}

    operations = [{operations}]
"""


# A step makemigrations cannot write: a recipe from the README, hand-written for each arm as (atomic, operations source) per migration.
# The NOT NULL recipes rewrite existing rows (NULL becomes the fill value) and neither arm puts them back on reversal, so a rollback before post-deploy is compared against the unmigrated native database on schema alone.
@dataclass
class ByHand:
    version: Version
    native: list[tuple[bool, str]]
    deferred: list[tuple[bool, str]]
    rewrites_existing_rows: bool = False


@dataclass
class Arm:
    database: str
    package: str
    directory: Path
    # "django" writes with Django's own makemigrations, "package" with this package's, and "fixed" with Django's and then rewrites the result with fix_deploy_safety.
    writer: str

    def settings(self) -> override_settings:
        return override_settings(MIGRATION_MODULES={**settings.MIGRATION_MODULES, TEST_APP: self.package})

    def operation_types(self) -> set[type]:
        return {type(operation) for path in self.directory.glob("0*.py") for operation in importlib.import_module(f"{self.package}.{path.stem}").Migration.operations}

    def makemigrations(self) -> None:
        # Django's own command rather than this package's with DEFERRED_MIGRATIONS_FIX_ON_MAKEMIGRATIONS off, so the comparison is against what Django writes with nothing of this package in the way.
        command = "makemigrations" if self.writer == "package" else DjangoMakemigrations()

        with self.settings(), answer_yes_to_renames():
            run_command(command, TEST_APP, interactive=True)

            # Pointed at this arm's own database, where earlier steps' migrations are applied and so must be left untouched.
            if self.writer == "fixed":
                run_command("fix_deploy_safety", TEST_APP, database=self.database)
                self.forget_imported_migrations()

    # In a real project fix_deploy_safety and migrate are separate processes. Here makemigrations has already imported the files the fixer just rewrote, and Django's loader would keep using those stale modules.
    def forget_imported_migrations(self) -> None:
        for name in [name for name in sys.modules if name.startswith(f"{self.package}.")]:
            del sys.modules[name]

        importlib.invalidate_caches()

    def write_by_hand(self, migrations: list[tuple[bool, str]]) -> None:
        previous = self.leaf()

        for atomic, operations in migrations:
            name = f"{len(list(self.directory.glob('0*.py'))) + 1:04d}_by_hand"
            dependencies = [(TEST_APP, previous), *([("deferred_migrations", "0001_initial")] if self.database == DEFERRED else [])]
            (self.directory / f"{name}.py").write_text(HAND_WRITTEN_MIGRATION.format(atomic=atomic, dependencies=dependencies, operations=operations))
            previous = name

        importlib.invalidate_caches()

    def leaf(self) -> str:
        with self.settings():
            [(_, name)] = MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes(TEST_APP)

        return name

    def migrate_back(self, target: str) -> None:
        with self.settings():
            run_command("migrate", TEST_APP, target, database=self.database, interactive=False, verbosity=0)


@pytest.fixture
def arms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[tuple[Arm, Arm]]:
    root = f"dm_parity_{uuid4().hex[:8]}"
    (tmp_path / root).mkdir()
    (tmp_path / root / "__init__.py").write_text("")
    native = Arm(NATIVE, f"{root}.native", tmp_path / root / "native", "django")
    deferred = Arm(DEFERRED, f"{root}.deferred", tmp_path / root / "deferred", request.param)

    for arm in (native, deferred):
        arm.directory.mkdir()
        (arm.directory / "__init__.py").write_text("")

    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        with isolated_test_app_settings(native.package):
            try:
                yield native, deferred
            finally:
                # The flush after a transactional test empties tables but does not drop the ones these migrations created, or forget that they were applied.
                for arm in (native, deferred):
                    drop_relations(list(app_relations(arm.database)), arm.database)
                    MigrationRecorder(connections[arm.database]).migration_qs.filter(app=TEST_APP).delete()
    finally:
        for name in [name for name in sys.modules if name == root or name.startswith(f"{root}.")]:
            del sys.modules[name]


def migrate_natively(arm: Arm, old_models: list[type[models.Model]] | None) -> None:
    # Old code keeps writing until the migration runs, and nothing old runs after it.
    if old_models is not None:
        old_code_activity(old_models, arm.database)

    with arm.settings():
        run_command("migrate", database=arm.database, interactive=False, verbosity=0)


def migrate_deferred(arm: Arm, old_models: list[type[models.Model]] | None) -> None:
    with arm.settings():
        run_command("migrate_pre_deploy", database=arm.database)

        # The rollout: old code writes after pre-deploy and before post-deploy, which is the window the package exists to keep working.
        if old_models is not None:
            old_code_activity(old_models, arm.database)

        run_command("migrate_post_deploy", database=arm.database, fail_on_error=True)

    assert_nothing_queued(arm, "after post-deploy")


def assert_databases_match(native: Arm, deferred: Arm, when: str, compare_rows: bool = True) -> None:
    expected, actual = database_state(native.database), database_state(deferred.database)

    assert actual.relations == expected.relations, when
    assert actual.schema == expected.schema, when

    if compare_rows:
        assert actual.rows == expected.rows, when

    assert actual.content_types == expected.content_types, when


def assert_nothing_queued(arm: Arm, when: str) -> None:
    unfinished = list(DeferredOperation.objects.using(arm.database).exclude(status=DeferredOperation.Status.DONE))
    assert not unfinished, f"{when}: queued operations left behind: {unfinished}"


def remove(**fields: None) -> Version:
    return lambda: [owner(), gadget(), widget(**fields), ledger()]


def change_widget(options: dict[str, object] | None = None, **fields: models.Field | None) -> Version:
    return lambda: [owner(), gadget(), widget(options, **fields), ledger()]


def change_ledger(**fields: models.Field | None) -> Version:
    return lambda: [owner(), gadget(), widget(), ledger(**fields)]


WIDGET_TABLE = f"{TEST_APP}_widget"

# The native arms are the hand-written equivalents a careful developer would write without this package: fill then tighten, and add then copy then drop, each change in its own migration.
TIGHTEN_SCORE = ByHand(
    change_widget(score=models.IntegerField()),
    native=[
        (True, f'migrations.RunSQL("UPDATE {WIDGET_TABLE} SET score = 0 WHERE score IS NULL", migrations.RunSQL.noop)'),
        (True, 'migrations.AlterField("widget", "score", models.IntegerField())'),
    ],
    deferred=[
        (True, 'deferred.InstallNotNullFill("widget", "score", fill_sql="0")'),
        (False, 'deferred.BackfillNotNull("widget", "score", fill_sql="0"), deferred.SetNotNull("widget", "score", models.IntegerField())'),
    ],
    rewrites_existing_rows=True,
)

RESHAPE_SCORE = ByHand(
    change_widget(score=None, score_cents=models.BigIntegerField(null=True)),
    native=[
        (True, 'migrations.AddField("widget", "score_cents", models.BigIntegerField(null=True))'),
        (True, f'migrations.RunSQL("UPDATE {WIDGET_TABLE} SET score_cents = (score * 100)::bigint", migrations.RunSQL.noop)'),
        (True, 'migrations.RemoveField("widget", "score")'),
    ],
    deferred=[
        (True, 'migrations.AddField("widget", "score_cents", models.BigIntegerField(null=True)), deferred.InstallColumnSync("widget", from_field="score", to_field="score_cents", forwards_sql="({from} * 100)::bigint", backwards_sql="({to} / 100)::integer")'),
        (False, 'deferred.BackfillColumnSync("widget", from_field="score", to_field="score_cents", forwards_sql="({from} * 100)::bigint"), deferred.DeferredRemoveField("widget", "score")'),
    ],
)


# The README's NOT NULL target: added nullable, filled by an identity sync from a NOT NULL source, then tightened with SetNotNull straight after the backfill, with no fill trigger.
def reshape_count_not_null(**widget_fields: models.Field | None) -> ByHand:
    return ByHand(
        change_widget(count=None, count_big=models.BigIntegerField(), **widget_fields),
        native=[
            (True, 'migrations.AddField("widget", "count_big", models.BigIntegerField(null=True))'),
            (True, f'migrations.RunSQL("UPDATE {WIDGET_TABLE} SET count_big = count", migrations.RunSQL.noop)'),
            (True, 'migrations.AlterField("widget", "count_big", models.BigIntegerField())'),
            (True, 'migrations.RemoveField("widget", "count")'),
        ],
        deferred=[
            (True, 'migrations.AddField("widget", "count_big", models.BigIntegerField(null=True)), deferred.InstallColumnSync("widget", from_field="count", to_field="count_big", forwards_sql="{from}", backwards_sql="{to}")'),
            (False, 'deferred.BackfillColumnSync("widget", from_field="count", to_field="count_big", forwards_sql="{from}"), deferred.SetNotNull("widget", "count_big", models.BigIntegerField()), deferred.DeferredRemoveField("widget", "count")'),
        ],
    )


RESHAPE_COUNT_NOT_NULL = reshape_count_not_null()
RESHAPE_COUNT_NOT_NULL_AFTER_TIGHTEN = reshape_count_not_null(score=models.IntegerField())


# Cases fix_deploy_safety can make deploy-safe on its own: it rewrites removals and deletions, and leaves renames and builds to makemigrations or a person.
REWRITABLE = [
    pytest.param([remove(note=None)], {"DeferredRemoveField"}, id="remove a nullable column"),
    pytest.param([remove(flag=None)], {"DeferredRemoveField"}, id="remove a column with a default"),
    pytest.param([remove(count=None)], {"DeferredRemoveField"}, id="remove a NOT NULL column without a default"),
    pytest.param([remove(code=None)], {"DeferredRemoveField"}, id="remove an indexed column"),
    pytest.param([remove(legacy_ref=None)], {"DeferredRemoveField"}, id="remove a unique column"),
    pytest.param([remove(owner=None)], {"DeferredRemoveField"}, id="remove a foreign key"),
    pytest.param([remove(tags=None)], {"DeferredRemoveField"}, id="remove a many-to-many field"),
    pytest.param([change_ledger(fee=None)], {"DeferredRemoveField"}, id="remove a decimal column with a constant default"),
    pytest.param([change_ledger(day=None)], {"DeferredRemoveField"}, id="remove a nullable date column"),
    pytest.param([change_ledger(payload=None)], {"DeferredRemoveField"}, id="remove a column with a callable default"),
    pytest.param([change_ledger(status=None)], {"DeferredRemoveField"}, id="remove a column with a db_default"),
    pytest.param([change_ledger(total=None)], {"DeferredRemoveField"}, id="remove a generated column"),
    pytest.param([change_ledger(token=None)], {"DeferredRemoveField"}, id="remove a unique uuid column"),
    pytest.param([lambda: [owner(), widget(), ledger()]], {"DeferredDeleteModel"}, id="delete a model"),
    pytest.param([lambda: [owner(), gadget(), widget()]], {"DeferredDeleteModel"}, id="delete a model with a generated column"),
    pytest.param([remove(note=None), base], {"DeferredRemoveField"}, id="remove a column then add it back in a later deploy"),
]


# Each case is a list of model versions after the base one, each migrated as its own deploy, and the package operations that must have been written for it: without them the deferred arm could match the native one only because nothing was made deploy-safe.
@pytest.mark.django_db(transaction=True, databases=["default", NATIVE, DEFERRED])
@pytest.mark.parametrize("arms", ["package"], indirect=True)
@pytest.mark.parametrize(
    ("versions", "expected_operations"),
    [
        *REWRITABLE,
        pytest.param([lambda: [owner(), gadget("Gizmo"), widget(), ledger()]], {"DeferredRenameModel"}, id="rename a model"),
        pytest.param([change_widget(note=None, memo=models.TextField(null=True))], {"InstallColumnSync", "BackfillColumnSync", "DeferredRemoveField"}, id="rename a nullable column"),
        pytest.param([change_widget(flag=None, enabled=models.BooleanField(default=False))], {"InstallColumnSync", "BackfillColumnSync", "DeferredRemoveField"}, id="rename a NOT NULL column with a default"),
        pytest.param([change_widget(count=None, quantity=models.IntegerField())], {"InstallColumnSync", "BackfillColumnSync", "SetNotNull", "DeferredRemoveField"}, id="rename a NOT NULL column without a default"),
        pytest.param([change_ledger(fee=None, charge=models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1.50")))], {"InstallColumnSync", "DeferredRemoveField"}, id="rename a decimal column with a constant default"),
        pytest.param([change_ledger(day=None, booked_on=models.DateField(null=True))], {"InstallColumnSync", "DeferredRemoveField"}, id="rename a nullable date column"),
        pytest.param([change_ledger(stamp=None, logged_at=models.DateTimeField(null=True))], {"InstallColumnSync", "DeferredRemoveField"}, id="rename a nullable datetime column"),
        pytest.param([change_widget(extra=models.TextField(null=True))], set(), id="add a nullable column"),
        pytest.param([change_widget(batch=models.IntegerField(null=True, db_index=True))], {"AddFieldConcurrently"}, id="add an indexed column"),
        pytest.param([change_widget(channel=models.CharField(max_length=10, db_default="web"))], set(), id="add a NOT NULL column with a db_default"),
        pytest.param([change_widget(parent=models.ForeignKey(f"{TEST_APP}.Widget", models.SET_NULL, null=True, related_name="children"))], {"AddFieldConcurrently"}, id="add a foreign key"),
        pytest.param([change_widget(sku=models.CharField(max_length=10, null=True, unique=True))], {"AddFieldConcurrently"}, id="add a unique column"),
        pytest.param([change_widget(profile=models.OneToOneField(f"{TEST_APP}.Owner", models.SET_NULL, null=True, related_name="profile_widget"))], {"AddFieldConcurrently"}, id="add a one-to-one field"),
        pytest.param([change_widget({"indexes": [models.Index(fields=["name"], name="widget_name_idx")]})], {"AddIndexConcurrently"}, id="add an index"),
        pytest.param(
            [change_widget({"constraints": [models.CheckConstraint(condition=models.Q(count__gte=0), name="widget_count_positive"), models.UniqueConstraint(fields=["name", "code"], name="widget_name_code_uniq")]})],
            {"AddConstraintConcurrently"},
            id="add check and unique constraints",
        ),
        pytest.param(
            [change_widget({"constraints": [models.UniqueConstraint(fields=["name", "vessel"], name="widget_name_vessel")]}, note=None, vessel=models.IntegerField(null=True))],
            {"DeferredRemoveField", "AddConstraintConcurrently"},
            id="remove a column and add a constrained one in one run",
        ),
        pytest.param([change_widget(note=None, memo=models.TextField(null=True), flag=None)], {"InstallColumnSync", "DeferredRemoveField"}, id="rename one column and remove another in one run"),
        pytest.param([lambda: [owner(), gadget("Gizmo"), widget(), ledger()], lambda: [owner(), gadget("Gizmo"), widget(note=None), ledger()]], {"DeferredRenameModel", "DeferredRemoveField"}, id="rename a model then remove a column in a later deploy"),
        pytest.param([TIGHTEN_SCORE], {"InstallNotNullFill", "BackfillNotNull", "SetNotNull"}, id="make a column NOT NULL by hand"),
        pytest.param([RESHAPE_SCORE], {"InstallColumnSync", "BackfillColumnSync", "DeferredRemoveField"}, id="reshape a column by hand"),
        pytest.param([RESHAPE_COUNT_NOT_NULL], {"InstallColumnSync", "BackfillColumnSync", "SetNotNull", "DeferredRemoveField"}, id="reshape a NOT NULL column into a NOT NULL target by hand"),
        pytest.param([TIGHTEN_SCORE, RESHAPE_COUNT_NOT_NULL_AFTER_TIGHTEN], {"InstallNotNullFill", "SetNotNull", "InstallColumnSync"}, id="make a column NOT NULL then reshape another in a later deploy"),
    ],
)
def test_generated_migrations_build_the_same_database_as_djangos(arms: tuple[Arm, Arm], versions: list[Version], expected_operations: set[str]) -> None:
    assert_the_arms_build_the_same_database(arms, versions, expected_operations)


@pytest.mark.django_db(transaction=True, databases=["default", NATIVE, DEFERRED])
@pytest.mark.parametrize("arms", ["fixed"], indirect=True)
@pytest.mark.parametrize(("versions", "expected_operations"), REWRITABLE)
def test_migrations_fixed_by_fix_deploy_safety_build_the_same_database_as_djangos(arms: tuple[Arm, Arm], versions: list[Version], expected_operations: set[str]) -> None:
    assert_the_arms_build_the_same_database(arms, versions, expected_operations)


def rollback_error(arm: Arm) -> BaseException | None:
    try:
        arm.migrate_back("0001_initial")
    except CommandFailed as failure:
        return failure.__cause__

    return None


def assert_the_arms_build_the_same_database(arms: tuple[Arm, Arm], versions: list[Version], expected_operations: set[str]) -> None:
    native, deferred = arms
    previous_models: list[type[models.Model]] | None = None

    for step, version in enumerate([base, *versions]):
        by_hand = version if isinstance(version, ByHand) else None
        current_models = define_version(by_hand.version if by_hand else version)
        deferred_leaf = deferred.leaf() if previous_models is not None else None

        if by_hand:
            native.write_by_hand(by_hand.native)
            deferred.write_by_hand(by_hand.deferred)
        else:
            for arm in arms:
                arm.makemigrations()

        # A deploy abandoned after pre-deploy and rolled back must leave exactly the schema and rows it started from, which the native database, not yet migrated, still holds.
        if deferred_leaf is not None:
            with deferred.settings():
                run_command("migrate_pre_deploy", database=deferred.database)

            deferred.migrate_back(deferred_leaf)
            assert_nothing_queued(deferred, f"after rolling back step {step} before post-deploy")
            assert_databases_match(native, deferred, f"after rolling back step {step} before post-deploy", compare_rows=not (by_hand and by_hand.rewrites_existing_rows))

        migrate_natively(native, previous_models)
        migrate_deferred(deferred, previous_models)

        if previous_models is None:
            for arm in arms:
                write_rows(current_models, arm.database, "seed", SEED_ROWS)

        assert_databases_match(native, deferred, f"after step {step}")
        previous_models = current_models

    assert previous_models is not None

    # New code writes after the deploy, leaving out whatever the database should fill in, so a default, NOT NULL or sequence that differs shows up as a row that differs.
    for arm in arms:
        write_rows(previous_models, arm.database, "new", 1, explicit=False)

    assert_databases_match(native, deferred, "after new code wrote")

    # Reversing every deploy after its drops have run: the data those drops removed is gone in both, and whatever each reversal restores must match.
    # Django cannot reverse some of them on a populated table (re-adding a NOT NULL column with no default fails), and then the package must fail the same way rather than build something Django would not.
    native_error, deferred_error = (rollback_error(arm) for arm in arms)
    assert type(deferred_error) is type(native_error), f"rolling everything back: Django raised {native_error!r}, the package raised {deferred_error!r}"

    if native_error is None:
        assert_nothing_queued(deferred, "after rolling everything back")
        assert_databases_match(native, deferred, "after rolling everything back")

    written_by_package = {operation.__name__ for operation in deferred.operation_types() if operation.__module__.startswith("deferred_migrations.")}
    assert expected_operations <= written_by_package
    assert not [operation for operation in native.operation_types() if operation.__module__.startswith("deferred_migrations.")]
