from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [('reviews', '0018_organization_review_phases')]

    operations = [
        migrations.AlterField(
            model_name='organizationalreviewcycle',
            name='self_questionnaire',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='questionnaires.questionnaire'),
        ),
        migrations.AlterField(
            model_name='organizationalreviewcycle',
            name='peer_questionnaire',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='questionnaires.questionnaire'),
        ),
    ]
