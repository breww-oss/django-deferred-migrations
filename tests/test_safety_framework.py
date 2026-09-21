from django.db import connection
from django.db import migrations
from django.db import models

from deferred_migrations.operations import AddFieldConcurrently
from deferred_migrations.operations import DeferredRemoveField
from deferred_migrations.safety.walker import check_graph
from tests.safety_graph import build_graph
from tests.safety_graph import make_migration


def rule_ids(findings: list) -> list[str]:
    return sorted(finding.rule_id for finding in findings)


def initial() -> list:
    return [
        make_migration("deferred_migrations", "0001_initial", []),
        make_migration("shop", "0001_initial", [migrations.CreateModel("Order", [("id", models.BigAutoField(primary_key=True)), ("note", models.TextField(null=True))])]),
    ]


def test_e008_package_operation_without_queue_dependency() -> None:
    graph = build_graph([*initial(), make_migration("shop", "0002", [DeferredRemoveField("order", "note")], [("shop", "0001_initial")])])

    assert rule_ids(check_graph(graph, lambda key: key == ("shop", "0002"), connection)) == ["E008"]


def test_e008_satisfied_by_the_dependency() -> None:
    graph = build_graph([*initial(), make_migration("shop", "0002", [DeferredRemoveField("order", "note")], [("shop", "0001_initial"), ("deferred_migrations", "0001_initial")])])

    assert check_graph(graph, lambda key: key == ("shop", "0002"), connection) == []


def test_e008_skipped_when_the_package_has_no_migrations_in_the_graph() -> None:
    graph = build_graph([initial()[1], make_migration("shop", "0002", [DeferredRemoveField("order", "note")], [("shop", "0001_initial")])])

    assert check_graph(graph, lambda key: key == ("shop", "0002"), connection) == []


def test_unchecked_migrations_are_not_reported() -> None:
    graph = build_graph([*initial(), make_migration("shop", "0002", [DeferredRemoveField("order", "note")], [("shop", "0001_initial")])])

    assert check_graph(graph, lambda key: False, connection) == []
    assert rule_ids(check_graph(graph, lambda key: True, connection)) == ["E008"]


def test_suppression_hides_a_rule_with_a_reason() -> None:
    graph = build_graph([*initial(), make_migration("shop", "0002", [DeferredRemoveField("order", "note")], [("shop", "0001_initial")], deploy_safety_allowed={"E008": "The queue table is created by a squashed migration."})])

    assert check_graph(graph, lambda key: key == ("shop", "0002"), connection) == []


def test_e009_empty_reason_or_unknown_rule() -> None:
    graph = build_graph([*initial(), make_migration("shop", "0002", [], [("shop", "0001_initial")], deploy_safety_allowed={"E001": " ", "E999": "because"})])

    assert rule_ids(check_graph(graph, lambda key: key == ("shop", "0002"), connection)) == ["E009", "E009"]


def test_e008_add_field_concurrently_without_queue_dependency() -> None:
    graph = build_graph([*initial(), make_migration("shop", "0002", [AddFieldConcurrently("order", "memo", models.TextField(null=True))], [("shop", "0001_initial")], atomic=False)])

    assert rule_ids(check_graph(graph, lambda key: key == ("shop", "0002"), connection)) == ["E008"]


def test_e008_add_field_concurrently_satisfied_by_the_dependency() -> None:
    graph = build_graph([*initial(), make_migration("shop", "0002", [AddFieldConcurrently("order", "memo", models.TextField(null=True))], [("shop", "0001_initial"), ("deferred_migrations", "0001_initial")], atomic=False)])

    assert check_graph(graph, lambda key: key == ("shop", "0002"), connection) == []
