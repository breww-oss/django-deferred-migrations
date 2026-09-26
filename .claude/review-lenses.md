# Review lenses: django-deferred-migrations

Read by the `breww-workflows:multi-agent-review` skill, which supplies the harness (scope, diff
capture, dispatch, triage). This file supplies the opinions: which reviewers to run, what each one
hunts for, and how to verify a fix.

## Verification commands

Run these before dispatching and put the results in every agent prompt (the agents have no shell).
The suite needs PostgreSQL on 127.0.0.1:55432 (user/password/db `ddm`). It creates `test_ddm`,
`test_ddm_native` and `test_ddm_deferred` itself.

```bash
uv run pytest -q                 # full suite, about a minute
uv run ruff check .
uv run ruff format --check .
uv run zizmor .github            # only when .github/ changed
```

For DDL, SQL-generation or Django-API changes, also run the matrix's edge cells. `--no-sync` is
required; without it uv reinstalls the locked Django and silently undoes the pin:

```bash
uv pip install "django~=5.2.0" && uv run --no-sync pytest -q          # oldest Django
uv pip install "django~=6.0.0" && PGPORT=<pg14 port> uv run --no-sync pytest -q   # PostgreSQL 14 floor
uv sync                                                                # restore the locked Django
```

After fixing findings, re-run the suite and report the count.

## Sources for the "don't re-flag" list

- README "Limitations and FAQ": documented limitations are accepted decisions, not findings.
- README "Equivalence with Django's own migrations": column order and statement timing are
  deliberately not compared.
- CHANGELOG.md and the version in pyproject.toml are written by python-semantic-release; never
  flag them as missing a hand edit.

## The lens set

| Lens | Skip when |
|---|---|
| A: DDL & lock safety | no SQL, schema-editor or operation code changed |
| B: Deploy-window correctness | no operation, runner, queue, trigger or command code changed |
| C: Logic & bugs | never |
| D: Django & PostgreSQL version compatibility | no production code changed |
| E: Test quality & effectiveness | no test file and no production code path changed |
| F: Docs & public contract | no README, public operation, command, setting or check-ID change |
| G: Slop, simplification & house style | a one-or-two-line fix |

Every lens must be told: read surrounding and sibling code freely (`deferred_migrations/` is small,
so read the whole module a change lives in), but judge only the `+` lines.

**Agent A: DDL & lock safety.** Every statement the package runs must be safe on a large, busy
production table. Hunt for:
- a lock stronger than necessary, or held longer than necessary, including for a moment on a
  referenced table;
- `CONCURRENTLY` inside a transaction, or a concurrent operation missing `NotInTransactionMixin`;
- DDL that bypasses the lock-timeout-and-retry wrapper, and table rewrites or full scans under
  `ACCESS EXCLUSIVE` (type changes, `SET NOT NULL` without a validated CHECK, volatile defaults);
- operations that are not re-runnable after an interruption at every statement boundary. An
  INVALID index, a half-attached constraint or a column added without its default must all be
  handled on the next run;
- identifiers not quoted with `quote_name`, or SQL built by string interpolation from anything a
  user controls;
- catalog lookups resolved with `current_schema()` where `pg_table_is_visible` or
  `search_path`-correct resolution is needed;
- anything that behaves differently between the PostgreSQL 14 floor and current versions.

Cite the lock level each flagged statement takes.

**Agent B: Deploy-window correctness.** The package's promise: between `migrate_pre_deploy` and
`migrate_post_deploy`, old code (which still has the previous models) and new code both work
against the same schema, and after post-deploy the schema equals what Django's own migrations
build. For every changed operation, walk four states:
1. before;
2. after pre-deploy, with old code reading and writing: inserts that omit or set every column,
   updates, upserts, `select_for_update`, deletes;
3. after post-deploy;
4. reversed at state 2 and reversed at state 3.

Check each of the following:
- Django state and the database never diverge in a way a later `makemigrations` or migration trips
  over.
- Queued rows are keyed stably, run in a safe order (trigger drops before column drops, view drops
  first), and are cleaned up on reversal.
- Triggers never corrupt the column old code reads, and cannot loop.
- A name held by a queued or skipped drop is never reused silently.
- Behaviour with several migrations and pull requests stacked in one app between deploys. It must
  never require a migration-level ordering that django-linear-migrations would break.

**Agent C: Logic & bugs.** Trace the changed paths end to end: off-by-ones, missing branches,
None/empty cases, wrong early returns, exceptions swallowed or mis-classified (lock timeout vs real
error), non-idempotent retries, partial-failure states, transaction boundaries, and state leaking
between `database` aliases. For a change to shared code (`schema.py`, `concurrent.py`, `runner.py`,
`safety/`), verify it is correct for every caller, not just the one that motivated it. For the
safety checks and the autofixer, look for false negatives (unsafe code passing) as hard as false
positives, and confirm the autofixer never edits a migration that has already been applied.

**Agent D: Django & PostgreSQL version compatibility.** Supported: Django 5.2, 6.0 and 6.1;
PostgreSQL 14+ (15+ on Django 6.1); Python 3.12-3.14. Flag:
- private Django APIs (`schema_editor._create_*`, `_field_indexes_sql`, `Statement.parts`,
  autodetector and questioner internals, `MigrationWriter` output), with a note of whether their
  signature or behaviour differs across the supported versions;
- features used without a version guard (`nulls_distinct`, `security_invoker` views and
  `CREATE OR REPLACE TRIGGER` are PostgreSQL 15/14+);
- differences in generated SQL or naming between versions that the parity tests would only catch
  in one matrix cell.

Name the version where the behaviour differs.

**Agent E: Test quality & effectiveness.** Would each new or changed test fail if the behaviour it
names broke? Hunt for:
- **Vacuous comparisons.** A parity test where both sides built nothing or queued nothing. A
  schema snapshot that omits the attribute the change affects (index validity, constraint
  deferrability, defaults, triggers, functions, views, comments). A row comparison over empty
  tables.
- **Cases the arms can't tell apart.** Scenarios where the package's operation was never actually
  used. Check that the expected-operations or `result.ran` guards are present.
- **Assertions that can't fail.** Assertions inside a `pytest.raises` block, and assertions on
  construction rather than on database state.
- **Cleanup gaps.** Transactional tests that run `CONCURRENTLY` DDL, and so commit, without
  dropping what they created: tables, trigger functions (`dm1_sync_*`/`dm2_fill_*`), sequences,
  `django_migrations` rows for the test app.
- **Cross-test pollution.** `settings`, `sys.modules`, the app registry or `apps.all_models` left
  modified.
- **Version handling.** PostgreSQL-version-dependent cases without a skip, or a skip that hides a
  real failure.

Test style for this repo: plain functions, fully typed, `-> None`, `pytest.param(..., id=...)` for
parametrised cases, and `#` comments that explain why. Coverage gaps: for each changed operation
branch, name the test that exercises it forwards, backwards, while queued, after the drop, on
re-run after an interruption, for an unmanaged or router-excluded model, and through `sqlmigrate`
(`collect_sql`). Where one is missing, propose it. Where the right check is a mutation (break the
code, and this test must fail), say which.

**Agent F: Docs & public contract.** README.md is the product's only documentation and makes
specific claims. For every change, is each affected claim still true? This covers:
- operation behaviour and the rule order for relaxing a column;
- lock levels;
- the check IDs (E0xx/E1xx/W0xx) and their anchors;
- command flags and settings;
- the version support table;
- the Equivalence section.

Also flag:
- a new public operation, command, setting or check with no README entry;
- a changed signature or deconstruct() output, which would break already-written migrations;
- anything that changes what existing users' applied migrations do on re-run or reversal;
- `deferred_migrations/skills/fixing-deploy-safety/SKILL.md`, which is shipped to users' AI
  assistants, falling out of date with the checks.

Breaking changes need a `BREAKING CHANGE:` footer in the conventional commit.

**Agent G: Slop, simplification & house style.** At most 2 simplification recommendations, plus
uncapped outright slop; "nothing found" is a valid result. Hunt for:
- speculative parameters or abstractions with a single use;
- defensive handling of states that can't occur;
- scratch files in the diff;
- comments that narrate the task or the debugging instead of explaining why.

House style comes from `pyproject.toml` and the existing code:
- ruff with `force-single-line` imports and 300-character lines;
- comments are a single line stating why, placed above the code;
- helpers are plain module functions, not classes;
- SQL helpers live in `schema.py`/`concurrent.py`/`triggers.py`, not inline in operations.

Flag divergences with a concrete sibling reference (`file:line`). Never recommend a change solely
for consistency or line count.
