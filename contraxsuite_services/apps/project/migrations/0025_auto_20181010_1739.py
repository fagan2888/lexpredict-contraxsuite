# -*- coding: utf-8 -*-
# Generated by Django 1.11.3 on 2018-10-10 17:39
from __future__ import unicode_literals

import apps.document.models
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('document', '0105_auto_20181128_1238'),
        ('project', '0024_project_send_email_notification'),
    ]

    operations = [
        migrations.CreateModel(
            name='DocumentFilter',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=1024)),
                ('order', models.PositiveSmallIntegerField(default=0)),
                ('filter_query', models.TextField()),
                ('document_sort_order', models.CharField(max_length=1024)),
                ('document_type', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='document.DocumentType')),
            ],
        ),
        migrations.AlterField(
            model_name='project',
            name='type',
            field=models.ForeignKey(blank=True, default=apps.document.models.DocumentType.generic_pk, null=True, on_delete=django.db.models.deletion.CASCADE, to='document.DocumentType'),
        ),
        migrations.AddField(
            model_name='documentfilter',
            name='project',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='project.Project'),
        ),
    ]
