from django.contrib.postgres.functions import RandomUUID
from django.contrib.postgres.operations import AddConstraintNotValid
from django.contrib.postgres.operations import AddIndexConcurrently as DjangoAddIndexConcurrently
from django.contrib.postgres.operations import RemoveIndexConcurrently
from django.contrib.postgres.operations import ValidateConstraint
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.models import F
from django.db.models import Q
from django.db.models import Value
from django.db.models.functions import Now

from deferred_migrations.operations import AddConstraintConcurrently
from deferred_migrations.operations import AddFieldConcurrently
from deferred_migrations.operations import AddIndexConcurrently
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.safety.walker import check_graph
from tests.safety_graph import build_graph
from tests.safety_graph import make_migration


def ids(operations: list, atomic: bool = True, **attributes: object) -> list[str]:
    base = [
        make_migration("deferred_migrations", "0001_initial", []),
        make_migration(
            "shop",
            "0001_initial",
            [
                migrations.CreateModel("Order", [("id", models.BigAutoField(primary_key=True)), ("code", models.CharField(max_length=10)), ("price", models.IntegerField())]),
                migrations.AddField("order", "idx_field", models.CharField(max_length=10, db_index=True)),
                migrations.AddField("order", "uniq_field", models.CharField(max_length=10, unique=True)),
                migrations.AddField("order", "fk_a", models.ForeignKey("shop.Order", models.CASCADE, null=True, db_constraint=False)),
                migrations.AddField("order", "fk_b", models.ForeignKey("shop.Order", models.CASCADE, null=True, db_index=False)),
            ],
        ),
    ]
    migration = make_migration("shop", "0002", operations, [("shop", "0001_initial"), ("deferred_migrations", "0001_initial")], atomic=atomic, **attributes)
    return sorted(finding.rule_id for finding in check_graph(build_graph([*base, migration]), lambda key: key == ("shop", "0002"), connection))


def test_e101_add_index_but_not_concurrently() -> None:
    assert ids([migrations.AddIndex("order", models.Index(fields=["code"], name="order_code"))]) == ["E101"]
    assert ids([AddIndexConcurrently("order", models.Index(fields=["code"], name="order_code"))], atomic=False) == []


def test_e102_fields_that_create_indexes() -> None:
    assert ids([migrations.AddField("order", "ref", models.CharField(max_length=5, null=True, db_index=True))]) == ["E102"]
    assert ids([migrations.AddField("order", "parent", models.ForeignKey("shop.Order", models.CASCADE, null=True))]) == ["E102"]
    assert ids([migrations.AlterField("order", "code", models.CharField(max_length=10, unique=True))]) == ["E102"]


def test_e102_string_to_text_only_when_indexed() -> None:
    assert ids([migrations.AlterField("order", "code", models.TextField(db_index=True))]) == ["E102"]
    assert ids([migrations.AlterField("order", "code", models.TextField())]) == []


def test_e102_alterfield_indexed_field_becomes_unique() -> None:
    assert ids([migrations.AlterField("order", "idx_field", models.CharField(max_length=10, unique=True))]) == ["E102"]


def test_e102_alterfield_unique_field_becomes_plain_index() -> None:
    assert ids([migrations.AlterField("order", "uniq_field", models.CharField(max_length=10, db_index=True, unique=False))]) == ["E102"]


def test_e102_alterfield_fk_constraint_added() -> None:
    assert ids([migrations.AlterField("order", "fk_a", models.ForeignKey("shop.Order", models.CASCADE, null=True, db_constraint=True))]) == ["E102"]


def test_e102_alterfield_fk_index_added() -> None:
    assert ids([migrations.AlterField("order", "fk_b", models.ForeignKey("shop.Order", models.CASCADE, null=True, db_index=True))]) == ["E102"]


def test_e102_alterfield_non_database_attribute_changes_are_not_flagged() -> None:
    assert ids([migrations.AlterField("order", "idx_field", models.CharField(max_length=10, db_index=True, help_text="Updated help text"))]) == []
    assert ids([migrations.AlterField("order", "fk_b", models.ForeignKey("shop.Order", models.PROTECT, null=True, db_index=False, help_text="Updated help text"))]) == []


def test_e103_constraints_on_existing_models() -> None:
    assert ids([migrations.AddConstraint("order", models.CheckConstraint(condition=Q(price__gte=0), name="price_positive"))]) == ["E103"]
    assert ids([AddConstraintNotValid("order", models.CheckConstraint(condition=Q(price__gte=0), name="price_positive"))]) == []
    assert ids([migrations.AlterUniqueTogether("order", {("code", "price")})]) == ["E103"]


def test_e104_table_rewrites_on_add_field_only() -> None:
    assert ids([migrations.AddField("order", "token", models.UUIDField(db_default=RandomUUID()))]) == ["E104"]
    assert ids([migrations.AddField("order", "made", models.DateTimeField(db_default=Now()))]) == []
    assert ids([migrations.AddField("order", "flag", models.BooleanField(db_default=Value(True)))]) == []
    assert ids([migrations.AddField("order", "flag", models.BooleanField(db_default=False))]) == []
    assert ids([migrations.AddField("order", "channel", models.CharField(max_length=5, db_default="web"))]) == []
    assert ids([migrations.AddField("order", "double", models.GeneratedField(expression=F("price") * 2, output_field=models.IntegerField(), db_persist=True))]) == ["E104"]
    assert ids([migrations.AddField("order", "token", models.UUIDField(null=True)), migrations.AlterField("order", "token", models.UUIDField(null=True, db_default=RandomUUID()))]) == []


def test_the_concurrent_operations_are_exempt_from_e101_to_e103() -> None:
    assert ids([AddFieldConcurrently("order", "parent", models.ForeignKey("shop.Order", models.CASCADE, null=True))], atomic=False) == []
    assert ids([AddFieldConcurrently("order", "ref", models.CharField(max_length=5, null=True, unique=True))], atomic=False) == []
    assert ids([AddConstraintConcurrently("order", models.UniqueConstraint(fields=["code"], name="order_code_uniq"))], atomic=False) == []
    assert ids([AddConstraintConcurrently("order", models.CheckConstraint(condition=Q(price__gte=0), name="price_positive"))], atomic=False) == []


def test_add_field_concurrently_still_gets_e003_for_a_not_null_column_without_a_default() -> None:
    assert ids([AddFieldConcurrently("order", "ref", models.CharField(max_length=5, db_index=True))], atomic=False) == ["E003"]


def test_e105_a_concurrent_operation_in_an_atomic_migration() -> None:
    assert ids([AddFieldConcurrently("order", "ref", models.CharField(max_length=5, null=True, db_index=True))]) == ["E105"]
    assert ids([AddConstraintConcurrently("order", models.UniqueConstraint(fields=["code"], name="order_code_uniq"))]) == ["E105"]
    assert ids([AddIndexConcurrently("order", models.Index(fields=["code"], name="order_code"))]) == ["E105"]
    assert ids([RemoveIndexConcurrently("order", "order_code")]) == ["E105"]


def test_e106_an_operation_that_cannot_be_rerun_in_a_non_atomic_migration() -> None:
    assert ids([migrations.RemoveConstraint("order", "missing"), AddFieldConcurrently("order", "ref", models.CharField(max_length=5, null=True))], atomic=False) == ["E106"]
    assert ids([DjangoAddIndexConcurrently("order", models.Index(fields=["code"], name="order_code"))], atomic=False) == ["E106"]
    assert ids([AddConstraintNotValid("order", models.CheckConstraint(condition=Q(price__gte=0), name="price_positive"))], atomic=False) == ["E106"]
    assert ids([migrations.AddField("order", "note", models.TextField(null=True))], atomic=False) == ["E106"]


def test_e106_accepts_every_operation_that_can_be_rerun() -> None:
    safe = [
        AddIndexConcurrently("order", models.Index(fields=["code"], name="order_code")),
        ValidateConstraint("order", "price_positive"),
        migrations.RunSQL("SELECT 1", migrations.RunSQL.noop),
        migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop),
        migrations.AlterModelOptions("order", {"verbose_name": "Order"}),
        migrations.SeparateDatabaseAndState(state_operations=[migrations.AddField("order", "note", models.TextField(null=True))], database_operations=[migrations.RunSQL('ALTER TABLE "shop_order" ADD COLUMN IF NOT EXISTS "note" text NULL', migrations.RunSQL.noop)]),
    ]

    assert ids(safe, atomic=False) == []


def test_e106_flags_a_hand_written_0159_shape() -> None:
    operations = [
        migrations.RemoveConstraint("order", "missing"),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AddField("order", "vessel", models.IntegerField(null=True))],
            database_operations=[migrations.RunSQL('ALTER TABLE "shop_order" ADD COLUMN IF NOT EXISTS "vessel" integer NULL', migrations.RunSQL.noop)],
        ),
    ]

    assert ids(operations, atomic=False) == ["E106"]


def test_e106_does_not_flag_a_deferred_removal() -> None:
    assert ids([DeferredRemoveField("order", "price")], atomic=False) == []


def test_add_field_concurrently_still_gets_e104_for_a_volatile_db_default() -> None:
    assert ids([AddFieldConcurrently("order", "token", models.UUIDField(null=True, db_default=RandomUUID()))], atomic=False) == ["E104"]


def test_e105_and_e106_can_be_suppressed_with_a_reason() -> None:
    assert ids([migrations.AddField("order", "note", models.TextField(null=True))], atomic=False, deploy_safety_allowed={"E106": "The table has three rows."}) == []
    assert ids([AddIndexConcurrently("order", models.Index(fields=["code"], name="order_code"))], deploy_safety_allowed={"E105": "Checked by hand."}) == []
