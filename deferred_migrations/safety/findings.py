from dataclasses import dataclass

DOCS = "https://github.com/breww-oss/django-deferred-migrations/blob/main/README.md"
RULE_IDS = frozenset({"E001", "E002", "E003", "E004", "E005", "E006", "E007", "E008", "E009", "E010", "E011", "E012", "E013", "E014", "E101", "E102", "E103", "E104", "E105", "E106"})


@dataclass(frozen=True)
class Finding:
    rule_id: str
    app_label: str
    migration_name: str
    operation_index: int | None
    message: str

    def format(self) -> str:
        location = f"{self.app_label}.{self.migration_name}" if self.operation_index is None else f"{self.app_label}.{self.migration_name}[{self.operation_index}]"
        return f"{location} deferred_migrations.{self.rule_id}: {self.message} (see {DOCS}#{self.rule_id.lower()})"
