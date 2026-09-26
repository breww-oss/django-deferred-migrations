from collections.abc import Callable

import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState
from django.test.utils import CaptureQueriesContext
from pytest_django import Settings

from deferred_migrations.models import DeferredOperation
from deferred_migrations.operations import AddConstraintConcurrently
from deferred_migrations.operations import AddFieldConcurrently
from deferred_migrations.operations import AddIndexConcurrently
from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import BackfillNotNull
from deferred_migrations.operations import DeferredDeleteModel
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import DeferredRenameModel
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import InstallNotNullFill
from deferred_migrations.operations import SetNotNull
from tests.migration_helpers import apply_operations
from tests.migration_helpers import unapply_operations

APP = "dm_skip"


class ExcludeSkipApp:
    def allow_migrate(self, db: str, app_label: str, **hints: object) -> bool | None:
        return False if app_label == APP else None


# Built in state only: no table exists, so an operation that ignores the exclusion fails on the missing table or shows up in the captured SQL.
def thing_state(options: dict[str, object]) -> ProjectState:
    state = ProjectState()
    migrations.CreateModel(
        "Thing",
        [
            ("id", models.BigAutoField(primary_key=True)),
            ("name", models.CharField(max_length=20, null=True)),
            ("old", models.CharField(max_length=20, null=True)),
            ("amount", models.IntegerField(null=True)),
            ("tags", models.ManyToManyField("self")),
        ],
        options=options,
    ).state_forwards(APP, state)
    return state


OPERATIONS: list[Callable[[], Operation]] = [
    lambda: DeferredRemoveField("thing", "old"),
    lambda: DeferredRemoveField("thing", "tags"),
    lambda: DeferredDeleteModel("Thing"),
    lambda: DeferredRenameModel("Thing", "Renamed"),
    lambda: InstallColumnSync("thing", from_field="old", to_field="name", forwards_sql="{from}", backwards_sql="{to}"),
    lambda: BackfillColumnSync("thing", from_field="old", to_field="name", forwards_sql="{from}"),
    lambda: InstallNotNullFill("thing", "amount", fill_sql="0"),
    lambda: BackfillNotNull("thing", "amount", fill_sql="0"),
    lambda: SetNotNull("thing", "amount", models.IntegerField()),
    lambda: AddIndexConcurrently("thing", models.Index(fields=["name"], name="thing_name_idx")),
    lambda: AddConstraintConcurrently("thing", models.CheckConstraint(condition=models.Q(amount__gte=0), name="thing_amount_positive")),
    lambda: AddConstraintConcurrently("thing", models.UniqueConstraint(fields=["name"], name="thing_name_uniq")),
    lambda: AddFieldConcurrently("thing", "extra", models.CharField(max_length=10, null=True, unique=True)),
    lambda: AddFieldConcurrently("thing", "parent", models.ForeignKey(f"{APP}.Thing", models.SET_NULL, null=True)),
]


# The README promises every operation does nothing on the database for an unmanaged model or one a router excludes, exactly as Django's own operations behave. That covers both directions.
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("exclusion", ["unmanaged", "router"])
@pytest.mark.parametrize("make_operation", OPERATIONS, ids=lambda make: make().describe())
def test_an_excluded_model_is_left_alone_in_both_directions(make_operation: Callable[[], Operation], exclusion: str, settings: Settings) -> None:
    options: dict[str, object] = {}

    if exclusion == "unmanaged":
        options = {"managed": False}
    else:
        settings.DATABASE_ROUTERS = [f"{__name__}.ExcludeSkipApp"]

    state = thing_state(options)
    operations = [make_operation()]

    with CaptureQueriesContext(connection) as queries:
        apply_operations(APP, state, operations, atomic=False, name="0002")
        unapply_operations(APP, state, operations, atomic=False, name="0002")

    assert [query["sql"] for query in queries.captured_queries] == []
    assert not DeferredOperation.objects.exists()
