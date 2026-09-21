import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import BackfillNotNull
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import DeferredRenameModel
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import InstallNotNullFill
from deferred_migrations.operations import SetNotNull
from deferred_migrations.runner import MigrationKeys
from deferred_migrations.runner import run_deferred_operations
from tests.migration_helpers import apply_operations
from tests.schema_snapshot import drop_relations
from tests.schema_snapshot import schema_snapshot

BASE = [
    migrations.CreateModel(
        "Row",
        [
            ("id", models.BigAutoField(primary_key=True)),
            ("label", models.CharField(max_length=20)),
            ("amount", models.IntegerField(null=True)),
        ],
    )
]


# A single %, not %%. psycopg only processes placeholders when params are supplied, and this call
# passes none, so %% would reach the server literally and fail.
def _seed() -> None:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_dat_row (label, amount) SELECT 'row-' || n, CASE WHEN n % 3 = 0 THEN NULL ELSE n END FROM generate_series(1, 200) AS n")


def _contents(table: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)} ORDER BY id")
        return cursor.fetchall()


def _drop_everything() -> None:
    drop_relations(["dm_dat_renamed", "dm_dat_row"])


def _build(operations: list[list[Operation]], run_queue: bool) -> tuple[list[tuple], dict[str, object]]:
    try:
        state = apply_operations("dm_dat", ProjectState(), BASE)
        _seed()

        for number, group in enumerate(operations, start=2):
            state = apply_operations("dm_dat", state, group, atomic=False, name=f"{number:04d}_step")

        if run_queue:
            keys = {("dm_dat", f"{number:04d}_step") for number in range(2, len(operations) + 2)}
            result = run_deferred_operations(migration_keys=MigrationKeys(known=keys, applied=keys), sleep=lambda seconds: None)

            assert result.failed is None, f"queued row failed: {result.failed}"
            assert not result.blocked, f"queued rows blocked: {result.blocked}"
            assert not result.unknown, f"queued rows unknown: {result.unknown}"

        return _contents("dm_dat_row"), schema_snapshot(["dm_dat_row"])
    finally:
        _drop_everything()


# The recipe comparison, not the naive one: plain AlterField to null=False fails outright while rows
# hold NULL, so the native arm is the three-step sequence a careful developer writes by hand.
@pytest.mark.django_db(transaction=True)
def test_the_not_null_operations_match_the_hand_written_safe_recipe() -> None:
    native = [
        [migrations.RunSQL("UPDATE dm_dat_row SET amount = 0 WHERE amount IS NULL", migrations.RunSQL.noop)],
        [migrations.AlterField("row", "amount", models.IntegerField())],
    ]
    deferred = [
        [InstallNotNullFill("row", "amount", fill_sql="0")],
        [BackfillNotNull("row", "amount", fill_sql="0"), SetNotNull("row", "amount", models.IntegerField())],
    ]

    expected_rows, expected_schema = _build(native, run_queue=False)
    actual_rows, actual_schema = _build(deferred, run_queue=True)

    assert actual_rows == expected_rows
    assert actual_schema == expected_schema


# DeferredRenameModel renames the table and leaves a view under the old name, so this proves the rows
# survive the rename and the queued view drop, not that anything carried data across.
@pytest.mark.django_db(transaction=True)
def test_a_deferred_model_rename_leaves_the_rows_untouched() -> None:
    state = apply_operations("dm_dat", ProjectState(), BASE)
    _seed()
    before = _contents("dm_dat_row")

    try:
        apply_operations("dm_dat", state, [DeferredRenameModel("Row", "Renamed")], atomic=False, name="0002_step")
        keys = {("dm_dat", "0002_step")}
        result = run_deferred_operations(migration_keys=MigrationKeys(known=keys, applied=keys), sleep=lambda seconds: None)

        assert result.failed is None, f"queued row failed: {result.failed}"
        assert not result.blocked, f"queued rows blocked: {result.blocked}"
        assert not result.unknown, f"queued rows unknown: {result.unknown}"
        assert result.ran, "the rename queued nothing, so the view drop never ran"
        assert _contents("dm_dat_renamed") == before
    finally:
        _drop_everything()


# The native arm is the hand-written recipe: add the new column, copy the data across in one UPDATE,
# drop the old column. The deferred arm keeps both columns in step with a trigger across the rollout.
@pytest.mark.django_db(transaction=True)
def test_a_column_reshape_matches_the_hand_written_copy_and_drop() -> None:
    new_column = models.BigIntegerField(null=True)
    native = [
        [migrations.AddField("row", "amount_cents", new_column)],
        [migrations.RunSQL("UPDATE dm_dat_row SET amount_cents = (amount * 100)::bigint", migrations.RunSQL.noop)],
        [migrations.RemoveField("row", "amount")],
    ]
    deferred = [
        [migrations.AddField("row", "amount_cents", new_column)],
        [InstallColumnSync("row", from_field="amount", to_field="amount_cents", forwards_sql="({from} * 100)::bigint", backwards_sql=None)],
        [BackfillColumnSync("row", from_field="amount", to_field="amount_cents", forwards_sql="({from} * 100)::bigint")],
        [DeferredRemoveField("row", "amount")],
    ]

    expected_rows, expected_schema = _build(native, run_queue=False)
    actual_rows, actual_schema = _build(deferred, run_queue=True)

    assert actual_rows == expected_rows
    assert actual_schema == expected_schema
