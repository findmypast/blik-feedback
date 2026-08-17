from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0014_organisation_cycle_teams'),
    ]

    operations = [
        migrations.AlterField(
            model_name='reviewcampaign',
            name='target_type',
            field=models.CharField(
                choices=[
                    ('team', 'Team'),
                    ('individual', 'Individual'),
                    ('organization', 'Organisation'),
                ],
                max_length=20,
            ),
        ),
    ]
