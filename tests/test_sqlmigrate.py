from collections.abc import Callable

import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState
from django.test.utils import CaptureQueriesContext

from deferred_migrations.models import DeferredOperation
from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import BackfillNotNull
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import InstallNotNullFill
from deferred_migrations.operations import SetNotNull

APP = "dm_sqlm"
WRITES = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE")


# State only: no table exists, so sqlmigrate has to describe every statement without running any of them.
def row_state() -> ProjectState:
    state = ProjectState()
    migrations.CreateModel(
        "Row",
        [("id", models.BigAutoField(primary_key=True)), ("amount", models.IntegerField(null=True)), ("amount_cents", models.BigIntegerField(null=True))],
    ).state_forwards(APP, state)
    return state


# The README promises every operation's SQL goes through the schema editor, so sqlmigrate shows it. Each expected fragment is a statement the operation runs for real.
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("make_operation", "expected"),
    [
        pytest.param(lambda: InstallColumnSync("row", from_field="amount", to_field="amount_cents", forwards_sql="({from} * 100)::bigint", backwards_sql="({to} / 100)::integer"), ["CREATE OR REPLACE FUNCTION", "CREATE OR REPLACE TRIGGER", '(NEW."amount" * 100)::bigint'], id="InstallColumnSync"),
        pytest.param(lambda: BackfillColumnSync("row", from_field="amount", to_field="amount_cents", forwards_sql="({from} * 100)::bigint"), ["-- BackfillColumnSync runs batched UPDATEs on dm_sqlm_row"], id="BackfillColumnSync"),
        pytest.param(lambda: InstallNotNullFill("row", "amount", fill_sql="0"), ["CREATE OR REPLACE FUNCTION", "CREATE OR REPLACE TRIGGER", "dm2_fill_"], id="InstallNotNullFill"),
        pytest.param(lambda: BackfillNotNull("row", "amount", fill_sql="0"), ["-- BackfillNotNull runs batched UPDATEs on dm_sqlm_row"], id="BackfillNotNull"),
        pytest.param(lambda: SetNotNull("row", "amount", models.IntegerField()), ["NOT VALID", "VALIDATE CONSTRAINT", "SET NOT NULL"], id="SetNotNull"),
    ],
)
def test_sqlmigrate_shows_the_sql_without_running_any_of_it(make_operation: Callable[[], Operation], expected: list[str]) -> None:
    # Through Migration.apply, as sqlmigrate itself calls it: the queueing operations key their rows by migration and position.
    migration = Migration("0002_step", APP)
    migration.operations = [make_operation()]

    with CaptureQueriesContext(connection) as queries, connection.schema_editor(collect_sql=True, atomic=False) as editor:
        migration.apply(row_state(), editor, collect_sql=True)

    collected = "\n".join(editor.collected_sql)
    assert [fragment for fragment in expected if fragment not in collected] == []
    assert [query["sql"] for query in queries.captured_queries if query["sql"].lstrip().upper().startswith(WRITES)] == []
    assert not DeferredOperation.objects.exists()
