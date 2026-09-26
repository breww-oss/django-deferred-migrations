# Changelog

<!-- version list -->

## v0.2.0 (2026-09-26)

### Bug Fixes

- Handle partial indexes and quoted tables in unique naming
  ([`3c242ec`](https://github.com/breww-oss/django-deferred-migrations/commit/3c242eccd340fae338dfc5334261579db04e6bc0))

- Name a unique field added concurrently as Django's AddField would
  ([`095bdb2`](https://github.com/breww-oss/django-deferred-migrations/commit/095bdb2f2aff20797a9c10e91ef8457133ba7b74))

### Chores

- Add review lenses for multi-agent code review
  ([`8b979b6`](https://github.com/breww-oss/django-deferred-migrations/commit/8b979b6b03d2f72b0c307eae6702c5137c02de38))

- **deps**: Update ruff to v0.16.8
  ([`4fe73c0`](https://github.com/breww-oss/django-deferred-migrations/commit/4fe73c003e220d33197156cbe76a63e8efddf090))

### Continuous Integration

- Add an all-checks-pass gate job for the ruleset to require
  ([`e69fc25`](https://github.com/breww-oss/django-deferred-migrations/commit/e69fc253b38131a694be91956825a937380326de))

- Add zizmor, dependency review and OpenSSF Scorecard
  ([`6a39dae`](https://github.com/breww-oss/django-deferred-migrations/commit/6a39dae87336585873663d76016e025ab2a239fd))

- Allow the release to be dispatched manually
  ([#15](https://github.com/breww-oss/django-deferred-migrations/pull/15),
  [`c2f3162`](https://github.com/breww-oss/django-deferred-migrations/commit/c2f31620e49af2828dee1643b887af988f41e63a))

- Configure Renovate
  ([`9c87ccd`](https://github.com/breww-oss/django-deferred-migrations/commit/9c87ccd17b69584d613e4573bad7d5eb39200c22))

- Dispatch CI for the workflow change above
  ([#15](https://github.com/breww-oss/django-deferred-migrations/pull/15),
  [`c2f3162`](https://github.com/breww-oss/django-deferred-migrations/commit/c2f31620e49af2828dee1643b887af988f41e63a))

- Push the release commit with a dedicated GitHub App token
  ([`f8504ad`](https://github.com/breww-oss/django-deferred-migrations/commit/f8504ad83aef98061f08dabb5655e63dabcb8755))

- Restrict the test workflow's token to read-only contents
  ([`ff69335`](https://github.com/breww-oss/django-deferred-migrations/commit/ff69335cb97dc731ac878ebb5c6bf399891d1f40))

- Test Django 5.2 on Python 3.14
  ([`a3d1969`](https://github.com/breww-oss/django-deferred-migrations/commit/a3d196964917753a9604f0dc8177054c1ea6499a))

- **deps**: Pin dependencies
  ([`117e75a`](https://github.com/breww-oss/django-deferred-migrations/commit/117e75a6331e031b2f8ed9a22e81aa8462428806))

- **deps**: Update actions/checkout action to v7
  ([`8275529`](https://github.com/breww-oss/django-deferred-migrations/commit/827552974992014ec27ea9cb8e69c08c10db3221))

- **deps**: Update actions/setup-node action to v7
  ([`522ef43`](https://github.com/breww-oss/django-deferred-migrations/commit/522ef437eac5c640728dc8077d5d9a228a46b770))

- **deps**: Update astral-sh/setup-uv action to v10
  ([`ee22c0e`](https://github.com/breww-oss/django-deferred-migrations/commit/ee22c0e53bf8b93807d1069c03c332a83e825007))

- **deps**: Update dependency node to v24
  ([`4b3e3c7`](https://github.com/breww-oss/django-deferred-migrations/commit/4b3e3c7eddfe1d2cf1465ae0b1c62ffaa28f1370))

- **deps**: Update github artifact actions
  ([`19ea532`](https://github.com/breww-oss/django-deferred-migrations/commit/19ea5320872a38afcde48a3214e5f0d929f02f4d))

### Documentation

- Correct the E008 fix and describe what the parity tests check
  ([`ac415ba`](https://github.com/breww-oss/django-deferred-migrations/commit/ac415ba6136864f437047e7a165f39d0b047e11b))

### Features

- Add --database to fix_deploy_safety
  ([`5186fa5`](https://github.com/breww-oss/django-deferred-migrations/commit/5186fa5821847f8c30ee4cdf68862159fe3ebb06))

### Testing

- Compare generated migrations end to end against Django's
  ([`e897c2b`](https://github.com/breww-oss/django-deferred-migrations/commit/e897c2beb9d68c264182f016834ba107ac5a566c))

- Leave the test database clean so --reuse-db runs pass
  ([`0ea35dc`](https://github.com/breww-oss/django-deferred-migrations/commit/0ea35dc67e512b0d8a9682f7bd4a8ab4ce1257a8))


## v0.1.0 (2026-09-21)

- Initial Release
