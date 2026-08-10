from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('reviews', '0009_reviewcampaign_reviewcycle_campaign')]

    operations = [
        migrations.AddField(
            model_name='reviewcampaign',
            name='minimum_peer_reviewers',
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text='Minimum number of peer nominations each participant must submit.',
            ),
        ),
    ]
