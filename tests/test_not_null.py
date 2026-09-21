import pytest
from django.db import connection
from django.db import models
from django.db.migrations.state import ProjectState

from deferred_migrations.models import DeferredOperation
from deferred_migrations.operations import BackfillNotNull
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import InstallNotNullFill
from deferred_migrations.operations import SetNotNull
from tests.migration_helpers import apply_operations
from tests.migration_helpers import unapply_operations


@pytest.fixture
def customer_id(shop_state: ProjectState) -> int:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_customer (name) VALUES ('Acme') RETURNING id")
        return cursor.fetchone()[0]


def is_nullable(table: str, column: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT is_nullable FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s", [table, column])
        return cursor.fetchone()[0] == "YES"


@pytest.mark.django_db
def test_explicit_null_from_old_code_is_filled(shop_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", shop_state, [InstallNotNullFill("order", "note", fill_sql="'none: ' || {reference}")], name="0002")

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, status, code, amount, created, note) VALUES (%s, 'R7', 'new', 'c', 0, now(), NULL) RETURNING note", [customer_id])
        assert cursor.fetchone()[0] == "none: R7"


@pytest.mark.django_db
def test_backfill_and_set_not_null_leave_no_nulls_and_no_helper_constraint(shop_state: ProjectState, customer_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, status, code, amount, created) VALUES (%s, 'R1', 'new', 'c', 0, now())", [customer_id])
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    state = apply_operations("dm_shop", shop_state, [InstallNotNullFill("order", "note", fill_sql="''")], name="0002")
    operations = [BackfillNotNull("order", "note", fill_sql="''"), SetNotNull("order", "note", models.TextField())]
    after = apply_operations("dm_shop", state, operations, atomic=False, name="0003")
    apply_operations("dm_shop", state, operations, atomic=False, name="0003")

    assert not is_nullable("dm_shop_order", "note")
    assert after.models["dm_shop", "order"].fields["note"].null is False

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_constraint WHERE conname LIKE 'dm_nn_%'")
        assert cursor.fetchone()[0] == 0

    unapply_operations("dm_shop", state, operations, atomic=False, name="0003")
    assert is_nullable("dm_shop_order", "note")


@pytest.mark.django_db
def test_set_not_null_refuses_other_field_changes(shop_state: ProjectState) -> None:
    with pytest.raises(ValueError, match="only change nullability"):
        SetNotNull("order", "note", models.CharField(max_length=10)).state_forwards("dm_shop", shop_state.clone())


@pytest.mark.django_db
def test_sync_trigger_fires_before_fill_trigger_on_the_same_column(shop_state: ProjectState, customer_id: int) -> None:
    apply_operations("dm_shop", shop_state, [InstallColumnSync("order", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint", backwards_sql=None), InstallNotNullFill("order", "amount_pence", fill_sql="0")], name="0002")

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, status, code, amount, created) VALUES (%s, 'R', 'new', 'c', 4.20, now()) RETURNING amount_pence", [customer_id])
        assert cursor.fetchone()[0] == 420


@pytest.mark.django_db
def test_fill_trigger_drop_is_queued_and_backwards_deletes_it(shop_state: ProjectState) -> None:
    operations = [InstallNotNullFill("order", "note", fill_sql="''")]
    apply_operations("dm_shop", shop_state, operations, name="0002")
    assert DeferredOperation.objects.get().kind == DeferredOperation.Kind.DROP_TRIGGER

    unapply_operations("dm_shop", shop_state, operations, name="0002")
    assert not DeferredOperation.objects.exists()
