from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('questionnaires', '0018_questionnaire_review_type_flags'),
        ('reviews', '0015_organisation_target_label'),
    ]

    operations = [
        migrations.AlterField(
            model_name='organizationalreviewcycle',
            name='manager_questionnaire',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='+',
                to='questionnaires.questionnaire',
            ),
        ),
    ]
