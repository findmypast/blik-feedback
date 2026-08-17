from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0014_organization_roles'),
        ('reviews', '0013_reviewer_token_assigned_team'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationalreviewcycle',
            name='audience_type',
            field=models.CharField(
                choices=[
                    ('individuals', 'Individual(s)'),
                    ('teams', 'Team(s)'),
                    ('entire', 'Entire organisation'),
                ],
                default='entire',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='organizationalreviewcycle',
            name='teams',
            field=models.ManyToManyField(
                blank=True,
                help_text='Teams included in this organisation review.',
                related_name='organizational_review_cycles',
                to='accounts.team',
            ),
        ),
        migrations.AddField(
            model_name='organizationalreviewcycle',
            name='selected_reviewees',
            field=models.ManyToManyField(
                blank=True,
                related_name='selected_organizational_review_cycles',
                to='accounts.reviewee',
            ),
        ),
    ]
