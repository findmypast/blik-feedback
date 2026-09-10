from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from accounts.factories import RevieweeFactory, UserProfileFactory
from accounts.models import Team, TeamMembership
from core.factories import OrganizationFactory


class RepairTeamRelationshipsCommandTests(TestCase):
    def setUp(self):
        self.organization = OrganizationFactory()
        self.leader = UserProfileFactory(organization=self.organization)
        self.team = Team.objects.create(
            organization=self.organization,
            name='Legacy team',
            manager=self.leader,
        )
        self.leader_reviewee = self.leader.reviewee
        TeamMembership.objects.filter(
            reviewee=self.leader_reviewee,
            team=self.team,
        ).delete()
        self.member = RevieweeFactory(organization=self.organization, team=self.team)
        self.member.teams.add(self.team)

    def test_apply_adds_team_leader_and_backfills_reporting_manager(self):
        call_command('repair_team_relationships', '--apply', stdout=StringIO())

        self.member.refresh_from_db()
        self.assertEqual(self.member.reporting_manager, self.leader)
        self.assertTrue(
            TeamMembership.objects.filter(
                reviewee=self.leader_reviewee,
                team=self.team,
            ).exists()
        )

    def test_dry_run_reports_but_does_not_change_records(self):
        output = StringIO()

        call_command('repair_team_relationships', stdout=output)

        self.member.refresh_from_db()
        self.assertIsNone(self.member.reporting_manager)
        self.assertFalse(
            TeamMembership.objects.filter(
                reviewee=self.leader_reviewee,
                team=self.team,
            ).exists()
        )
        self.assertIn('Would repair:', output.getvalue())

    def test_multiple_team_leaders_are_reported_without_guessing(self):
        second_leader = UserProfileFactory(organization=self.organization)
        second_team = Team.objects.create(
            organization=self.organization,
            name='Second team',
            manager=second_leader,
        )
        self.member.teams.add(second_team)
        output = StringIO()

        call_command('repair_team_relationships', '--apply', stdout=output)

        self.member.refresh_from_db()
        self.assertIsNone(self.member.reporting_manager)
        self.assertIn('different leaders', output.getvalue())

    def test_pending_team_leader_email_is_resolved_case_insensitively(self):
        pending_team = Team.objects.create(
            organization=self.organization,
            name='Pending legacy team',
            pending_manager_email=self.leader.user.email.upper(),
        )

        call_command('repair_team_relationships', '--apply', stdout=StringIO())

        pending_team.refresh_from_db()
        self.assertEqual(pending_team.manager, self.leader)
        self.assertEqual(pending_team.pending_manager_email, '')
