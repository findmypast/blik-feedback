"""Validate structural and review-flow invariants in a scale-test organisation."""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, F, Q

from accounts.authorization import visible_cycles
from accounts.models import Reviewee, Team, TeamMembership, UserProfile
from core.models import Organization
from questionnaires.models import Questionnaire
from reviews.models import OrganizationalReviewCycle, ReviewCycle, ReviewerToken


class Command(BaseCommand):
    help = 'Validate a synthetic scale-test organisation and its review assignments.'

    MARKER_EMAIL = 'scale-test@blik.invalid'

    def add_arguments(self, parser):
        parser.add_argument('--organization', default='Blik Scale Test')

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(name=options['organization'])
        except Organization.DoesNotExist as exc:
            raise CommandError('Scale-test organisation was not found.') from exc
        if organization.email != self.MARKER_EMAIL:
            raise CommandError('Validation refused: the organisation is not marked as synthetic.')

        self.failures = []
        self.warnings = []
        self.stdout.write(f'Validating {organization.name}\n')
        self._validate_structure(organization)
        self._validate_identifiers(organization)
        self._validate_cycles(organization)

        self.stdout.write('')
        if self.failures:
            self.stdout.write(self.style.ERROR(
                f'FAILED: {len(self.failures)} check(s) failed; '
                f'{len(self.warnings)} warning(s).'
            ))
            raise CommandError('Scale-test validation failed.')
        self.stdout.write(self.style.SUCCESS(
            f'PASSED: all checks succeeded with {len(self.warnings)} warning(s).'
        ))

    def _pass(self, message):
        self.stdout.write(self.style.SUCCESS(f'PASS  {message}'))

    def _warn(self, message):
        self.warnings.append(message)
        self.stdout.write(self.style.WARNING(f'WARN  {message}'))

    def _fail(self, message):
        self.failures.append(message)
        self.stdout.write(self.style.ERROR(f'FAIL  {message}'))

    def _check(self, condition, success, failure):
        self._pass(success) if condition else self._fail(failure)

    def _validate_structure(self, organization):
        profiles = UserProfile.objects.filter(organization=organization)
        reviewees = Reviewee.objects.filter(organization=organization)
        teams = Team.objects.filter(organization=organization)

        self._check(
            profiles.count() == reviewees.filter(profile__isnull=False).count(),
            f'{profiles.count()} user profiles have linked reviewees',
            'One or more user profiles do not have a linked reviewee',
        )
        self._check(
            teams.filter(parent__isnull=False).exists(),
            f'{teams.filter(parent__isnull=False).count()} nested teams exist',
            'No nested teams exist',
        )
        self._check(
            not teams.filter(manager__isnull=True).exists(),
            f'All {teams.count()} teams have managers',
            'One or more teams have no manager',
        )
        missing_manager_memberships = [
            team.name for team in teams.select_related('manager__reviewee')
            if team.manager_id and not TeamMembership.objects.filter(
                reviewee=team.manager.reviewee, team=team
            ).exists()
        ]
        self._check(
            not missing_manager_memberships,
            'Every team manager is a member of their team',
            f'Managers are missing team membership: {", ".join(missing_manager_memberships)}',
        )
        multi_team_count = reviewees.annotate(
            membership_count=Count('team_memberships', distinct=True)
        ).filter(membership_count__gt=1).count()
        self._check(
            multi_team_count > 0,
            f'{multi_team_count} multi-team users exist',
            'No multi-team users exist',
        )
        self._check(
            reviewees.filter(is_active=False).exists(),
            f'{reviewees.filter(is_active=False).count()} inactive users exist',
            'No inactive users exist',
        )
        self._check(
            reviewees.filter(team__isnull=True, team_memberships__isnull=True).exists(),
            f'{reviewees.filter(team__isnull=True, team_memberships__isnull=True).count()} unassigned users exist',
            'No users without teams exist',
        )
        questionnaires = Questionnaire.objects.for_organization(organization)
        for field, label in (
            ('allow_self_assessment', 'self-assessment'),
            ('allow_peer_review', 'peer-review'),
            ('allow_manager_assessment', 'manager-assessment'),
        ):
            self._check(
                questionnaires.filter(**{field: True}).exists(),
                f'At least one {label} questionnaire exists',
                f'No {label} questionnaire exists',
            )

    def _validate_identifiers(self, organization):
        cycles = ReviewCycle.objects.for_organization(organization)
        tokens = ReviewerToken.objects.filter(cycle__reviewee__organization=organization)
        questionnaires = Questionnaire.objects.for_organization(organization)
        for queryset, label in (
            (cycles, 'cycle'), (tokens, 'review assignment'),
            (questionnaires, 'questionnaire'),
        ):
            uuid_field = 'token' if label == 'review assignment' else 'uuid'
            total = queryset.count()
            distinct = queryset.values(uuid_field).distinct().count()
            self._check(
                total == distinct,
                f'All {total} {label} identifiers are unique',
                f'Duplicate {label} identifiers exist',
            )

    def _validate_cycles(self, organization):
        parents = OrganizationalReviewCycle.objects.filter(organization=organization)
        if not parents.exists():
            self._warn(
                'No organisation cycle exists yet; create one in the UI and rerun '
                'this command to validate assignments.'
            )
            return

        inactive_assignments = ReviewerToken.objects.filter(
            cycle__campaign__organizational_cycle__in=parents,
            reviewer_email__in=Reviewee.objects.filter(
                organization=organization, is_active=False
            ).values('email'),
        ).count()
        self._check(
            inactive_assignments == 0,
            'Inactive users have no review assignments',
            f'{inactive_assignments} assignments were sent to inactive users',
        )

        duplicates = ReviewerToken.objects.filter(
            cycle__campaign__organizational_cycle__in=parents,
        ).values(
            'cycle_id', 'reviewer_email', 'category', 'assigned_team_id'
        ).annotate(total=Count('id')).filter(total__gt=1).count()
        self._check(
            duplicates == 0,
            'No duplicate reviewer assignments exist',
            f'{duplicates} duplicate reviewer assignment group(s) exist',
        )

        for parent in parents.prefetch_related('selected_reviewees', 'campaigns__cycles'):
            self_campaigns = parent.campaigns.filter(cycle_type='self')
            self._check(
                self_campaigns.count() == 1,
                f'{parent.display_name}: one shared self-assessment campaign exists',
                f'{parent.display_name}: expected one shared self campaign, found {self_campaigns.count()}',
            )
            if self_campaigns.count() != 1:
                continue
            self_campaign = self_campaigns.first()
            participants = parent.selected_reviewees.filter(is_active=True)
            duplicate_self = self_campaign.cycles.values('reviewee_id').annotate(
                total=Count('id')
            ).filter(total__gt=1).count()
            missing_self = participants.exclude(
                id__in=self_campaign.cycles.values('reviewee_id')
            ).count()
            self._check(
                duplicate_self == 0 and missing_self == 0,
                f'{parent.display_name}: every active participant has one shared self-assessment',
                f'{parent.display_name}: {missing_self} missing and {duplicate_self} duplicate self-assessments',
            )
            self._validate_manager_targets(parent)
            self._validate_manager_visibility(parent, self_campaign)

    def _validate_manager_targets(self, parent):
        invalid = ReviewerToken.objects.filter(
            cycle__campaign__organizational_cycle=parent,
            cycle__campaign__cycle_type='manager',
            assigned_team__isnull=False,
        ).exclude(
            cycle__reviewee__profile=F('assigned_team__manager')
        ).count()
        self._check(
            invalid == 0,
            f'{parent.display_name}: manager assessments target each assigned team manager',
            f'{parent.display_name}: {invalid} manager assignments target the wrong person',
        )

    def _validate_manager_visibility(self, parent, self_campaign):
        hidden = 0
        for cycle in self_campaign.cycles.select_related('reviewee'):
            teams = Team.objects.filter(
                organization=parent.organization,
            ).filter(
                Q(reviewees=cycle.reviewee) | Q(members=cycle.reviewee)
            ).filter(manager__isnull=False).select_related('manager__user').distinct()
            for team in teams:
                if not visible_cycles(
                    team.manager.user,
                    ReviewCycle.objects.filter(pk=cycle.pk),
                    parent.organization,
                ).exists():
                    hidden += 1
        self._check(
            hidden == 0,
            f'{parent.display_name}: team managers can see members’ shared self-assessments',
            f'{parent.display_name}: {hidden} manager/self-assessment visibility checks failed',
        )
