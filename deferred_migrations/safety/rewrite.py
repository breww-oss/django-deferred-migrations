import re
from dataclasses import dataclass
from dataclasses import field

DEPENDENCY_LINE = '("deferred_migrations", "0001_initial"),'
DIRECT_IMPORT = re.compile(r"^from django\.db\.migrations(?:\.operations)?(?:\.\w+)? import .*\b(RemoveField|DeleteModel)\b", re.MULTILINE)
PACKAGE_IMPORT = re.compile(r"^from deferred_migrations\.operations import (?P<names>.+)$", re.MULTILINE)
DJANGO_IMPORT = re.compile(r"^from django\.db import .*\bmigrations\b.*$", re.MULTILINE)
DEPENDENCIES_OPEN = re.compile(r"^(?P<indent>[ \t]*)dependencies = \[\n", re.MULTILINE)
DEPENDENCIES_EMPTY = re.compile(r"^(?P<indent>[ \t]*)dependencies = \[\]\n", re.MULTILINE)


@dataclass
class RewriteResult:
    source: str
    changed: bool
    problems: list[str] = field(default_factory=list)


def rewrite_migration_source(source: str, remove_field_count: int, delete_model_count: int) -> RewriteResult:
    problems: list[str] = []
    text = source

    if DIRECT_IMPORT.search(text):
        return RewriteResult(source, False, ["RemoveField/DeleteModel is imported directly; rewrite this migration by hand."])

    # A deferred operation raises ImproperlyConfigured inside SeparateDatabaseAndState, so a blind textual swap here would produce a migration that cannot be applied.
    # Deliberately a whole-text match: it also refuses a migration whose wrapper has nothing to do with the removal, and one that only mentions the name in a comment. That costs the automation, not safety, since check_deploy_safety still reports the E001.
    if (remove_field_count or delete_model_count) and "SeparateDatabaseAndState" in text:
        return RewriteResult(source, False, ["The migration uses SeparateDatabaseAndState, which the deferred operations cannot be nested in; rewrite this migration by hand."])

    replacements = {"migrations.RemoveField(": ("DeferredRemoveField(", remove_field_count), "migrations.DeleteModel(": ("DeferredDeleteModel(", delete_model_count)}
    needed: set[str] = set()

    for old, (new, expected) in replacements.items():
        if text.count(old) != expected:
            problems.append(f"Expected {expected} occurrence(s) of {old} but found {text.count(old)}; rewrite by hand.")
            continue

        if expected:
            text = text.replace(old, new)
            needed.add(new.rstrip("("))

    if problems:
        return RewriteResult(source, False, problems)

    if needed:
        if (match := PACKAGE_IMPORT.search(text)) and match.group("names").strip().startswith("("):
            return RewriteResult(source, False, ["The deferred_migrations import is parenthesised; rewrite this migration by hand."])

        if match := PACKAGE_IMPORT.search(text):
            names = sorted(needed | {name.strip() for name in match.group("names").split(",")})
            text = text[: match.start()] + chr(10).join(f"from deferred_migrations.operations import {name}" for name in names) + text[match.end() :]
        elif match := DJANGO_IMPORT.search(text):
            text = text[: match.end()] + "\n\n" + chr(10).join(f"from deferred_migrations.operations import {name}" for name in sorted(needed)) + text[match.end() :]
        else:
            return RewriteResult(source, False, ["Could not find `from django.db import migrations` to add the import after."])

    if DEPENDENCY_LINE not in text:
        if match := DEPENDENCIES_OPEN.search(text):
            indent = match.group("indent")
            text = text[: match.end()] + f"{indent}    {DEPENDENCY_LINE}\n" + text[match.end() :]
        elif match := DEPENDENCIES_EMPTY.search(text):
            indent = match.group("indent")
            text = text[: match.start()] + f"{indent}dependencies = [\n{indent}    {DEPENDENCY_LINE}\n{indent}]\n" + text[match.end() :]
        else:
            return RewriteResult(source, False, ["Could not find the dependencies list to add the deferred_migrations dependency."])

    return RewriteResult(text, text != source, [])
