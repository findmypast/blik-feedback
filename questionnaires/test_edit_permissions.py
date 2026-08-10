from django.test import Client, TestCase
from django.urls import reverse

from accounts.factories import UserProfileFactory
from accounts.models import Team, TeamLeadGrant
from core.factories import OrganizationFactory, UserFactory
from questionnaires.factories import QuestionnaireFactory
from questionnaires.models import Questionnaire


class QuestionnaireEditPermissionTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.owner = UserFactory(email='owner@example.com')
        self.owner_profile = UserProfileFactory(user=self.owner, organization=self.org)
        self.other = UserFactory(email='other@example.com')
        self.other_profile = UserProfileFactory(user=self.other, organization=self.org)
        self.questionnaire = QuestionnaireFactory(
            organization=self.org, created_by=self.owner
        )
        self.client = Client()

    def test_member_can_create_and_continue_editing_own_questionnaire(self):
        self.client.force_login(self.other)

        response = self.client.post(reverse('questionnaire_create'), {
            'name': 'My questionnaire',
            'description': 'Created by a member',
        })

        questionnaire = Questionnaire.objects.get(name='My questionnaire')
        self.assertEqual(questionnaire.created_by, self.other)
        self.assertRedirects(
            response, reverse('questionnaire_edit', args=[questionnaire.id])
        )

    def test_member_cannot_edit_another_members_questionnaire(self):
        self.client.force_login(self.other)

        response = self.client.get(
            reverse('questionnaire_edit', args=[self.questionnaire.id])
        )

        self.assertEqual(response.status_code, 403)

    def test_top_level_team_manager_can_edit_any_questionnaire(self):
        Team.objects.create(
            organization=self.org, name='Root team', manager=self.other_profile
        )
        self.client.force_login(self.other)

        response = self.client.get(
            reverse('questionnaire_edit', args=[self.questionnaire.id])
        )

        self.assertEqual(response.status_code, 200)

    def test_nested_team_manager_cannot_edit_another_questionnaire(self):
        root = Team.objects.create(organization=self.org, name='Root team')
        Team.objects.create(
            organization=self.org, name='Nested team', parent=root,
            manager=self.other_profile,
        )
        self.client.force_login(self.other)

        response = self.client.get(
            reverse('questionnaire_edit', args=[self.questionnaire.id])
        )

        self.assertEqual(response.status_code, 403)

    def test_top_level_lead_grant_can_edit_any_questionnaire(self):
        root = Team.objects.create(organization=self.org, name='Root team')
        TeamLeadGrant.objects.create(profile=self.other_profile, team=root)
        self.client.force_login(self.other)

        response = self.client.get(
            reverse('questionnaire_edit', args=[self.questionnaire.id])
        )

        self.assertEqual(response.status_code, 200)

    def test_organization_admin_can_edit_any_questionnaire(self):
        admin = UserFactory(email='admin@example.com', is_superuser=True, is_staff=True)
        UserProfileFactory(user=admin, organization=self.org)
        self.client.force_login(admin)

        response = self.client.get(
            reverse('questionnaire_edit', args=[self.questionnaire.id])
        )

        self.assertEqual(response.status_code, 200)

    def test_list_hides_edit_link_for_questionnaires_member_does_not_own(self):
        self.client.force_login(self.other)

        response = self.client.get(reverse('questionnaire_list'))

        self.assertNotContains(
            response, reverse('questionnaire_edit', args=[self.questionnaire.id])
        )
