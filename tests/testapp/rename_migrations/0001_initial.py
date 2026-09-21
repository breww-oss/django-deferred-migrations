import django.db.models.deletion
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(name="Gadget", fields=[("id", models.BigAutoField(primary_key=True, serialize=False)), ("name", models.CharField(max_length=50))]),
        migrations.CreateModel(name="Part", fields=[("id", models.BigAutoField(primary_key=True, serialize=False)), ("gadget", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="deferred_migrations_testapp.gadget"))]),
    ]
