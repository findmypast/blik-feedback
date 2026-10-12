"""Send automatic assessment reminders around review-cycle due dates."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from reviews.models import ReviewCycle
from reviews.services import send_reminder_emails


# Days relative to the due date: negative is before it, positive is overdue.
REMINDER_OFFSETS = (-1, 0, 3, 7)


class Command(BaseCommand):
    help = (
        'Send reminders 1 day before, on the due date, and 3 and 7 days '
        'after it for incomplete assessments.'
    )

    def handle(self, *args, **options):
        today = timezone.localdate()
        due_dates = {today - timedelta(days=offset) for offset in REMINDER_OFFSETS}
        sent = 0
        skipped = 0
        errors = []

        cycles = ReviewCycle.objects.filter(
            status='active',
            due_date__in=due_dates,
        ).select_related('reviewee', 'questionnaire')

        for cycle in cycles:
            # A daily scheduler may retry the command; don't send the same
            # reviewer more than one automatic reminder per calendar day.
            token_ids = list(
                cycle.tokens.filter(
                    reviewer_email__isnull=False,
                    invitation_sent_at__isnull=False,
                    completed_at__isnull=True,
                ).exclude(
                    last_reminder_sent_at__date=today,
                ).values_list('id', flat=True)
            )
            if not token_ids:
                skipped += 1
                continue

            result = send_reminder_emails(cycle, token_ids=token_ids)
            sent += result['sent']
            errors.extend(result['errors'])

        self.stdout.write(
            self.style.SUCCESS(
                f'Sent {sent} due-date reminder(s); skipped {skipped} cycle(s).'
            )
        )
        for error in errors:
            self.stderr.write(self.style.ERROR(error))
