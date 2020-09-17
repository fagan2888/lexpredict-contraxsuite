# Generated by Django 2.2.13 on 2020-08-21 10:30

from django.db import migrations


def migrate(apps, schema_editor):
    FieldAnnotation = apps.get_model('document', 'FieldAnnotation')
    FieldAnnotation.objects.filter(field__type='related_info').update(value=None)
    FieldAnnotationFalseMatch = apps.get_model('document', 'FieldAnnotationFalseMatch')
    FieldAnnotationFalseMatch.objects.filter(field__type='related_info').update(value=None)


class Migration(migrations.Migration):

    dependencies = [
        ('document', '0210_fill_note_user'),
    ]

    operations = [
        migrations.RunPython(
            migrate,
            reverse_code=migrations.RunPython.noop
        )
    ]
