from django.db.migrations.graph import MigrationGraph
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation


def make_migration(app_label: str, name: str, operations: list[Operation], dependencies: list[tuple[str, str]] | None = None, atomic: bool = True, **attributes: object) -> Migration:
    migration = type("Migration", (Migration,), {"operations": operations, "dependencies": dependencies or [], "atomic": atomic, **attributes})(name, app_label)
    return migration


def build_graph(migrations: list[Migration]) -> MigrationGraph:
    graph = MigrationGraph()

    for migration in migrations:
        graph.add_node((migration.app_label, migration.name), migration)

    for migration in migrations:
        for dependency in migration.dependencies:
            graph.add_dependency(migration, (migration.app_label, migration.name), dependency)

    graph.validate_consistency()
    return graph
