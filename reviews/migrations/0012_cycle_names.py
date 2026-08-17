from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0011_organizational_review_cycles'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationalreviewcycle',
            name='name',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='reviewcampaign',
            name='name',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
