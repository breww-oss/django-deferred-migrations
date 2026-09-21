import pytest
from django.db import connection

from deferred_migrations.concurrent import constraint_validity
from deferred_migrations.concurrent import index_validity
from deferred_migrations.schema import column_is_not_null
from deferred_migrations.schema import create_compatibility_view


# Every catalogue lookup must resolve an unqualified table the way the DDL beside it does. current_schema() is not that:
# it names the first schema on the search_path, which need not be the one holding the table.
@pytest.fixture
def table_in_a_later_schema() -> str:
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA dm_sp_first")
        cursor.execute("CREATE SCHEMA dm_sp_second")
        cursor.execute("CREATE TABLE dm_sp_second.dm_sp_thing (id bigint PRIMARY KEY, code text NOT NULL, note text NULL)")
        cursor.execute("SET LOCAL search_path = dm_sp_first, dm_sp_second")

    return "dm_sp_thing"


@pytest.mark.django_db
def test_the_fixture_puts_the_table_outside_the_current_schema(table_in_a_later_schema: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_schema(), n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = %s::regclass", [table_in_a_later_schema])

        assert cursor.fetchone() == ("dm_sp_first", "dm_sp_second")


@pytest.mark.django_db
def test_a_constraint_is_found_on_a_table_outside_the_current_schema(table_in_a_later_schema: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE dm_sp_second.dm_sp_thing ADD CONSTRAINT dm_sp_thing_code_uniq UNIQUE (code)")

    with connection.schema_editor() as editor:
        assert constraint_validity(editor, table_in_a_later_schema, "dm_sp_thing_code_uniq") is True
        assert constraint_validity(editor, table_in_a_later_schema, "dm_sp_thing_absent") is None


@pytest.mark.django_db
def test_an_index_is_found_on_a_table_outside_the_current_schema(table_in_a_later_schema: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("CREATE INDEX dm_sp_thing_code_idx ON dm_sp_second.dm_sp_thing (code)")

    with connection.schema_editor() as editor:
        assert index_validity(editor, table_in_a_later_schema, "dm_sp_thing_code_idx") is True
        assert index_validity(editor, table_in_a_later_schema, "dm_sp_thing_absent") is None


@pytest.mark.django_db
def test_nullability_is_read_from_a_table_outside_the_current_schema(table_in_a_later_schema: str) -> None:
    with connection.schema_editor() as editor:
        assert column_is_not_null(editor, table_in_a_later_schema, "code") is True
        assert column_is_not_null(editor, table_in_a_later_schema, "note") is False
        assert column_is_not_null(editor, table_in_a_later_schema, "absent") is None


@pytest.mark.django_db
def test_a_compatibility_view_copies_grants_from_a_table_outside_the_current_schema(table_in_a_later_schema: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("CREATE ROLE dm_sp_reader NOLOGIN")
        cursor.execute("GRANT SELECT ON dm_sp_second.dm_sp_thing TO dm_sp_reader")

    with connection.schema_editor() as editor:
        create_compatibility_view(editor, "dm_sp_view", table_in_a_later_schema)

    with connection.cursor() as cursor:
        cursor.execute("SELECT has_table_privilege('dm_sp_reader', 'dm_sp_view', 'SELECT'), has_table_privilege('dm_sp_reader', 'dm_sp_view', 'DELETE')")

        assert cursor.fetchone() == (True, False)
