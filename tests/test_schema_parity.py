import pytest
from django.db import migrations
from django.db import models
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

from deferred_migrations.operations import DeferredDeleteModel
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import DeferredRenameModel
from deferred_migrations.runner import MigrationKeys
from deferred_migrations.runner import run_deferred_operations
from tests.migration_helpers import apply_operations
from tests.schema_snapshot import drop_relations
from tests.schema_snapshot import schema_snapshot

TABLES = ["dm_eq_parent", "dm_eq_child", "dm_eq_renamed"]

BASE = [
    migrations.CreateModel("Parent", [("id", models.BigAutoField(primary_key=True)), ("name", models.CharField(max_length=50))]),
    migrations.CreateModel(
        "Child",
        [
            ("id", models.BigAutoField(primary_key=True)),
            ("parent", models.ForeignKey("dm_eq.Parent", models.CASCADE)),
            ("note", models.TextField(null=True)),
            ("code", models.CharField(max_length=10, null=True, unique=True)),
            ("tally", models.IntegerField(default=0, db_index=True)),
        ],
    ),
]


def _drop_everything() -> None:
    drop_relations(["dm_eq_renamed", "dm_eq_child", "dm_eq_parent"])


def _build(operations: list[Operation], run_queue: bool) -> dict[str, object]:
    try:
        state = apply_operations("dm_eq", ProjectState(), BASE)

        for number, operation in enumerate(operations, start=2):
            state = apply_operations("dm_eq", state, [operation], atomic=False, name=f"{number:04d}_step")

        if run_queue:
            keys = {("dm_eq", f"{number:04d}_step") for number in range(2, len(operations) + 2)}
            result = run_deferred_operations(migration_keys=MigrationKeys(known=keys, applied=keys), sleep=lambda seconds: None)

            # run_deferred_operations reports failures in its result rather than raising
            # (runner.py:137-141). Without this, a failed or unrunnable row would surface only as a
            # confusing schema diff that looks like a package bug.
            assert result.failed is None, f"queued row failed: {result.failed}"
            assert not result.blocked, f"queued rows blocked: {result.blocked}"
            assert not result.unknown, f"queued rows unknown: {result.unknown}"
            assert result.ran, "the deferred arm queued nothing, so the comparison proves nothing"

        return schema_snapshot(TABLES)
    finally:
        _drop_everything()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("native", "deferred"),
    [
        pytest.param([migrations.RemoveField("child", "note")], [DeferredRemoveField("child", "note")], id="remove a nullable column"),
        pytest.param([migrations.RemoveField("child", "tally")], [DeferredRemoveField("child", "tally")], id="remove an indexed column with a default"),
        pytest.param([migrations.RemoveField("child", "code")], [DeferredRemoveField("child", "code")], id="remove a unique column"),
        pytest.param([migrations.RemoveField("child", "parent")], [DeferredRemoveField("child", "parent")], id="remove a foreign key column"),
        pytest.param([migrations.DeleteModel("Child")], [DeferredDeleteModel("Child")], id="delete a model"),
        pytest.param([migrations.RenameModel("Child", "Renamed")], [DeferredRenameModel("Child", "Renamed")], id="rename a model"),
    ],
)
def test_the_deferred_operation_ends_at_the_schema_django_builds(native: list[Operation], deferred: list[Operation]) -> None:
    expected = _build(native, run_queue=False)
    actual = _build(deferred, run_queue=True)

    assert actual == expected
