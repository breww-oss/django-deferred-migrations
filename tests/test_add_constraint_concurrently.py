import pytest
from django.contrib.postgres.constraints import ExclusionConstraint
from django.db import IntegrityError
from django.db import NotSupportedError
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.state import ProjectState
from django.db.models import Deferrable
from django.db.models import Q
from django.db.models.functions import Abs

from deferred_migrations.operations import AddConstraintConcurrently
from tests.migration_helpers import apply_operations
from tests.migration_helpers import unapply_operations


def reading_model() -> migrations.CreateModel:
    return migrations.CreateModel("Reading", [("id", models.BigAutoField(primary_key=True)), ("batch", models.IntegerField()), ("vessel", models.IntegerField(null=True)), ("kind", models.IntegerField()), ("value", models.IntegerField(default=0))])


def reading_state(scratch_tables: list[str]) -> ProjectState:
    scratch_tables.append("dm_acc_reading")
    return apply_operations("dm_acc", ProjectState(), [reading_model()])


def insert(*rows: tuple[int, int | None, int, int]) -> None:
    with connection.cursor() as cursor:
        for row in rows:
            cursor.execute("INSERT INTO dm_acc_reading (batch, vessel, kind, value) VALUES (%s, %s, %s, %s)", list(row))


def constraint_definition(name: str) -> tuple[str, str, bool] | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT contype, pg_get_constraintdef(oid), convalidated FROM pg_constraint WHERE conname = %s", [name])
        return cursor.fetchone()


def index_definition(name: str) -> tuple[str, bool] | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_indexdef(i.indexrelid), i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = %s", [name])
        return cursor.fetchone()


@pytest.mark.django_db(transaction=True)
def test_a_nulls_not_distinct_unique_constraint_is_a_real_constraint_that_remove_constraint_can_drop(scratch_tables: list[str]) -> None:
    if connection.pg_version < 150000:
        pytest.skip("needs PostgreSQL 15")

    state = reading_state(scratch_tables)
    constraint = models.UniqueConstraint(fields=["batch", "vessel", "kind"], name="dm_acc_unique_reading", nulls_distinct=False)

    after = apply_operations("dm_acc", state, [AddConstraintConcurrently("reading", constraint)], atomic=False, name="0002")

    assert constraint_definition("dm_acc_unique_reading") == ("u", "UNIQUE NULLS NOT DISTINCT (batch, vessel, kind)", True)
    insert((1, None, 1, 0))

    with pytest.raises(IntegrityError):
        insert((1, None, 1, 0))

    apply_operations("dm_acc", after, [migrations.RemoveConstraint("reading", "dm_acc_unique_reading")], name="0003")

    assert constraint_definition("dm_acc_unique_reading") is None


@pytest.mark.django_db(transaction=True)
def test_a_deferrable_unique_constraint_keeps_its_deferral(scratch_tables: list[str]) -> None:
    state = reading_state(scratch_tables)
    constraint = models.UniqueConstraint(fields=["batch", "kind"], name="dm_acc_deferred", deferrable=Deferrable.DEFERRED)

    apply_operations("dm_acc", state, [AddConstraintConcurrently("reading", constraint)], atomic=False, name="0002")

    assert constraint_definition("dm_acc_deferred") == ("u", "UNIQUE (batch, kind) DEFERRABLE INITIALLY DEFERRED", True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("constraint", "expected_suffix"),
    [
        (models.UniqueConstraint(fields=["batch"], condition=Q(kind=1), name="dm_acc_partial"), "USING btree (batch) WHERE (kind = 1)"),
        (models.UniqueConstraint(Abs("value"), name="dm_acc_expression"), "USING btree (abs(value))"),
    ],
)
def test_an_index_shaped_unique_constraint_is_a_concurrent_unique_index(scratch_tables: list[str], constraint: models.UniqueConstraint, expected_suffix: str) -> None:
    state = reading_state(scratch_tables)

    apply_operations("dm_acc", state, [AddConstraintConcurrently("reading", constraint)], atomic=False, name="0002")

    definition, valid = index_definition(constraint.name)
    assert definition.startswith(f"CREATE UNIQUE INDEX {constraint.name} ON public.dm_acc_reading")
    assert definition.endswith(expected_suffix)
    assert valid is True
    assert constraint_definition(constraint.name) is None


@pytest.mark.django_db(transaction=True)
def test_a_check_constraint_is_added_not_valid_then_validated(scratch_tables: list[str]) -> None:
    state = reading_state(scratch_tables)
    insert((1, 1, 1, 5))

    apply_operations("dm_acc", state, [AddConstraintConcurrently("reading", models.CheckConstraint(condition=Q(value__gte=0), name="dm_acc_value_positive"))], atomic=False, name="0002")

    assert constraint_definition("dm_acc_value_positive") == ("c", "CHECK ((value >= 0))", True)


@pytest.mark.django_db(transaction=True)
def test_a_failed_unique_build_leaves_no_index_behind(scratch_tables: list[str]) -> None:
    state = reading_state(scratch_tables)
    insert((1, 1, 1, 0), (1, 1, 1, 0))

    with pytest.raises(IntegrityError):
        apply_operations("dm_acc", state, [AddConstraintConcurrently("reading", models.UniqueConstraint(fields=["batch", "vessel", "kind"], name="dm_acc_dupes"))], atomic=False, name="0002")

    assert index_definition("dm_acc_dupes") is None


@pytest.mark.django_db(transaction=True)
def test_a_rerun_attaches_an_index_built_before_the_interruption(scratch_tables: list[str]) -> None:
    state = reading_state(scratch_tables)
    operation = AddConstraintConcurrently("reading", models.UniqueConstraint(fields=["batch", "kind"], name="dm_acc_resume"))
    after = state.clone()
    operation.state_forwards("dm_acc", after)

    with connection.cursor() as cursor:
        cursor.execute("CREATE UNIQUE INDEX CONCURRENTLY dm_acc_resume ON dm_acc_reading (batch, kind)")

    with connection.schema_editor(atomic=False) as editor:
        operation.database_forwards("dm_acc", editor, state, after)

    with connection.schema_editor(atomic=False) as editor:
        operation.database_forwards("dm_acc", editor, state, after)

    assert constraint_definition("dm_acc_resume") == ("u", "UNIQUE (batch, kind)", True)


@pytest.mark.django_db(transaction=True)
def test_a_rerun_replaces_an_invalid_leftover_index(scratch_tables: list[str]) -> None:
    state = reading_state(scratch_tables)
    insert((1, 1, 1, 0), (1, 1, 1, 0))

    with pytest.raises(IntegrityError), connection.cursor() as cursor:
        cursor.execute("CREATE UNIQUE INDEX CONCURRENTLY dm_acc_leftover ON dm_acc_reading (batch, kind)")

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM dm_acc_reading WHERE id = (SELECT max(id) FROM dm_acc_reading)")

    apply_operations("dm_acc", state, [AddConstraintConcurrently("reading", models.UniqueConstraint(fields=["batch", "kind"], name="dm_acc_leftover"))], atomic=False, name="0002")

    assert constraint_definition("dm_acc_leftover") == ("u", "UNIQUE (batch, kind)", True)


@pytest.mark.django_db(transaction=True)
def test_backwards_removes_the_constraint(scratch_tables: list[str]) -> None:
    state = reading_state(scratch_tables)
    operations = [AddConstraintConcurrently("reading", models.UniqueConstraint(fields=["batch", "kind"], name="dm_acc_back"))]
    apply_operations("dm_acc", state, operations, atomic=False, name="0002")

    unapply_operations("dm_acc", state, operations, atomic=False, name="0002")

    assert constraint_definition("dm_acc_back") is None
    assert index_definition("dm_acc_back") is None


@pytest.mark.django_db(transaction=True)
def test_sqlmigrate_emits_every_statement_unconditionally() -> None:
    state = ProjectState()
    reading_model().state_forwards("dm_acc", state)
    operation = AddConstraintConcurrently("reading", models.UniqueConstraint(fields=["batch", "kind"], name="dm_acc_sql"))
    after = state.clone()
    operation.state_forwards("dm_acc", after)

    with connection.schema_editor(collect_sql=True, atomic=False) as editor:
        operation.database_forwards("dm_acc", editor, state, after)

    assert editor.collected_sql == [
        'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "dm_acc_sql" ON "dm_acc_reading" ("batch", "kind");',
        'ALTER TABLE "dm_acc_reading" ADD CONSTRAINT "dm_acc_sql" UNIQUE USING INDEX "dm_acc_sql";',
    ]


@pytest.mark.django_db
def test_it_refuses_to_run_inside_a_transaction() -> None:
    state = ProjectState()
    reading_model().state_forwards("dm_acc", state)
    operation = AddConstraintConcurrently("reading", models.UniqueConstraint(fields=["batch"], name="dm_acc_tx"))
    after = state.clone()
    operation.state_forwards("dm_acc", after)

    with pytest.raises(NotSupportedError), connection.schema_editor(atomic=True) as editor:
        operation.database_forwards("dm_acc", editor, state, after)


def test_it_rejects_constraints_it_cannot_build_concurrently() -> None:
    with pytest.raises(ValueError, match="UniqueConstraint or CheckConstraint"):
        AddConstraintConcurrently("reading", ExclusionConstraint(name="dm_acc_excl", expressions=[("batch", "=")]))
