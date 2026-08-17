from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ReviewCycle, ReviewerToken


@transaction.atomic
def renew_cycle(source_cycle, created_by, *, start_date=None, due_date=None):
    """Create a fresh cycle while preserving the source cycle and its results."""
    start_date = start_date or timezone.localdate()
    if due_date is None and source_cycle.start_date and source_cycle.due_date:
        due_date = start_date + (source_cycle.due_date - source_cycle.start_date)
    elif due_date is None:
        due_date = start_date + timedelta(days=30)

    cycle = ReviewCycle.objects.create(
        reviewee=source_cycle.reviewee,
        questionnaire=source_cycle.questionnaire,
        created_by=created_by,
        status='active',
        cycle_type=source_cycle.cycle_type,
        start_date=start_date,
        due_date=due_date,
        renewed_from=source_cycle,
    )
    ReviewerToken.objects.bulk_create([
        ReviewerToken(
            cycle=cycle,
            category=token.category,
            reviewer_email=token.reviewer_email,
            assigned_team=token.assigned_team,
        )
        for token in source_cycle.tokens.all()
    ])
    return cycle
