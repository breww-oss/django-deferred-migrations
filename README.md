# django-deferred-migrations

[![Tests](https://github.com/breww-oss/django-deferred-migrations/actions/workflows/test.yml/badge.svg)](https://github.com/breww-oss/django-deferred-migrations/actions/workflows/test.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/breww-oss/django-deferred-migrations/badge)](https://scorecard.dev/viewer/?uri=github.com/breww-oss/django-deferred-migrations)
[![PyPI](https://img.shields.io/pypi/v/django-deferred-migrations.svg)](https://pypi.org/project/django-deferred-migrations/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-deferred-migrations.svg)](https://pypi.org/project/django-deferred-migrations/)
[![Django versions](https://img.shields.io/pypi/frameworkversions/django/django-deferred-migrations.svg)](https://pypi.org/project/django-deferred-migrations/)
[![Licence](https://img.shields.io/pypi/l/django-deferred-migrations.svg)](https://github.com/breww-oss/django-deferred-migrations/blob/main/LICENSE)

Deploy-safe Django migrations for PostgreSQL. Destructive schema changes (dropping a column or table, tightening a column to NOT NULL, reshaping a column) ship in one pull request and one deploy, and old code keeps working while the new release rolls out.

Built and used in production at [Breww](https://breww.com).

## Contents

1. [What it does](#what-it-does)
2. [Why](#why)
3. [How it compares](#how-it-compares)
4. [Equivalence with Django's own migrations](#equivalence-with-djangos-own-migrations)
5. [Works with django-linear-migrations](#works-with-django-linear-migrations)
6. [makemigrations](#makemigrations)
7. [Requirements](#requirements)
8. [Installation](#installation)
9. [Deploy integration](#deploy-integration)
10. [Writing safe migrations](#writing-safe-migrations)
11. [Checks reference](#checks-reference)
12. [Suppressions, baselines and third-party apps](#suppressions-baselines-and-third-party-apps)
13. [Commands and settings reference](#commands-and-settings-reference)
14. [Lock safety](#lock-safety)
15. [Limitations and FAQ](#limitations-and-faq)
16. [AI coding assistants](#ai-coding-assistants)
17. [Licence](#licence)

## What it does

Migrations run before new code rolls out, and old code keeps serving requests until the rollout ends. django-deferred-migrations splits every migration run into two phases around that rollout. Operations such as `DeferredRemoveField` update Django's state immediately, make the old schema tolerate the new code, and queue the physical `DROP` for a post-deploy step that runs once the old code is gone. Only the destructive SQL is deferred, never a whole migration, so later migrations never wait on unrun work. That is also what lets it work with [django-linear-migrations](#works-with-django-linear-migrations), however many pull requests land in one app between deploys. A `check_deploy_safety` command catches unsafe operations in CI, `fix_deploy_safety` rewrites the mechanical cases, and every DDL statement runs with a short lock timeout and retry, so a migration never queues behind a long transaction and stalls the site.

A field removal across a rolling deploy, with a plain `RemoveField`:

| Step | Database | Old code | New code |
|---|---|---|---|
| `migrate` | `DROP COLUMN legacy_ref` | Errors: column `legacy_ref` does not exist | Not running yet |
| Rollout | | Errors on every query that selects `legacy_ref` | Works |
| Rollout ends | | Gone | Works |

With `DeferredRemoveField`:

| Step | Database | Old code | New code |
|---|---|---|---|
| `migrate_pre_deploy` | `legacy_ref` relaxed so inserts may omit it; `DROP COLUMN` queued | Works | Not running yet |
| Rollout | | Works: the column is still there | Works: its model has no such field |
| `migrate_post_deploy` | Waits for terminating pods, then `DROP COLUMN legacy_ref` | Gone | Works |

## Why

In a rolling deploy, migrations run first while the old release serves all traffic, and old and new processes then overlap until the rollout completes. Any migration that breaks the old code's view of the schema causes errors for the length of that overlap:

- `RemoveField` / `DeleteModel`: old code still selects the column or table.
- Adding a NOT NULL column without a database default: old code's inserts omit it and fail.
- Tightening a column to NOT NULL: old code still writes NULL.
- Renames that change a physical table or column name.
- Index and constraint builds that block writes for the whole build.

The usual workaround is to split each destructive change across two deploys and remember to ship the second one. That second deploy is easy to forget, and it has to wait for every environment to take the first. django-deferred-migrations makes the second step automatic: it is queued by the migration itself and runs after the rollout of the same deploy.

## How it compares

| Tool | Version evaluated | Approach | Strengths | Why this package differs | Ideas credited |
|---|---|---|---|---|---|
| [django-syzygy](https://github.com/charettes/django-syzygy) | 1.2.3 (2026-03-13) | Classifies whole migrations as pre-deploy or post-deploy and runs them in two passes | Automatic pre-drop preparation (a database default or nullable column before removal); checks that inspect operations rather than relying on authors | Whole migrations are deferred, so `migrate --pre-deploy` raises `AmbiguousPlan` when a pre-deploy migration depends on an unapplied post-deploy one, which happens whenever two pull requests to one app land between deploys. It provides its own `makemigrations`, which django-linear-migrations also hooks. It does not manage lock timeouts. Django 6 support was unconfirmed at the version evaluated | Pre-drop preparation; operation-inspecting checks |
| [django-safemigrate](https://github.com/aspiredu/django-safemigrate) | 6.0 (2025-06-03) | Authors mark each migration `before`, `after` or `always` deploy | Simple mental model; clear grouped deploy logs; fails loudly on real conflicts | Strict mode blocks whole migrations in the same way, and migrations are classified by their authors rather than by inspecting operations. Django 6 was untested at the version evaluated | Grouped deploy logs; failing loudly on real conflicts |
| [pgroll](https://github.com/xataio/pgroll) | 0.16.3 (2026-09) | A standalone tool that serves old and new schema versions side by side through views, with start and complete phases | Two-phase start/complete lifecycle; trigger-synced columns with batched, primary-key-ordered backfills; short `lock_timeout` with retry and jitter | No Django integration, so migrations are written twice and `RunPython` has no equivalent. Each client selects its schema version through `search_path`, which is awkward behind a connection pooler, and only one migration can stay open per deploy | The two-phase lifecycle; trigger-based column sync and batched backfills; lock timeout with jittered retry. This package decides trigger direction by comparing values, not by `search_path` |
| [django-pg-zero-downtime-migrations](https://github.com/tbicr/django-pg-zero-downtime-migrations) | Not evaluated in depth | Replaces Django's PostgreSQL schema editor backend to add lock timeouts and rewrite unsafe DDL into safe sequences | Broad coverage of unsafe DDL with no change to how migrations are written | A database backend replacement is a bigger commitment than a set of operations, and it does not defer drops until after the rollout | Complementary: the two address different parts of the problem |

The key difference: **only the physical destructive SQL is deferred, not the migration.** The field leaves Django's state immediately, so nothing in the migration graph ever depends on unrun work, and a deploy is never blocked by an earlier migration waiting for its post-deploy phase.

## Equivalence with Django's own migrations

After `migrate_pre_deploy` and `migrate_post_deploy`, the schema matches what plain Django migrations build for removing a field, deleting a model and renaming a model, and for the concurrent index, constraint and field operations. The comparison covers columns, types, nullability, defaults, indexes, constraints, sequences, triggers and functions.

Tightening a column to NOT NULL and reshaping a column through a synced copy have no single native equivalent that succeeds on a populated table. They are compared instead against the hand-written safe recipe a careful developer would write (backfill, then tighten; add the new column, copy the data across, then drop the old one), and the schema matches that recipe.

Row contents are compared for tightening to NOT NULL, renaming a model and reshaping a column, the cases where data moves or could be lost. The intermediate state is deliberately different, which is what lets old code keep running during a rollout.

Two things the comparison deliberately does not assert: physical column order, which PostgreSQL attaches no meaning to and which Django's own drop-and-re-add changes anyway, and the duration or lock behaviour of each statement, which is covered by separate tests.

## Works with django-linear-migrations

django-deferred-migrations is designed to be used alongside [django-linear-migrations](https://github.com/adamchainz/django-linear-migrations):

- Nothing is ever deferred at the migration level, so a later migration in the same app never waits on an unrun one. Deploys never block, however many pull requests land in one app between deploys.
- Its `makemigrations` subclasses django-linear-migrations' command and changes only the migration objects before they are written, so `max_migration.txt` still names each app's last migration, including a generated follow-up migration.
- Its `deferred_migrations_baseline.txt` sits next to `max_migration.txt` in each app's `migrations/` directory and never touches it.
- `fix_deploy_safety` edits operations and dependencies inside existing migration files and never changes which migration is an app's leaf.

An integration test runs both packages together: django-linear-migrations' system checks pass alongside deferred operations, and two stacked migrations in one app (a deferred removal followed by an `AddField`) apply in a single `migrate_pre_deploy` run.

## makemigrations

The package ships a `makemigrations` command that runs whichever `makemigrations` would otherwise run (django-linear-migrations' when it is installed below this package, otherwise Django's), then makes the migrations it generated deploy-safe before writing them:

- A plain `RemoveField` or `DeleteModel` is written as `DeferredRemoveField` or `DeferredDeleteModel`.
- A rename you confirm is written deploy-safe, when it is eligible: see [Renaming a field](#renaming-a-field) and [Renaming a model](#renaming-a-model).
- `AddField`, `AddIndex` and `AddConstraint` (unique or check) that would lock an existing table are written as `AddFieldConcurrently`, `AddIndexConcurrently` and `AddConstraintConcurrently`. A migration left holding only work that is safe to re-run becomes `atomic = False`, unless it also holds `RunSQL` or `RunPython`: hand-written code is never made non-atomic without its author choosing that. Otherwise the builds move to a non-atomic follow-up migration, and other migrations generated in the same run that depended on the original are pointed at it. When neither is safe, the operation is left as written and reported: when a later migration in the same app from the same run follows it, or when `RunSQL` or `RunPython` follows the build in the same migration, since hand-written code may read what the build creates and moving the build would run it too late.
- A migration that uses the package's operations gets the dependency it needs.
- Anything else the safety check would flag is written as generated, and the command lists each finding and ends with a summary such as `2 migrations made deploy-safe; 1 finding needs a manual fix (see the fixing-deploy-safety skill).`

Only migrations generated in the same run are touched. They have never been applied anywhere, so nothing applied is ever rewritten.

A non-interactive run (`--noinput`) that detects a rename refuses and lists every rename it detected: Django's non-interactive default treats a rename as a removal plus an addition, which loses the column's data. Run `makemigrations` interactively and answer the rename question. If it really is a removal plus an unrelated addition, make the two changes in separate runs, or answer "no" and suppress the resulting [E014](#e014) with a reason.

`--dry-run` and `--check` write nothing and print what would change. `--merge` and `--empty` are unchanged. `--update` applies only the in-place fixes; a rename that needs a generated follow-up migration asks for a normal run. Running `--update` onto a leaf that is already a non-atomic concurrent follow-up reports [E106](#e106) for any plain operation it merges in, since `--update` cannot split the result into a new migration.

Keep `deferred_migrations` above any other app in `INSTALLED_APPS` that ships a `makemigrations` command. If one is above it, the system check `deferred_migrations.W001` warns that this package's command never runs. To turn the command off, set `DEFERRED_MIGRATIONS_FIX_ON_MAKEMIGRATIONS = False`; `fix_deploy_safety` still handles removals.

## Requirements

- Python 3.12 or later
- Django 5.2, 6.0 or 6.1
- PostgreSQL 14 or later (for `CREATE OR REPLACE TRIGGER`) with Django 5.2 or 6.0; PostgreSQL 15 or later with Django 6.1
- psycopg 3

Other database backends are not supported: `migrate_pre_deploy` and `migrate_post_deploy` raise `ImproperlyConfigured` on them.

## Installation

```console
pip install django-deferred-migrations
```

Or with uv:

```console
uv add django-deferred-migrations
```

Add the app, above any other app in `INSTALLED_APPS` that ships a `makemigrations` or `migrate` command (see [`makemigrations`](#makemigrations) and [plain `migrate`](#plain-migrate); the system checks `deferred_migrations.W001` and `W002` catch it):

```python
INSTALLED_APPS = [
    ...,
    "deferred_migrations",
]
```

`migrate_pre_deploy` and `migrate_full` create the package's own tables before anything else, so there is no separate step for them.

In an existing project, record where checking starts, so only migrations written after adoption are checked:

```console
python manage.py deferred_migrations_baseline
```

This writes `migrations/deferred_migrations_baseline.txt` for every first-party app that has migrations, naming its current leaf. Commit those files, then add `python manage.py check_deploy_safety` to CI.

The app must be in `INSTALLED_APPS` wherever migrations run: its `ready()` records which migration is being applied, which the operations need to key their queue rows.

## Deploy integration

### The two-phase lifecycle

1. **Pre-deploy** (`migrate_pre_deploy`), while the old release still serves all traffic:
   1. Runs `check_deploy_safety --unapplied-only` and aborts on any error.
   2. Deletes `pending` and `failed` queue rows belonging to migrations that are not recorded as applied, except trigger drops from a migration that has also left the migration graph (see [interrupted migrations](#interrupted-non-atomic-migrations)).
   3. Runs `migrate` with a DDL lock timeout and retry. Deferred and trigger operations change state, relax the old schema and queue rows. In an atomic migration this all happens inside the migration's transaction; a non-atomic migration commits each statement as it runs.
   4. Prints each statement queued for post-deploy.

   It never runs queued rows. If an earlier deploy's rollout failed after its migrations ran, pods from that release may still be serving.
2. **Rollout** of the new code, waiting until it completes.
3. **Post-deploy** (`migrate_post_deploy --wait-before-seconds N`):
   1. Selects `pending` and `failed` rows whose migration is both applied and known to the running code (a node in the migration graph, or replaced by a squashed migration in it).
   2. If nothing is runnable, exits straight away without waiting.
   3. Sleeps `N` seconds so old pods still finishing requests are gone.
   4. Runs rows in queue order, each in its own transaction with the lock timeout and retry. Marks each row `done`, or marks it `failed` and stops.

Two post-deploy runners never overlap: the runner holds a PostgreSQL advisory lock for its whole run, and a second runner reports that and exits 0.

### Helm

Run the pre-deploy phase in a pre-upgrade hook Job and the post-deploy phase in a post-upgrade hook Job, and deploy with `helm upgrade --install --wait`.

```yaml
# templates/migrate-job.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .Release.Name }}-migrate
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "0"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 5
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          args: ["python", "manage.py", "migrate_pre_deploy"]
```

```yaml
# templates/post-deploy-migrate-job.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .Release.Name }}-post-deploy-migrate
  annotations:
    "helm.sh/hook": post-install,post-upgrade
    "helm.sh/hook-weight": "10"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: post-deploy-migrate
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          args: ["python", "manage.py", "migrate_post_deploy", "--wait-before-seconds", "{{ .Values.terminationGracePeriodSeconds }}"]
```

```yaml
# templates/deployment.yaml (every Deployment that serves the app)
spec:
  template:
    spec:
      terminationGracePeriodSeconds: {{ .Values.terminationGracePeriodSeconds }}
```

The two Jobs retry differently on purpose. `migrate_pre_deploy` retries lock timeouts itself, but not failures outside that loop: a database failover, a dropped connection, an evicted pod. The migrate Job's `backoffLimit: 5` covers those, with Kubernetes' backoff spreading the attempts over about five minutes. Re-running it is safe for this package's work, because applied migrations are skipped, every operation is idempotent and queue rows are inserted idempotently. An interrupted non-atomic migration re-runs from its first operation, so any hand-written `RunPython` or `RunSQL` in it must be idempotent too; the safety check does not inspect them. Nothing is dropped while it retries, so old pods keep serving. The cost is that a migration which genuinely cannot succeed takes longer to fail the deploy. The post-deploy Job keeps `backoffLimit: 0`: it exits 0 when a queued operation fails, leaving it for the next deploy to retry, so a pod restart would mostly repeat its wait.

If the migrate Job needs Secrets or ConfigMaps created by the same chart, a `pre-install` hook runs before they exist. Charts in that position run the migrate Job as `post-install` on a first install instead; the post-deploy Job's higher hook weight keeps it running after the migrate Job.

### A generic CI pipeline

```console
python manage.py check_deploy_safety                       # in the test stage
python manage.py migrate_pre_deploy                        # old release still serving
<roll out the new release and wait for it to complete>
python manage.py migrate_post_deploy --wait-before-seconds 300
```

### Local development

`migrate_full` runs both phases one after the other:

```console
python manage.py migrate_full
```

It is for local development only. In a deploy it would run the drops while old code is still serving, which is the failure this package exists to prevent.

To see what production looks like during a rollout, pass `--prompt-before-post`: it applies migrations, lists the queued operations and asks before running them. Answer no, exercise the new code and inspect the database, then run `migrate_post_deploy`.

### Plain `migrate`

Plain `migrate` cannot tell whether old code is still running, so this package makes it safe by default:

- **Typed at the command line**, it refuses and points to `migrate_pre_deploy`, `migrate_post_deploy` and `migrate_full`. `--plan` and `--check` still work, because they change nothing, and so do `--fake` and `--prune`, which change only migration records; `--fake-initial` can apply migrations for real, so it is refused. Set `DEFERRED_MIGRATIONS_ALLOW_COMMAND_LINE_MIGRATE = True` to allow it; it then behaves as below.
- **Called from code**, such as Django's test database setup or a third-party tool, it runs the pre-deploy phase without the safety check, the lock timeout or retry, and leaves the drops queued, saying so:

  ```text
  3 deferred operations are queued: the columns and tables they drop still exist. Run "python manage.py migrate_post_deploy" to complete them.
  ```

- **On a new database**, one with no `django_migrations` table yet, it also runs the queued drops straight away, so it ends fully migrated like Django's own `migrate`. Nothing can be running old code against a database that has never been migrated. That covers test databases and a first local setup.

The deploy commands report the queue themselves, so neither of them repeats the notice, and `migrate_pre_deploy` never runs the drops, even on a new database.

A `migrate` command from another app above `deferred_migrations` in `INSTALLED_APPS` replaces this one, so the command-line refusal is lost; the system check `deferred_migrations.W002` warns about it. The new-database behaviour does not depend on the command and still applies.

### The grace-period wait

When `helm upgrade --wait` returns, every old pod is either gone or terminating (Helm 4's `--wait` uses kstatus, which reports a Deployment current only once its replica counts match and it is available), and a terminating pod lives at most `terminationGracePeriodSeconds`. Passing the same value to `--wait-before-seconds` covers exactly that remainder. Other rollout tools need the equivalent: wait for the rollout to finish, then for the longest time an old process can keep serving. The wait only happens when at least one row is runnable.

Long-running background workers are outside this wait. A task still running old code when its column is dropped may fail; see [limitations](#limitations-and-faq).

### Rows from other releases

A row is only run when its migration is applied and known to the running code. Anything else was queued by a different release, for example after redeploying an older commit or switching branches locally, and that release may still use the column. Such rows are left alone and reported as `Left alone row <id>` with the date they were queued. The runner's result exposes them as `RunResult.unknown` with `created_at`, so a deployment can alert on rows that have waited for weeks and are never going to run; `deferred_migrations_resolve --skip` clears them.

Rows from a migration later replaced by a squashed migration still run, including nested squashes, even after the replaced migration's files are deleted: the migration stays known for as long as the squashed migration lists it in `replaces`. Its rows only become unknown once that `replaces` attribute is removed, so run `migrate_post_deploy` until the queue is empty first.

### Failures and `deferred_migrations_resolve`

When a row fails, the runner records `failed`, increments `attempts`, stores `last_error` and **stops**: later rows stay untouched, because they may depend on the failed one. `migrate_post_deploy` prints the failure and exits 0 by default; pass `--fail-on-error` to exit non-zero instead. Failed rows need no action to retry: the next `migrate_post_deploy` run starts again from the failed row.

A row that can never succeed (for example a `DROP COLUMN` blocked by a database view that depends on the column) blocks every later row. Mark it skipped with a reason:

```console
python manage.py deferred_migrations_resolve 42 --skip --reason "Column is used by a reporting view that is kept"
python manage.py deferred_migrations_resolve 42 --unskip
```

Skipped rows never run and do not block later rows. `--unskip` returns a skipped row to `pending`.

`deferred_migrations_resolve` takes the same advisory lock as the runner, and refuses while a `migrate_post_deploy` run holds it. A run works from a snapshot of the queue taken before its wait, so a status changed underneath it would be overwritten and the drop run anyway. If the command refuses, wait for the run to finish and try again.

Trigger drops (`kind=drop_trigger`) cannot be skipped. Their SQL is `DROP TRIGGER IF EXISTS ...; DROP FUNCTION IF EXISTS ...`, which only fails transiently, and skipping one would let a later `DROP COLUMN` remove a column the trigger still references, breaking every write to the table. As a second guard, a `drop_trigger` row that is `skipped` by any other means blocks every later row on the same table.

`migrate_post_deploy --dry-run` executes nothing and lists what would run (a failed row that would be retried is listed as the blocking row), what is skipped, and what is blocked behind a skipped trigger drop.

### Roll-forward policy

Before the post-deploy phase runs, rolling back is safe: nothing has been dropped, and old code works against the relaxed schema. After it, roll forward only, because rolling back puts old code on a schema without the dropped columns. Redeploying an older commit leaves rows it does not know alone, but columns already dropped stay dropped.

If the rollout fails, the post-upgrade hook does not run, so nothing is dropped while old pods may still be serving.

On a first install, a failed `post-install` hook leaves the Helm release with no deployed revision, which later `helm upgrade` runs treat differently from a failed upgrade. A fresh database has an empty queue, so the post-deploy Job normally exits straight away.

## Writing safe migrations

Imports used below:

```python
from django.db import migrations, models
from django.db.models import F, Q

from deferred_migrations.operations import (
    AddConstraintConcurrently,
    AddFieldConcurrently,
    AddIndexConcurrently,
    BackfillColumnSync,
    BackfillNotNull,
    DeferredDeleteModel,
    DeferredRemoveField,
    InstallColumnSync,
    InstallNotNullFill,
    SetNotNull,
)
```

Every migration that uses `DeferredRemoveField`, `DeferredDeleteModel`, `InstallColumnSync`, `InstallNotNullFill`, `BackfillColumnSync`, `BackfillNotNull` or `AddFieldConcurrently` depends on `("deferred_migrations", "0001_initial")` ([E008](#e008)). `DeferredRemoveField`, `DeferredDeleteModel`, `InstallColumnSync` and `InstallNotNullFill` queue rows keyed by their position in the migration, so they must be top-level operations, not nested inside `SeparateDatabaseAndState`.

Every operation does nothing on the database for proxy, unmanaged and swapped models, and for models a database router excludes, exactly like Django's own operations. Every operation is safe to re-run, so an interrupted non-atomic migration can be applied again as long as any `RunPython` or `RunSQL` it also contains is idempotent, and all SQL goes through the schema editor, so `sqlmigrate` shows it.

### Removing a field

Fixes [E001](#e001).

```python
from deferred_migrations.operations import DeferredRemoveField

dependencies = [("deferred_migrations", "0001_initial"), ("sales", "0219_previous")]
operations = [DeferredRemoveField(model_name="invoice", name="legacy_ref")]
```

`DeferredRemoveField` has the same arguments and state behaviour as `RemoveField`. Before the rollout it:

1. Drops the column's foreign key constraints, if it is a `ForeignKey` or `OneToOneField`, looking up their real names. New code no longer knows the relation, so deleting a referenced row must not be blocked by the database; Django enforces `on_delete` in Python, so old code does not rely on the constraint.
2. Makes the column tolerate inserts that omit it. The first matching rule wins:
   - a `GeneratedField`: nothing, PostgreSQL computes it;
   - unique (`unique=True`, or part of a unique constraint or `unique_together`): `DROP NOT NULL` if it is NOT NULL and has no `db_default`, and never `SET DEFAULT`, since a shared default would violate uniqueness on the second insert;
   - has a `db_default`: nothing, the database already fills omitted inserts;
   - has a callable `default`: `DROP NOT NULL` if it is NOT NULL, because one value baked into the column would be wrong for every row;
   - otherwise `SET DEFAULT <value>`, taking Django's effective default for the field: an explicit non-callable `default`, `""` for a string column that has none, or the migration's own timestamp for `auto_now_add` / `auto_now`. Old code then reads a value from rows new code inserted, rather than NULL;
   - otherwise, if NOT NULL: `DROP NOT NULL`.
3. Queues `ALTER TABLE IF EXISTS ... DROP COLUMN IF EXISTS ...`.

For a `ManyToManyField` with an auto-created through table, it drops the through table's foreign key constraints and queues `DROP TABLE IF EXISTS` for it. A custom `through` model is removed by its own `DeferredDeleteModel`.

Reversing it while the drop is still queued restores NOT NULL, removes the default it set and re-adds the foreign key constraint. After the drop has run, it re-adds the column as `RemoveField` does (the data is gone). Either way it deletes its queue rows, so re-applying the migration queues the drop again.

### Deleting a model

Fixes [E002](#e002). Remove fields on other models that point at it first (Django's autodetector orders this for you), then:

```python
operations = [DeferredDeleteModel(name="OldThing")]
```

Before the rollout it drops the table's outgoing foreign key constraints, and those of its auto-created many-to-many through tables, then queues `DROP TABLE IF EXISTS` for the through tables and the table.

### Changing a GeneratedField

Django cannot alter a `GeneratedField`, and PostgreSQL refuses to change the type of a column one depends on. Remove it and add it again **in the same atomic migration**; old code never sees the gap, so this stays a plain `RemoveField` + `AddField` and [E001](#e001) allows it:

```python
class Migration(migrations.Migration):
    deploy_safety_allowed = {"E104": "order has ~2,000 rows; the rewrite takes well under the lock timeout."}

    operations = [
        migrations.RemoveField("order", "total"),
        migrations.AddField("order", "total", models.GeneratedField(expression=F("price") * F("quantity"), output_field=models.BigIntegerField(), db_persist=True)),
    ]
```

The exception applies only when the migration is atomic and the `AddField` on the same model produces the same column name. Outside this generated-column case it also requires the same `db_type`: re-adding `code` as an `IntegerField` where it was a `CharField` leaves old code reading an integer, and is flagged. `fix_deploy_safety` leaves this pattern alone.

Adding a stored generated column (`db_persist=True`) rewrites the table, so [E104](#e104) always fires for it on an existing table, including in this pattern; the suppression above records the table-size evidence. A virtual generated column (`db_persist=False`, PostgreSQL 18+) does not rewrite the table and is not flagged.

### Renaming a field

Fixes [E005](#e005) for a column. Rename the field in the model and run `makemigrations` interactively, answering yes to the rename question. The rename is written as two migrations:

```python
# 0220 (atomic)
operations = [
    migrations.AddField("invoice", "sent_to_accountant", models.BooleanField(default=False)),
    InstallColumnSync("invoice", from_field="emailed_to_accountant", to_field="sent_to_accountant", forwards_sql="{from}", backwards_sql="{to}"),
]

# 0221_rename_invoice_emailed_to_accountant_to_sent_to_accountant_backfill
atomic = False
operations = [
    BackfillColumnSync("invoice", from_field="emailed_to_accountant", to_field="sent_to_accountant", forwards_sql="{from}"),
    DeferredRemoveField("invoice", "emailed_to_accountant", renamed_to="sent_to_accountant"),
]
```

The resulting state equals a plain rename. The trigger keeps both columns equal while old and new code run side by side; after the rollout the trigger and then the old column are dropped. A NOT NULL field with a constant default is added with its final definition. A NOT NULL field without one is added nullable and tightened with `SetNotNull` after the backfill. Several renames in one app share one follow-up migration, `NNNN_backfill_renamed_columns`.

`renamed_to` tells `DeferredRemoveField` that a trigger fills the old column from the new one: the old column only loses NOT NULL, rather than getting a default the trigger would copy over new code's explicit NULL, and reversing the migration after the column is dropped copies the values back from the new column.

A rename is written this way when the column changes and the field is a plain, non-relational column with no index, unique constraint or `db_default`, not a primary key or generated field, not used by a generated field or by `Meta.indexes`, `Meta.constraints`, `unique_together` or `index_together`, of a type with an equality operator (not `json`, `xml` or a geometric type), on a managed, non-proxy model with a single-column primary key, and not changed again later in the same run, whether by another operation on the field or by a new `Meta.indexes`, `Meta.constraints`, `unique_together` or `index_together` entry on the model. Unrelated index or constraint changes elsewhere in the run do not block the rename. Anything else stays a `RenameField` and is reported as E005 with the reason. For those, keep the old column name:

```python
migrations.AlterField("customer", "name", models.CharField(max_length=50, db_column="name"))
migrations.RenameField("customer", "name", "full_name")
```

Deploy time: the backfill runs pre-deploy, reads every row and writes those whose new column differs. After a constant default, only rows that differ from it are written. Until the drop, the table carries both columns.

### Renaming a model

Fixes [E005](#e005) for a table. Rename the model and run `makemigrations` interactively. The rename is written as `DeferredRenameModel("Gadget", "Gizmo")`:

- Pre-deploy it renames the table and creates a view under the old name, `CREATE VIEW old WITH (security_invoker = true) AS SELECT * FROM new` (PostgreSQL 15+; the view checks the caller's privileges and row-level security), and copies the table's `SELECT`, `INSERT`, `UPDATE` and `DELETE` grants onto it. Old code keeps using the old name through the view: inserts (including `ON CONFLICT`), updates, `UPDATE ... FROM`, deletes and `SELECT ... FOR UPDATE` all work. Queued drops that name the old table are pointed at the new one.
- Post-deploy the view is dropped, before any other queued row.
- Django's contenttypes app renames the model's ContentType pre-deploy, as for a plain `RenameModel`. The package records each rename, and old code that looks up the ContentType under the old name is given the renamed row instead of creating a duplicate. `model_class()` on the renamed row resolves under the old name too.

The package must be deployed once before relying on automatic model renames, because the ContentType handling has to be in the old code. A model rename is written this way when the table name changes (no `Meta.db_table`) and the model is managed, not a proxy, and in no auto-created many-to-many table in either direction; anything else stays a `RenameModel` and is reported as E005. A hand-written `DeferredRenameModel` on an ineligible model is reported as [E012](#e012) and refuses to run.

A column type change on the renamed table has to wait for the next deploy: PostgreSQL refuses it while the view exists. [E013](#e013) reports it when the rename and the type change are unapplied together, which `check_deploy_safety --unapplied-only` and `migrate_pre_deploy` can tell and a full `check_deploy_safety` run cannot; either way PostgreSQL refuses the change loudly at pre-deploy, before any new code runs.

### Adding a NOT NULL column

Fixes [E003](#e003).

```python
migrations.AddField("invoice", "channel", models.CharField(max_length=20, db_default="web"))
```

Old code's inserts omit the column and get the database default. To drop a `db_default` later, make the column nullable in the same change or keep the default.

### Making an existing column NOT NULL

Fixes [E004](#e004). A `db_default` does not help here: old code's model has `null=True`, so its inserts send an explicit NULL, which bypasses a database default.

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

- `InstallNotNullFill(model_name, name, fill_sql)` installs a `BEFORE INSERT OR UPDATE` trigger that replaces NULL with `fill_sql`, and queues the trigger's removal for post-deploy, so old code writing NULL keeps being filled until it is gone. `fill_sql` can use the row's other columns as `{field_name}`. It changes no state.
- `BackfillNotNull(model_name, name, fill_sql)` fills existing NULLs in primary-key ranges sized to take about `DEFERRED_MIGRATIONS_BACKFILL_TARGET_SECONDS` each (see [Backfill tuning](#backfill-tuning)), each in its own short transaction with the lock timeout and retry. It is idempotent and resumable. Its `fill_sql` must match the install's ([E007](#e007)).
- `SetNotNull(model_name, name, field)` is an `AlterField` whose field must match the current field exactly except for `null`; anything else, including an added `default`, raises `ValueError`. It adds a `CHECK (col IS NOT NULL) NOT VALID` constraint, validates it without blocking reads or writes, sets NOT NULL using that constraint instead of a table scan, and drops the constraint. Each step checks the catalog first, so it is idempotent.

Placeholders are substituted by plain string replacement, so SQL containing braces (`'{}'::jsonb`, array literals) is safe.

### Reshaping a column

Fixes [E006](#e006). Add the new column, keep both in sync with a trigger for the length of the rollout, backfill, and remove the old one:

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

`InstallColumnSync(model_name, from_field, to_field, forwards_sql, backwards_sql)` installs a `BEFORE INSERT OR UPDATE` trigger in which `{from}` and `{to}` refer to the row's columns:

- On insert, if `to` is NULL and `from` is not, `to` is set to `forwards(from)`. Otherwise, if `to` is set and differs from `forwards(from)`, `from` is set to `backwards(to)`. This does not test `from IS NULL`, because PostgreSQL fills column defaults before `BEFORE` triggers fire.
- On update, if only `from` changed, `to` is set to `forwards(from)`. If only `to` changed and it differs from `forwards(from)`, `from` is set to `backwards(to)`. The comparison means the backfill, which writes exactly `forwards(from)`, never rewrites `from`, so a lossy conversion cannot corrupt the column old code reads.
- `backwards_sql` is required; pass `None` explicitly to accept that old code sees stale values for rows new code writes.
- It queues the trigger's removal, which runs before the column drop queued by the next migration. Stop-on-failure means the column is never dropped while the trigger remains.

`BackfillColumnSync(model_name, from_field, to_field, forwards_sql)` updates rows in primary-key ranges sized to take about `DEFERRED_MIGRATIONS_BACKFILL_TARGET_SECONDS` each (see [Backfill tuning](#backfill-tuning)) where `to` differs from `forwards(from)`, each batch in its own short transaction with the lock timeout and retry. It is idempotent and resumable, and runs pre-deploy because new code reads `to` as soon as it is live.

Rules for the target column:

- Add it nullable, or NOT NULL with a constant default (`default=0`). Keep `preserve_default` at its default: with `preserve_default=False` the model still declares the default while the migration state does not, so every later `makemigrations` generates an `AlterField`.
- It must not have a `db_default`: the trigger could not tell that an old insert needs filling.
- A NOT NULL target needs a NOT NULL `from` field.
- A nullable target can be tightened in the same pull request with the NOT NULL recipe above. Sync triggers are named `dm1_sync_...` and fill triggers `dm2_fill_...`, and PostgreSQL fires `BEFORE ROW` triggers in name order, so the sync always runs before the fill.
- A NOT NULL target added nullable can be tightened with `SetNotNull` straight after an identity (`"{from}"`) backfill from a NOT NULL source, with no fill trigger: the sync trigger never leaves it NULL.

`forwards_sql` and `backwards_sql` must be deterministic (no `random()`, `now()` or sequences). Installing a trigger whose name already exists with a different body raises `ValueError` rather than replacing another operation's trigger. Widening `varchar`, widening `numeric` precision, or changing an unindexed string column to `text` needs none of this.

### Adding an index

Fixes [E101](#e101) and [E102](#e102).

```python
atomic = False
operations = [AddIndexConcurrently("invoice", models.Index(fields=["channel"], name="invoice_channel_idx"))]
```

`deferred_migrations.operations.AddIndexConcurrently` extends Django's own: before building, it drops an INVALID index of the same name left by an earlier failed concurrent build, so re-running the migration succeeds. A re-run skips an index that is already valid. For a new field, use `AddFieldConcurrently` instead - it adds the column with no index, then builds the index concurrently as part of the same migration. See [Adding a foreign key to an existing table](#adding-a-foreign-key-to-an-existing-table).

### Adding a foreign key to an existing table

Fixes [E102](#e102).

```python
atomic = False
operations = [AddFieldConcurrently("invoice", "store", models.ForeignKey("shop.Store", models.SET_NULL, null=True))]
```

`AddFieldConcurrently` adds the column with no index or constraint, builds Django's indexes with `CREATE INDEX CONCURRENTLY`, and adds the foreign key `NOT VALID` before `VALIDATE CONSTRAINT`. A unique field (including a `OneToOneField`) gets a concurrent unique index attached with `ADD CONSTRAINT ... UNIQUE USING INDEX`, named `..._key`, exactly as PostgreSQL names the inline `UNIQUE` of the plain `AddField` that `makemigrations` would have written, including its truncation to 63 bytes and its `..._key1` fallback when the name is taken. It can be re-run after an interruption. It refuses a column whose drop from an earlier removal has not run, whether that drop is still queued or was skipped. The `NOT VALID` step briefly takes `SHARE ROW EXCLUSIVE` on both the table and the table it references, which blocks writes to both for that moment. `VALIDATE CONSTRAINT` then takes `SHARE UPDATE EXCLUSIVE` on the table and `ROW SHARE` on the referenced table, both of which allow ordinary writes.

### Adding check and unique constraints

Fixes [E103](#e103).

```python
atomic = False
operations = [
    AddConstraintConcurrently("order", models.CheckConstraint(condition=Q(price__gte=0), name="price_positive")),
    AddConstraintConcurrently("order", models.UniqueConstraint(fields=["reference"], name="order_reference_unique")),
]
```

A unique constraint on plain fields (with `nulls_distinct` or `deferrable` if set) is built as a concurrent unique index and attached with `USING INDEX`; one with a `condition`, `include`, `opclasses` or expressions is a concurrent unique index, as Django creates it. A failed unique build drops its INVALID index before raising, because PostgreSQL keeps enforcing it. Check constraints are added `NOT VALID` and then validated.

### Adding a unique or foreign key constraint to an existing field

Fixes [E102](#e102) on an `AlterField`. `AddFieldConcurrently` and `AddConstraintConcurrently` both need a column that does not yet exist or a constraint that does not yet apply to the state; an `AlterField` changes a field that is already there, so wrap the state change in `SeparateDatabaseAndState` and build the constraint separately, in an `atomic = False` migration.

Unique:

```python
atomic = False
operations = [
    migrations.SeparateDatabaseAndState(
        state_operations=[migrations.AlterField("order", "reference", models.CharField(max_length=20, unique=True))],
        database_operations=[AddConstraintConcurrently("order", models.UniqueConstraint(fields=["reference"], name="order_reference_uniq"))],
    ),
]
```

The constraint's name is yours to choose: Django finds a model's unique constraints by introspecting the database, not by name.

Foreign key:

```python
atomic = False
operations = [
    migrations.SeparateDatabaseAndState(
        state_operations=[migrations.AlterField("invoice", "store", models.ForeignKey("shop.Store", models.SET_NULL, null=True))],
        database_operations=[
            migrations.RunSQL("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint co JOIN pg_class c ON c.oid = co.conrelid WHERE co.conname = 'sales_invoice_store_id_fk' AND c.relname = 'sales_invoice' AND pg_catalog.pg_table_is_visible(c.oid)) THEN
                        ALTER TABLE sales_invoice ADD CONSTRAINT sales_invoice_store_id_fk FOREIGN KEY (store_id) REFERENCES shop_store (id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
                    END IF;
                END $$;
            """),
            migrations.RunSQL("ALTER TABLE sales_invoice VALIDATE CONSTRAINT sales_invoice_store_id_fk"),
        ],
    ),
]
```

The migration is non-atomic, so an interrupted run applies its operations again from the first one, and nothing checks hand-written SQL for you: both statements have to survive that themselves. `ADD CONSTRAINT` does not - it raises `constraint ... already exists` - which is why it is wrapped in the `pg_constraint` lookup above, the same lookup `AddConstraintConcurrently` makes internally. It matches on the constraint's name and `pg_table_is_visible`, so it resolves `sales_invoice` through the `search_path` exactly as the `ALTER TABLE` beside it does; `current_schema()` would not, because the first schema on the path need not be the one holding the table. `VALIDATE CONSTRAINT` needs no guard: validating a constraint that is already valid succeeds and does no work.

## Checks reference

`check_deploy_safety` walks the whole migration plan once, in dependency order, applying each operation to an in-memory project state and inspecting the model state before and after each operation. It needs no database connection. Output looks like:

```text
sales.0220_remove_invoice_legacy_ref[0] deferred_migrations.E001: RemoveField('invoice', 'legacy_ref') drops the column while old code still reads it. Use DeferredRemoveField with the same arguments. (see https://github.com/breww-oss/django-deferred-migrations/blob/main/README.md#e001)
```

"Existing model" below means the model exists in project state before the migration. Models created earlier in the same migration are not existing. Proxy, unmanaged and swapped models have no table of their own and are never flagged by the name-based rules.

| Rule | Summary | Auto-fixed |
|---|---|---|
| [E001](#e001) | `RemoveField` | Yes |
| [E002](#e002) | `DeleteModel` | Yes |
| [E003](#e003) | NOT NULL column without a database default | |
| [E004](#e004) | Column tightened to NOT NULL | |
| [E005](#e005) | Physical table or column renamed | |
| [E006](#e006) | Column type change that rewrites the table | |
| [E007](#e007) | Trigger operations split or configured wrongly | |
| [E008](#e008) | Missing queue dependency | Yes |
| [E009](#e009) | Invalid suppression | |
| [E010](#e010) | Baseline names a missing migration | |
| [E011](#e011) | Deferred removal of a column a generated field uses | |
| [E012](#e012) | `DeferredRenameModel` on an ineligible model | |
| [E013](#e013) | Column type change on a model renamed earlier in the same set of unapplied migrations | |
| [E014](#e014) | A rename written as a removal plus an identical addition | |
| [E101](#e101) | `AddIndex` on an existing table | |
| [E102](#e102) | Field add or alter that builds an index or FK constraint | |
| [E103](#e103) | Constraint or together-index on an existing table | |
| [E104](#e104) | `AddField` that rewrites the table | |
| [E105](#e105) | A concurrent operation in an atomic migration | |
| [E106](#e106) | A non-atomic migration holding an operation that cannot be re-run | |

### E001

**Triggers:** a plain `RemoveField`, including one inside a `SeparateDatabaseAndState`'s `database_operations`. Not flagged: a removal followed, later in the same atomic migration, by an `AddField` on the same model that produces the same column name *and* the same `db_type` (two `GeneratedField`s count as matching, since the column is computed and old code never writes it); proxy, unmanaged and swapped models; anything in `state_operations`, which changes no schema.

**Why it is unsafe:** the column is dropped before the rollout while old code still selects it. A re-add that changes the column's type is equally unsafe: the column never disappears, so nothing else flags it, but old code then reads a value its model cannot handle.

**Fix:** `DeferredRemoveField` with the same arguments. `fix_deploy_safety` does this, except in a migration that uses `SeparateDatabaseAndState` - the deferred operations cannot be nested in one, so take the removal out of the wrapper by hand. See [Removing a field](#removing-a-field).

### E002

**Triggers:** a plain `DeleteModel`, including one inside a `SeparateDatabaseAndState`'s `database_operations`, except for proxy, unmanaged and swapped models.

**Why it is unsafe:** the table is dropped while old code still uses it.

**Fix:** `DeferredDeleteModel`. `fix_deploy_safety` does this, except in a migration that uses `SeparateDatabaseAndState` - the deferred operations cannot be nested in one, so take the deletion out of the wrapper by hand. See [Deleting a model](#deleting-a-model).

### E003

**Triggers:** an `AddField` on an existing model that is `null=False` with no `db_default`. Exempt: `GeneratedField`, fields without a column (`ManyToManyField`), and the `to_field` of an `InstallColumnSync` in the same migration added with a constant default. Also triggers on an `AlterField` that removes a `db_default` from a field that is NOT NULL afterwards.

**Why it is unsafe:** old code's inserts omit the column, so they fail on NOT NULL.

**Fix:** add a constant `db_default`, or `null=True`. To drop a `db_default`, make the column nullable in the same change or keep the default. See [Adding a NOT NULL column](#adding-a-not-null-column).

### E004

**Triggers:** an `AlterField` (other than `SetNotNull`) changing `null=True` to `null=False`.

**Why it is unsafe:** old code still writes NULL, and a plain `SET NOT NULL` scans the table under an exclusive lock.

**Fix:** `InstallNotNullFill`, then `BackfillNotNull` + `SetNotNull`. See [Making an existing column NOT NULL](#making-an-existing-column-not-null).

### E005

**Triggers:** any operation that changes a physical name: a model's table, an auto-created many-to-many through table, a field's column, or a through table's columns (which `RenameModel` renames even when `db_table` is kept, including through tables of many-to-many fields in other apps that point at the model). Detected by comparing the derived names before and after each `RenameField`, `RenameModel`, `AlterField` and `AlterModelTable`.

**Why it is unsafe:** old code queries the old name.

**Fix:** Regenerate the migration with an interactive `makemigrations`, which writes eligible renames deploy-safe. For the rest, keep the old names with `db_column` / `db_table`; for many-to-many fields, convert to an explicit `through` model first. See [Renaming a field](#renaming-a-field) and [Renaming a model](#renaming-a-model).

### E006

**Triggers:** an `AlterField` whose column type changes, other than `varchar(n)` to `varchar(m)` with `m >= n` or unbounded `varchar`, `numeric(p, s)` to `numeric(p2, s)` with `p2 >= p`, or a string type to `text`. Relation and generated fields are not compared. An indexed string column changed to `text` is [E102](#e102), not E006.

**Why it is unsafe:** the change rewrites the table under an exclusive lock, or old code cannot read or write the new type.

**Fix:** `InstallColumnSync`, then `BackfillColumnSync`. See [Reshaping a column](#reshaping-a-column).

### E007

**Triggers:** any of:

- `InstallColumnSync` or `InstallNotNullFill` in a non-atomic migration;
- `BackfillColumnSync`, `BackfillNotNull` or `SetNotNull` in an atomic migration;
- a backfill or `SetNotNull` without a matching install in an earlier migration of the same app;
- an install with no later backfill;
- a backfill whose SQL differs from its install's;
- a sync target with a `db_default`, a NOT NULL sync target not added in the same migration with a constant default, or a NOT NULL sync target whose source is nullable;
- `SetNotNull` after a sync whose SQL is not the identity `{from}`, or whose source is nullable, without a fill trigger.

**Why it is unsafe:** installs must be committed before backfills start, and only idempotent work may be re-run when a non-atomic migration is interrupted. Mismatched SQL would backfill different values from the ones the trigger writes.

**Fix:** install in an atomic migration, backfill in a later `atomic = False` migration of the same app, repeating the SQL exactly. See [Making an existing column NOT NULL](#making-an-existing-column-not-null) and [Reshaping a column](#reshaping-a-column).

### E008

**Triggers:** a migration using `DeferredRemoveField`, `DeferredDeleteModel`, `DeferredRenameModel`, `InstallColumnSync`, `InstallNotNullFill`, `BackfillColumnSync`, `BackfillNotNull` or `AddFieldConcurrently` that does not depend, directly or transitively, on `("deferred_migrations", "0001_initial")`. A `DeferredRenameModel` also needs `("deferred_migrations", "0002_modelrename")`.

**Why it is unsafe:** the operations write to the queue table, which may not exist yet.

**Fix:** add `("deferred_migrations", "0001_initial")` to `dependencies`, and `("deferred_migrations", "0002_modelrename")` for a `DeferredRenameModel`. `fix_deploy_safety` does this.

### E009

**Triggers:** a `deploy_safety_allowed` entry with an empty reason, or naming an unknown rule ID.

**Why it is unsafe:** a suppression without a reason cannot be reviewed. E009 itself cannot be suppressed.

**Fix:** give a real reason, with evidence a reviewer can check. See [Suppressions](#suppressions).

### E010

**Triggers:** a `deferred_migrations_baseline.txt` naming a migration that is not in the graph. The app is then not checked at all until the file is fixed.

**Why it is unsafe:** without a valid baseline the check cannot tell which migrations to check.

**Fix:** point the file at an existing migration, usually the squashed migration that replaced the old name.

### E011

**Triggers:** a `DeferredRemoveField` of a field that a `GeneratedField` expression on the same model still references after the operation.

**Why it is unsafe:** the queued `DROP COLUMN` would fail (PostgreSQL error `2BP01`) and block every later row in the queue.

**Fix:** remove or change the `GeneratedField` first, in an earlier migration or earlier in the same one.

### E012

**Triggers:** a `DeferredRenameModel` on a model with `Meta.db_table`, an unmanaged or proxy model, or a model in an auto-created many-to-many table.

**Why it is unsafe:** the compatibility view cannot cover it: with `db_table` nothing is renamed, and a through table's names derive from the model name.

**Fix:** use a plain `RenameModel` with `Meta.db_table` kept, or convert the many-to-many to an explicit `through` model first.

### E013

**Triggers:** an `AlterField` that changes a column's type on a model renamed by a `DeferredRenameModel` earlier in the same set of unapplied migrations. Only `check_deploy_safety --unapplied-only` and `migrate_pre_deploy` report it, since only they know which migrations belong to this deploy; a full `check_deploy_safety` run checks migrations that shipped long ago, whose views are already gone. A type change that slips past a full run still fails loudly at pre-deploy: the pre-deploy check reports it, and PostgreSQL would refuse it anyway.

**Why it is unsafe:** PostgreSQL refuses to change the type of a column a view uses, so the migration fails pre-deploy.

**Fix:** ship the type change in the next deploy, after the view has been dropped.

### E014

**Triggers:** in one migration, a `RemoveField` (plain or deferred) of a field and an `AddField` of a differently named field with an identical definition on the same model; or a `DeleteModel` (plain or deferred) and a `CreateModel` of a differently named model with identical fields. This is what answering "no" to Django's rename question leaves behind, and what a hand-written rename looks like.

**Why it is unsafe:** if it is really a rename, the new column or table starts empty and the old one is dropped after deploy, so the data is lost.

**Fix:** delete the migration (it must not have been applied anywhere) and regenerate it with an interactive `makemigrations`, answering yes to the rename question. If the removal and the addition really are unrelated, suppress E014 with a [`deploy_safety_allowed`](#suppressions) reason saying so.

### E101

**Triggers:** `AddIndex` (other than a concurrent one) on an existing model.

**Why it is unsafe:** a plain `CREATE INDEX` blocks writes to the table for the whole build.

**Fix:** `deferred_migrations.operations.AddIndexConcurrently` in an `atomic = False` migration; `makemigrations` writes it. See [Adding an index](#adding-an-index).

### E102

**Triggers:** an `AddField` on an existing model whose field has `db_index`, `unique`, or is a `ForeignKey` with `db_constraint`. On `AlterField`, separately for each of:

- adding `unique=True`: build the unique index concurrently, then `ADD CONSTRAINT ... UNIQUE USING INDEX` (see [Adding a unique or foreign key constraint to an existing field](#adding-a-unique-or-foreign-key-constraint-to-an-existing-field));
- adding a plain `db_index`: remove it from the field and use `AddIndexConcurrently`;
- adding a foreign key constraint: add it `NOT VALID`, then `VALIDATE CONSTRAINT` (see [Adding a unique or foreign key constraint to an existing field](#adding-a-unique-or-foreign-key-constraint-to-an-existing-field));
- changing an indexed string column to `text`: Django drops and recreates its pattern-ops index without `CONCURRENTLY`. Suppress it with table-size evidence, or perform the change with `SeparateDatabaseAndState` and build the replacement index concurrently.

**Why it is unsafe:** the index build or constraint validation blocks writes for its whole duration.

**Fix:** `AddFieldConcurrently`; `makemigrations` writes it. For an `AlterField`, see the bullets above.

### E103

**Triggers:** `AddConstraint` (other than `AddConstraintNotValid`) on an existing model, or `AlterUniqueTogether` / `AlterIndexTogether` adding entries on an existing model.

**Why it is unsafe:** the constraint is validated, or its index built, under a lock that blocks writes.

**Fix:** `AddConstraintConcurrently`; `makemigrations` writes it. See [Adding check and unique constraints](#adding-check-and-unique-constraints).

### E104

**Triggers:** an `AddField` on an existing model with a `db_default` that is not a constant (a plain value, `Value` or `Now()`), such as `RandomUUID()`, or a `GeneratedField` with `db_persist=True`. An `AlterField` adding a volatile `db_default` is not flagged: `ALTER COLUMN SET DEFAULT` never rewrites.

**Why it is unsafe:** PostgreSQL's fast default only applies to constant expressions; anything else rewrites the whole table under an exclusive lock.

**Fix:** add the field nullable with no `db_default`; follow the [NOT NULL recipe](#making-an-existing-column-not-null) with the expression as `fill_sql`; then an `AlterField` adding the `db_default`. For a stored generated column on a small table, suppress with table-size evidence.

### E105

**Triggers:** `AddIndexConcurrently`, `RemoveIndexConcurrently`, `AddFieldConcurrently` or `AddConstraintConcurrently` in an atomic migration.

**Why it is unsafe:** PostgreSQL refuses `CREATE INDEX CONCURRENTLY` inside a transaction, so the migration fails at deploy time.

**Fix:** `atomic = False` on the migration.

### E106

**Triggers:** an `atomic = False` migration containing an operation that fails if the migration is re-run after an interruption: anything other than `deferred_migrations`'s own `AddIndexConcurrently`, `AddFieldConcurrently`, `AddConstraintConcurrently`, `BackfillColumnSync`, `BackfillNotNull`, `SetNotNull`, `DeferredRemoveField` and `DeferredDeleteModel`, Django's `RemoveIndexConcurrently`, `ValidateConstraint`, `RunSQL`, `RunPython`, `AlterModelOptions` and `AlterModelManagers`, and `SeparateDatabaseAndState` whose database operations are all of those. `DeferredRenameModel`, `InstallColumnSync` and `InstallNotNullFill` are not on that list, because each queues a row keyed by its position in the migration, and a re-run would queue a duplicate. Django's own `AddIndexConcurrently` and `AddConstraintNotValid` are flagged too, because both fail once their object exists.

`RunSQL` and `RunPython` are on the safe list because they are trusted, not because they were analysed: the check cannot read SQL or a callable, so their re-run safety is yours to establish. Flagging every one of them would mean a suppression on every data migration for a check that verified nothing about it. See [Adding a unique or foreign key constraint to an existing field](#adding-a-unique-or-foreign-key-constraint-to-an-existing-field) for what making a statement re-runnable looks like.

**Why it is unsafe:** a non-atomic migration is recorded only when it finishes, so an interrupted one runs again from its first operation.

**Fix:** move the operation to an atomic migration, or use the package's operation that does the same job.

## Suppressions, baselines and third-party apps

### Suppressions

A migration can allow specific rules with a plain attribute on its `Migration` class:

```python
class Migration(migrations.Migration):
    deploy_safety_allowed = {"E103": "lead_status has ~40 rows per account."}
```

A suppression applies to that rule for the whole migration. The reason is required ([E009](#e009)). The `E1xx` rules fire regardless of table size, so a small table is the usual legitimate reason; never suppress to make CI green without evidence.

### Baselines

Each app's `migrations/deferred_migrations_baseline.txt` holds one migration name. That migration and every migration of the same app it depends on are not checked; everything after it is.

- `deferred_migrations_baseline` writes a file for every first-party app with migrations, naming its current leaf. It keeps existing files unless `--overwrite` is passed.
- A first-party app with no baseline file is checked in full, so new apps are covered and an accidentally deleted file fails loudly.
- When squashing replaces the named migration, point the baseline at the squashed migration ([E010](#e010)).
- When a package upgrade adds or tightens a rule, CI may flag historical migrations. Advance the affected baselines; never edit migrations that have already been applied.

`migrate_pre_deploy` only reports migrations that are not yet applied (`--unapplied-only`), so a new rule never blocks a deploy over migrations that already ran.

### Third-party apps

Apps installed under `site-packages` or `dist-packages` are skipped, unless they ship their own `deferred_migrations_baseline.txt`. The state walk still includes their migrations, so first-party rules see the correct model state.

## Commands and settings reference

All commands skip Django's system checks, and the rules are not registered as system checks, so they never block `makemigrations` or unrelated commands.

| Command | Options | Description |
|---|---|---|
| `check_deploy_safety` | `--unapplied-only`, `--database` | Runs the rules. Exits non-zero on any finding. Without `--unapplied-only` it needs no database connection |
| `fix_deploy_safety` | `[app_label] [migration_name]`, `--database` | Never edits a migration already applied to `--database`. Rewrites `migrations.RemoveField(` and `migrations.DeleteModel(` to the deferred operations, adds the import and the queue dependency, and prints everything else that needs a decision. Idempotent. A file is only edited when the number of occurrences matches the number of findings; files that import `RemoveField` or `DeleteModel` directly are reported for a manual fix |
| `migrate_pre_deploy` | `--database` | Safety check, stale row cleanup, then `migrate` with the lock timeout and retry |
| `migrate_post_deploy` | `--database`, `--wait-before-seconds N`, `--dry-run`, `--fail-on-error` | Runs the queue. Exits 0 when a row fails unless `--fail-on-error` is passed |
| `migrate_full` | `--database`, `--prompt-before-post`, `--fail-on-error` | Local development only: `migrate_pre_deploy` then `migrate_post_deploy` with no wait, optionally asking in between |
| `migrate` | Django's | Refused at the command line unless `DEFERRED_MIGRATIONS_ALLOW_COMMAND_LINE_MIGRATE` is set or it is `--plan` / `--check`; see [plain `migrate`](#plain-migrate) |
| `deferred_migrations_baseline` | `--app LABEL` (repeatable), `--overwrite` | Writes baseline files |
| `deferred_migrations_resolve` | `ID --skip --reason "..."` or `ID --unskip` | Skips a row that can never succeed, or returns a skipped row to `pending`. Refuses to skip trigger drops and view drops, and refuses while a post-deploy run holds the queue lock |

To report failures to your own alerting, call the runner directly:

```python
from deferred_migrations.runner import describe_run_result, run_deferred_operations

result = run_deferred_operations(using="default", wait_before_seconds=300)

for line in describe_run_result(result):
    print(line)

if result.failed is not None:
    alert(result.failed.pk, result.failed.last_error)
```

`RunResult` has `ran`, `would_run` (with `dry_run=True`), `failed`, `blocked`, `skipped`, `unknown` and `lock_held_elsewhere`. Rows are `deferred_migrations.models.DeferredOperation` instances with `app_label`, `migration_name`, `kind` (`drop_column`, `drop_table`, `drop_trigger` or `drop_view`), `table_name`, `column_name`, `sql`, `status`, `attempts`, `last_error`, `resolution_reason`, `created_at` and `executed_at`.

| Setting | Default | Description |
|---|---|---|
| `DEFERRED_MIGRATIONS_DDL_LOCK_TIMEOUT` | `"2s"` | `lock_timeout` for DDL statements and backfill batches |
| `DEFERRED_MIGRATIONS_LOCK_RETRIES` | `10` | Attempts before giving up on a lock (at least 1) |
| `DEFERRED_MIGRATIONS_RETRY_BASE_SECONDS` | `1` | Base of the exponential backoff |
| `DEFERRED_MIGRATIONS_RETRY_MAX_SECONDS` | `30` | Cap on a single backoff |
| `DEFERRED_MIGRATIONS_FIX_ON_MAKEMIGRATIONS` | `True` | Whether `makemigrations` makes the migrations it generates deploy-safe |
| `DEFERRED_MIGRATIONS_ALLOW_COMMAND_LINE_MIGRATE` | `False` | Whether plain `migrate` may be typed at the command line |
| `DEFERRED_MIGRATIONS_BACKFILL_TARGET_SECONDS` | `0.5` | Target duration of one backfill batch |
| `DEFERRED_MIGRATIONS_BACKFILL_MIN_ROWS` | `100` | Smallest key range a batch covers |
| `DEFERRED_MIGRATIONS_BACKFILL_MAX_ROWS` | `10000` | Largest key range a batch covers |
| `DEFERRED_MIGRATIONS_BACKFILL_PAUSE_SECONDS` | `0.05` | Pause between batches |

## Lock safety

The failure being prevented is a DDL statement waiting for a lock behind a long transaction while every later query on the table queues behind the DDL.

**Where the timeout applies.** `migrate_pre_deploy` and the queue runner wrap the PostgreSQL schema editor's `execute` for their duration, so every statement the schema editor issues gets a `lock_timeout`: operation DDL, deferred index and constraint SQL at the end of an atomic block, Django's compound `SET CONSTRAINTS ...; ALTER TABLE ... DROP CONSTRAINT ...`, and `RunSQL`. ORM queries in `RunPython` use the cursor directly and are unaffected.

- **Data statements are exempt.** A statement whose first keyword, after leading whitespace, comments and parentheses, is `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `WITH`, `MERGE`, `COPY` or `VALUES` waits on row locks as usual. Anything else, including anything unrecognised, gets the timeout.
- **Concurrent statements** run with no lock timeout: concurrent builds wait on open transactions by design, and that wait does not block reads or writes. The exemption is decided from the statement's leading keywords (`CREATE [UNIQUE] INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`, `REINDEX ... CONCURRENTLY`, `REFRESH MATERIALIZED VIEW CONCURRENTLY`), not by looking for the word anywhere in the statement, so a column or table named `concurrently` keeps the timeout.
- **`sqlmigrate`** output is unchanged.

**Atomic migrations.** Each statement runs after `SET LOCAL lock_timeout`, and the previous value is restored after it succeeds. On a lock timeout the migration rolls back, and `migrate_pre_deploy` waits with exponential backoff and full jitter and runs `migrate` again, which resumes at the rolled-back migration. After `DEFERRED_MIGRATIONS_LOCK_RETRIES` attempts it raises `CommandError`, so a deploy aborts with old code still serving.

**Non-atomic migrations.** The single statement that timed out is retried in place with backoff and jitter; earlier work in the migration, such as a backfill, is not repeated. When retries are exhausted it raises `CommandError` rather than re-running the migration from its first operation. The exception is a lock timeout inside an atomic step of a non-atomic migration, such as `RunPython(atomic=True)`: that follows the atomic rule, so `migrate` runs again and restarts the unrecorded migration from its first operation, which is harmless because the operations are idempotent.

**Backfills** run each batch in its own short transaction with `SET LOCAL lock_timeout` and retry, so a batch blocked on one locked row never holds its other row locks for long.

**Queue rows** each run in their own transaction with the timeout, retried the same way. A row that exhausts its retries is recorded as `failed`.

Choose `atomic = False` for migrations with concurrent index builds, backfills and `SetNotNull`, and keep trigger installs atomic ([E007](#e007)).

### Backfill tuning

Backfills walk the primary key in ranges sized to take about `DEFERRED_MIGRATIONS_BACKFILL_TARGET_SECONDS`, after pt-osc's `--chunk-time`. The first range is 1,000 rows. After each batch the rate (rows scanned per second, a weighted average giving the newest sample a quarter of the weight) sets the next range, which can at most double from one batch to the next and shrinks at once. A lock timeout halves the range, since the locked row may lie in its second half; at the minimum size, timeouts fall back to backoff, and `DEFERRED_MIGRATIONS_LOCK_RETRIES` timeouts on one range raise `LockRetriesExhausted`.

The maximum is 10,000 rows because the rate counts rows scanned, not written: after a stretch where nothing needs writing, the range grows, and the first batch in a region where every row changes writes the whole range under row locks before it can shrink.

`migrate_pre_deploy` prints progress. In a terminal it draws a live bar when [rich](https://github.com/Textualize/rich) is installed (the `django-deferred-migrations[rich]` extra); elsewhere, including a Kubernetes Job's log, it prints a flushed line every 10 seconds, such as `backfill sales_invoice.sent_to_accountant: 3,100,000/6,195,549 (50%), 1,204 written, 5,300 rows/s, batch 8,000, ~4m left`. rich's `TTY_INTERACTIVE=0` or `1` forces either mode, and `--verbosity 0` silences it.

## Limitations and FAQ

**Long-running background workers.** The post-deploy wait covers web processes that finish within their termination grace period. A background task started on old code that is still running when its column is dropped may fail; after a model rename's view is dropped, such a task fails on any query against the model, not only on one column.

**A relaxed column can read back NULL to old code.** Relaxation lets new code insert without the column, and sets a database default wherever there is a safe one: an explicit default, `""` for a string column, the timestamp for `auto_now_add` / `auto_now`. A NOT NULL column with none of those (an integer, a date, a foreign key) can only have its NOT NULL dropped, so a row inserted by new code during the rollout has NULL there, and old code loading that row sees `None` where its model says the field is non-nullable. This is usually harmless, since the field is being removed, but it breaks old code that does arithmetic or attribute access on the value without a guard. Where that matters, keep the column for one more release instead of relying on the overlap.

**`empty_strings_allowed` is taken at its word.** Relaxation offers `''` to any NOT NULL column whose field says `empty_strings_allowed`, which is how Django's own field classes mark themselves as string-backed. A custom field that inherits `Field.empty_strings_allowed = True` while overriding `db_type` to something that is not a string type would therefore be given a `SET DEFAULT ''` that PostgreSQL rejects, failing the migration that relaxes it. Override `empty_strings_allowed = False` on such a field, as Django's own non-string fields do.

**The compatibility view only copies table-level grants.** A model rename copies the renamed table's `SELECT`, `INSERT`, `UPDATE` and `DELETE` grants onto the view, read from `information_schema.role_table_grants`. Column-level grants and `WITH GRANT OPTION` are not copied.

**A renamed table's indexes keep their old names.** `ALTER TABLE ... RENAME TO ...` does not rename PostgreSQL's indexes, so they still carry names derived from the old table name after the rename. If a later migration reuses the old table name for a different model, and that model has an indexed column with a name that produces the same derived index name, the two can collide once the compatibility view is gone.

**Default model permissions are not carried over on a model rename**, as with Django's own `RenameModel`. If you rely on the `add_` / `change_` / `delete_` / `view_` permissions, grant them under the new name in a data migration.

**Old code cannot `TRUNCATE` or `COPY` into a renamed table while its view exists.** Both operate on tables, not views, so either fails against the old name until the view is dropped post-deploy.

**A direct `ContentType.objects.get(app_label=..., model=...)` call in old code is not covered by the ContentType handling.** Only `get_or_create`, `create` and `get_by_natural_key` (which backs `get_for_model`) are patched to serve the renamed row; a plain `.get()` under the old name raises `DoesNotExist`.

**On PostgreSQL 14 a table with row-level security cannot be renamed automatically.** The compatibility view needs `security_invoker`, which PostgreSQL adds in version 15; without it the view would run as its owner and bypass row-level security. `DeferredRenameModel` raises rather than create an unsafe view. Upgrade PostgreSQL, or keep the old name with `Meta.db_table`.

**A view drop cannot be skipped.** `deferred_migrations_resolve --skip` refuses a `drop_view` row, because the view depends on every column of the renamed table, so leaving it in place would block every other drop on that table for good. If the queued view drop is stuck, drop the view by hand, then rerun `migrate_post_deploy`, which finds it gone and marks the row done.

**Renaming a multi-table inheritance parent is not automatic.** The rename also renames the child's `<parent>_ptr` field, which is a relation and the child's primary key, so it stays a `RenameField` and is reported as [E005](#e005). Rename such a model with `Meta.db_table` kept.

**A rename nested in `SeparateDatabaseAndState` is not checked.** The operations inside `database_operations` are checked by every other rule, but the rename rule compares the physical names either side of an operation, and the walker does not compute those snapshots for a nested one. So a `RenameField`, `RenameModel`, `AlterField` or `AlterModelTable` that changes a physical name from inside the wrapper is not reported as E005. The documented recipes put only `RunSQL` in `database_operations`, which is unchecked anyway; if you nest a rename there, apply the [rename recipe](#renaming-a-field) yourself.

**`RunSQL` and `RunPython` are not inspected.** DDL written by hand is not checked, and DDL hidden inside a data statement (for example `SELECT some_function_that_alters()`) runs without the lock timeout.

**Trigger operations need a single-column primary key.** `InstallColumnSync`, `BackfillColumnSync`, `InstallNotNullFill` and `BackfillNotNull` raise `ValueError` for a composite primary key.

**PostgreSQL 14+ only.** Trigger installs use `CREATE OR REPLACE TRIGGER`. Other backends are not supported.

**Dropped columns still take space.** PostgreSQL's `DROP COLUMN` only marks the column dropped. The space, and the column's slot in PostgreSQL's 1600-column limit per table, are only reclaimed when the table is rewritten.

**Advancing baselines when rules change.** A package upgrade can add or tighten a rule, which CI then reports for historical migrations. Advance the affected apps' baselines; never edit applied migrations.

**Altering a foreign key at the database level is not flagged.** Any `AlterField` on a `ForeignKey` that changes its column (for example making it nullable) makes Django drop and re-create its foreign key constraint, which re-validates every row. The lock timeout bounds how long the statement waits for its lock, not how long validation scans the table once the lock is held. On a large table, use `SeparateDatabaseAndState` and add the constraint `NOT VALID` followed by `VALIDATE CONSTRAINT`.

**A failed unique build is dropped before the error is raised.** PostgreSQL keeps enforcing an INVALID unique index left by a failed `CREATE UNIQUE INDEX CONCURRENTLY`, which would reject duplicate writes from the release still serving, so the concurrent operations drop it first. The next run also drops any leftover it finds.

**Adding a foreign key briefly blocks writes to both tables.** `ADD CONSTRAINT ... NOT VALID` takes `SHARE ROW EXCLUSIVE` on the table and on the table it references, for a moment. `VALIDATE CONSTRAINT` takes `SHARE UPDATE EXCLUSIVE` on the table and `ROW SHARE` on the referenced table, so ordinary writes continue on both while it scans.

**Squashing, `replaces` and `migrate --prune`.** Queue rows keyed to a replaced migration stay runnable while the squashed migration lists it in `replaces`. Once `replaces` is removed they become unknown and are left alone, and `migrate --prune` (which refuses to run while `replaces` is present) then deletes the replaced migrations' recorded rows, so `migrate_pre_deploy`'s stale row cleanup deletes any column and table drops still `pending` or `failed`. The column is then never dropped, and a later queued drop that depended on it runs against a schema nobody is tracking. Trigger drops whose migration has left the graph are exempt from that cleanup and block every later row on their table instead, because losing one would leave a `dm1_sync_*` or `dm2_fill_*` trigger reading a column that a later drop removes, and every `INSERT` and `UPDATE` on that table would then fail until someone dropped the trigger by hand. Run `migrate_post_deploy` until the queue is empty before removing a squashed migration's `replaces` attribute, and before pruning.

**`DeferredDeleteModel` has no dependency check.** [E011](#e011) refuses a `DeferredRemoveField` whose column a generated field uses, because the queued `DROP COLUMN` would fail and stall the queue. There is no equivalent check for `DeferredDeleteModel`: nothing inspects what else in the database depends on the table. A queued `DROP TABLE` that another object still depends on - a view, a generated column reading it, an inherited child table, a foreign key from a table the migration did not touch - fails in the post-deploy phase and blocks every later row, exactly like the database-view case for a column. Drop the dependant first, or [skip the row](#failures-and-deferred_migrations_resolve).

**Reverse migrations rename foreign key constraints.** Reversing a `DeferredRemoveField` or `DeferredDeleteModel` recreates the foreign keys the forward direction dropped. They are dropped by whatever name the catalogue holds, but recreated under Django's canonical `_fk_%(to_table)s_%(to_column)s` suffix, so a table carrying a legacy or hand-named constraint comes back with a different constraint name. The constraint itself is equivalent; only the name changes.

**Reversing after new code has run.** Reversing a `DeferredRemoveField` whose drop is still queued restores `SET NOT NULL`. If new code has inserted rows with NULL in that column since it was relaxed, the reverse fails until those rows are fixed. The same applies to reversing a `SetNotNull` and then re-applying it. Rolling back code is only safe before the post-deploy phase runs; see [Roll-forward policy](#roll-forward-policy).

#### Interrupted non-atomic migrations

A non-atomic migration commits queue rows as each operation runs but is only recorded as applied at the end. If the deploy aborts and the migration is then edited, stale rows would sit under the keys its operations use, and queueing skips a key that already has a row, so the corrected row would never be written. `migrate_pre_deploy` therefore deletes `pending` and `failed` rows whose migration is not recorded as applied but is still in the migration graph, trigger drops included: Django re-runs an unrecorded migration from its first operation, and every operation re-queues its rows. A row whose migration has left the graph is a different case - nothing is going to re-queue it - and is kept, as described under [squashing and `migrate --prune`](#limitations-and-faq).

**Re-adding a name whose drop has not run.** If a new migration adds a column or table with the same name as one whose drop has not run, the `AddField`, `AddFieldConcurrently` or `CreateModel` fails before any new code runs, and `migrate_pre_deploy` raises a `CommandError` naming the row that holds the name. `AddFieldConcurrently` never adopts such a column. Run the previous release's post-deploy phase first, or use a different name. A skipped drop never runs at all, so it holds its name for good: the error says so, and frees it only by `deferred_migrations_resolve --unskip` followed by `migrate_post_deploy`.

**Why store SQL rather than a description of the change?** The SQL is generated at migration time from Django's historical state, with the schema editor's quoting, like the rest of the migration. Regenerating it later would run newer code against an older intent. Locking and retry wrap the SQL when it executes, so improvements to them still apply to queued rows.

**Why is a queue row's operation index one higher than the operation's position in the file?** Django inserts a `RenameContentType` operation after every `RenameModel` at migrate time, so in a migration that contains a `DeferredRenameModel`, rows queued by later operations carry an index one higher per rename before them.

**Can package operations be nested in `SeparateDatabaseAndState`?** Not the ones that queue rows: `DeferredRemoveField`, `DeferredDeleteModel`, `InstallColumnSync` and `InstallNotNullFill` key their rows by their position in the migration's `operations`, and raise `ImproperlyConfigured` when nested or when `deferred_migrations` is not installed. `SetNotNull`, `BackfillColumnSync`, `BackfillNotNull`, `AddIndexConcurrently` and `AddConstraintConcurrently` queue nothing and have no such restriction. `AddIndexConcurrently`, `AddConstraintConcurrently` and `AddFieldConcurrently` need a non-atomic migration, and `AddFieldConcurrently` reads the queue, so it needs `deferred_migrations` installed and the `0001_initial` dependency.

## AI coding assistants

The package ships an [Agent Skills](https://agentskills.io) skill at `deferred_migrations/skills/fixing-deploy-safety/SKILL.md`. It maps every rule ID to its fix with a minimal migration snippet, sets the order of work (`fix_deploy_safety` first, then the rules that need judgement), and explains when a suppression is legitimate. Copy or symlink the `fixing-deploy-safety` directory into your assistant's skills directory, for example `.claude/skills/` for Claude Code. In an installed package it sits under site-packages; this prints the directory:

```console
python -c "import deferred_migrations, pathlib; print(pathlib.Path(deferred_migrations.__file__).parent / 'skills')"
```

## Licence

MIT.
