from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('questionnaires', '0016_fix_agency_questionnaire_template_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='questionnaire',
            name='created_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='created_questionnaires',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
