---
name: fixing-deploy-safety
description: Use when check_deploy_safety or CI reports a deferred_migrations E0xx or E1xx error, when a migration fails a deploy safety check, or when asked to remove, rename, reshape or tighten (NOT NULL) a Django model field, add an index or constraint to an existing table, or delete a model in a project using django-deferred-migrations.
---

# Fixing deploy safety errors

Migrations run before new code rolls out, and old code keeps running until the rollout ends. Every rule protects that overlap: the schema must work for the old code and the new code at the same time.

## Order of work

1. The package's `makemigrations` already writes plain removals, deletions and the queue dependency deploy-safe as it generates migrations. For a migration written by hand or generated before the package's `makemigrations` was in use, run `fix_deploy_safety`, which fixes E001, E002 and E008 in it.
2. Run `check_deploy_safety`. Everything left needs a decision; use the table and recipes below.
3. Verify in two phases: `migrate_pre_deploy`, exercise the new code against that schema, then `migrate_post_deploy`.

## Rules

| Rule | What it flags | Fix |
|---|---|---|
| E001 | `RemoveField` | `DeferredRemoveField` with the same arguments |
| E002 | `DeleteModel` | `DeferredDeleteModel` |
| E003 | NOT NULL `AddField` with no `db_default`, or removing `db_default` from a NOT NULL field | Constant `db_default`, or `null=True` |
| E004 | `AlterField` from `null=True` to `null=False` | `InstallNotNullFill`, then `BackfillNotNull` + `SetNotNull` |
| E005 | Renaming a physical table or column | Re-run `makemigrations` interactively; keep the old name with `db_column` / `db_table` only for renames it cannot handle |
| E006 | Column type change that rewrites the table | `InstallColumnSync`, then `BackfillColumnSync` |
| E007 | Trigger operations split or configured wrongly | Install in an atomic migration, backfill in a later `atomic = False` one |
| E008 | Package operation without the queue dependency | Depend on `("deferred_migrations", "0001_initial")` |
| E009 | Suppression with no reason or an unknown rule | Give a real reason |
| E010 | Baseline file names a missing migration | Point it at an existing migration |
| E011 | Deferred removal of a field a `GeneratedField` uses | Remove or change the generated field first |
| E012 | `DeferredRenameModel` on an ineligible model | Plain `RenameModel` with `Meta.db_table` kept, or an explicit `through` model |
| E013 | Column type change on a model renamed earlier in the same run | Ship the type change in the next deploy |
| E014 | Removal plus an identically-defined addition (a rename that loses data) | Regenerate interactively and answer yes to the rename; suppress only if the two are unrelated |
| E101 | `AddIndex` on an existing table | `AddIndexConcurrently` in an `atomic = False` migration (`makemigrations` writes it) |
| E102 | Field add/alter that builds an index or FK constraint | `AddFieldConcurrently` for a new field (`makemigrations` writes it); `AlterField` variants: see below |
| E103 | Constraint or unique/index together on an existing table | `AddConstraintConcurrently` (`makemigrations` writes it) |
| E104 | `AddField` that rewrites the table (volatile `db_default`, stored generated column) | Nullable add, fill, `SetNotNull`, then add the `db_default` |
| E105 | A concurrent operation in an atomic migration | `atomic = False` |
| E106 | A non-atomic migration holding an operation that cannot be re-run | Move it to an atomic migration, or use the package's operation |

## Recipes

### E001: remove a field

```python
from deferred_migrations.operations import DeferredRemoveField

dependencies = [("deferred_migrations", "0001_initial"), ("sales", "0219_previous")]
operations = [DeferredRemoveField(model_name="invoice", name="legacy_ref")]
```

Exception: removing and re-adding the same column in one atomic migration (for example to change a `GeneratedField`) stays a plain `RemoveField` + `AddField`. The re-add must keep the column's `db_type` unless both fields are `GeneratedField`s, otherwise old code reads a value its model cannot handle and E001 fires. Re-adding a stored generated column (`db_persist=True`) on an existing table always fires E104, so suppress it with table-size evidence; a virtual generated column (`db_persist=False`, PostgreSQL 18+) is not flagged.

E001 and E002 also fire for a `RemoveField` or `DeleteModel` inside a `SeparateDatabaseAndState`'s `database_operations`. The deferred operations cannot be nested in one, so take the operation out of the wrapper rather than swapping it in place; `fix_deploy_safety` refuses these migrations for the same reason.

### E002: delete a model

Remove fields on other models that point at it first (Django orders this for you), then:

```python
operations = [DeferredDeleteModel(name="OldThing")]
```

### E003: add a NOT NULL column

```python
migrations.AddField("invoice", "channel", models.CharField(max_length=20, db_default="web"))
```

To drop a `db_default` later, make the column nullable in the same change or keep the default.

### E004: make an existing column NOT NULL

```python
# 0220 (atomic)
operations = [InstallNotNullFill("lead", "source", fill_sql="'unknown'")]

# 0221
atomic = False
operations = [
    BackfillNotNull("lead", "source", fill_sql="'unknown'"),
    SetNotNull("lead", "source", models.CharField(max_length=50)),
]
```

`SetNotNull`'s field must match the current field except for `null`. `fill_sql` can use other columns as `{field_name}`.

### E005: rename a field or model

An E005 on a `RenameField` or `RenameModel` that is already written is not fixed by re-running `makemigrations`, which only transforms migrations it generates in that run. Delete the migration (it must not have been applied anywhere) and regenerate it with an interactive `makemigrations`, answering yes to the rename question. If the output says the rename was left as a `RenameField` or `RenameModel`, it is ineligible for the reason given; then keep the old name with `db_column` / `db_table` as below.

```python
migrations.AlterField("customer", "name", models.CharField(max_length=50, db_column="name"))
migrations.RenameField("customer", "name", "full_name")
```

For a model, set `Meta.db_table` to the old table name before `RenameModel`. If any auto-created many-to-many table points at the model, convert it to an explicit `through` model first, or don't rename.

### E006: change a column's type

```python
# 0220 (atomic)
operations = [
    migrations.AddField("invoice", "amount_pence", models.BigIntegerField(null=True)),
    InstallColumnSync("invoice", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint", backwards_sql="({to} / 100.0)::numeric(12,2)"),
]

# 0221
atomic = False
operations = [
    BackfillColumnSync("invoice", from_field="amount", to_field="amount_pence", forwards_sql="round({from} * 100)::bigint"),
    DeferredRemoveField("invoice", "amount"),
]
```

SQL must be deterministic. Widening `varchar`, `numeric` precision, or an unindexed string to `text` needs none of this.

### E007: trigger operation setup

- Install operations go in an atomic migration; backfills and `SetNotNull` in a later `atomic = False` migration of the same app.
- The backfill's SQL must match the install's exactly.
- A sync target has no `db_default`. If it is NOT NULL, add it with a constant `default=...` (keep `preserve_default` at its default) and a NOT NULL source.

### E008: queue dependency

Add `("deferred_migrations", "0001_initial")` to `dependencies`.

### E009: suppression

```python
deploy_safety_allowed = {"E103": "lead_status has ~40 rows per account."}
```

### E010: baseline

Edit `migrations/deferred_migrations_baseline.txt` to name an existing migration, usually the squashed migration that replaced the old name.

### E011: generated field dependency

Remove or change the `GeneratedField` that uses the column (in an earlier migration or earlier in the same one), then remove the column.

### E012: ineligible model rename

A `DeferredRenameModel` on a model with `Meta.db_table`, an unmanaged or proxy model, or a model in an auto-created many-to-many table cannot use the compatibility view. Use a plain `RenameModel` with `Meta.db_table` kept, or convert the many-to-many to an explicit `through` model first.

### E013: column type change on a renamed table

PostgreSQL refuses to change the type of a column a view depends on, so an `AlterField` that changes a column's type on a model a `DeferredRenameModel` renamed earlier in the same run fails pre-deploy. Only `--unapplied-only` runs and `migrate_pre_deploy` report it, since a full run cannot tell which renames are in this deploy. Ship the type change in the next deploy, after the view has been dropped.

### E014: a rename that will lose data

A migration that removes a field and adds a differently named field with the same definition, or deletes a model and creates a differently named one with the same fields, is a rename written as a removal plus an addition: the new column or table starts empty. Delete the unapplied migration and regenerate it with an interactive `makemigrations`, answering yes to the rename question. Only when the two really are unrelated, suppress it with `deploy_safety_allowed = {"E014": "<why the old data is not needed>"}`.

### E101 / E102: indexes

```python
atomic = False
operations = [AddIndexConcurrently("invoice", models.Index(fields=["channel"], name="invoice_channel_idx"))]
```

For a new field, use `AddFieldConcurrently`. For a foreign key on an existing table:

```python
atomic = False
operations = [AddFieldConcurrently("invoice", "store", models.ForeignKey("shop.Store", models.SET_NULL, null=True))]
```

E102 on an `AlterField` names what it adds, each with its own fix: `unique=True` or a foreign key constraint on a field that already exists needs `SeparateDatabaseAndState`, with the `AlterField` in `state_operations` and the concurrent build in `database_operations` (see `https://github.com/breww-oss/django-deferred-migrations/blob/main/README.md#adding-a-unique-or-foreign-key-constraint-to-an-existing-field`); a plain `db_index` uses `AddIndexConcurrently`; an indexed string column changed to `text` rebuilds its index, so suppress it with table-size evidence or build the replacement index concurrently.

### E103: constraints

```python
atomic = False
operations = [
    AddConstraintConcurrently("order", models.CheckConstraint(condition=Q(price__gte=0), name="price_positive")),
    AddConstraintConcurrently("order", models.UniqueConstraint(fields=["reference"], name="order_reference_unique")),
]
```

A unique constraint on plain fields is built as a concurrent unique index and attached with `USING INDEX`; one with a `condition`, `include`, `opclasses` or expressions is a concurrent unique index, as Django creates it.

### E104: table rewrites

Add the field nullable with no `db_default`, then follow the E004 recipe with the expression as `fill_sql`, then `AlterField` to add the `db_default`. For a stored generated column on a small table, suppress with table-size evidence.

### E105 / E106: non-atomic migrations

Concurrent operations need `atomic = False`. Everything else in a non-atomic migration must survive being run twice, so keep plain operations such as `RemoveConstraint`, `AddField` and `CreateModel` in a separate atomic migration.

## Never

- Never edit or rename a migration that has already been applied to silence a check. If a new rule flags old migrations, advance that app's baseline file.
- Never change `DeferredRemoveField` back to `RemoveField`, except for the remove-and-re-add pattern above.
- Never add a suppression without evidence a reviewer can check.
