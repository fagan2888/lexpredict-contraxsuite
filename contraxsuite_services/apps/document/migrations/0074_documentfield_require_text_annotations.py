# -*- coding: utf-8 -*-
# Generated by Django 1.11.3 on 2018-09-24 17:03
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('document', '0073_auto_20180919_0715'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentfield',
            name='requires_text_annotations',
            field=models.BooleanField(default=True),
        ),
    ]