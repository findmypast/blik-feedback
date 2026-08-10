import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_keep_team_manager_in_team'),
        ('questionnaires', '0018_questionnaire_review_type_flags'),
        ('reviews', '0008_reviewcycle_renewal_fields'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ReviewCampaign',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('uuid', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ('target_type', models.CharField(choices=[('team', 'Team'), ('individual', 'Individual')], max_length=20)),
                ('include_descendants', models.BooleanField(default=False)),
                ('cycle_type', models.CharField(choices=[('peer', 'Peer review'), ('self', 'Self-assessment'), ('manager', 'Manager assessment')], max_length=20)),
                ('start_date', models.DateField(blank=True, null=True)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('active', 'Active'), ('completed', 'Completed')], default='draft', max_length=20)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_review_campaigns', to=settings.AUTH_USER_MODEL)),
                ('individual', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='individual_review_campaigns', to='accounts.reviewee')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='review_campaigns', to='core.organization')),
                ('questionnaire', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='review_campaigns', to='questionnaires.questionnaire')),
                ('renewed_from', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='renewals', to='reviews.reviewcampaign')),
                ('team', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='review_campaigns', to='accounts.team')),
            ],
            options={'db_table': 'review_campaigns', 'ordering': ['-created_at']},
        ),
        migrations.AddField(
            model_name='reviewcycle',
            name='campaign',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='cycles', to='reviews.reviewcampaign'),
        ),
    ]
