from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0016_optional_organisation_manager_questionnaire'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamCycleCompletionNotification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('organizational_cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_completion_notifications', to='reviews.organizationalreviewcycle')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='organizational_cycle_completion_notifications', to='accounts.team')),
            ],
        ),
        migrations.AddConstraint(
            model_name='teamcyclecompletionnotification',
            constraint=models.UniqueConstraint(fields=('organizational_cycle', 'team'), name='unique_organizational_team_completion_notification'),
        ),
        migrations.AddField(
            model_name='reviewcampaign',
            name='completion_notification_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]