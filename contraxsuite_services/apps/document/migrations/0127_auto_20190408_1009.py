# -*- coding: utf-8 -*-
# Generated by Django 1.11.18 on 2019-04-08 10:09
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('document', '0126_merge_20190327_1150'),
    ]

    operations = [
        migrations.AlterField(
            model_name='documentfield',
            name='code',
            field=models.CharField(db_index=True, help_text='Field codes are used for creating \n    columns in DB tables, in field formulas (Python syntax), for human-readable data representation in APIs. Should \n    start with a latin letter and contain only latin letters, digits and underscores. Should follow \n    the snake_case convention.', max_length=40),
        ),
    ]