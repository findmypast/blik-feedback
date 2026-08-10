from django.test import TestCase
from django.urls import reverse

from accounts.authorization import effective_scope
from accounts.factories import RevieweeFactory, UserProfileFactory
from accounts.models import Team, TeamLeadGrant, TeamLeadRevocation
from core.factories import OrganizationFactory, UserFactory


class EffectiveScopeTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.other_org = OrganizationFactory()
        self.user = UserFactory()
        self.profile = UserProfileFactory(user=self.user, organization=self.org)
        self.root = Team.objects.create(organization=self.org, name='Engineering')
        self.child = Team.objects.create(
            organization=self.org, name='Platform', parent=self.root
        )
        self.other_team = Team.objects.create(organization=self.org, name='Sales')

        self.self_reviewee = self.org.reviewees.get(email=self.user.email)
        self.self_reviewee.profile = self.profile
        self.self_reviewee.save(update_fields=['profile'])
        self.direct_report = RevieweeFactory(
            organization=self.org, reporting_manager=self.profile, email='direct@example.test'
        )
        self.root_member = RevieweeFactory(
            organization=self.org, team=self.root, email='root@example.test'
        )
        self.child_member = RevieweeFactory(
            organization=self.org, team=self.child, email='child@example.test'
        )
        self.unrelated = RevieweeFactory(
            organization=self.org, team=self.other_team, email='unrelated@example.test'
        )
        self.cross_org = RevieweeFactory(
            organization=self.other_org, email='cross@example.test'
        )

    def ids(self):
        return effective_scope(self.user, self.org).reviewee_ids

    def test_member_sees_self_and_direct_reports_only(self):
        self.assertEqual(self.ids(), {self.self_reviewee.id, self.direct_report.id})

    def test_direct_only_team_grant_does_not_include_descendants(self):
        TeamLeadGrant.objects.create(profile=self.profile, team=self.root)
        self.assertEqual(
            self.ids(),
            {self.self_reviewee.id, self.direct_report.id, self.root_member.id},
        )

    def test_primary_team_manager_gets_direct_team_scope(self):
        self.root.manager = self.profile
        self.root.save(update_fields=['manager'])
        self.assertEqual(
            self.ids(),
            {self.self_reviewee.id, self.direct_report.id, self.root_member.id},
        )

    def test_descendant_grant_unions_with_reporting_line(self):
        TeamLeadGrant.objects.create(
            profile=self.profile, team=self.root, include_descendants=True
        )
        self.assertEqual(
            self.ids(),
            {
                self.self_reviewee.id, self.direct_report.id,
                self.root_member.id, self.child_member.id,
            },
        )

    def test_revoked_subtree_can_be_restored_by_independent_grant(self):
        inherited = TeamLeadGrant.objects.create(
            profile=self.profile, team=self.root, include_descendants=True
        )
        TeamLeadRevocation.objects.create(grant=inherited, team=self.child)
        self.assertNotIn(self.child_member.id, self.ids())

        TeamLeadGrant.objects.create(profile=self.profile, team=self.child)
        self.assertIn(self.child_member.id, self.ids())

    def test_cross_organization_records_never_enter_scope(self):
        TeamLeadGrant.objects.create(
            profile=self.profile, team=self.root, include_descendants=True
        )
        self.assertNotIn(self.cross_org.id, self.ids())

    def test_direct_api_identifier_returns_not_found_outside_scope(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse('api:reviewee-detail', kwargs={'uuid': self.unrelated.uuid})
        )
        self.assertEqual(response.status_code, 404)
