# Contributing

Thanks for helping. Bug reports, fixes and improvements are all welcome. To report a security problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Commit messages

This is the one thing most likely to fail your first pull request. CI lints every commit in a pull request with [commitlint](https://commitlint.js.org/) and [`@commitlint/config-angular`](https://github.com/conventional-changelog/commitlint/tree/master/%40commitlint/config-angular), so a message like `fixed the bug` fails the `commitlint` check. Each commit header must look like this:

```
type(optional scope): short summary in the imperative
```

For example, `fix: keep the lock timeout when a retry reconnects` or `docs(readme): explain the queue runner`. The header can be at most 100 characters.

The allowed types are `build`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, `test` and `chore`.

Releases are automatic. When a pull request merges to `main`, [python-semantic-release](https://python-semantic-release.readthedocs.io/) reads the commit and decides whether to publish a new version:

| Commit | Release |
| --- | --- |
| `feat` | Minor version |
| `fix`, `perf` | Patch version |
| A `BREAKING CHANGE:` footer, or `!` after the type (`feat!:`) | Major version |
| Any other type | No release |

Write the pull request title in the same format. When a pull request with several commits is squash-merged, its title becomes the commit on `main` that decides the release.

To catch a bad message before you push, install the repo's [pre-commit](https://pre-commit.com/) hooks. This adds the commit-message check and the ruff hooks:

```sh
uvx pre-commit install --hook-type pre-commit --hook-type commit-msg
```

If a commit already in your branch fails, reword it with `git commit --amend` (for the latest commit) or `git rebase -i` and force-push.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and a PostgreSQL server. Install the package and its development dependencies:

```sh
uv sync
```

The tests need a real PostgreSQL, version 14 or later (15 or later to test against Django 6.1). By default they connect to `127.0.0.1:55432` as user `ddm` with password `ddm` and database `ddm`. The standard `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and `PGPASSWORD` environment variables override those values (see `tests/settings.py`). The user needs permission to create databases, because the tests create their own.

The quickest way to get a matching server is the same container CI uses:

```sh
docker run -d --name ddm-postgres -p 55432:5432 \
  -e POSTGRES_USER=ddm -e POSTGRES_PASSWORD=ddm -e POSTGRES_DB=ddm \
  postgres:16
```

### Running the tests

```sh
uv run pytest
```

CI also runs the suite against every supported Django version. To reproduce one of those runs locally, install that Django over the locked one and pass `--no-sync`. Without `--no-sync`, `uv run` reinstalls the locked Django first and you'd be testing the wrong version:

```sh
uv pip install "django~=5.2.0"
uv run --no-sync pytest
```

Run `uv sync` afterwards to go back to the locked version.

### Linting and formatting

```sh
uv run ruff check .
uv run ruff format .
```

CI runs `uv run ruff format --check .`, so commit the formatted result. If you change anything under `.github/`, also run `uv run zizmor .github`, which CI uses to audit the workflows.

## Before you start

- **Small fixes:** open a pull request directly.
- **Larger changes** (a new operation or check, a change to a command's behaviour, or anything that touches the migration queue): open an issue first and describe what you want to do. That way we can agree the approach before you spend time on it.
- **Supported versions and dependencies:** the supported Django and Python ranges, the PostgreSQL floor and the dependency list are deliberate choices, each tied to the CI test matrix in `.github/workflows/test.yml`. We make those changes ourselves, alongside the matrix, and won't accept them from a drive-by pull request. If you need a version supported or think a dependency is worth adding, open an issue.

## Pull requests

- Add or update tests for any change in behaviour. A bug fix should come with a test that fails without it.
- Update the README when you change something it documents.
- Keep a pull request to one change. Unrelated fixes are easier to review and release separately.
