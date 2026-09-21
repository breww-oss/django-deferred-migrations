from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation

from deferred_migrations.operations import BackfillColumnSync
from deferred_migrations.operations import DeferredRenameModel
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.safety.walker import check_graph
from tests.safety_graph import build_graph
from tests.safety_graph import make_migration


def base() -> list[Migration]:
    return [
        make_migration("deferred_migrations", "0001_initial", []),
        make_migration("deferred_migrations", "0002_modelrename", [], [("deferred_migrations", "0001_initial")]),
        make_migration(
            "shop",
            "0001_initial",
            [
                migrations.CreateModel("Gadget", [("id", models.BigAutoField(primary_key=True)), ("name", models.CharField(max_length=50)), ("amount", models.IntegerField()), ("pence", models.BigIntegerField(null=True))]),
                migrations.CreateModel("Fixed", [("id", models.BigAutoField(primary_key=True))], options={"db_table": "shop_fixed"}),
                migrations.CreateModel("Tagged", [("id", models.BigAutoField(primary_key=True)), ("gadgets", models.ManyToManyField("shop.Gadget"))]),
                migrations.CreateModel("Plain", [("id", models.BigAutoField(primary_key=True)), ("fee", models.IntegerField()), ("fee_pence", models.BigIntegerField(null=True))]),
            ],
        ),
    ]


def shop(name: str, operations: list[Operation], dependencies: list[tuple[str, str]] | None = None, atomic: bool = True) -> Migration:
    return make_migration("shop", name, operations, dependencies or [("shop", "0001_initial"), ("deferred_migrations", "0002_modelrename")], atomic=atomic)


def ids(*extra: Migration, checked: set[str] | None = None, track_renamed_in_batch: bool = True) -> list[str]:
    graph = build_graph([*base(), *extra])
    names = checked if checked is not None else {migration.name for migration in extra}
    return sorted(finding.rule_id for finding in check_graph(graph, lambda key: key[0] == "shop" and key[1] in names, connection, track_renamed_in_batch=track_renamed_in_batch))


def test_an_eligible_deferred_rename_model_passes() -> None:
    assert ids(shop("0002", [DeferredRenameModel("Plain", "Simple")])) == []


def test_a_plain_rename_model_is_still_e005() -> None:
    assert ids(shop("0002", [migrations.RenameModel("Plain", "Simple")])) == ["E005"]


def test_e012_for_a_pinned_table_or_an_auto_many_to_many() -> None:
    assert ids(shop("0002", [DeferredRenameModel("Fixed", "Moved")])) == ["E012"]
    assert ids(shop("0002", [DeferredRenameModel("Gadget", "Gizmo")])) == ["E012"]


def test_e008_needs_the_rename_record_migration() -> None:
    assert ids(shop("0002", [DeferredRenameModel("Plain", "Simple")], [("shop", "0001_initial"), ("deferred_migrations", "0001_initial")])) == ["E008"]


# A varchar widening is fine for E006, so E013 is the only finding: PostgreSQL still refuses it while the view exists.
def test_e013_type_change_on_a_model_renamed_in_the_same_deploy() -> None:
    renamed = shop("0002", [DeferredRenameModel("Plain", "Simple"), migrations.AddField("simple", "size", models.CharField(max_length=10, null=True))])
    widened = shop("0003", [migrations.AlterField("simple", "size", models.CharField(max_length=20, null=True))], [("shop", "0002")])

    assert ids(renamed, widened) == ["E013"]


# Full mode checks shipped migrations too, so it cannot tell which renames are still in this deploy.
def test_no_e013_when_deploy_boundaries_are_unknown() -> None:
    renamed = shop("0002", [DeferredRenameModel("Plain", "Simple"), migrations.AddField("simple", "size", models.CharField(max_length=10, null=True))])
    widened = shop("0003", [migrations.AlterField("simple", "size", models.CharField(max_length=20, null=True))], [("shop", "0002")])

    assert ids(renamed, widened, track_renamed_in_batch=False) == []


def test_no_e013_once_the_rename_has_shipped() -> None:
    renamed = shop("0002", [DeferredRenameModel("Plain", "Simple"), migrations.AddField("simple", "size", models.CharField(max_length=10, null=True))])
    widened = shop("0003", [migrations.AlterField("simple", "size", models.CharField(max_length=20, null=True))], [("shop", "0002")])

    assert ids(renamed, widened, checked={"0003"}) == []


def test_a_sync_install_and_its_backfill_pair_across_a_model_rename() -> None:
    install = shop("0002", [InstallColumnSync("gadget", from_field="amount", to_field="pence", forwards_sql="{from}::bigint", backwards_sql=None)])
    plain = shop("0003", [migrations.RenameModel("Plain", "Simple")], [("shop", "0002")])
    backfill = shop("0004", [BackfillColumnSync("gadget", from_field="amount", to_field="pence", forwards_sql="{from}::bigint")], [("shop", "0003")], atomic=False)

    assert ids(install, plain, backfill, checked={"0002", "0004"}) == []


def test_a_sync_install_follows_its_model_through_a_rename() -> None:
    install = shop("0002", [InstallColumnSync("plain", from_field="fee", to_field="fee_pence", forwards_sql="{from}::bigint", backwards_sql=None)])
    renamed = shop("0003", [DeferredRenameModel("Plain", "Simple")], [("shop", "0002")])
    backfill = shop("0004", [BackfillColumnSync("simple", from_field="fee", to_field="fee_pence", forwards_sql="{from}::bigint")], [("shop", "0003")], atomic=False)

    assert ids(install, renamed, backfill) == []
