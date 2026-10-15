from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0020_invitation_last_sent_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='exclude_from_review_cycles',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Exclude this person from new review cycles and assessment invitations.'
                ),
            ),
        ),
    ]
