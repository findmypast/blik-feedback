import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('questionnaires', '0018_questionnaire_review_type_flags'),
        ('reviews', '0010_reviewcampaign_minimum_peer_reviewers'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrganizationalReviewCycle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('uuid', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ('minimum_peer_reviewers', models.PositiveSmallIntegerField(default=3)),
                ('start_date', models.DateField(blank=True, null=True)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(choices=[('active', 'Active'), ('completed', 'Completed')], default='active', max_length=20)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_organizational_review_cycles', to=settings.AUTH_USER_MODEL)),
                ('manager_questionnaire', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='questionnaires.questionnaire')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='organizational_review_cycles', to='core.organization')),
                ('peer_questionnaire', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='questionnaires.questionnaire')),
                ('self_questionnaire', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='questionnaires.questionnaire')),
            ],
            options={'db_table': 'organizational_review_cycles', 'ordering': ['-created_at']},
        ),
        migrations.AddField(
            model_name='reviewcampaign',
            name='organizational_cycle',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='campaigns', to='reviews.organizationalreviewcycle'),
        ),
        migrations.AlterField(
            model_name='reviewcampaign',
            name='target_type',
            field=models.CharField(choices=[('team', 'Team'), ('individual', 'Individual'), ('organization', 'Organization')], max_length=20),
        ),
    ]
