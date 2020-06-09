# Generated by Django 2.2.4 on 2019-11-14 18:08

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('document', '0167_remove_textunit_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='folder',
            field=models.CharField(db_index=True, max_length=1024, null=True),
        ),
        migrations.AddField(
            model_name='historicaldocument',
            name='folder',
            field=models.CharField(db_index=True, max_length=1024, null=True),
        ),
    ]