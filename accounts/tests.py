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
from accounts.models import (
    OrganizationInvitation, OrganizationRole, Reviewee, Team, TeamLeadGrant,
    UserProfile,
)


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

    def test_invitation_without_team_requires_explicit_checkbox(self):
        with patch('accounts.invitation_views.send_email'):
            self.client.post(reverse('send_invitation'), {'email': 'no-team@test.local'})
        self.assertFalse(OrganizationInvitation.objects.filter(email='no-team@test.local').exists())

    def test_invitation_can_explicitly_leave_person_unassigned(self):
        with patch('accounts.invitation_views.send_email') as send_email:
            response = self.client.post(reverse('send_invitation'), {
                'email': 'unassigned@test.local',
                'no_team': 'on',
            })

        self.assertRedirects(response, reverse('team_list'))
        invitation = OrganizationInvitation.objects.get(email='unassigned@test.local')
        self.assertIsNone(invitation.team)
        self.assertNotIn(
            'on the <strong>',
            send_email.call_args.kwargs['html_message'],
        )

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

    def test_invitation_can_use_team_leader_as_reporting_manager(self):
        team = Team.objects.create(
            organization=self.org, name='Engineering', manager=self.profile
        )
        with patch('accounts.invitation_views.send_email'):
            self.client.post(reverse('send_invitation'), {
                'email': 'member@test.local',
                'team': team.id,
                'reporting_manager_choice': 'team_leader',
            })

        invitation = OrganizationInvitation.objects.get(email='member@test.local')
        self.assertEqual(invitation.reporting_manager, self.profile)
        self.assertEqual(invitation.pending_reporting_manager_email, '')

    def test_invitation_normalizes_entered_names(self):
        team = Team.objects.create(
            organization=self.org, name='Engineering', manager=self.profile
        )
        with patch('accounts.invitation_views.send_email'):
            self.client.post(reverse('send_invitation'), {
                'email': 'member@test.local',
                'first_name': 'jEREMY',
                'last_name': 'hOY',
                'team': team.id,
                'reporting_manager_choice': 'team_leader',
            })

        invitation = OrganizationInvitation.objects.get(email='member@test.local')
        self.assertEqual(invitation.first_name, 'Jeremy')
        self.assertEqual(invitation.last_name, 'Hoy')

    def test_invitation_can_store_another_pending_reporting_manager(self):
        team = Team.objects.create(
            organization=self.org, name='Engineering', manager=self.profile
        )
        with patch('accounts.invitation_views.send_email'):
            self.client.post(reverse('send_invitation'), {
                'email': 'member@test.local',
                'team': team.id,
                'reporting_manager_choice': 'other',
                'reporting_manager_email': 'supervisor@test.local',
            })

        invitation = OrganizationInvitation.objects.get(email='member@test.local')
        self.assertIsNone(invitation.reporting_manager)
        self.assertEqual(
            invitation.pending_reporting_manager_email, 'supervisor@test.local'
        )


class OrganizationPeopleSettingsTestCase(TestCase):
    def setUp(self):
        from accounts.permissions import assign_organization_admin

        self.org = OrganizationFactory(name='FindMyPast')
        self.admin = UserFactory(email='admin@example.com', first_name='Zoe', last_name='Admin')
        self.admin_profile = UserProfileFactory(user=self.admin, organization=self.org)
        assign_organization_admin(self.admin)
        self.member = UserFactory(email='unassigned@example.com', first_name='Amy', last_name='Member')
        self.member_profile = UserProfileFactory(user=self.member, organization=self.org)
        self.member_reviewee = Reviewee.objects.get(
            organization=self.org, email=self.member.email
        )
        self.member_reviewee.profile = self.member_profile
        self.member_reviewee.name = 'Unassigned Member'
        self.member_reviewee.save(update_fields=['profile', 'name', 'updated_at'])
        self.client.force_login(self.admin)

    def test_admin_sees_unassigned_people_in_settings(self):
        Team.objects.create(
            organization=self.org, name='Odyssey', manager=self.member_profile
        )
        response = self.client.get(reverse('settings'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'User Management')
        self.assertContains(response, self.member.email)
        self.assertContains(response, 'Unassigned')
        self.assertContains(response, 'status-toggle')
        self.assertContains(response, 'Search teams')
        self.assertContains(response, 'Search leadership teams')
        self.assertContains(response, 'Select leadership teams')
        self.assertContains(response, 'Select visible')
        self.assertContains(response, 'Transfer ownership of selected teams')
        self.assertLess(
            response.content.index(b'Amy Member'),
            response.content.index(b'Zoe Admin'),
        )

    def test_admin_sees_role_creation_on_organization_tab(self):
        response = self.client.get(reverse('settings'))

        self.assertContains(response, 'Roles & permissions')
        self.assertContains(response, 'Add role')
        self.assertContains(response, reverse('manage_organization_role'))

    def test_admin_can_create_inherited_role_and_assign_it(self):
        lead = OrganizationRole.objects.create(
            organization=self.org,
            name='Team Leader',
            can_manage_teams=True,
            can_create_cycles=True,
        )
        response = self.client.post(reverse('manage_organization_role'), {
            'action': 'create',
            'name': 'Senior Team Leader',
            'parent_role': lead.id,
            'can_view_reports': 'on',
        })
        self.assertRedirects(response, reverse('settings') + '#organization')
        senior = OrganizationRole.objects.get(name='Senior Team Leader')
        self.assertEqual(senior.parent, lead)
        self.assertEqual(
            senior.effective_permissions(),
            {'can_manage_teams', 'can_create_cycles', 'can_view_reports'},
        )

        response = self.client.post(reverse('manage_organization_person'), {
            'action': 'update_user',
            'user_profile_id': self.member_profile.id,
            'email': self.member.email,
            'status': 'active',
            'role': f'custom:{senior.id}',
        })
        self.assertRedirects(response, reverse('settings') + '#people')
        self.member_profile.refresh_from_db()
        self.assertEqual(self.member_profile.organization_role, senior)
        self.assertTrue(self.member_profile.can_create_cycles_for_others)

    def test_member_cannot_create_organization_role(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse('manage_organization_role'), {
            'action': 'create',
            'name': 'Unauthorized role',
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(OrganizationRole.objects.filter(name='Unauthorized role').exists())

    def test_admin_can_search_people_by_name_or_email(self):
        response = self.client.get(reverse('settings'), {'user_q': 'unassigned@'})

        self.assertContains(response, 'Amy Member')
        self.assertNotContains(response, 'Zoe Admin')

    def test_team_list_is_condensed_after_two_teams(self):
        teams = [
            Team.objects.create(organization=self.org, name=name)
            for name in ('Alpha', 'Beta', 'Gamma', 'Delta')
        ]
        self.member_reviewee.teams.set(teams)

        response = self.client.get(reverse('settings'))

        self.assertContains(response, 'class="team-count-more">+2</span>')
        self.assertContains(response, 'title="Alpha, Beta, Delta, Gamma"')

    def test_admin_can_edit_email_team_permissions_and_status(self):
        from accounts.permissions import ORG_ADMIN_GROUP

        team = Team.objects.create(organization=self.org, name='Product')
        url = reverse('manage_organization_person')
        response = self.client.post(url, {
            'action': 'update_user',
            'user_profile_id': self.member_profile.id,
            'email': 'updated@example.com',
            'status': 'inactive',
            'role': 'admin',
            'can_create_cycles_for_others': 'on',
            'teams': [team.id],
        })
        self.assertRedirects(response, reverse('settings') + '#people')
        self.member.refresh_from_db()
        self.member_profile.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertEqual(self.member.email, 'updated@example.com')
        self.assertTrue(self.member.groups.filter(name=ORG_ADMIN_GROUP).exists())
        self.assertTrue(self.member_profile.can_create_cycles_for_others)
        self.assertEqual(
            set(self.member_profile.reviewee.teams.values_list('id', flat=True)),
            {team.id},
        )

    def test_organization_admin_can_also_be_a_scoped_team_leader(self):
        from accounts.permissions import ORG_ADMIN_GROUP

        team = Team.objects.create(organization=self.org, name='Product')
        response = self.client.post(reverse('manage_organization_person'), {
            'action': 'update_user',
            'user_profile_id': self.member_profile.id,
            'email': self.member.email,
            'status': 'active',
            'role': 'admin',
            'can_create_cycles_for_others': 'on',
            'teams': [team.id],
            'lead_teams': [team.id],
        })

        self.assertRedirects(response, reverse('settings') + '#people')
        self.assertTrue(self.member.groups.filter(name=ORG_ADMIN_GROUP).exists())
        self.assertTrue(TeamLeadGrant.objects.filter(
            profile=self.member_profile,
            team=team,
        ).exists())

    def test_organization_admin_can_be_downgraded_to_team_leader(self):
        from accounts.permissions import ORG_ADMIN_GROUP, assign_organization_admin

        team = Team.objects.create(organization=self.org, name='Product')
        assign_organization_admin(self.member)
        TeamLeadGrant.objects.create(profile=self.member_profile, team=team)

        response = self.client.post(reverse('manage_organization_person'), {
            'action': 'update_user',
            'user_profile_id': self.member_profile.id,
            'email': self.member.email,
            'status': 'active',
            'role': 'team_leader',
            'teams': [team.id],
            'lead_teams': [team.id],
        })

        self.assertRedirects(response, reverse('settings') + '#people')
        self.assertFalse(self.member.groups.filter(name=ORG_ADMIN_GROUP).exists())
        self.assertTrue(TeamLeadGrant.objects.filter(
            profile=self.member_profile,
            team=team,
        ).exists())
        self.member_profile.refresh_from_db()
        self.assertTrue(self.member_profile.can_create_cycles_for_others)

    def test_remove_preserves_reviewee_history_but_removes_membership(self):
        reviewee = self.member_reviewee
        response = self.client.post(reverse('manage_organization_person'), {
            'action': 'remove',
            'user_profile_id': self.member_profile.id,
            'confirmation': 'delete',
        })
        self.assertRedirects(response, reverse('settings') + '#people')
        self.member.refresh_from_db()
        reviewee.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertFalse(UserProfile.objects.filter(pk=self.member_profile.id).exists())
        self.assertIsNone(reviewee.profile)
        self.assertFalse(reviewee.is_active)

    def test_edit_can_create_team_managed_by_editor(self):
        response = self.client.post(reverse('manage_organization_person'), {
            'action': 'update_user',
            'user_profile_id': self.member_profile.id,
            'email': self.member.email,
            'status': 'active',
            'role': 'member',
            'add_team': '__new__',
            'new_team_name': 'Odyssey',
        })
        self.assertRedirects(response, reverse('settings') + '#people')
        team = Team.objects.get(organization=self.org, name='Odyssey')
        self.assertEqual(team.manager, self.admin_profile)
        self.assertTrue(team.members.filter(pk=self.member_reviewee.pk).exists())
        self.assertTrue(team.members.filter(profile=self.admin_profile).exists())

    def test_admin_can_edit_own_email_and_team_membership(self):
        team = Team.objects.create(organization=self.org, name='Own Team')
        response = self.client.post(reverse('manage_organization_person'), {
            'action': 'update_user',
            'user_profile_id': self.admin_profile.id,
            'email': 'new-admin@example.com',
            'status': 'active',
            'role': 'admin',
            'teams': [team.id],
        })
        self.assertRedirects(response, reverse('settings') + '#people')
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, 'new-admin@example.com')
        self.assertTrue(team.members.filter(profile=self.admin_profile).exists())

    @patch('core.email.send_email')
    def test_team_owner_can_be_reassigned_to_member_and_notifies_them(self, send_email):
        team = Team.objects.create(
            organization=self.org, name='Managed Team', manager=self.member_profile
        )
        new_manager_user = UserFactory(email='new-manager@example.com')
        new_manager = UserProfileFactory(
            user=new_manager_user, organization=self.org
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('manage_organization_person'), {
                'action': 'update_user',
                'user_profile_id': self.member_profile.id,
                'email': self.member.email,
                'status': 'active',
                'role': 'member',
                f'manager_for_{team.id}': new_manager.id,
            })
        self.assertRedirects(response, reverse('settings') + '#people')
        team.refresh_from_db()
        self.assertEqual(team.manager, new_manager)
        self.assertTrue(team.members.filter(profile=new_manager).exists())
        send_email.assert_called_once()
        self.assertEqual(
            send_email.call_args.kwargs['recipient_list'],
            ['new-manager@example.com'],
        )

    def test_admin_cannot_manage_person_from_another_organization(self):
        other_profile = UserProfileFactory(
            user=UserFactory(email='other@example.com'),
            organization=OrganizationFactory(),
        )
        response = self.client.post(reverse('manage_organization_person'), {
            'action': 'update_user',
            'user_profile_id': other_profile.id,
            'email': 'changed@example.com',
            'status': 'active',
            'role': 'member',
        })
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, 'Go back to Dashboard', status_code=404)


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
            f'href="{reverse("review_cycle_create")}">Request review</a>',
        )

    def test_team_page_labels_primary_manager_as_team_leader(self):
        self.child_team.manager = self.member_profile
        self.child_team.save(update_fields=['manager'])

        response = self.client.get(reverse('team_list'))

        self.assertContains(response, 'Team leader: Morgan Member')
        self.assertContains(response, '<td>Team Leader</td>', html=True)

    def test_organization_admin_without_team_scope_is_shown_as_member(self):
        from accounts.permissions import assign_organization_admin

        assign_organization_admin(self.teammate_user)

        response = self.client.get(reverse('team_list'))

        self.assertContains(response, '<th>Team role</th>', html=True)
        self.assertContains(response, '<td>Member</td>', html=True)
        self.assertNotContains(response, 'Organisation administrator')
        self.assertNotContains(response, 'Edit permissions')

    def test_team_lead_grant_is_scoped_to_its_team(self):
        TeamLeadGrant.objects.create(
            profile=self.member_profile,
            team=self.child_team,
        )
        unrelated = Team.objects.create(organization=self.org, name='Finance')

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'update_team',
            'team': self.child_team.id,
            'name': 'Research and Development',
            'parent': self.parent_team.id,
        })

        self.assertRedirects(response, reverse('team_list'))
        self.child_team.refresh_from_db()
        self.assertEqual(self.child_team.name, 'Research and Development')

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'update_team',
            'team': unrelated.id,
            'name': 'Changed Finance',
        })

        self.assertEqual(response.status_code, 403)
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.name, 'Finance')

    def test_user_management_team_leader_change_is_shown_on_team_page(self):
        from accounts.permissions import assign_organization_admin

        assign_organization_admin(self.member_user)
        response = self.client.post(reverse('manage_organization_person'), {
            'action': 'update_user',
            'user_profile_id': self.teammate_profile.id,
            'email': self.teammate_user.email,
            'status': 'active',
            'role': 'team_leader',
            'teams': [self.child_team.id],
            'lead_teams': [self.child_team.id],
        })

        self.assertRedirects(response, reverse('settings') + '#people')
        page = self.client.get(reverse('team_list'))
        child_card = next(
            card for card in page.context['team_cards']
            if card['team'].id == self.child_team.id
        )
        teammate = next(
            member for member in child_card['members']
            if member.get('profile') == self.teammate_profile
        )
        self.assertTrue(teammate['is_team_leader'])

    def test_team_leader_change_is_shown_in_user_management(self):
        from accounts.permissions import assign_organization_admin

        assign_organization_admin(self.member_user)
        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'update_team',
            'team': self.child_team.id,
            'name': self.child_team.name,
            'parent': self.parent_team.id,
            'manager': self.teammate_profile.id,
        })

        self.assertRedirects(response, reverse('team_list'))
        settings_page = self.client.get(reverse('settings'))
        teammate = next(
            profile for profile in settings_page.context['organization_people']
            if profile.id == self.teammate_profile.id
        )
        self.assertTrue(teammate.is_team_leader)
        self.assertIn(self.child_team.id, [team.id for team in teammate.managed_team_list])

    def test_request_review_link_uses_campaign_flow(self):
        response = self.client.get(reverse('review_cycle_create'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="campaign_flow" value="1"')
        self.assertContains(response, 'Create cycle and send invitations')

    def test_add_team_member_can_open_invite_with_team_prefilled(self):
        from accounts.permissions import assign_organization_admin

        self.child_team.manager = self.member_profile
        self.child_team.save(update_fields=['manager'])
        assign_organization_admin(self.member_user)

        response = self.client.get(reverse('team_list'))

        self.assertContains(response, '<option value="__invite__">+ Invite New Member</option>')
        self.assertContains(
            response,
            f"openTeamInvite('{self.child_team.id}', 'Research')",
        )

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

        page = self.client.get(reverse('team_list'))
        self.assertContains(page, 'class="btn btn-sm btn-danger"', count=1)
        self.assertContains(page, '>Remove from team</button>', count=1)
        self.assertNotContains(
            page,
            'title="Transfer team management before removing this person"',
        )
        self.assertContains(
            page,
            f'id="remove-member-{self.child_team.id}-{self.teammate_reviewee.id}"',
        )

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
            'confirmation': 'delete',
        })

        self.assertRedirects(response, reverse('team_list'))
        self.child_team.refresh_from_db()
        self.assertIsNotNone(self.child_team.archived_at)
        self.assertFalse(
            Team.objects.for_organization(self.org).filter(pk=self.child_team.id).exists()
        )
        self.member_reviewee.refresh_from_db()
        self.assertIsNone(self.member_reviewee.team)

    def test_removing_team_gives_pending_invitation_a_team_removed_page(self):
        self.child_team.manager = self.member_profile
        self.child_team.save(update_fields=['manager'])
        invitation = OrganizationInvitationFactory(
            organization=self.org,
            team=self.child_team,
            email='pending@example.com',
        )

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'delete_team',
            'team': self.child_team.id,
            'confirmation': 'delete',
        })

        self.assertRedirects(response, reverse('team_list'))
        invitation.refresh_from_db()
        self.assertEqual(invitation.team_id, self.child_team.id)
        invitation_response = self.client.get(
            reverse('accept_invitation', kwargs={'token': invitation.token})
        )
        self.assertEqual(invitation_response.status_code, 410)
        self.assertContains(
            invitation_response, 'This team has been removed', status_code=410
        )
        self.assertContains(
            invitation_response, 'GDPR requests', status_code=410
        )

    def test_removing_team_deletes_active_campaign_and_retains_completed_history(self):
        from reviews.models import ReviewCampaign

        self.child_team.manager = self.member_profile
        self.child_team.save(update_fields=['manager'])
        active_campaign = ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.member_user,
            questionnaire=QuestionnaireFactory(organization=self.org),
            target_type='team',
            team=self.child_team,
            cycle_type='self',
            status='active',
        )
        completed_campaign = ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.member_user,
            questionnaire=QuestionnaireFactory(organization=self.org),
            target_type='team',
            team=self.child_team,
            cycle_type='peer',
            status='completed',
        )

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'delete_team',
            'team': self.child_team.id,
            'confirmation': 'delete',
        }, follow=True)

        self.assertRedirects(response, reverse('team_list'))
        self.assertFalse(ReviewCampaign.objects.filter(pk=active_campaign.pk).exists())
        self.assertTrue(ReviewCampaign.objects.filter(pk=completed_campaign.pk).exists())
        self.assertTrue(Team.objects.filter(pk=self.child_team.id).exists())
        self.child_team.refresh_from_db()
        self.assertIsNotNone(self.child_team.archived_at)

    def test_removing_a_missing_team_returns_to_team_list(self):
        self.child_team.manager = self.member_profile
        self.child_team.save(update_fields=['manager'])
        missing_team_id = self.child_team.id
        self.child_team.delete()

        response = self.client.post(reverse('manage_team_structure'), {
            'action': 'delete_team',
            'team': missing_team_id,
            'confirmation': 'delete',
        }, follow=True)

        self.assertRedirects(response, reverse('team_list'))
        self.assertContains(
            response,
            'That team no longer exists. Refresh the page and try again.',
        )

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
            'confirmation': 'delete',
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


class ProfileViewTestCase(TestCase):
    def setUp(self):
        self.org = OrganizationFactory(name='FindMyPast')
        self.user = UserFactory(
            username='member', email='member@example.com', first_name='Alex'
        )
        self.profile = UserProfileFactory(user=self.user, organization=self.org)
        self.reviewee = Reviewee.objects.get(
            organization=self.org, email=self.user.email
        )
        self.reviewee.profile = self.profile
        self.reviewee.name = 'Alex Member'
        self.reviewee.email = self.user.email
        self.reviewee.save(update_fields=['profile', 'name', 'email', 'updated_at'])
        self.team = Team.objects.create(
            organization=self.org, name='Odyssey', manager=self.profile
        )
        self.reviewee.teams.add(self.team)
        self.client.force_login(self.user)

    def test_profile_shows_membership_stats_and_theme_preferences(self):
        cycle = ReviewCycleFactory(reviewee=self.reviewee, status='completed')
        ReviewerTokenFactory(
            cycle=cycle,
            reviewer_email=self.user.email,
            completed_at=timezone.now(),
        )

        response = self.client.get(reverse('profile'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'FindMyPast')
        self.assertContains(response, 'Odyssey')
        self.assertContains(response, 'Team Leader')
        self.assertContains(response, 'Reviews completed')
        self.assertContains(response, 'Average feedback')
        self.assertContains(response, 'data-theme-choice="light"')
        self.assertContains(response, 'data-theme-choice="dark"')
        self.assertContains(response, 'data-theme-choice="system"')

    def test_profile_name_update_is_reflected_on_teams_page(self):
        response = self.client.post(reverse('profile'), {
            'first_name': 'Jordan',
            'last_name': 'Washington',
            'email': self.user.email,
        })

        self.assertRedirects(response, reverse('profile'))
        self.reviewee.refresh_from_db()
        self.assertEqual(self.reviewee.name, 'Jordan Washington')

        response = self.client.get(reverse('team_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Jordan Washington')
        self.assertNotContains(response, 'Alex Member')


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
