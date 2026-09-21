from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

from deferred_migrations.safety.names import PhysicalNameKey
from deferred_migrations.safety.names import has_own_table
from deferred_migrations.safety.names import physical_names


def state_from(*apps_and_operations: tuple[str, list[Operation]]) -> ProjectState:
    state = ProjectState()

    for app_label, operations in apps_and_operations:
        for operation in operations:
            operation.state_forwards(app_label, state)

    return state


def django_names(state: ProjectState) -> dict[PhysicalNameKey, str]:
    names: dict[PhysicalNameKey, str] = {}

    for (app_label, model_name), model_state in state.models.items():
        if not has_own_table(model_state):
            continue

        model = state.apps.get_model(app_label, model_name)
        names[PhysicalNameKey("table", app_label, model_name)] = model._meta.db_table

        for field in model._meta.local_fields:
            if field.column is not None:
                names[PhysicalNameKey("column", app_label, model_name, field.name)] = field.column

        for field in model._meta.local_many_to_many:
            if not field.remote_field.through._meta.auto_created:
                continue

            names[PhysicalNameKey("m2m_table", app_label, model_name, field.name)] = field.m2m_db_table()
            names[PhysicalNameKey("m2m_column", app_label, model_name, field.name, "from")] = field.m2m_column_name()
            names[PhysicalNameKey("m2m_column", app_label, model_name, field.name, "to")] = field.m2m_reverse_name()

    return names


def test_every_physical_name_matches_the_one_django_renders() -> None:
    state = state_from(
        ("other_app", [migrations.CreateModel("Order", [("id", models.BigAutoField(primary_key=True))])]),
        (
            "shop",
            [
                migrations.CreateModel("Customer", [("id", models.BigAutoField(primary_key=True))]),
                migrations.CreateModel(
                    "Order",
                    [
                        ("id", models.BigAutoField(primary_key=True)),
                        ("customer", models.ForeignKey("shop.Customer", models.CASCADE)),
                        ("ref", models.CharField(max_length=5, db_column="reference")),
                        ("tags", models.ManyToManyField("shop.Customer", related_name="+")),
                        ("peers", models.ManyToManyField("self")),
                        ("related", models.ManyToManyField("other_app.Order", related_name="+")),
                        ("labels", models.ManyToManyField("shop.Customer", db_table="order_labels", related_name="+")),
                        ("members", models.ManyToManyField("shop.Customer", through="shop.Membership", related_name="+")),
                    ],
                    options={"db_table": "orders"},
                ),
                migrations.CreateModel("Membership", [("id", models.BigAutoField(primary_key=True)), ("order", models.ForeignKey("shop.Order", models.CASCADE)), ("customer", models.ForeignKey("shop.Customer", models.CASCADE))]),
                migrations.CreateModel("Line", [("pk", models.CompositePrimaryKey("order_id", "number")), ("order", models.ForeignKey("shop.Order", models.CASCADE)), ("number", models.IntegerField())]),
                migrations.CreateModel("VipOrder", [], options={"proxy": True}, bases=("shop.order",)),
                migrations.CreateModel("A" + "b" * 70, [("id", models.BigAutoField(primary_key=True)), ("friends", models.ManyToManyField("shop.Customer", related_name="+"))]),
            ],
        ),
    )

    assert physical_names(state, connection.ops.max_name_length()) == django_names(state)
