"""Create a deterministic, realistic organisation for local scale testing."""

import random

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction

from accounts.models import OrganizationRole, Reviewee, Team, TeamMembership, UserProfile
from accounts.permissions import assign_organization_admin, assign_organization_member
from core.models import Organization
from questionnaires.models import Question, Questionnaire, QuestionSection

User = get_user_model()


class Command(BaseCommand):
    help = 'Create a deterministic synthetic organisation for local scale testing.'

    MARKER_EMAIL = 'scale-test@blik.invalid'
    EMAIL_DOMAIN = 'scale-test.invalid'
    DEFAULT_PASSWORD = 'blik-test-password'

    TEAM_BLUEPRINT = (
        ('Engineering', None),
        ('Platform', 'Engineering'),
        ('Infrastructure', 'Platform'),
        ('Developer Experience', 'Platform'),
        ('Product Engineering', 'Engineering'),
        ('Odyssey', 'Product Engineering'),
        ('Beehive', 'Product Engineering'),
        ('Data', 'Engineering'),
        ('Analytics', 'Data'),
        ('Machine Learning', 'Data'),
        ('Quality Engineering', 'Engineering'),
        ('Security', 'Engineering'),
    )

    FIRST_NAMES = (
        'Alex', 'Amara', 'Avery', 'Casey', 'Charlie', 'Devon', 'Elliot', 'Harper',
        'Jamie', 'Jordan', 'Kai', 'Morgan', 'Nadia', 'Noah', 'Priya', 'Quinn',
        'Riley', 'Robin', 'Sam', 'Taylor', 'Zara',
    )
    LAST_NAMES = (
        'Ahmed', 'Brown', 'Campbell', 'Chen', 'Davies', 'Evans', 'Garcia',
        'Hughes', 'Jones', 'Khan', 'Lewis', 'Martin', 'Patel', 'Roberts',
        'Singh', 'Smith', 'Taylor', 'Thomas', 'Walker', 'Wilson',
    )

    def add_arguments(self, parser):
        parser.add_argument('--organization', default='Blik Scale Test')
        parser.add_argument('--users', type=int, default=100)
        parser.add_argument('--teams', type=int, default=12)
        parser.add_argument('--seed', type=int, default=1183)
        parser.add_argument('--password', default=self.DEFAULT_PASSWORD)
        parser.add_argument(
            '--reset', action='store_true',
            help='Delete and recreate only the marked synthetic organisation.',
        )
        parser.add_argument(
            '--allow-non-debug', action='store_true',
            help='Explicitly permit use when DEBUG is false.',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['allow_non_debug']:
            raise CommandError(
                'Refusing to seed scale-test data while DEBUG is false. '
                'Use --allow-non-debug only in an isolated non-production environment.'
            )

        name = options['organization'].strip()
        user_count = options['users']
        team_count = options['teams']
        if not name:
            raise CommandError('Organisation name cannot be empty.')
        if team_count < 3:
            raise CommandError('--teams must be at least 3.')
        if user_count < team_count + 3:
            raise CommandError('--users must allow one manager per team and at least three members.')
        if not options['password']:
            raise CommandError('--password cannot be empty.')

        existing = Organization.objects.filter(name=name).first()
        if existing and existing.email != self.MARKER_EMAIL:
            raise CommandError(
                f'Organisation “{name}” exists but is not marked as synthetic. '
                'It will not be modified.'
            )
        if existing and not options['reset']:
            raise CommandError(
                f'Organisation “{name}” already exists. Use --reset to recreate it.'
            )

        with transaction.atomic():
            if existing:
                self._delete_synthetic_organization(existing)
            self._repair_sequences()
            result = self._create_organization(
                name=name,
                user_count=user_count,
                team_count=team_count,
                password=options['password'],
                seed=options['seed'],
            )

        self._print_summary(result, options['password'])

    def _delete_synthetic_organization(self, organization):
        if organization.email != self.MARKER_EMAIL:
            raise CommandError('Reset refused: the organisation does not have the scale-test marker.')
        user_ids = list(organization.users.values_list('user_id', flat=True))
        # Team and role hierarchies deliberately protect their parents. Remove
        # only this marked organisation's hierarchy from the leaves upward.
        while organization.teams.exists():
            leaves = organization.teams.filter(children__isnull=True)
            if not leaves.exists():
                raise CommandError('Reset refused: the synthetic team hierarchy contains a cycle.')
            leaves.delete()
        organization.users.update(organization_role=None)
        while organization.roles.exists():
            leaves = organization.roles.filter(children__isnull=True)
            if not leaves.exists():
                raise CommandError('Reset refused: the synthetic role hierarchy contains a cycle.')
            leaves.delete()
        organization.delete()
        User.objects.filter(id__in=user_ids).delete()

    def _repair_sequences(self):
        """Align fixture-sensitive primary-key sequences before inserting."""
        models = [
            Organization, Questionnaire, QuestionSection, Question, User,
            UserProfile, Reviewee, Team, TeamMembership, OrganizationRole,
        ]
        statements = connection.ops.sequence_reset_sql(no_style(), models)
        if statements:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)

    def _create_organization(self, *, name, user_count, team_count, password, seed):
        rng = random.Random(seed)
        encoded_password = make_password(password)
        organization = Organization.objects.create(
            name=name,
            email=self.MARKER_EMAIL,
            from_email='reviews@scale-test.invalid',
            min_responses_for_anonymity=3,
            allow_registration=False,
            default_users_can_create_cycles=False,
        )
        member_role = OrganizationRole.objects.create(
            organization=organization,
            name='Member',
        )
        leader_role = OrganizationRole.objects.create(
            organization=organization,
            name='Team Leader',
            parent=member_role,
            can_create_cycles=True,
            can_manage_teams=True,
            can_view_reports=True,
        )

        admin_profile = self._create_person(
            organization=organization,
            email=f'scale-admin@{self.EMAIL_DOMAIN}',
            first_name='Scale',
            last_name='Administrator',
            encoded_password=encoded_password,
            role=leader_role,
        )
        assign_organization_admin(admin_profile.user)

        manager_profiles = [admin_profile]
        for index in range(1, team_count):
            manager_profiles.append(self._create_person(
                organization=organization,
                email=f'team-lead-{index:02d}@{self.EMAIL_DOMAIN}',
                first_name='Team',
                last_name=f'Lead {index:02d}',
                encoded_password=encoded_password,
                role=leader_role,
                can_create_cycles=True,
            ))

        blueprints = self._team_blueprints(team_count)
        teams_by_name = {}
        teams = []
        for index, (team_name, parent_name) in enumerate(blueprints):
            team = Team.objects.create(
                organization=organization,
                name=team_name,
                parent=teams_by_name.get(parent_name),
                manager=manager_profiles[index],
            )
            teams_by_name[team_name] = team
            teams.append(team)

        ordinary_profiles = []
        ordinary_count = user_count - len(manager_profiles)
        for index in range(1, ordinary_count + 1):
            first_name = self.FIRST_NAMES[(index - 1) % len(self.FIRST_NAMES)]
            last_name = self.LAST_NAMES[((index - 1) // len(self.FIRST_NAMES)) % len(self.LAST_NAMES)]
            ordinary_profiles.append(self._create_person(
                organization=organization,
                email=f'engineer-{index:03d}@{self.EMAIL_DOMAIN}',
                first_name=first_name,
                last_name=f'{last_name} {index:03d}',
                encoded_password=encoded_password,
                role=member_role,
            ))

        unassigned_count = min(2, len(ordinary_profiles))
        assignable_profiles = ordinary_profiles[:-unassigned_count] if unassigned_count else ordinary_profiles
        leaf_teams = [team for team in teams if not team.children.exists()] or teams
        for index, profile in enumerate(assignable_profiles):
            team = leaf_teams[index % len(leaf_teams)]
            reviewee = profile.reviewee
            reviewee.team = team
            reviewee.reporting_manager = team.manager
            reviewee.department = team.name
            reviewee.save(update_fields=[
                'team', 'reporting_manager', 'department', 'updated_at',
            ])

        multi_team_count = min(
            len(assignable_profiles), max(1, round(len(ordinary_profiles) * 0.15))
        )
        multi_team_profiles = rng.sample(assignable_profiles, multi_team_count)
        for profile in multi_team_profiles:
            reviewee = profile.reviewee
            alternatives = [team for team in leaf_teams if team.id != reviewee.team_id]
            if alternatives:
                TeamMembership.objects.get_or_create(
                    reviewee=reviewee,
                    team=rng.choice(alternatives),
                )

        inactive_count = min(4, max(1, round(len(ordinary_profiles) * 0.04)))
        inactive_profiles = rng.sample(ordinary_profiles, inactive_count)
        for profile in inactive_profiles:
            profile.user.is_active = False
            profile.user.save(update_fields=['is_active'])
            reviewee = profile.reviewee
            reviewee.is_active = False
            reviewee.save(update_fields=['is_active', 'updated_at'])

        return {
            'organization': organization,
            'admin': admin_profile.user,
            'teams': teams,
            'profiles': manager_profiles + ordinary_profiles,
            'inactive_count': inactive_count,
            'unassigned_count': unassigned_count,
            'multi_team_count': multi_team_count,
            'questionnaire_count': Questionnaire.objects.for_organization(organization).count(),
        }

    def _create_person(
        self, *, organization, email, first_name, last_name, encoded_password, role,
        can_create_cycles=False,
    ):
        user = User.objects.create(
            username=email,
            email=email,
            password=encoded_password,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )
        profile = UserProfile.objects.create(
            user=user,
            organization=organization,
            organization_role=role,
            can_create_cycles_for_others=can_create_cycles,
            has_seen_welcome=True,
        )
        reviewee = Reviewee.objects.get(organization=organization, email=email)
        reviewee.profile = profile
        reviewee.save(update_fields=['profile', 'updated_at'])
        assign_organization_member(user, can_create_cycles_for_others=can_create_cycles)
        return profile

    def _team_blueprints(self, count):
        blueprints = list(self.TEAM_BLUEPRINT[:count])
        while len(blueprints) < count:
            index = len(blueprints) + 1
            blueprints.append((f'Engineering Team {index:02d}', 'Engineering'))
        return blueprints

    def _print_summary(self, result, password):
        organization = result['organization']
        self.stdout.write(self.style.SUCCESS('\nScale-test organisation created successfully.'))
        self.stdout.write(f'Organisation: {organization.name}')
        self.stdout.write(f'Administrator: {result["admin"].email}')
        self.stdout.write(f'Password: {password}')
        self.stdout.write(f'Teams: {len(result["teams"])}')
        self.stdout.write(f'Users: {len(result["profiles"])}')
        self.stdout.write(f'Inactive users: {result["inactive_count"]}')
        self.stdout.write(f'Unassigned users: {result["unassigned_count"]}')
        self.stdout.write(f'Multi-team users: {result["multi_team_count"]}')
        self.stdout.write(f'Questionnaires: {result["questionnaire_count"]}')
        self.stdout.write('\nExample accounts:')
        self.stdout.write(f'  {result["admin"].email}')
        self.stdout.write(f'  team-lead-01@{self.EMAIL_DOMAIN}')
        self.stdout.write(f'  engineer-001@{self.EMAIL_DOMAIN}')
