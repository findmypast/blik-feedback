import uuid
from django.db import models
from django.contrib.auth.models import User
from core.models import TimeStampedModel
from core.managers import ReviewCycleManager, ReviewerTokenManager, ResponseManager
from accounts.models import Reviewee
from accounts.models import Team
from core.models import Organization
from questionnaires.models import Questionnaire, Question


class ReviewCampaign(TimeStampedModel):
    """A manager-created campaign grouping one or more individual cycles."""

    TARGET_CHOICES = [('team', 'Team'), ('individual', 'Individual')]
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
    renewed_from = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='renewals'
    )

    class Meta:
        db_table = 'review_campaigns'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_cycle_type_display()} – {self.organization}'


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
