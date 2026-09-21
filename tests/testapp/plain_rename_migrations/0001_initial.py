from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(name="Gadget", fields=[("id", models.BigAutoField(primary_key=True, serialize=False))]),
    ]
