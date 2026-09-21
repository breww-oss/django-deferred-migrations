import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.models import Case
from django.db.models import F
from django.db.models import Q
from django.db.models import Value
from django.db.models import When
from django.db.models.expressions import RawSQL
from django.db.models.functions import Lower

from deferred_migrations.operations import DeferredDeleteModel
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.operations import InstallColumnSync
from deferred_migrations.operations import SetNotNull
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
                migrations.CreateModel(
                    "Order",
                    [
                        ("id", models.BigAutoField(primary_key=True)),
                        ("price", models.IntegerField()),
                        ("qty", models.IntegerField()),
                        ("note", models.TextField(null=True)),
                        ("status", models.TextField(db_default="new")),
                        ("total", models.GeneratedField(expression=F("price") * F("qty"), output_field=models.IntegerField(), db_persist=False)),
                        ("parent", models.ForeignKey("shop.Order", on_delete=models.CASCADE, null=True)),
                        ("tags", models.ManyToManyField("shop.Order")),
                    ],
                ),
                migrations.CreateModel("VipOrder", [], options={"proxy": True}, bases=("shop.order",)),
            ],
        ),
    ]


def ids(operations: list, atomic: bool = True) -> list[str]:
    migration = make_migration("shop", "0002", operations, [("shop", "0001_initial"), ("deferred_migrations", "0001_initial")], atomic=atomic)
    return sorted(finding.rule_id for finding in check_graph(build_graph([*base(), migration]), lambda key: key == ("shop", "0002"), connection))


def test_e001_plain_remove_field() -> None:
    assert ids([migrations.RemoveField("order", "note")]) == ["E001"]


def test_e001_allows_deferred_remove_field() -> None:
    assert ids([DeferredRemoveField("order", "note")]) == []


def test_e001_allows_remove_and_readd_of_the_same_column_in_an_atomic_migration() -> None:
    assert ids([migrations.RemoveField("order", "total"), migrations.AddField("order", "total", models.GeneratedField(expression=F("price") * F("qty"), output_field=models.BigIntegerField(), db_persist=False))]) == []


def test_e001_flags_remove_and_readd_in_a_non_atomic_migration() -> None:
    assert ids([migrations.RemoveField("order", "total"), migrations.AddField("order", "total", models.GeneratedField(expression=F("price") * F("qty"), output_field=models.BigIntegerField(), db_persist=False))], atomic=False) == ["E001", "E106", "E106"]


def test_e001_flags_remove_and_readd_that_changes_the_column_type() -> None:
    assert ids([migrations.RemoveField("order", "note"), migrations.AddField("order", "note", models.IntegerField(null=True))]) == ["E001"]


def test_e001_allows_remove_and_readd_that_keeps_the_column_type() -> None:
    assert ids([migrations.RemoveField("order", "note"), migrations.AddField("order", "note", models.TextField(null=True))]) == []


def test_e001_flags_remove_and_readd_of_a_foreign_key() -> None:
    assert ids([migrations.RemoveField("order", "parent"), migrations.AddField("order", "parent", models.ForeignKey("shop.Order", on_delete=models.CASCADE, null=True))]) == ["E001", "E102"]


def test_e001_flags_a_remove_field_nested_in_separate_database_and_state() -> None:
    remove = migrations.RemoveField("order", "note")
    assert ids([migrations.SeparateDatabaseAndState(database_operations=[remove], state_operations=[remove])]) == ["E001"]


def test_e002_flags_a_delete_model_nested_in_separate_database_and_state() -> None:
    nested = [migrations.RemoveField("order", "tags"), migrations.DeleteModel("order")]
    assert ids([migrations.SeparateDatabaseAndState(database_operations=nested, state_operations=nested)]) == ["E001", "E002"]


def test_a_state_only_separate_database_and_state_is_not_flagged() -> None:
    assert ids([migrations.SeparateDatabaseAndState(state_operations=[migrations.RemoveField("order", "note")])]) == []


def test_e002_plain_delete_model_but_not_proxies() -> None:
    assert ids([migrations.DeleteModel("viporder")]) == []
    assert ids([migrations.RemoveField("order", "tags"), DeferredDeleteModel("viporder"), migrations.DeleteModel("order")]) == ["E001", "E002"]


def test_e003_not_null_add_without_db_default() -> None:
    assert ids([migrations.AddField("order", "ref", models.CharField(max_length=5, default="x"))]) == ["E003"]


def test_e003_exemptions() -> None:
    assert ids([migrations.AddField("order", "ref", models.CharField(max_length=5, db_default=Value("x")))]) == []
    assert ids([migrations.AddField("order", "ref", models.CharField(max_length=5, null=True))]) == []
    assert ids([migrations.AddField("order", "links", models.ManyToManyField("shop.Order"))]) == []
    assert ids([migrations.AddField("order", "double", models.GeneratedField(expression=F("price") * 2, output_field=models.IntegerField(), db_persist=False))]) == []


def test_e003_sync_target_exemption_needs_a_constant_default() -> None:
    install = InstallColumnSync("order", from_field="price", to_field="pence", forwards_sql="{from} * 100", backwards_sql=None)

    assert "E003" not in ids([migrations.AddField("order", "pence", models.IntegerField(default=0), preserve_default=False), install])
    assert "E003" not in ids([migrations.AddField("order", "pence", models.IntegerField(default=0)), install])
    assert "E003" in ids([migrations.AddField("order", "pence", models.IntegerField(default=int)), install])
    assert "E003" in ids([migrations.AddField("order", "pence", models.IntegerField(default=0))])


def test_e003_removing_db_default_from_a_not_null_field() -> None:
    assert ids([migrations.AlterField("order", "status", models.TextField())]) == ["E003"]
    assert ids([migrations.AlterField("order", "status", models.TextField(null=True))]) == []


def test_e003_ignores_models_created_in_the_same_migration() -> None:
    assert ids([migrations.CreateModel("Line", [("id", models.BigAutoField(primary_key=True))]), migrations.AddField("line", "qty", models.IntegerField())]) == []


def test_e004_tightening_to_not_null() -> None:
    assert ids([migrations.AlterField("order", "note", models.TextField())]) == ["E004"]
    assert "E004" not in ids([SetNotNull("order", "note", models.TextField())], atomic=False)


def test_e011_deferred_removal_of_a_field_a_generated_field_uses() -> None:
    assert ids([DeferredRemoveField("order", "qty")]) == ["E011"]


def test_e011_field_referenced_only_in_a_case_when_condition() -> None:
    add_flag = migrations.AddField("order", "flag", models.GeneratedField(expression=Case(When(status="pending", then=Value(1)), default=Value(0), output_field=models.IntegerField()), output_field=models.IntegerField(), db_persist=False))
    assert ids([add_flag, DeferredRemoveField("order", "status")]) == ["E011"]


def test_e011_handles_a_q_object_holding_a_bare_expression() -> None:
    condition = Q(RawSQL("price > 0", []), status="pending")
    add_flag = migrations.AddField("order", "flag", models.GeneratedField(expression=Case(When(condition, then=Value(1)), default=Value(0), output_field=models.IntegerField()), output_field=models.IntegerField(), db_persist=False))
    assert ids([add_flag, DeferredRemoveField("order", "status")]) == ["E011"]


def test_e011_field_referenced_only_through_a_nested_function_in_a_when_value() -> None:
    add_flag = migrations.AddField("order", "flag", models.GeneratedField(expression=Case(When(Q(status=Lower("note")), then=Value(1)), default=Value(0), output_field=models.IntegerField()), output_field=models.IntegerField(), db_persist=False))
    assert ids([add_flag, DeferredRemoveField("order", "note")]) == ["E011"]


# The shape an interactive "no" to the rename question leaves behind: the new column starts empty and the old one is dropped after deploy.
def test_e014_a_removal_and_an_identically_defined_addition() -> None:
    assert ids([DeferredRemoveField("order", "note"), migrations.AddField("order", "memo", models.TextField(null=True))]) == ["E014"]
    assert ids([migrations.AddField("order", "memo", models.TextField(null=True)), migrations.RemoveField("order", "note")]) == ["E001", "E014"]


def test_e014_not_for_a_differently_defined_addition_or_a_readd_of_the_same_name() -> None:
    assert ids([DeferredRemoveField("order", "note"), migrations.AddField("order", "memo", models.CharField(max_length=20, null=True))]) == []
    assert ids([migrations.RemoveField("order", "note"), migrations.AddField("order", "note", models.TextField(null=True))]) == []


@pytest.mark.parametrize(("new_field_name", "expected"), [("body", ["E014"]), ("text", [])])
def test_e014_a_deleted_model_and_an_identically_defined_new_one(new_field_name: str, expected: list[str]) -> None:
    draft = make_migration("shop", "0002", [migrations.CreateModel("Draft", [("id", models.BigAutoField(primary_key=True)), ("body", models.TextField())])], [("shop", "0001_initial"), ("deferred_migrations", "0001_initial")])
    swap = make_migration("shop", "0003", [DeferredDeleteModel("Draft"), migrations.CreateModel("Sketch", [("id", models.BigAutoField(primary_key=True)), (new_field_name, models.TextField())])], [("shop", "0002")])

    findings = check_graph(build_graph([*base(), draft, swap]), lambda key: key == ("shop", "0003"), connection)

    assert sorted(finding.rule_id for finding in findings) == expected


@pytest.mark.parametrize(("allowed", "expected"), [({}, ["E014"]), ({"E014": "memo holds new content; the old notes are obsolete."}, [])])
def test_e014_can_be_suppressed_when_the_replacement_is_intentional(allowed: dict[str, str], expected: list[str]) -> None:
    migration = make_migration("shop", "0002", [DeferredRemoveField("order", "note"), migrations.AddField("order", "memo", models.TextField(null=True))], [("shop", "0001_initial"), ("deferred_migrations", "0001_initial")], deploy_safety_allowed=allowed)

    assert [finding.rule_id for finding in check_graph(build_graph([*base(), migration]), lambda key: key == ("shop", "0002"), connection)] == expected
