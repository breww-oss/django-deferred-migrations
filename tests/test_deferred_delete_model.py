import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.migration import Migration
from django.db.migrations.state import ProjectState

from deferred_migrations.models import DeferredOperation
from deferred_migrations.operations import DeferredDeleteModel
from deferred_migrations.operations import DeferredRemoveField
from tests.migration_helpers import apply_operations
from tests.migration_helpers import foreign_key_constraint_names
from tests.migration_helpers import table_exists
from tests.migration_helpers import unapply_operations


@pytest.mark.django_db
def test_outgoing_foreign_keys_are_dropped_and_old_style_inserts_still_work(shop_state: ProjectState) -> None:
    state = apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "tags")], name="0002")
    apply_operations("dm_shop", state, [DeferredDeleteModel("order")], name="0003")

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO dm_shop_customer (name) VALUES ('Acme') RETURNING id")
        customer_id = cursor.fetchone()[0]
        cursor.execute("INSERT INTO dm_shop_order (customer_id, reference, status, code, amount, created) VALUES (%s, 'R1', 'new', 'c1', 0, now())", [customer_id])
        cursor.execute("DELETE FROM dm_shop_customer WHERE id = %s", [customer_id])

    assert foreign_key_constraint_names("dm_shop_order", "customer_id") == []
    assert table_exists("dm_shop_order")


@pytest.mark.django_db
def test_table_and_auto_created_through_tables_are_queued_through_first(shop_state: ProjectState) -> None:
    state = apply_operations("dm_shop", shop_state, [migrations.CreateModel("Label", [("id", models.BigAutoField(primary_key=True)), ("customers", models.ManyToManyField("dm_shop.Customer"))])], name="0002")

    apply_operations("dm_shop", state, [DeferredDeleteModel("label")], name="0003")

    assert list(DeferredOperation.objects.order_by("id").values_list("table_name", flat=True)) == ["dm_shop_label_customers", "dm_shop_label"]
    assert foreign_key_constraint_names("dm_shop_label_customers", "customer_id") == []
    assert table_exists("dm_shop_label")


@pytest.mark.django_db
def test_backwards_with_a_pending_row_restores_foreign_keys(shop_state: ProjectState) -> None:
    state = apply_operations("dm_shop", shop_state, [DeferredRemoveField("order", "tags")], name="0002")
    operations = [DeferredDeleteModel("order")]
    apply_operations("dm_shop", state, operations, name="0003")

    unapply_operations("dm_shop", state, operations, name="0003")

    assert len(foreign_key_constraint_names("dm_shop_order", "customer_id")) == 1
    assert not DeferredOperation.objects.filter(migration_name="0003").exists()


@pytest.mark.django_db
def test_proxy_model_deletion_touches_nothing(shop_state: ProjectState) -> None:
    state = apply_operations("dm_shop", shop_state, [migrations.CreateModel("VipCustomer", [], options={"proxy": True}, bases=("dm_shop.customer",))], name="0002")

    apply_operations("dm_shop", state, [DeferredDeleteModel("vipcustomer")], name="0003")

    assert not DeferredOperation.objects.exists()
    assert table_exists("dm_shop_customer")


@pytest.mark.django_db
def test_unmanaged_model_field_removal_touches_nothing(shop_state: ProjectState) -> None:
    state = apply_operations("dm_shop", shop_state, [migrations.CreateModel("External", [("id", models.BigAutoField(primary_key=True)), ("value", models.TextField())], options={"managed": False, "db_table": "dm_shop_customer"})], name="0002")

    apply_operations("dm_shop", state, [DeferredRemoveField("external", "value")], name="0003")

    assert not DeferredOperation.objects.exists()


# The post-deploy run drops the through table first, so a run that stops at the model's own drop leaves the through table gone while the rest is still queued.
@pytest.mark.django_db
def test_rollback_after_a_partial_run_recreates_the_dropped_through_table(shop_state: ProjectState) -> None:
    state = apply_operations("dm_shop", shop_state, [migrations.CreateModel("Label", [("id", models.BigAutoField(primary_key=True)), ("customers", models.ManyToManyField("dm_shop.Customer"))])], name="0002")
    operations = [DeferredDeleteModel("label")]
    apply_operations("dm_shop", state, operations, name="0003")

    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE dm_shop_label_customers")

    unapply_operations("dm_shop", state, operations, name="0003")

    assert table_exists("dm_shop_label_customers")
    assert len(foreign_key_constraint_names("dm_shop_label_customers", "customer_id")) == 1
    assert not DeferredOperation.objects.filter(migration_name="0003").exists()


@pytest.mark.django_db
def test_an_unsafe_table_name_is_refused_before_any_constraint_is_dropped(shop_state: ProjectState) -> None:
    state = shop_state.clone()
    migrations.CreateModel("Weird", [("id", models.BigAutoField(primary_key=True)), ("customer", models.ForeignKey("dm_shop.Customer", on_delete=models.CASCADE))], options={"db_table": 'dm_shop_"weird'}).state_forwards("dm_shop", state)
    migration = Migration("0003_delete_weird", "dm_shop")
    migration.operations = [DeferredDeleteModel("weird")]

    with connection.schema_editor(collect_sql=True, atomic=False) as editor, pytest.raises(ValueError, match="double quote"):
        migration.apply(state, editor, collect_sql=True)

    assert not any("foreign key" in statement.lower() for statement in editor.collected_sql)
