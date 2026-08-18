from io import StringIO
from typing import ClassVar

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from accounts.models import Reviewee, Team, TeamMembership, UserProfile
from core.models import Organization


@override_settings(DEBUG=True)
class SeedScaleTestCommandTests(TestCase):
    command_options: ClassVar[dict] = {
        'organization': 'Blik Scale Test',
        'users': 10,
        'teams': 5,
        'seed': 1183,
        'password': 'test-password',
    }

    def run_command(self, **overrides):
        options = {**self.command_options, **overrides}
        output = StringIO()
        call_command('seed_scale_test', stdout=output, **options)
        return output.getvalue()

    def test_creates_realistic_repeatable_organization(self):
        output = self.run_command()

        organization = Organization.objects.get(name='Blik Scale Test')
        self.assertEqual(organization.email, 'scale-test@blik.invalid')
        self.assertEqual(UserProfile.objects.filter(organization=organization).count(), 10)
        self.assertEqual(Reviewee.objects.filter(organization=organization).count(), 10)
        self.assertEqual(Team.objects.filter(organization=organization).count(), 5)
        self.assertEqual(
            Reviewee.objects.filter(organization=organization, is_active=False).count(),
            1,
        )
        self.assertEqual(
            Reviewee.objects.filter(organization=organization, team__isnull=True).count(),
            2,
        )
        self.assertEqual(
            Reviewee.objects.filter(
                organization=organization,
                team_memberships__isnull=False,
            ).distinct().count(),
            8,
        )
        for team in Team.objects.filter(organization=organization):
            self.assertIsNotNone(team.manager_id)
            self.assertTrue(TeamMembership.objects.filter(
                reviewee=team.manager.reviewee,
                team=team,
            ).exists())
        self.assertIn('scale-admin@scale-test.invalid', output)
        self.assertIn('Multi-team users: 1', output)
        self.assertTrue(
            UserProfile.objects.get(
                organization=organization,
                user__email='scale-admin@scale-test.invalid',
            ).user.check_password('test-password')
        )

    def test_reset_recreates_only_the_marked_organization(self):
        self.run_command()
        original = Organization.objects.get(name='Blik Scale Test')
        original_id = original.id
        unrelated = Organization.objects.create(
            name='Real Organisation', email='real@example.com'
        )

        self.run_command(reset=True)

        recreated = Organization.objects.get(name='Blik Scale Test')
        self.assertNotEqual(recreated.id, original_id)
        self.assertTrue(Organization.objects.filter(pk=unrelated.pk).exists())
        self.assertEqual(UserProfile.objects.filter(organization=recreated).count(), 10)

    def test_refuses_to_modify_an_unmarked_organization(self):
        Organization.objects.create(
            name='Blik Scale Test', email='real@example.com'
        )

        with self.assertRaisesMessage(CommandError, 'not marked as synthetic'):
            self.run_command(reset=True)

    @override_settings(DEBUG=False)
    def test_refuses_non_debug_environment_without_explicit_override(self):
        with self.assertRaisesMessage(CommandError, 'DEBUG is false'):
            self.run_command()
