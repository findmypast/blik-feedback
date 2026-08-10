from django.db import migrations, models


def set_existing_questionnaire_flags(apps, schema_editor):
    Questionnaire = apps.get_model('questionnaires', 'Questionnaire')
    Questionnaire.objects.update(
        allow_peer_review=True,
        allow_self_assessment=True,
        allow_manager_assessment=False,
    )
    Questionnaire.objects.filter(name__icontains='Manager 360').update(
        allow_peer_review=False,
        allow_self_assessment=False,
        allow_manager_assessment=True,
    )
    Questionnaire.objects.filter(name__icontains='Developer Skills Assessment').update(
        allow_peer_review=False,
        allow_self_assessment=True,
        allow_manager_assessment=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('questionnaires', '0017_questionnaire_created_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='questionnaire',
            name='allow_manager_assessment',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='questionnaire',
            name='allow_peer_review',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='questionnaire',
            name='allow_self_assessment',
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(set_existing_questionnaire_flags, migrations.RunPython.noop),
    ]
