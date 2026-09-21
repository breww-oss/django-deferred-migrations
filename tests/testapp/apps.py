from django.apps import AppConfig


class TestAppConfig(AppConfig):
    name = "tests.testapp"
    label = "deferred_migrations_testapp"
    default_auto_field = "django.db.models.BigAutoField"
