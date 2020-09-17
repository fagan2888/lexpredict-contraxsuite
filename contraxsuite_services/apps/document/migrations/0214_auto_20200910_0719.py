# Generated by Django 2.2.13 on 2020-09-10 07:19

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('document', '0213_field_category_per_document'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='alt_source_path',
            field=models.CharField(db_index=True, max_length=1024, null=True),
        ),
        migrations.AddField(
            model_name='historicaldocument',
            name='alt_source_path',
            field=models.CharField(db_index=True, max_length=1024, null=True),
        ),
    ]
