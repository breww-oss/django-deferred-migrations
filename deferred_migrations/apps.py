from django.apps import AppConfig


class DeferredMigrationsConfig(AppConfig):
    name = "deferred_migrations"
    verbose_name = "Deferred migrations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from django.core import checks
        from django.db.models.signals import post_migrate
        from django.db.models.signals import pre_migrate

        from deferred_migrations.checks import check_makemigrations_order
        from deferred_migrations.checks import check_migrate_order
        from deferred_migrations.contenttypes import install_content_type_compatibility
        from deferred_migrations.context import install_migration_context
        from deferred_migrations.notice import after_migrate
        from deferred_migrations.notice import note_whether_database_is_new

        install_migration_context()
        install_content_type_compatibility()
        checks.register(check_makemigrations_order)
        checks.register(check_migrate_order)
        # sender=self so these run once per migrate, not once per app config.
        pre_migrate.connect(note_whether_database_is_new, sender=self)
        post_migrate.connect(after_migrate, sender=self)
