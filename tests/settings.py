import os

SECRET_KEY = "django-deferred-migrations-test-key-not-secret"

USE_TZ = True

# deferred_migrations must sit above any app shipping a makemigrations or migrate command, which is
# what its own W001 and W002 system checks enforce.
INSTALLED_APPS = [
    "deferred_migrations",
    "django_linear_migrations",
    "django.contrib.contenttypes",
    "django.contrib.postgres",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ.get("PGHOST", "127.0.0.1"),
        "PORT": os.environ.get("PGPORT", "55432"),
        "NAME": os.environ.get("PGDATABASE", "ddm"),
        "USER": os.environ.get("PGUSER", "ddm"),
        "PASSWORD": os.environ.get("PGPASSWORD", "ddm"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
