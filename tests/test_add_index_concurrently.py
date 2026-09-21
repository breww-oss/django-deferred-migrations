import pytest
from django.db import IntegrityError
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.state import ProjectState

from deferred_migrations.operations import AddIndexConcurrently
from tests.migration_helpers import apply_operations


@pytest.mark.django_db(transaction=True)
def test_an_invalid_leftover_index_is_dropped_before_building() -> None:
    state = apply_operations("dm_idx", ProjectState(), [migrations.CreateModel("Item", [("id", models.BigAutoField(primary_key=True)), ("code", models.IntegerField())])])

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_idx_item (code) VALUES (1), (1)")

    with pytest.raises(IntegrityError), connection.cursor() as cursor:
        cursor.execute("CREATE UNIQUE INDEX CONCURRENTLY dm_idx_item_code ON dm_idx_item (code)")

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM dm_idx_item WHERE id = (SELECT max(id) FROM dm_idx_item)")

    apply_operations("dm_idx", state, [AddIndexConcurrently("item", models.Index(fields=["code"], name="dm_idx_item_code"))], atomic=False, name="0002")

    with connection.cursor() as cursor:
        cursor.execute("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'dm_idx_item_code'")
        assert cursor.fetchall() == [(True,)]


@pytest.mark.django_db(transaction=True)
def test_rerunning_after_the_index_was_built_is_a_no_op(scratch_tables: list[str]) -> None:
    scratch_tables.append("dm_idx2_item")
    state = apply_operations("dm_idx2", ProjectState(), [migrations.CreateModel("Item", [("id", models.BigAutoField(primary_key=True)), ("code", models.IntegerField())])])
    operation = AddIndexConcurrently("item", models.Index(fields=["code"], name="dm_idx2_item_code"))
    after = apply_operations("dm_idx2", state, [operation], atomic=False, name="0002")

    with connection.schema_editor(atomic=False) as editor:
        operation.database_forwards("dm_idx2", editor, state, after)

    with connection.cursor() as cursor:
        cursor.execute("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'dm_idx2_item_code'")
        assert cursor.fetchall() == [(True,)]


@pytest.mark.django_db(transaction=True)
def test_sqlmigrate_emits_the_concurrent_build_without_touching_the_database() -> None:
    state = ProjectState()
    migrations.CreateModel("Item", [("id", models.BigAutoField(primary_key=True)), ("code", models.IntegerField())]).state_forwards("dm_idx3", state)
    operation = AddIndexConcurrently("item", models.Index(fields=["code"], name="dm_idx3_item_code"))
    after = state.clone()
    operation.state_forwards("dm_idx3", after)

    with connection.schema_editor(collect_sql=True, atomic=False) as editor:
        operation.database_forwards("dm_idx3", editor, state, after)

    assert [sql for sql in editor.collected_sql if "INDEX" in sql] == ['CREATE INDEX CONCURRENTLY IF NOT EXISTS "dm_idx3_item_code" ON "dm_idx3_item" ("code");']
