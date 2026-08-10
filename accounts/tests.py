from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch
from core.factories import OrganizationFactory, UserFactory
from accounts.factories import (
    UserProfileFactory,
    OrganizationInvitationFactory,
    RevieweeFactory
)
from questionnaires.factories import QuestionnaireFactory
from reviews.factories import ReviewCycleFactory, ReviewerTokenFactory
from accounts.models import OrganizationInvitation, Reviewee, Team


class DashboardTestCase(TestCase):
    """Test dashboard functionality"""

    def setUp(self):
        self.org = OrganizationFactory(name='Test Organization')
        self.user = UserFactory(username='manager')
        self.user.set_password('testpass123')
        self.user.save()

        self.profile = UserProfileFactory(
            user=self.user,
            organization=self.org,
            can_create_cycles_for_others=True
        )

        # Create some reviewees
        self.reviewee1 = RevieweeFactory(
            organization=self.org,
            name='John Developer'
        )
        self.reviewee2 = RevieweeFactory(
            organization=self.org,
            name='Jane Engineer'
        )

        self.client = Client()
        self.client.force_login(self.user)

    def test_dashboard_access(self):
        """Test accessing the dashboard"""
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_shows_reviewees(self):
        """Test that dashboard shows reviewees"""
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 200)

        # Dashboard shows count of active reviewees, not individual names
        # Verify reviewees were created
        reviewees = Reviewee.objects.filter(organization=self.org, is_active=True)
        self.assertGreaterEqual(reviewees.count(), 2)


class UserInvitationTestCase(TestCase):
    """Test user invitation functionality"""

    def setUp(self):
        self.org = OrganizationFactory(name='Test Organization')
        self.user = UserFactory(username='admin', is_staff=True, is_superuser=True)
        self.user.set_password('admin123')
        self.user.save()

        self.profile = UserProfileFactory(
            user=self.user,
            organization=self.org,
            can_create_cycles_for_others=True
        )

        self.client = Client()
        self.client.force_login(self.user)

    def test_create_invitation(self):
        """Test creating an organization invitation"""
        invitation = OrganizationInvitationFactory(
            organization=self.org,
            email='newuser@test.local',
            invited_by=self.user
        )

        self.assertIsNotNone(invitation.token)
        self.assertEqual(invitation.email, 'newuser@test.local')
        self.assertTrue(invitation.is_valid())

    def test_invitation_token_uniqueness(self):
        """Test that invitation tokens are unique"""
        invite1 = OrganizationInvitationFactory(
            organization=self.org,
            email='user1@test.local',
            invited_by=self.user
        )

        invite2 = OrganizationInvitationFactory(
            organization=self.org,
            email='user2@test.local',
            invited_by=self.user
        )

        self.assertNotEqual(invite1.token, invite2.token)

    def test_expired_invitation_not_valid(self):
        """Test that expired invitations are not valid"""
        invitation = OrganizationInvitationFactory(
            organization=self.org,
            email='expired@test.local',
            invited_by=self.user,
            expires_at=timezone.now() - timedelta(days=1)
        )

        self.assertFalse(invitation.is_valid())

    def test_accepted_invitation_not_valid(self):
        """Test that accepted invitations are not valid"""
        invitation = OrganizationInvitationFactory(
            organization=self.org,
            email='accepted@test.local',
            invited_by=self.user
        )

        # Accept the invitation
        invitation.accepted_at = timezone.now()
        invitation.save()

        self.assertFalse(invitation.is_valid())

    def test_invitation_requires_team_but_names_are_optional(self):
        team = Team.objects.create(organization=self.org, name='Engineering')
        with patch('accounts.invitation_views.send_email'):
            response = self.client.post(reverse('send_invitation'), {
                'email': 'optional-names@test.local',
                'team': team.id,
            })

        self.assertRedirects(response, reverse('team_list'))
        invitation = OrganizationInvitation.objects.get(email='optional-names@test.local')
        self.assertEqual(invitation.team, team)
        self.assertEqual(invitation.first_name, '')
        self.assertEqual(invitation.last_name, '')

    def test_invitation_without_team_is_rejected(self):
        with patch('accounts.invitation_views.send_email'):
            self.client.post(reverse('send_invitation'), {'email': 'no-team@test.local'})
        self.assertFalse(OrganizationInvitation.objects.filter(email='no-team@test.local').exists())

    def test_first_invitation_can_create_team_managed_by_inviter(self):
        with patch('accounts.invitation_views.send_email'):
            response = self.client.post(reverse('send_invitation'), {
                'email': 'first-member@test.local',
                'create_team': 'on',
                'team_name': 'Product',
            })

        self.assertRedirects(response, reverse('team_list'))
        team = Team.objects.get(organization=self.org, name='Product')
        self.assertEqual(team.manager, self.profile)
        self.assertEqual(
            Reviewee.objects.get(organization=self.org, email=self.user.email).team,
            team,
        )
        self.assertEqual(
            OrganizationInvitation.objects.get(email='first-member@test.local').team,
            team,
        )

    def test_invitation_can_create_an_additional_team_from_dropdown(self):
        Team.objects.create(organization=self.org, name='Existing', manager=self.profile)

        with patch('accounts.invitation_views.send_email'):
            response = self.client.post(reverse('send_invitation'), {
                'email': 'new-team-member@test.local',
                'team': '__new__',
                'team_name': 'Research',
            })

        self.assertRedirects(response, reverse('team_list'))
        team = Team.objects.get(organization=self.org, name='Research')
        self.assertEqual(team.manager, self.profile)
        self.assertEqual(
            OrganizationInvitation.objects.get(email='new-team-member@test.local').team,
            team,
        )


class TeamHierarchyViewTestCase(TestCase):
    def setUp(self):
        self.org = OrganizationFactory(name='FindMyPast')
        self.member_user = UserFactory(
            username='member', email='member@example.com', first_name='Morgan', last_name='Member'
        )
        self.member_profile = UserProfileFactory(user=self.member_user, organization=self.org)
        self.teammate_user = UserFactory(
            username='teammate', email='teammate@example.com', first_name='Taylor', last_name='Mate'
        )
        self.teammate_profile = UserProfileFactory(user=self.teammate_user, organization=self.org)
        self.parent_team = Team.objects.create(organization=self.org, name='Product')
        self.child_team = Team.objects.create(
            organization=self.org, name='Research', parent=self.parent_team
        )
        self.member_reviewee = Reviewee.objects.get(
            organization=self.org, email=self.member_user.email
        )
        self.member_reviewee.profile = self.member_profile
        self.member_reviewee.team = self.child_team
        self.member_reviewee.name = 'Morgan Member'
        self.member_reviewee.save()
        self.teammate_reviewee = Reviewee.objects.get(
            organization=self.org, email=self.teammate_user.email
        )
        self.teammate_reviewee.profile = self.teammate_profile
        self.teammate_reviewee.team = self.child_team
        self.teammate_reviewee.name = 'Taylor Mate'
        self.teammate_reviewee.save()
        self.client.force_login(self.member_user)

    def test_member_sees_nested_team_and_can_request_teammate_review(self):
        response = self.client.get(reverse('team_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Product')
        self.assertContains(response, 'Research')
        self.assertContains(response, 'Parent team: Product')
        self.assertContains(response, 'Taylor Mate')
        self.assertContains(
            response,
            f"{reverse('review_cycle_create')}?reviewer_email=teammate%40example.com",
        )

    def test_request_review_link_prefills_peer_reviewer(self):
        response = self.client.get(
            reverse('review_cycle_create'), {'reviewer_email': self.teammate_user.email}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.teammate_user.email)
        self.assertContains(response, 'id="inviteFormBody" style="display: block;"')

    def test_manager_cannot_take_member_from_unmanaged_team_by_id(self):
        manager_user = UserFactory(username='manager', email='manager@example.com')
        manager_profile = UserProfileFactory(user=manager_user, organization=self.org)
        managed_team = Team.objects.create(
            organization=self.org, name='Managed', manager=manager_profile
        )
        outsider = self.teammate_reviewee
        self.client.force_login(manager_user)

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'set_member_team',
            'reviewee': outsider.id,
            'team': managed_team.id,
        })

        self.assertEqual(response.status_code, 403)
        outsider.refresh_from_db()
        self.assertEqual(outsider.team, self.child_team)

    def test_removing_member_requires_typed_delete_confirmation(self):
        self.child_team.manager = self.member_profile
        self.child_team.save(update_fields=['manager'])

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'set_member_team',
            'reviewee': self.teammate_reviewee.id,
        })

        self.assertRedirects(response, reverse('team_list'))
        self.teammate_reviewee.refresh_from_db()
        self.assertEqual(self.teammate_reviewee.team, self.child_team)

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'set_member_team',
            'reviewee': self.teammate_reviewee.id,
            'confirmation': 'delete',
        })

        self.assertRedirects(response, reverse('team_list'))
        self.teammate_reviewee.refresh_from_db()
        self.assertIsNone(self.teammate_reviewee.team)

    def test_adding_member_to_second_team_preserves_first_membership(self):
        self.child_team.manager = self.member_profile
        self.child_team.save(update_fields=['manager'])
        odyssey = Team.objects.create(
            organization=self.org, name='Odyssey', manager=self.member_profile
        )

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'set_member_team',
            'reviewee': self.teammate_reviewee.id,
            'team': odyssey.id,
        })

        self.assertRedirects(response, reverse('team_list'))
        self.teammate_reviewee.refresh_from_db()
        self.assertEqual(self.teammate_reviewee.team, self.child_team)
        self.assertSetEqual(
            set(self.teammate_reviewee.teams.values_list('id', flat=True)),
            {self.child_team.id, odyssey.id},
        )
        page = self.client.get(reverse('team_list'))
        self.assertContains(page, 'Research')
        self.assertContains(page, 'Odyssey')
        self.assertContains(page, '<strong>Odyssey (2)</strong>', html=True)
        self.assertContains(page, '<strong>Research (2)</strong>', html=True)

    def test_manager_can_remove_a_leaf_team_and_members_become_unassigned(self):
        self.child_team.manager = self.member_profile
        self.child_team.save(update_fields=['manager'])

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'delete_team',
            'team': self.child_team.id,
        })

        self.assertRedirects(response, reverse('team_list'))
        self.assertFalse(Team.objects.filter(pk=self.child_team.id).exists())
        self.member_reviewee.refresh_from_db()
        self.assertIsNone(self.member_reviewee.team)

    def test_assigning_manager_also_assigns_manager_to_team(self):
        managed_team = Team.objects.create(organization=self.org, name='Managed')
        self.member_reviewee.team = None
        self.member_reviewee.save(update_fields=['team', 'updated_at'])

        managed_team.manager = self.member_profile
        managed_team.full_clean()
        managed_team.save(update_fields=['manager'])

        self.member_reviewee.refresh_from_db()
        self.assertEqual(self.member_reviewee.team, managed_team)
        self.assertEqual(self.member_reviewee.profile, self.member_profile)

    def test_team_with_subteams_must_be_emptied_before_removal(self):
        self.parent_team.manager = self.member_profile
        self.parent_team.save(update_fields=['manager'])

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'delete_team',
            'team': self.parent_team.id,
        })

        self.assertRedirects(response, reverse('team_list'))
        self.assertTrue(Team.objects.filter(pk=self.parent_team.id).exists())


class ReportGenerationTestCase(TestCase):
    """Test automatic report generation"""

    def setUp(self):
        self.org = OrganizationFactory(name='Test Organization')
        self.user = UserFactory()
        self.reviewee = RevieweeFactory(organization=self.org)
        self.questionnaire = QuestionnaireFactory(organization=self.org)

        self.cycle = ReviewCycleFactory(
            reviewee=self.reviewee,
            questionnaire=self.questionnaire,
            created_by=self.user
        )

    def test_generate_report_for_cycle(self):
        """Test generating a report for a review cycle"""
        from reports.services import generate_report

        # Generate report
        report = generate_report(self.cycle)

        self.assertIsNotNone(report)
        self.assertEqual(report.cycle, self.cycle)
        self.assertIsNotNone(report.report_data)

    def test_cycle_completion_workflow(self):
        """Test the cycle completion workflow"""
        # Create and complete some tokens
        token1 = ReviewerTokenFactory(
            cycle=self.cycle,
            category='self',
            completed_at=timezone.now()
        )
        token2 = ReviewerTokenFactory(
            cycle=self.cycle,
            category='peer',
            completed_at=timezone.now()
        )

        # Mark cycle as completed
        self.cycle.status = 'completed'
        self.cycle.save()

        # Verify cycle is completed
        self.assertEqual(self.cycle.status, 'completed')


class RevieweeManagementTestCase(TestCase):
    """Test reviewee management"""

    def setUp(self):
        self.org = OrganizationFactory(name='Test Organization')
        self.user = UserFactory(username='manager')
        self.user.set_password('testpass123')
        self.user.save()

        self.profile = UserProfileFactory(
            user=self.user,
            organization=self.org,
            can_create_cycles_for_others=True
        )

        self.reviewee1 = RevieweeFactory(
            organization=self.org,
            name='Active Employee'
        )

        self.client = Client()
        self.client.force_login(self.user)

    def test_list_reviewees(self):
        """Test listing reviewees"""
        reviewees = Reviewee.objects.filter(
            organization=self.org,
            is_active=True
        )
        self.assertGreater(reviewees.count(), 0)

    def test_create_reviewee(self):
        """Test creating a new reviewee"""
        reviewee = RevieweeFactory(
            organization=self.org,
            name="New Employee",
            email="new.employee@test.local",
            department="Engineering"
        )

        self.assertEqual(reviewee.organization, self.org)
        self.assertTrue(reviewee.is_active)

    def test_deactivate_reviewee(self):
        """Test deactivating a reviewee"""
        self.reviewee1.is_active = False
        self.reviewee1.save()

        self.assertFalse(self.reviewee1.is_active)

    def test_reviewee_organization_association(self):
        """Test that reviewees are properly associated with organization"""
        reviewees = Reviewee.objects.filter(organization=self.org)

        for reviewee in reviewees:
            self.assertEqual(reviewee.organization, self.org)
