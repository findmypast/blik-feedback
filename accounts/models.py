import uuid
from django.contrib.auth.models import User
from django.db import models
from django.utils.crypto import get_random_string
from core.models import TimeStampedModel, Organization
from core.managers import OrganizationManager


class UserProfile(TimeStampedModel):
    """Extended user profile with organization relationship"""
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='profile'
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='users'
    )
    can_create_cycles_for_others = models.BooleanField(
        default=False,
        help_text='If False, user can only create cycles for themselves'
    )
    has_seen_welcome = models.BooleanField(
        default=False,
        help_text='Whether user has seen the welcome modal'
    )

    objects = OrganizationManager()

    class Meta:
        db_table = 'user_profiles'
        ordering = ['user__username']
        permissions = [
            ('can_invite_members', 'Can invite team members'),
            ('can_manage_organization', 'Can manage organization settings'),
            ('can_delete_organization', 'Can delete organization'),
            ('can_view_all_reports', 'Can view all organization reports'),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.organization.name}"


class OrganizationInvitation(TimeStampedModel):
    """Invitation to join an organization"""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='invitations'
    )
    email = models.EmailField()
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    team = models.ForeignKey(
        'Team', on_delete=models.PROTECT, null=True, blank=True,
        related_name='invitations'
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sent_invitations'
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    objects = OrganizationManager()

    class Meta:
        db_table = 'organization_invitations'
        ordering = ['-created_at']
        unique_together = ['organization', 'email']

    def __str__(self):
        return f"Invite {self.email} to {self.organization.name}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.team_id and self.team.organization_id != self.organization_id:
            raise ValidationError({'team': 'Invitation team must belong to the same organization.'})

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = get_random_string(64)
        super().save(*args, **kwargs)

    def is_valid(self):
        """Check if invitation is still valid"""
        from django.utils import timezone
        return (
            self.accepted_at is None and
            self.expires_at > timezone.now()
        )


class PasswordResetToken(TimeStampedModel):
    """Token for password reset requests"""
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='password_reset_tokens'
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'password_reset_tokens'
        ordering = ['-created_at']

    def __str__(self):
        return f"Password reset for {self.user.email}"

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = get_random_string(64)
        if not self.expires_at:
            from django.utils import timezone
            from datetime import timedelta
            self.expires_at = timezone.now() + timedelta(hours=1)
        super().save(*args, **kwargs)

    def is_valid(self):
        """Check if token is still valid (not expired and not used)"""
        from django.utils import timezone
        return self.used_at is None and self.expires_at > timezone.now()


class Team(TimeStampedModel):
    """A hierarchical team within an organization."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='teams')
    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True, related_name='children'
    )
    manager = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='managed_teams'
    )

    objects = OrganizationManager()

    class Meta:
        db_table = 'teams'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'name'], name='unique_team_name_per_org'),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.parent_id:
            if self.parent.organization_id != self.organization_id:
                raise ValidationError({'parent': 'Parent team must belong to the same organization.'})
            ancestor = self.parent
            while ancestor:
                if ancestor.pk == self.pk:
                    raise ValidationError({'parent': 'Team hierarchy cannot contain a cycle.'})
                ancestor = ancestor.parent
        if self.manager_id and self.manager.organization_id != self.organization_id:
            raise ValidationError({'manager': 'Team manager must belong to the same organization.'})

    def __str__(self):
        return self.name


class TeamLeadGrant(TimeStampedModel):
    """Grants a member access to a team, optionally including descendants."""
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='team_lead_grants')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='lead_grants')
    include_descendants = models.BooleanField(default=False)

    class Meta:
        db_table = 'team_lead_grants'
        constraints = [
            models.UniqueConstraint(fields=['profile', 'team'], name='unique_profile_team_lead_grant'),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.profile.organization_id != self.team.organization_id:
            raise ValidationError('Lead and team must belong to the same organization.')


class TeamLeadRevocation(TimeStampedModel):
    """Removes an inherited team subtree from one lead grant."""
    grant = models.ForeignKey(TeamLeadGrant, on_delete=models.CASCADE, related_name='revocations')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='lead_revocations')

    class Meta:
        db_table = 'team_lead_revocations'
        constraints = [
            models.UniqueConstraint(fields=['grant', 'team'], name='unique_grant_team_revocation'),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.grant.team.organization_id != self.team.organization_id:
            raise ValidationError('Grant and revoked team must belong to the same organization.')


class Reviewee(TimeStampedModel):
    """Person being reviewed in 360 feedback"""
    # Public UUID for external references (API, URLs)
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
        help_text="Public identifier for API and URL usage (non-enumerable)"
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='reviewees'
    )
    profile = models.OneToOneField(
        UserProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewee'
    )
    team = models.ForeignKey(
        Team, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewees'
    )
    teams = models.ManyToManyField(
        Team,
        through='TeamMembership',
        related_name='members',
        blank=True,
        help_text='All teams this person belongs to. The team field remains the primary team.',
    )
    reporting_manager = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='direct_report_reviewees'
    )
    name = models.CharField(max_length=255)
    email = models.EmailField()
    department = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    objects = OrganizationManager()

    class Meta:
        db_table = 'reviewees'
        ordering = ['name']
        unique_together = ['organization', 'email']

    def __str__(self):
        return f"{self.name} ({self.email})"

    def clean(self):
        from django.core.exceptions import ValidationError
        for field in ('profile', 'reporting_manager'):
            value = getattr(self, field, None)
            if value and value.organization_id != self.organization_id:
                raise ValidationError({field: 'Must belong to the same organization.'})
        if self.team and self.team.organization_id != self.organization_id:
            raise ValidationError({'team': 'Must belong to the same organization.'})


class TeamMembership(TimeStampedModel):
    """An additive team membership; a person may belong to several teams."""
    reviewee = models.ForeignKey(
        Reviewee, on_delete=models.CASCADE, related_name='team_memberships'
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='memberships'
    )

    class Meta:
        db_table = 'team_memberships'
        constraints = [
            models.UniqueConstraint(
                fields=['reviewee', 'team'], name='unique_reviewee_team_membership'
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.reviewee.organization_id != self.team.organization_id:
            raise ValidationError('Person and team must belong to the same organization.')
