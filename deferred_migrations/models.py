from django.db import models
from django.utils import timezone


class DeferredOperation(models.Model):
    class Kind(models.TextChoices):
        DROP_COLUMN = "drop_column", "Drop column"
        DROP_TABLE = "drop_table", "Drop table"
        DROP_TRIGGER = "drop_trigger", "Drop trigger and function"
        DROP_VIEW = "drop_view", "Drop compatibility view"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    # A trigger reading a column, or a view selecting every column, makes each other drop on its table fail or break writes until it is gone.
    BLOCKING_KINDS = frozenset({Kind.DROP_TRIGGER, Kind.DROP_VIEW})

    app_label = models.CharField(max_length=100)
    migration_name = models.CharField(max_length=255)
    operation_index = models.PositiveIntegerField()
    sequence = models.PositiveIntegerField()
    kind = models.CharField(max_length=20, choices=Kind.choices)
    table_name = models.CharField(max_length=255)
    column_name = models.CharField(max_length=255, blank=True)
    sql = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    resolution_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app_label", "migration_name", "operation_index", "sequence"], name="deferred_migrations_unique_statement"),
        ]

    def __str__(self) -> str:
        return f"{self.app_label}.{self.migration_name}[{self.operation_index}.{self.sequence}] {self.kind} {self.table_name}"


class ModelRename(models.Model):
    app_label = models.CharField(max_length=100)
    old_model = models.CharField(max_length=100)
    new_model = models.CharField(max_length=100)
    rewritten_operation_ids = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.app_label}.{self.old_model} -> {self.new_model}"
