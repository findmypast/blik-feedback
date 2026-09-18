"""Send automatic assessment reminders around review-cycle due dates."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from reviews.models import ReviewCycle
from reviews.services import send_reminder_emails


class Command(BaseCommand):
    help = 'Send reminders two days before, and on the due date, for incomplete assessments.'

    def handle(self, *args, **options):
        today = timezone.localdate()
        reminder_dates = {today, today + timedelta(days=2)}
        sent = 0
        skipped = 0
        errors = []

        cycles = ReviewCycle.objects.filter(
            status='active',
            due_date__in=reminder_dates,
        ).select_related('reviewee', 'questionnaire')

        for cycle in cycles:
            # A daily scheduler may retry the command; don't send the same
            # automatic reminder more than once on a calendar day.
            already_sent_today = cycle.tokens.filter(
                last_reminder_sent_at__date=today,
            ).exists()
            if already_sent_today:
                skipped += 1
                continue

            result = send_reminder_emails(cycle)
            sent += result['sent']
            errors.extend(result['errors'])

        self.stdout.write(
            self.style.SUCCESS(
                f'Sent {sent} due-date reminder(s); skipped {skipped} cycle(s).'
            )
        )
        for error in errors:
            self.stderr.write(self.style.ERROR(error))
