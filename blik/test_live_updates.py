from django.test import TestCase
from django.urls import reverse

from accounts.factories import UserProfileFactory
from accounts.models import Reviewee, Team
from core.factories import OrganizationFactory


class DashboardRevisionTests(TestCase):
    def setUp(self):
        self.organization = OrganizationFactory()
        self.profile = UserProfileFactory(organization=self.organization)
        self.client.force_login(self.profile.user)

    def revision(self):
        response = self.client.get(reverse('dashboard_revision'))
        self.assertEqual(response.status_code, 200)
        return response.json()['revision']

    def test_visible_team_change_updates_revision(self):
        reviewee, _ = Reviewee.objects.get_or_create(
            organization=self.organization,
            email=self.profile.user.email,
            defaults={'name': 'Visible Person'},
        )
        reviewee.profile = self.profile
        reviewee.save(update_fields=['profile', 'updated_at'])
        team = Team.objects.create(organization=self.organization, name='One')
        reviewee.team = team
        reviewee.save(update_fields=['team', 'updated_at'])
        initial = self.revision()

        team.name = 'Renamed'
        team.save(update_fields=['name', 'updated_at'])

        self.assertNotEqual(self.revision(), initial)

    def test_other_organization_change_does_not_update_revision(self):
        initial = self.revision()
        other = OrganizationFactory()
        Team.objects.create(organization=other, name='Secret team')

        self.assertEqual(self.revision(), initial)

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse('dashboard_revision'))

        self.assertEqual(response.status_code, 302)
