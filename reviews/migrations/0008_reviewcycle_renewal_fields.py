from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0007_reviewcycle_close_check_sent_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='reviewcycle',
            name='cycle_type',
            field=models.CharField(
                choices=[
                    ('360', '360 feedback'),
                    ('peer', 'Peer review'),
                    ('self', 'Self-assessment'),
                    ('manager', 'Manager review'),
                ],
                default='360',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='reviewcycle',
            name='start_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='reviewcycle',
            name='due_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='reviewcycle',
            name='renewed_from',
            field=models.ForeignKey(
                blank=True,
                help_text='The previous cycle whose settings and reviewer list were reused.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='renewals',
                to='reviews.reviewcycle',
            ),
        ),
    ]
