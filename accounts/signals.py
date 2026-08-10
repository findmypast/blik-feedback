"""
Signal handlers for user registration
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from core.models import Organization
from core.email import send_welcome_email
from .models import Reviewee, Team, TeamMembership, UserProfile

User = get_user_model()


# NOTE: Allauth signals removed - registration now only via Stripe or invitations
# User profile creation happens in:
# 1. Stripe webhook (subscriptions/views.py) - primary registration path
# 2. Invitation acceptance (accounts/invitation_views.py) - team member invites


@receiver(post_save, sender=UserProfile)
def create_reviewee_from_user(sender, instance, created, **kwargs):
    """
    Auto-create a reviewee when a user profile is created.
    This ensures team members show up in the reviewee list automatically.
    """
    if created:
        from accounts.models import Reviewee

        # Check if reviewee already exists with this email
        existing = Reviewee.objects.filter(
            organization=instance.organization,
            email=instance.user.email
        ).first()

        if not existing:
            # Create reviewee from user
            name = instance.user.get_full_name() or instance.user.username
            Reviewee.objects.create(
                organization=instance.organization,
                name=name,
                email=instance.user.email,
                is_active=True
            )


@receiver(post_save, sender=Team)
def keep_team_manager_in_team(sender, instance, **kwargs):
    """A team's primary manager is always also a member of that team."""
    if not instance.manager_id:
        return

    manager = instance.manager
    reviewee = Reviewee.objects.filter(
        organization=instance.organization,
        email__iexact=manager.user.email,
    ).first()
    if reviewee is None:
        reviewee = Reviewee(
            organization=instance.organization,
            profile=manager,
            name=manager.user.get_full_name() or manager.user.username,
            email=manager.user.email,
        )
    else:
        reviewee.profile = manager
    if reviewee.team_id is None:
        reviewee.team = instance
    reviewee.full_clean()
    reviewee.save()
    TeamMembership.objects.get_or_create(reviewee=reviewee, team=instance)


@receiver(post_save, sender=Reviewee)
def keep_primary_team_membership(sender, instance, **kwargs):
    """Keep legacy primary-team writes represented in additive memberships."""
    if instance.team_id:
        TeamMembership.objects.get_or_create(
            reviewee_id=instance.id, team_id=instance.team_id
        )
