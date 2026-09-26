from collections.abc import Callable

import pytest
from django.db import connection

from deferred_migrations.concurrent import inline_unique_name
from deferred_migrations.concurrent import object_name


def create_table(table: str, column: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TABLE {connection.ops.quote_name(table)} ({connection.ops.quote_name(column)} integer)")


def name_postgresql_chooses(table: str, column: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} ADD UNIQUE ({connection.ops.quote_name(column)})")
        cursor.execute("SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'u'", [connection.ops.quote_name(table)])
        return cursor.fetchone()[0]


# Each name is checked against the one PostgreSQL itself picks, so the port of makeObjectName is held to the real thing rather than to a copy of its rules. Table and column names stay within 63 bytes, which PostgreSQL would otherwise truncate before naming anything.
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("table", "column"),
    [
        pytest.param("dm_nm_t", "c", id="short"),
        pytest.param("dm_nm_t", "c" * 60, id="long column"),
        pytest.param("dm_nm_" + "t" * 57, "code", id="long table"),
        pytest.param("dm_nm_" + "t" * 44, "c" * 50, id="both long"),
        pytest.param("dm_nm_" + "ü" * 28, "code", id="multibyte table clipped mid-character"),
        pytest.param("dm_nm_" + "é" * 20, "x" + "ß" * 25, id="multibyte both"),
    ],
)
def test_the_name_matches_the_one_postgresql_chooses(table: str, column: str) -> None:
    create_table(table, column)

    with connection.schema_editor() as editor:
        expected = inline_unique_name(editor, table, column)

    assert expected == name_postgresql_chooses(table, column)


# PostgreSQL moves on to key1, key2... past a relation of that name anywhere in the schema, and past a constraint of that name, which need not be a relation at all.
@pytest.mark.django_db
@pytest.mark.parametrize(
    "occupy",
    [
        pytest.param(lambda name: f"CREATE INDEX {name} ON dm_nm_other (id)", id="an index"),
        pytest.param(lambda name: f"CREATE SEQUENCE {name}", id="a sequence"),
        pytest.param(lambda name: f"ALTER TABLE dm_nm_other ADD CONSTRAINT {name} CHECK (id > 0)", id="a check constraint"),
    ],
)
@pytest.mark.parametrize("taken", [1, 2])
def test_a_taken_name_moves_on_as_postgresql_does(occupy: Callable[[str], str], taken: int) -> None:
    create_table("dm_nm_t", "c")

    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE dm_nm_other (id integer)")

        for label in ["key", *(f"key{number}" for number in range(1, taken))]:
            cursor.execute(occupy(object_name("dm_nm_t", "c", label)))

    with connection.schema_editor() as editor:
        expected = inline_unique_name(editor, "dm_nm_t", "c")

    assert expected == f"dm_nm_t_c_key{taken}"
    assert expected == name_postgresql_chooses("dm_nm_t", "c")


# With key1 the budget is odd, so two names of equal length cannot be trimmed evenly, and which one keeps the extra byte follows PostgreSQL's tie-break: the column is trimmed first.
@pytest.mark.django_db
def test_equally_long_names_are_trimmed_as_postgresql_trims_them_under_an_odd_budget() -> None:
    table, column = "dm_nm_" + "t" * 44, "c" * 50
    create_table(table, column)

    with connection.cursor() as cursor:
        cursor.execute(f"CREATE SEQUENCE {object_name(table, column, 'key')}")

    with connection.schema_editor() as editor:
        expected = inline_unique_name(editor, table, column)

    assert expected.endswith("_key1")
    assert expected == name_postgresql_chooses(table, column)
