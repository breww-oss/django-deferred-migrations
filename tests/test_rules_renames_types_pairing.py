import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.migration import Migration

from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import BackfillNotNull
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import InstallNotNullFill
from deferred_migrations.operations import SetNotNull
from deferred_migrations.safety.rules import is_allowed_type_change
from deferred_migrations.safety.rules import parse_type
from deferred_migrations.safety.walker import check_graph
from tests.safety_graph import build_graph
from tests.safety_graph import make_migration


def base() -> list:
    return [
        make_migration("deferred_migrations", "0001_initial", []),
        make_migration(
            "shop",
            "0001_initial",
            [
                migrations.CreateModel("Customer", [("id", models.BigAutoField(primary_key=True)), ("name", models.CharField(max_length=50)), ("code", models.CharField(max_length=10, db_index=True))]),
                migrations.CreateModel(
                    "Order", [("id", models.BigAutoField(primary_key=True)), ("amount", models.DecimalField(max_digits=10, decimal_places=2)), ("pence", models.BigIntegerField(null=True)), ("labels", models.ManyToManyField("shop.Customer", db_table="shop_labels", related_name="+"))]
                ),
            ],
        ),
        make_migration("crm", "0001_initial", [migrations.CreateModel("Campaign", [("id", models.BigAutoField(primary_key=True)), ("customers", models.ManyToManyField("shop.Customer"))])], [("shop", "0001_initial")]),
    ]


def ids(*extra: object) -> list[str]:
    graph = build_graph([*base(), *extra])
    checked = {(migration.app_label, migration.name) for migration in extra}
    return sorted(finding.rule_id for finding in check_graph(graph, lambda key: key in checked, connection))


def shop(name: str, operations: list, dependencies: list | None = None, atomic: bool = True) -> object:
    return make_migration("shop", name, operations, dependencies or [("shop", "0001_initial"), ("deferred_migrations", "0001_initial")], atomic=atomic)


def test_e005_rename_field_changing_the_column() -> None:
    assert ids(shop("0002", [migrations.RenameField("customer", "name", "full_name")])) == ["E005"]


def test_e005_rename_field_keeping_db_column_is_allowed() -> None:
    assert ids(shop("0002", [migrations.AlterField("customer", "name", models.CharField(max_length=50, db_column="name")), migrations.RenameField("customer", "name", "full_name")])) == []


def test_e005_rename_model_keeping_db_table_still_renames_incoming_m2m_columns() -> None:
    assert ids(shop("0002", [migrations.AlterModelTable("customer", "shop_customer"), migrations.RenameModel("Customer", "Client")])) == ["E005"]


def test_e005_m2m_db_table_change() -> None:
    assert ids(shop("0002", [migrations.AlterField("order", "labels", models.ManyToManyField("shop.Customer", db_table="order_labels", related_name="+"))])) == ["E005"]


def test_e005_rename_of_m2m_field_with_explicit_db_table_is_allowed() -> None:
    assert ids(shop("0002", [migrations.RenameField("order", "labels", "tags")])) == []


@pytest.mark.parametrize(
    ("old", "new", "indexed", "allowed"),
    [
        ("varchar(10)", "varchar(20)", False, True),
        ("varchar(20)", "varchar(10)", False, False),
        ("varchar(10)", "varchar", False, True),
        ("varchar(10)", "text", False, True),
        ("varchar(10)", "text", True, False),
        ("numeric(10, 2)", "numeric(12, 2)", False, True),
        ("numeric(10, 2)", "numeric(12, 3)", False, False),
        ("integer", "bigint", False, False),
        ("text", "text", True, True),
    ],
)
def test_type_change_allow_list(old: str, new: str, indexed: bool, allowed: bool) -> None:
    assert is_allowed_type_change(old, new, indexed) is allowed


def test_parse_type_keeps_only_the_numeric_arguments() -> None:
    assert parse_type("geometry(Point,4326)") == ("geometry", [4326])


def test_e006_type_change_that_rewrites() -> None:
    assert ids(shop("0002", [migrations.AlterField("order", "pence", models.IntegerField(null=True))])) == ["E006"]


def test_e006_numeric_widening_is_allowed() -> None:
    assert ids(shop("0002", [migrations.AlterField("order", "amount", models.DecimalField(max_digits=14, decimal_places=2))])) == []


def install() -> InstallColumnSync:
    return InstallColumnSync("order", from_field="amount", to_field="pence", forwards_sql="round({from} * 100)::bigint", backwards_sql=None)


def backfill(sql: str = "round({from} * 100)::bigint") -> BackfillColumnSync:
    return BackfillColumnSync("order", from_field="amount", to_field="pence", forwards_sql=sql)


def test_e007_valid_pairing() -> None:
    assert ids(shop("0002", [install()]), shop("0003", [backfill()], [("shop", "0002")], atomic=False)) == []


def test_e007_install_without_backfill() -> None:
    assert ids(shop("0002", [install()])) == ["E007"]


def test_e007_backfill_in_an_atomic_migration_or_same_migration() -> None:
    assert ids(shop("0002", [install()]), shop("0003", [backfill()], [("shop", "0002")])) == ["E007"]
    assert ids(shop("0002", [install(), backfill()], atomic=False)) == ["E007", "E007", "E106"]


def test_e007_backfill_sql_mismatch() -> None:
    assert ids(shop("0002", [install()]), shop("0003", [backfill("{from} * 100")], [("shop", "0002")], atomic=False)) == ["E007"]


def test_e007_sync_target_constraints() -> None:
    assert "E007" in ids(shop("0002", [migrations.AlterField("order", "pence", models.BigIntegerField(null=True, db_default=0)), install()]), shop("0003", [backfill()], [("shop", "0002")], atomic=False))


def test_e007_not_null_fill_pairing() -> None:
    fill = InstallNotNullFill("order", "pence", fill_sql="0")
    rest = [BackfillNotNull("order", "pence", fill_sql="0"), SetNotNull("order", "pence", models.BigIntegerField())]

    assert ids(shop("0002", [fill]), shop("0003", rest, [("shop", "0002")], atomic=False)) == []
    assert ids(shop("0003", rest, atomic=False)) == ["E007", "E007"]


def test_e007_set_not_null_before_its_backfill() -> None:
    fill = InstallNotNullFill("order", "pence", fill_sql="0")
    set_not_null = SetNotNull("order", "pence", models.BigIntegerField())
    backfill_nulls = BackfillNotNull("order", "pence", fill_sql="0")

    assert ids(shop("0002", [fill]), shop("0003", [set_not_null], [("shop", "0002")], atomic=False), shop("0004", [backfill_nulls], [("shop", "0003")], atomic=False)) == ["E007"]
    assert ids(shop("0002", [fill]), shop("0003", [backfill_nulls], [("shop", "0002")], atomic=False), shop("0004", [set_not_null], [("shop", "0003")], atomic=False)) == []


# A primary key also tightens the column to NOT NULL, so E004 is correct here alongside the index build.
def test_e102_primary_key_added_to_an_existing_column() -> None:
    assert ids(shop("0002", [migrations.AlterField("order", "pence", models.BigIntegerField(primary_key=True))])) == ["E004", "E102"]


def test_e007_not_null_sync_target_added_with_a_preserved_constant_default() -> None:
    add = migrations.AddField("order", "cents", models.BigIntegerField(default=0))
    install = InstallColumnSync("order", from_field="amount", to_field="cents", forwards_sql="round({from} * 100)::bigint", backwards_sql=None)
    backfill_cents = BackfillColumnSync("order", from_field="amount", to_field="cents", forwards_sql="round({from} * 100)::bigint")

    assert ids(shop("0002", [add, install]), shop("0003", [backfill_cents], [("shop", "0002")], atomic=False)) == []


def identity_rename(forwards_sql: str, source: str = "amount", target_field: models.Field | None = None) -> list[Migration]:
    target = target_field or models.DecimalField(max_digits=10, decimal_places=2, null=True)
    final = target.clone()
    final.null = False
    add = migrations.AddField("order", "total", target)
    install = InstallColumnSync("order", from_field=source, to_field="total", forwards_sql=forwards_sql, backwards_sql="{to}")
    follow_up = [BackfillColumnSync("order", from_field=source, to_field="total", forwards_sql=forwards_sql), SetNotNull("order", "total", final)]
    return [shop("0002", [add, install]), shop("0003", follow_up, [("shop", "0002")], atomic=False)]


def test_e007_set_not_null_after_an_identity_sync_backfill_needs_no_fill_trigger() -> None:
    assert ids(*identity_rename("{from}")) == []


def test_e007_set_not_null_after_a_non_identity_sync_still_needs_a_fill_trigger() -> None:
    assert ids(*identity_rename("NULLIF({from}, 0)")) == ["E007"]


def test_e007_set_not_null_after_an_identity_sync_needs_a_not_null_source() -> None:
    assert ids(*identity_rename("{from}", source="pence", target_field=models.BigIntegerField(null=True))) == ["E007"]


# Once a generated rename has shipped, every later check walks it unchecked, where there is no before_models snapshot to read.
def test_an_applied_identity_rename_walks_cleanly_when_a_later_migration_is_checked() -> None:
    later = shop("0004", [migrations.AddField("order", "memo", models.TextField(null=True))], [("shop", "0003")])
    graph = build_graph([*base(), *identity_rename("{from}"), later])

    assert check_graph(graph, lambda key: key == ("shop", "0004"), connection) == []
