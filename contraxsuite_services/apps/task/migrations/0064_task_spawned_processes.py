# Generated by Django 2.2.13 on 2020-06-10 12:01

import apps.common.model_utils.improved_django_json_encoder
import django.contrib.postgres.fields.jsonb
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('task', '0063_auto_20200630_1353'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='spawned_processes',
            field=django.contrib.postgres.fields.jsonb.JSONField(
                blank=True,
                encoder=apps.common.model_utils.improved_django_json_encoder.ImprovedDjangoJSONEncoder,
                null=True),
        ),
    ]
