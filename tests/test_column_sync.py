from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.state import ProjectState
from pytest_django import Settings

from deferred_migrations.models import DeferredOperation
from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.runner import MigrationKeys
from deferred_migrations.runner import run_deferred_operations
from tests.migration_helpers import apply_operations
from tests.migration_helpers import column_names
from tests.migration_helpers import unapply_operations


@pytest.fixture
def customer_id(shop_state: ProjectState) -> int:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_customer (name) VALUES ('Acme') RETURNING id")
        return cursor.fetchone()[0]


@pytest.fixture
def synced_state(shop_state: ProjectState) -> ProjectState:
    return apply_operations("dm_shop", shop_state, [InstallColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint", backwards_sql="({to} / 100.0)::numeric(12,2)")], name="0002_install")


def fetch(order_id: int) -> tuple[Decimal, int | None]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT amount, amount_pence FROM dm_shop_order WHERE id = %s", [order_id])
        return cursor.fetchone()


# Raw SQL on purpose: it plays the part of old code writing rows the ORM model no longer describes.
def insert_order(customer_id: int, columns: dict[str, object]) -> int:
    values = {"customer_id": customer_id, "reference": "R", "status": "new", "code": uuid4().hex[:12], "created": "2026-01-01T00:00:00Z", **columns}

    with connection.cursor() as cursor:
        cursor.execute(f"INSERT INTO dm_shop_order ({', '.join(values)}) VALUES ({', '.join(['%s'] * len(values))}) RETURNING id", list(values.values()))
        return cursor.fetchone()[0]


@pytest.mark.django_db
def test_old_side_insert_fills_the_new_column(synced_state: ProjectState, customer_id: int) -> None:
    order_id = insert_order(customer_id, {"amount": Decimal("12.34")})

    assert fetch(order_id) == (Decimal("12.34"), 1234)


@pytest.mark.django_db
def test_new_side_insert_fills_the_old_column_even_when_it_has_a_default(synced_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", synced_state, [DeferredRemoveField("order", "amount")], name="0003_remove")

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, status, code, created, amount_pence) VALUES (%s, 'R', 'new', 'n1', now(), 5678) RETURNING id", [customer_id])
        order_id = cursor.fetchone()[0]

    assert fetch(order_id) == (Decimal("56.78"), 5678)


@pytest.mark.django_db
def test_updates_on_either_side_are_copied_across(synced_state: ProjectState, customer_id: int) -> None:
    order_id = insert_order(customer_id, {"amount": Decimal("1.00")})

    with connection.cursor() as cursor:
        cursor.execute("UPDATE dm_shop_order SET amount = 2.50 WHERE id = %s", [order_id])
        assert fetch(order_id) == (Decimal("2.50"), 250)
        cursor.execute("UPDATE dm_shop_order SET amount_pence = 999 WHERE id = %s", [order_id])
        assert fetch(order_id) == (Decimal("9.99"), 999)
        cursor.execute("UPDATE dm_shop_order SET amount = 1.00, amount_pence = 7 WHERE id = %s", [order_id])
        assert fetch(order_id) == (Decimal("1.00"), 7)


@pytest.mark.django_db
def test_backfill_with_a_lossy_transform_never_rewrites_the_source(shop_state: ProjectState, customer_id: int) -> None:
    order_id = insert_order(customer_id, {"amount": Decimal("12.345")})
    state = apply_operations("dm_shop", shop_state, [InstallColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from})::bigint", backwards_sql="{to}::numeric(12,2)")], name="0002_install")

    with connection.cursor() as cursor:
        cursor.execute("UPDATE dm_shop_order SET amount = 12.35, amount_pence = NULL WHERE id = %s", [order_id])

    apply_operations("dm_shop", state, [BackfillColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from})::bigint")], atomic=False, name="0003_backfill")

    assert fetch(order_id) == (Decimal("12.35"), 12)


@pytest.mark.django_db
def test_backfill_over_sparse_primary_keys_updates_every_row(shop_state: ProjectState, customer_id: int, settings: Settings) -> None:
    settings.DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS = 1
    settings.DEFERRED_MIGRATIONS_BACKFILL_MAX_ROWS = 2
    settings.DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS = 0
    ids = [insert_order(customer_id, {"amount": Decimal(n), "code": f"s{n}"}) for n in range(1, 8)]

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM dm_shop_order WHERE id = ANY(%s)", [ids[1:5]])

    state = apply_operations("dm_shop", shop_state, [InstallColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint", backwards_sql=None)], name="0002_install")
    apply_operations("dm_shop", state, [BackfillColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint")], atomic=False, name="0003_backfill")

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM dm_shop_order WHERE amount_pence IS NULL")
        assert cursor.fetchone()[0] == 0


@pytest.mark.django_db
def test_backwards_sql_none_leaves_old_column_untouched_by_new_writes(shop_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", shop_state, [InstallColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint", backwards_sql=None)], name="0002_install")
    order_id = insert_order(customer_id, {"amount": Decimal("1.00")})

    with connection.cursor() as cursor:
        cursor.execute("UPDATE dm_shop_order SET amount_pence = 500 WHERE id = %s", [order_id])

    assert fetch(order_id) == (Decimal("1.00"), 500)


@pytest.mark.django_db
def test_braces_in_sql_are_left_alone(shop_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", shop_state, [InstallColumnSync("order", from_field="reference", to_field="note", forwards_sql="({from} || '{}')", backwards_sql=None)], name="0002_install")
    order_id = insert_order(customer_id, {"reference": "R9", "amount": 0})

    with connection.cursor() as cursor:
        cursor.execute("SELECT note FROM dm_shop_order WHERE id = %s", [order_id])
        assert cursor.fetchone()[0] == "R9{}"


@pytest.mark.django_db
def test_trigger_drop_is_queued_and_the_runner_removes_the_trigger(synced_state: ProjectState, customer_id: int) -> None:
    row = DeferredOperation.objects.get()
    assert row.kind == DeferredOperation.Kind.DROP_TRIGGER

    run_deferred_operations(migration_keys=MigrationKeys(known={("dm_shop", "0002_install")}, applied={("dm_shop", "0002_install")}), sleep=lambda seconds: None)
    order_id = insert_order(customer_id, {"amount": Decimal("3.00")})

    assert fetch(order_id) == (Decimal("3.00"), None)


@pytest.mark.django_db
def test_backwards_drops_the_trigger_and_reapply_queues_again(shop_state: ProjectState, customer_id: int) -> None:
    operations = [InstallColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint", backwards_sql=None)]
    apply_operations("dm_shop", shop_state, operations)
    DeferredOperation.objects.update(status=DeferredOperation.Status.DONE)

    unapply_operations("dm_shop", shop_state, operations)
    order_id = insert_order(customer_id, {"amount": Decimal("3.00")})
    assert fetch(order_id) == (Decimal("3.00"), None)
    assert not DeferredOperation.objects.exists()

    apply_operations("dm_shop", shop_state, operations)
    assert DeferredOperation.objects.get().status == DeferredOperation.Status.PENDING


@pytest.mark.django_db
def test_a_name_collision_with_a_different_body_raises(synced_state: ProjectState) -> None:
    name = DeferredOperation.objects.get().sql.split('"')[1]

    with connection.cursor() as cursor:
        cursor.execute(f'CREATE OR REPLACE FUNCTION "{name}"() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$')

    with pytest.raises(ValueError, match="already exists"):
        apply_operations("dm_shop", synced_state, [InstallColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint", backwards_sql="({to} / 100.0)::numeric(12,2)")], name="0009_again")


@pytest.mark.django_db
def test_models_without_a_single_column_primary_key_are_refused() -> None:
    state = apply_operations("dm_cpk", ProjectState(), [migrations.CreateModel("Pair", [("pk", models.CompositePrimaryKey("a", "b")), ("a", models.IntegerField()), ("b", models.IntegerField()), ("c", models.IntegerField(null=True))])])

    with pytest.raises(ValueError, match="single-column primary key"):
        apply_operations("dm_cpk", state, [InstallColumnSync("pair", from_field="a", to_field="c", forwards_sql="{from}", backwards_sql=None)], name="0002")


@pytest.mark.django_db
def test_forwards_twice_is_idempotent(shop_state: ProjectState) -> None:
    operation = InstallColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint", backwards_sql=None)
    apply_operations("dm_shop", shop_state, [operation])
    apply_operations("dm_shop", shop_state, [operation])

    assert DeferredOperation.objects.count() == 1
    assert "amount_pence" in column_names("dm_shop_order")
