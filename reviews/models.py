import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from core.models import TimeStampedModel
from core.managers import ReviewCycleManager, ReviewerTokenManager, ResponseManager
from accounts.models import Reviewee
from accounts.models import Team
from core.models import Organization
from questionnaires.models import Questionnaire, Question


class OrganizationalReviewCycle(TimeStampedModel):
    """Coordinates self, peer, and manager campaigns across an organization."""

    STATUS_CHOICES = [('active', 'Active'), ('completed', 'Completed')]
    AUDIENCE_CHOICES = [
        ('individuals', 'Individual(s)'),
        ('teams', 'Team(s)'),
        ('entire', 'Entire organisation'),
    ]
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    name = models.CharField(max_length=255, blank=True)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='organizational_review_cycles'
    )
    teams = models.ManyToManyField(
        Team,
        blank=True,
        related_name='organizational_review_cycles',
        help_text='Teams included in this organisation review.',
    )
    selected_reviewees = models.ManyToManyField(
        Reviewee,
        blank=True,
        related_name='selected_organizational_review_cycles',
    )
    audience_type = models.CharField(
        max_length=20,
        choices=AUDIENCE_CHOICES,
        default='entire',
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_organizational_review_cycles',
    )
    self_questionnaire = models.ForeignKey(
        Questionnaire, on_delete=models.PROTECT, related_name='+')
    peer_questionnaire = models.ForeignKey(
        Questionnaire, on_delete=models.PROTECT, related_name='+')
    manager_questionnaire = models.ForeignKey(
        Questionnaire, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    minimum_peer_reviewers = models.PositiveSmallIntegerField(default=3)
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    class Meta:
        db_table = 'organizational_review_cycles'
        ordering = ['-created_at']

    def __str__(self):
        return f'Organisation cycle – {self.organization}'

    @property
    def display_name(self):
        if self.name:
            return self.name
        cycle_date = self.start_date or self.created_at.date()
        return f'{cycle_date:%B %Y} Organisation Review'

    @property
    def audience_label(self):
        if self.audience_type == 'entire':
            return 'Entire organisation'
        if self.audience_type == 'individuals':
            return 'Selected individuals'
        names = list(self.teams.order_by('name').values_list('name', flat=True))
        return ', '.join(names) or 'Selected teams'

    @property
    def is_overdue(self):
        return bool(
            self.status == 'active'
            and self.due_date
            and self.due_date < timezone.localdate()
        )


class TeamCycleCompletionNotification(TimeStampedModel):
    """Records the one-time administrator notification for a completed team."""

    organizational_cycle = models.ForeignKey(
        OrganizationalReviewCycle,
        on_delete=models.CASCADE,
        related_name='team_completion_notifications',
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='organizational_cycle_completion_notifications',
    )
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organizational_cycle', 'team'],
                name='unique_organizational_team_completion_notification',
            ),
        ]


class ReviewCampaign(TimeStampedModel):
    """A manager-created campaign grouping one or more individual cycles."""

    TARGET_CHOICES = [
        ('team', 'Team'), ('individual', 'Individual'),
        ('organization', 'Organisation'),
    ]
    TYPE_CHOICES = [
        ('peer', 'Peer review'),
        ('self', 'Self-assessment'),
        ('manager', 'Manager assessment'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('completed', 'Completed'),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    name = models.CharField(max_length=255, blank=True)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='review_campaigns'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='created_review_campaigns'
    )
    questionnaire = models.ForeignKey(
        Questionnaire, on_delete=models.PROTECT, related_name='review_campaigns'
    )
    target_type = models.CharField(max_length=20, choices=TARGET_CHOICES)
    team = models.ForeignKey(
        Team, on_delete=models.PROTECT, null=True, blank=True, related_name='review_campaigns'
    )
    individual = models.ForeignKey(
        Reviewee, on_delete=models.PROTECT, null=True, blank=True,
        related_name='individual_review_campaigns'
    )
    include_descendants = models.BooleanField(default=False)
    cycle_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    minimum_peer_reviewers = models.PositiveSmallIntegerField(
        default=1,
        help_text='Minimum number of peer nominations each participant must submit.',
    )
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    completion_notification_sent_at = models.DateTimeField(null=True, blank=True)
    renewed_from = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='renewals'
    )
    organizational_cycle = models.ForeignKey(
        OrganizationalReviewCycle,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='campaigns',
    )

    class Meta:
        db_table = 'review_campaigns'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_cycle_type_display()} – {self.organization}'

    @property
    def display_name(self):
        type_label = self.get_cycle_type_display().title()
        if self.name:
            return f'{self.name} — {type_label}'
        cycle_date = self.start_date or self.created_at.date()
        scope = f' — {self.team.name}' if self.team_id else ''
        return f'{cycle_date:%B %Y}{scope} — {type_label} Cycle'

    @property
    def scope_label(self):
        if self.target_type == 'organization':
            return 'Organisation-wide'
        if self.target_type == 'team' and self.team_id:
            suffix = ' and nested teams' if self.include_descendants else ''
            return f'{self.team.name}{suffix}'
        if self.target_type == 'individual' and self.individual_id:
            return self.individual.name
        return 'Scope not set'

    @property
    def is_overdue(self):
        return bool(
            self.status == 'active'
            and self.due_date
            and self.due_date < timezone.localdate()
        )


class ReviewCycle(TimeStampedModel):
    """360 feedback review cycle for a reviewee"""

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('completed', 'Completed'),
    ]

    TYPE_CHOICES = [
        ('360', '360 feedback'),
        ('peer', 'Peer review'),
        ('self', 'Self-assessment'),
        ('manager', 'Manager review'),
    ]

    # Public UUID for external references (API, URLs)
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
        help_text="Public identifier for API and URL usage (non-enumerable)"
    )

    reviewee = models.ForeignKey(
        Reviewee,
        on_delete=models.CASCADE,
        related_name='review_cycles'
    )
    questionnaire = models.ForeignKey(
        Questionnaire,
        on_delete=models.PROTECT,
        related_name='review_cycles'
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_review_cycles'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    cycle_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='360')
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    renewed_from = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='renewals',
        help_text='The previous cycle whose settings and reviewer list were reused.',
    )
    campaign = models.ForeignKey(
        ReviewCampaign,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='cycles',
    )
    close_check_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the close check-in email was sent to the reviewee"
    )

    @property
    def organization(self):
        """Get organization through reviewee relationship"""
        return self.reviewee.organization

    # Secure invitation tokens per category (non-enumerable)
    invitation_token_self = models.UUIDField(unique=True, db_index=True, null=True, blank=True)
    invitation_token_peer = models.UUIDField(unique=True, db_index=True, null=True, blank=True)
    invitation_token_manager = models.UUIDField(unique=True, db_index=True, null=True, blank=True)
    invitation_token_direct_report = models.UUIDField(unique=True, db_index=True, null=True, blank=True)

    objects = ReviewCycleManager()

    class Meta:
        db_table = 'review_cycles'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.reviewee.name} - {self.created_at.strftime('%Y-%m-%d')}"

    def save(self, *args, **kwargs):
        """Auto-generate invitation tokens for all categories on creation"""
        if not self.pk:  # New instance
            if not self.invitation_token_self:
                self.invitation_token_self = uuid.uuid4()
            if not self.invitation_token_peer:
                self.invitation_token_peer = uuid.uuid4()
            if not self.invitation_token_manager:
                self.invitation_token_manager = uuid.uuid4()
            if not self.invitation_token_direct_report:
                self.invitation_token_direct_report = uuid.uuid4()
        super().save(*args, **kwargs)

    def get_invitation_token(self, category):
        """Get the invitation token for a specific category"""
        token_map = {
            'self': self.invitation_token_self,
            'peer': self.invitation_token_peer,
            'manager': self.invitation_token_manager,
            'direct_report': self.invitation_token_direct_report,
        }
        return token_map.get(category)

    @property
    def participant_team_label(self):
        if self.campaign_id and self.campaign.team_id:
            return self.campaign.team.name
        names = {membership.team.name for membership in self.reviewee.team_memberships.all()}
        if self.reviewee.team_id:
            names.add(self.reviewee.team.name)
        return ', '.join(sorted(names)) or 'Organisation-wide'

    @property
    def is_overdue(self):
        return bool(
            self.status == 'active'
            and self.due_date
            and self.due_date < timezone.localdate()
        )


class ReviewerToken(TimeStampedModel):
    """Anonymous token for reviewer access"""

    CATEGORY_CHOICES = [
        ('self', 'Self Assessment'),
        ('peer', 'Peer Review'),
        ('manager', 'Manager Review'),
        ('direct_report', 'Direct Report Review'),
    ]

    cycle = models.ForeignKey(
        ReviewCycle,
        on_delete=models.CASCADE,
        related_name='tokens'
    )
    assigned_team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewer_assignments',
        help_text='Team that caused this review assignment, when applicable.',
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    reviewer_email = models.EmailField(
        null=True,
        blank=True,
        help_text="Email to send invitation to (not stored with responses for anonymity)"
    )
    invitation_sent_at = models.DateTimeField(null=True, blank=True)
    last_reminder_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the last reminder email was sent to this reviewer"
    )
    claimed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When someone claimed this token by clicking the invitation link"
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = ReviewerTokenManager()

    class Meta:
        db_table = 'reviewer_tokens'
        ordering = ['cycle', 'category']

    def __str__(self):
        return f"{self.cycle} - {self.get_category_display()}"

    @property
    def is_completed(self):
        return self.completed_at is not None

    @property
    def assignment_team_label(self):
        if self.assigned_team_id:
            return self.assigned_team.name
        if self.cycle.campaign_id and self.cycle.campaign.team_id:
            return self.cycle.campaign.team.name
        names = {membership.team.name for membership in self.cycle.reviewee.team_memberships.all()}
        if self.cycle.reviewee.team_id:
            names.add(self.cycle.reviewee.team.name)
        return ', '.join(sorted(names)) or 'Organisation-wide'


class Response(TimeStampedModel):
    """Individual response to a question"""

    cycle = models.ForeignKey(
        ReviewCycle,
        on_delete=models.CASCADE,
        related_name='responses'
    )
    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name='responses'
    )
    token = models.ForeignKey(
        ReviewerToken,
        on_delete=models.CASCADE,
        related_name='responses'
    )
    category = models.CharField(max_length=20)  # Denormalized from token for reporting

    # JSON field for storing answer data
    # For rating: {"value": 4}
    # For scale: {"value": 75}
    # For text: {"value": "text response"}
    # For single_choice: {"value": "Option 1"}
    # For multiple_choice: {"value": ["Option 1", "Option 2"]}
    answer_data = models.JSONField()

    objects = ResponseManager()

    class Meta:
        db_table = 'responses'
        ordering = ['cycle', 'question']
        unique_together = ['token', 'question']

    def __str__(self):
        return f"{self.cycle} - {self.question.question_text[:30]}"
