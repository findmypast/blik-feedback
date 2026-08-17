from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0014_organization_roles'),
        ('reviews', '0012_cycle_names'),
    ]

    operations = [
        migrations.AddField(
            model_name='reviewertoken',
            name='assigned_team',
            field=models.ForeignKey(
                blank=True,
                help_text='Team that caused this review assignment, when applicable.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='reviewer_assignments',
                to='accounts.team',
            ),
        ),
    ]
