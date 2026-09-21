import pytest
from django.db import IntegrityError
from django.db import NotSupportedError
from django.db import ProgrammingError
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.backends.utils import strip_quotes
from django.db.migrations.state import ProjectState

from deferred_migrations.models import DeferredOperation
from deferred_migrations.operations import FK_SUFFIX
from deferred_migrations.operations import AddFieldConcurrently
from tests.migration_helpers import apply_operations
from tests.migration_helpers import column_names
from tests.migration_helpers import nullability_and_default
from tests.migration_helpers import unapply_operations


def family_state(scratch_tables: list[str]) -> ProjectState:
    scratch_tables.extend(["dm_afc_parent", "dm_afc_child"])
    state = apply_operations(
        "dm_afc",
        ProjectState(),
        [
            migrations.CreateModel("Parent", [("id", models.BigAutoField(primary_key=True))]),
            migrations.CreateModel("Child", [("id", models.BigAutoField(primary_key=True)), ("name", models.CharField(max_length=20, default="x"))]),
        ],
    )

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_afc_parent DEFAULT VALUES")
        cursor.execute("INSERT INTO dm_afc_child (name) VALUES ('a'), ('b')")

    return state


def constraints_on(column: str) -> dict[str, dict]:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "dm_afc_child")

    return {name: info for name, info in constraints.items() if info["columns"] == [column]}


def convalidated(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT convalidated FROM pg_constraint WHERE conname = %s", [name])
        return cursor.fetchone()[0]


def add(state: ProjectState, field_name: str, field: models.Field, preserve_default: bool = True) -> ProjectState:
    return apply_operations("dm_afc", state, [AddFieldConcurrently("child", field_name, field, preserve_default)], atomic=False, name="0002")


@pytest.mark.django_db(transaction=True)
def test_a_nullable_foreign_key_gets_its_index_and_a_validated_constraint(scratch_tables: list[str]) -> None:
    add(family_state(scratch_tables), "parent", models.ForeignKey("dm_afc.Parent", models.SET_NULL, null=True))

    found = constraints_on("parent_id")
    foreign_keys = [name for name, info in found.items() if info["foreign_key"]]
    indexes = [name for name, info in found.items() if info["index"] and not info["unique"]]
    assert len(foreign_keys) == 1
    assert convalidated(foreign_keys[0]) is True
    assert len(indexes) == 1


@pytest.mark.django_db(transaction=True)
def test_a_one_to_one_gets_a_unique_constraint_and_no_plain_index(scratch_tables: list[str]) -> None:
    add(family_state(scratch_tables), "parent", models.OneToOneField("dm_afc.Parent", models.SET_NULL, null=True))

    found = constraints_on("parent_id")
    assert [name for name, info in found.items() if info["unique"]]
    assert all(name.endswith("_uniq") for name, info in found.items() if info["unique"])
    assert [name for name, info in found.items() if info["foreign_key"]]
    assert not [name for name, info in found.items() if info["index"] and not info["unique"]]


@pytest.mark.django_db(transaction=True)
def test_an_indexed_char_field_gets_its_pattern_ops_index_too(scratch_tables: list[str]) -> None:
    add(family_state(scratch_tables), "code", models.CharField(max_length=10, null=True, db_index=True))

    names = sorted(name for name, info in constraints_on("code").items() if info["index"])
    assert len(names) == 2
    assert names[1].endswith("_like")


@pytest.mark.django_db(transaction=True)
def test_a_unique_char_field_gets_a_unique_constraint_and_a_pattern_ops_index(scratch_tables: list[str]) -> None:
    add(family_state(scratch_tables), "code", models.CharField(max_length=10, null=True, unique=True))

    found = constraints_on("code")
    assert len([name for name, info in found.items() if info["unique"]]) == 1
    assert [name for name in found if name.endswith("_like")]


@pytest.mark.django_db(transaction=True)
def test_a_default_not_preserved_fills_existing_rows_and_leaves_no_database_default(scratch_tables: list[str]) -> None:
    add(family_state(scratch_tables), "rank", models.IntegerField(default=7), preserve_default=False)

    with connection.cursor() as cursor:
        cursor.execute("SELECT DISTINCT rank FROM dm_afc_child")
        assert cursor.fetchall() == [(7,)]

    assert nullability_and_default("dm_afc_child", "rank") == ("NO", None)


@pytest.mark.django_db(transaction=True)
def test_a_db_comment_is_applied(scratch_tables: list[str]) -> None:
    add(family_state(scratch_tables), "note", models.TextField(null=True, db_comment="Free text"))

    with connection.cursor() as cursor:
        cursor.execute("SELECT col_description('dm_afc_child'::regclass, attnum) FROM pg_attribute WHERE attrelid = 'dm_afc_child'::regclass AND attname = 'note'")
        assert cursor.fetchone() == ("Free text",)


@pytest.mark.django_db(transaction=True)
def test_a_rerun_finishes_after_the_column_and_a_not_valid_foreign_key_were_added(scratch_tables: list[str]) -> None:
    state = family_state(scratch_tables)
    operation = AddFieldConcurrently("child", "parent", models.ForeignKey("dm_afc.Parent", models.SET_NULL, null=True))
    after = state.clone()
    operation.state_forwards("dm_afc", after)

    with connection.schema_editor(atomic=False) as editor:
        model = after.apps.get_model("dm_afc", "child")
        editor.execute("ALTER TABLE dm_afc_child ADD COLUMN parent_id bigint NULL")
        editor.execute(f"{editor._create_fk_sql(model, model._meta.get_field('parent'), FK_SUFFIX)} NOT VALID")

    with connection.schema_editor(atomic=False) as editor:
        operation.database_forwards("dm_afc", editor, state, after)

    foreign_keys = [name for name, info in constraints_on("parent_id").items() if info["foreign_key"]]
    assert len(foreign_keys) == 1
    assert convalidated(foreign_keys[0]) is True
    assert [name for name, info in constraints_on("parent_id").items() if info["index"] and not info["unique"]]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", [DeferredOperation.Status.PENDING, DeferredOperation.Status.FAILED, DeferredOperation.Status.SKIPPED])
def test_a_column_whose_drop_has_not_run_is_never_adopted(scratch_tables: list[str], status: str) -> None:
    state = family_state(scratch_tables)

    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE dm_afc_child ADD COLUMN code varchar(10) NULL")

    DeferredOperation.objects.create(app_label="dm_afc", migration_name="0001_old", operation_index=0, sequence=0, kind=DeferredOperation.Kind.DROP_COLUMN, table_name="dm_afc_child", column_name="code", sql="-- drop", status=status)

    with pytest.raises(ProgrammingError, match="already exists"):
        add(state, "code", models.CharField(max_length=10, null=True, db_index=True))

    assert not [name for name, info in constraints_on("code").items() if info["index"]]


@pytest.mark.django_db(transaction=True)
def test_every_statement_uses_its_non_blocking_form() -> None:
    state = ProjectState()
    migrations.CreateModel("Parent", [("id", models.BigAutoField(primary_key=True))]).state_forwards("dm_afc", state)
    migrations.CreateModel("Child", [("id", models.BigAutoField(primary_key=True))]).state_forwards("dm_afc", state)
    operation = AddFieldConcurrently("child", "parent", models.OneToOneField("dm_afc.Parent", models.SET_NULL, null=True))
    after = state.clone()
    operation.state_forwards("dm_afc", after)

    with connection.schema_editor(collect_sql=True, atomic=False) as editor:
        operation.database_forwards("dm_afc", editor, state, after)

    statements = editor.collected_sql
    assert statements[0].startswith('ALTER TABLE "dm_afc_child" ADD COLUMN "parent_id" bigint NULL')
    assert "UNIQUE" not in statements[0]
    assert "REFERENCES" not in statements[0]
    assert all("CONCURRENTLY" in sql for sql in statements if "INDEX" in sql and "USING INDEX" not in sql)
    assert any("FOREIGN KEY" in sql and sql.endswith("NOT VALID;") for sql in statements)
    assert any("VALIDATE CONSTRAINT" in sql for sql in statements)


@pytest.mark.django_db(transaction=True)
def test_backwards_removes_the_column(scratch_tables: list[str]) -> None:
    state = family_state(scratch_tables)
    operations = [AddFieldConcurrently("child", "parent", models.ForeignKey("dm_afc.Parent", models.SET_NULL, null=True))]
    apply_operations("dm_afc", state, operations, atomic=False, name="0002")

    unapply_operations("dm_afc", state, operations, atomic=False, name="0002")

    assert "parent_id" not in column_names("dm_afc_child")


def unique_name(after: ProjectState, field_name: str) -> str:
    with connection.schema_editor(atomic=False) as editor:
        model = after.apps.get_model("dm_afc", "child")
        return strip_quotes(str(editor._create_unique_sql(model, [model._meta.get_field(field_name)]).parts["name"]))


@pytest.mark.django_db(transaction=True)
def test_a_rerun_replaces_an_invalid_unique_index_left_by_a_failed_build(scratch_tables: list[str]) -> None:
    state = family_state(scratch_tables)
    operation = AddFieldConcurrently("child", "code", models.CharField(max_length=10, null=True, unique=True))
    after = state.clone()
    operation.state_forwards("dm_afc", after)
    name = unique_name(after, "code")

    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE dm_afc_child ADD COLUMN code varchar(10) NULL")
        cursor.execute("UPDATE dm_afc_child SET code = 'same'")

    with pytest.raises(IntegrityError), connection.cursor() as cursor:
        cursor.execute(f'CREATE UNIQUE INDEX CONCURRENTLY "{name}" ON dm_afc_child (code)')

    with connection.cursor() as cursor:
        cursor.execute("UPDATE dm_afc_child SET code = NULL")

    with connection.schema_editor(atomic=False) as editor:
        operation.database_forwards("dm_afc", editor, state, after)

    assert convalidated(name) is True


@pytest.mark.django_db(transaction=True)
def test_a_rerun_attaches_a_unique_index_built_before_the_interruption(scratch_tables: list[str]) -> None:
    state = family_state(scratch_tables)
    operation = AddFieldConcurrently("child", "code", models.CharField(max_length=10, null=True, unique=True))
    after = state.clone()
    operation.state_forwards("dm_afc", after)
    name = unique_name(after, "code")

    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE dm_afc_child ADD COLUMN code varchar(10) NULL")
        cursor.execute(f'CREATE UNIQUE INDEX CONCURRENTLY "{name}" ON dm_afc_child (code)')

    with connection.schema_editor(atomic=False) as editor:
        operation.database_forwards("dm_afc", editor, state, after)

    assert convalidated(name) is True


@pytest.mark.django_db(transaction=True)
def test_a_failed_unique_build_leaves_no_index_behind(scratch_tables: list[str]) -> None:
    state = family_state(scratch_tables)
    operation = AddFieldConcurrently("child", "code", models.CharField(max_length=10, unique=True, default="same"), preserve_default=False)
    after = state.clone()
    operation.state_forwards("dm_afc", after)
    name = unique_name(after, "code")

    with pytest.raises(IntegrityError):
        apply_operations("dm_afc", state, [operation], atomic=False, name="0002")

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_class WHERE relname = %s", [name])
        assert cursor.fetchone() is None


@pytest.mark.django_db
def test_it_refuses_to_run_inside_a_transaction() -> None:
    state = ProjectState()
    migrations.CreateModel("Child", [("id", models.BigAutoField(primary_key=True))]).state_forwards("dm_afc", state)
    operation = AddFieldConcurrently("child", "code", models.CharField(max_length=10, null=True, db_index=True))
    after = state.clone()
    operation.state_forwards("dm_afc", after)

    with pytest.raises(NotSupportedError), connection.schema_editor(atomic=True) as editor:
        operation.database_forwards("dm_afc", editor, state, after)


@pytest.mark.parametrize("field", [models.ManyToManyField("dm_afc.Parent"), models.BigIntegerField(primary_key=True)])
def test_it_rejects_fields_it_cannot_add_concurrently(field: models.Field) -> None:
    with pytest.raises(ValueError, match="AddFieldConcurrently"):
        AddFieldConcurrently("child", "other", field)
