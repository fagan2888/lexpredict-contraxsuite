# -*- coding: utf-8 -*-
# Generated by Django 1.11.3 on 2017-11-07 19:50
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('employee', '0012_employee_vacation_yearly'),
    ]

    operations = [
        migrations.AddField(
            model_name='employee',
            name='governing_law',
            field=models.CharField(blank=True, max_length=1024, null=True),
        ),
        migrations.AlterField(
            model_name='employee',
            name='vacation_yearly',
            field=models.CharField(db_index=True, max_length=1024, null=True),
        ),
    ]
