from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [('reviews', '0017_team_cycle_completion_notification')]

    operations = [
        migrations.AlterField(
            model_name='organizationalreviewcycle',
            name='status',
            field=models.CharField(
                choices=[('draft', 'Not started'), ('active', 'Active'), ('completed', 'Completed')],
                default='active', max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='reviewcycle',
            name='status',
            field=models.CharField(
                choices=[('draft', 'Not started'), ('active', 'Active'), ('completed', 'Completed')],
                default='active', max_length=20,
            ),
        ),
        migrations.CreateModel(
            name='OrganizationReviewPhase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=255)),
                ('position', models.PositiveSmallIntegerField()),
                ('due_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(choices=[('draft', 'Not started'), ('active', 'Active'), ('completed', 'Completed')], default='draft', max_length=20)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('organizational_cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='phases', to='reviews.organizationalreviewcycle')),
            ],
            options={'db_table': 'organization_review_phases', 'ordering': ['position']},
        ),
        migrations.AddConstraint(
            model_name='organizationreviewphase',
            constraint=models.UniqueConstraint(fields=('organizational_cycle', 'position'), name='unique_organization_cycle_phase_position'),
        ),
        migrations.AddField(
            model_name='reviewcampaign',
            name='phase',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='campaigns', to='reviews.organizationreviewphase'),
        ),
    ]
