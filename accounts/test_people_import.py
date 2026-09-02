import json
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from accounts.factories import UserProfileFactory
from accounts.invitation_provisioning import apply_invitation_access
from accounts.models import OrganizationInvitation, Reviewee, Team, TeamLeadGrant
from accounts.people_import import apply_people_import, validate_people_import
from accounts.permissions import assign_organization_admin
from core.factories import OrganizationFactory

MAPPING = {
    'first_name': 'Known as',
    'last_name': 'Surname',
    'email': 'Work email',
    'team': 'Team',
    'manager': 'Manager',
    'role': '',
}


def csv_file(rows, name='people.csv'):
    content = 'Known as,Surname,Work email,Team,Manager\n' + '\n'.join(rows)
    return SimpleUploadedFile(name, content.encode(), content_type='text/csv')


def xlsx_file(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['Known as', 'Surname', 'Work email', 'Team', 'Manager'])
    for row in rows:
        sheet.append(row)
    content = BytesIO()
    workbook.save(content)
    return SimpleUploadedFile(
        'people.xlsx',
        content.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


class PeopleImportServiceTests(TestCase):
    def setUp(self):
        self.organization = OrganizationFactory()
        self.admin = UserProfileFactory(organization=self.organization)
        assign_organization_admin(self.admin.user)

    def preview(self, rows, mode='update'):
        return validate_people_import(
            self.organization,
            csv_file(rows),
            MAPPING,
            {},
            mode,
        )

    def test_existing_person_moves_team_without_duplicate(self):
        old_team = Team.objects.create(organization=self.organization, name='Old')
        profile = UserProfileFactory(
            organization=self.organization,
            user__first_name='Previous',
            user__last_name='Name',
            user__email='person@example.com',
        )
        reviewee = Reviewee.objects.get(organization=self.organization, email=profile.user.email)
        reviewee.profile = profile
        reviewee.team = old_team
        reviewee.save()
        reviewee.teams.add(old_team)

        preview = self.preview(['New,Name,person@example.com,Platform,'])
        counts, invitations = apply_people_import(
            self.organization, preview, self.admin.user
        )

        reviewee.refresh_from_db()
        profile.user.refresh_from_db()
        self.assertEqual(profile.user.get_full_name(), 'New Name')
        self.assertEqual(reviewee.team.name, 'Platform')
        self.assertEqual(list(reviewee.teams.values_list('name', flat=True)), ['Platform'])
        self.assertEqual(counts['updated'], 1)
        self.assertEqual(invitations, [])

    def test_new_people_create_invites_and_deferred_reporting_manager(self):
        preview = self.preview([
            'Morgan,Lead,morgan@example.com,Platform,',
            'Alex,Member,alex@example.com,Platform,Morgan Lead',
        ])
        self.assertEqual(preview['errors'], [])

        counts, invitations = apply_people_import(
            self.organization, preview, self.admin.user
        )

        employee = Reviewee.objects.get(email='alex@example.com')
        self.assertEqual(employee.pending_reporting_manager_email, 'morgan@example.com')
        self.assertEqual(counts['invited'], 2)
        self.assertEqual(len(invitations), 2)
        self.assertEqual(OrganizationInvitation.objects.filter(
            organization=self.organization
        ).count(), 2)

        manager_profile = UserProfileFactory(organization=self.organization)
        manager_profile.user.email = 'morgan@example.com'
        manager_profile.user.save(update_fields=['email'])
        invitation = OrganizationInvitation.objects.get(email='morgan@example.com')
        apply_invitation_access(invitation, manager_profile)
        employee.refresh_from_db()
        team = Team.objects.get(name='Platform')
        self.assertEqual(employee.reporting_manager, manager_profile)
        self.assertEqual(employee.pending_reporting_manager_email, '')
        self.assertEqual(team.manager, manager_profile)
        self.assertTrue(TeamLeadGrant.objects.filter(
            profile=manager_profile, team=team
        ).exists())

    def test_manager_email_is_invited_and_added_without_employee_row(self):
        preview = self.preview([
            'Alex,Member,alex@example.com,Platform,manager@example.com',
        ])
        self.assertEqual(preview['errors'], [])
        self.assertEqual(preview['manager_invitations'], ['manager@example.com'])

        counts, invitations = apply_people_import(
            self.organization, preview, self.admin.user
        )

        team = Team.objects.get(organization=self.organization, name='Platform')
        manager_invitation = OrganizationInvitation.objects.get(
            organization=self.organization, email='manager@example.com'
        )
        manager_reviewee = Reviewee.objects.get(
            organization=self.organization, email='manager@example.com'
        )
        self.assertEqual(manager_invitation.requested_role, 'team_leader')
        self.assertEqual(team.pending_manager_email, 'manager@example.com')
        employee = Reviewee.objects.get(email='alex@example.com')
        self.assertEqual(employee.pending_reporting_manager_email, 'manager@example.com')
        self.assertTrue(manager_reviewee.teams.filter(pk=team.pk).exists())
        self.assertEqual(counts['invited'], 2)
        self.assertEqual(
            {invitation.email for invitation in invitations},
            {'alex@example.com', 'manager@example.com'},
        )

    def test_sync_preview_lists_absent_members_but_not_admins(self):
        absent = UserProfileFactory(organization=self.organization)
        absent.user.email = 'absent@example.com'
        absent.user.save(update_fields=['email'])

        preview = self.preview(
            ['Current,Person,current@example.com,Platform,'], mode='sync'
        )

        self.assertEqual(
            [person['email'] for person in preview['missing']],
            ['absent@example.com'],
        )

    def test_ambiguous_manager_blocks_import(self):
        self.assertTrue(self.preview([
            'Sam,Smith,sam.one@example.com,One,',
            'Sam,Smith,sam.two@example.com,Two,',
            'Alex,Person,alex@example.com,Three,Sam Smith',
        ])['errors'])

    def test_xlsx_headers_and_rows_are_supported(self):
        preview = validate_people_import(
            self.organization,
            xlsx_file([['Alex', 'Person', 'alex@example.com', 'Platform', '']]),
            MAPPING,
            {},
            'update',
        )
        self.assertEqual(preview['errors'], [])
        self.assertEqual(preview['rows'][0]['email'], 'alex@example.com')

    def test_missing_teams_receive_unique_untitled_names(self):
        Team.objects.create(organization=self.organization, name='Untitled-1')
        preview = self.preview([
            'Alex,One,alex.one@example.com,,',
            'Alex,Two,alex.two@example.com,,',
        ])
        self.assertEqual(
            [row['team'] for row in preview['rows']],
            ['Untitled-2', 'Untitled-3'],
        )
        self.assertTrue(all(row['generated_team'] for row in preview['rows']))

    def test_missing_team_uses_reporting_managers_normalized_name(self):
        preview = self.preview([
            'JEREMY,HOY,jeremy@example.com,Development,',
            'ALICE,SMITH,alice@example.com,,jeremy@example.com',
            'BOB,JONES,bob@example.com,,jeremy@example.com',
        ])

        self.assertEqual(preview['errors'], [])
        self.assertEqual(preview['rows'][0]['name'], 'Jeremy Hoy')
        self.assertEqual(preview['rows'][1]['name'], 'Alice Smith')
        self.assertEqual(preview['rows'][1]['team'], "Jeremy Hoy's team")
        self.assertEqual(preview['rows'][2]['team'], "Jeremy Hoy's team")

    def test_manager_email_can_supply_generated_team_name(self):
        preview = self.preview([
            'Alice,Smith,alice@example.com,,unknown.manager@example.com',
        ])

        self.assertEqual(preview['rows'][0]['team'], "Unknown Manager's team")

    def test_comma_separated_teams_create_multiple_memberships(self):
        preview = self.preview([
            'Alex,Person,alex@example.com,"Mavericks, Alchemist",manager@example.com',
        ])
        self.assertEqual(preview['errors'], [])
        self.assertEqual(preview['rows'][0]['teams'], ['Mavericks', 'Alchemist'])

        apply_people_import(self.organization, preview, self.admin.user)

        reviewee = Reviewee.objects.get(email='alex@example.com')
        self.assertEqual(reviewee.team.name, 'Mavericks')
        self.assertEqual(
            set(reviewee.teams.values_list('name', flat=True)),
            {'Mavericks', 'Alchemist'},
        )
        self.assertEqual(
            set(Team.objects.values_list('name', flat=True)),
            {'Mavericks', 'Alchemist'},
        )
        self.assertEqual(reviewee.pending_reporting_manager_email, 'manager@example.com')

    def test_people_on_same_team_can_have_different_reporting_managers(self):
        preview = self.preview([
            'Alex,One,alex.one@example.com,"One, Shared",manager.one@example.com',
            'Alex,Two,alex.two@example.com,"Two, Shared",manager.two@example.com',
        ])

        self.assertEqual(preview['errors'], [])

    def test_different_reporting_managers_do_not_receive_team_lead_grants(self):
        manager_one = UserProfileFactory(
            organization=self.organization, user__email='manager.one@example.com'
        )
        manager_two = UserProfileFactory(
            organization=self.organization, user__email='manager.two@example.com'
        )
        preview = self.preview([
            'Alex,Person,alex@example.com,Platform,manager.one@example.com',
            'Sam,Person,sam@example.com,Platform,manager.two@example.com',
        ])

        apply_people_import(self.organization, preview, self.admin.user)

        employee = Reviewee.objects.get(email='alex@example.com')
        self.assertEqual(employee.reporting_manager, manager_one)
        self.assertFalse(TeamLeadGrant.objects.filter(
            profile__in=[manager_one, manager_two]
        ).exists())
        self.assertIsNone(Team.objects.get(name='Platform').manager)

    def test_majority_reporting_manager_becomes_team_leader(self):
        manager_one = UserProfileFactory(
            organization=self.organization, user__email='manager.one@example.com'
        )
        manager_two = UserProfileFactory(
            organization=self.organization, user__email='manager.two@example.com'
        )
        preview = self.preview([
            'Alex,One,alex@example.com,Platform,manager.one@example.com',
            'Sam,Two,sam@example.com,Platform,manager.one@example.com',
            'Jo,Three,jo@example.com,Platform,manager.two@example.com',
        ])

        apply_people_import(self.organization, preview, self.admin.user)

        team = Team.objects.get(name='Platform')
        self.assertEqual(team.manager, manager_one)
        self.assertTrue(TeamLeadGrant.objects.filter(
            profile=manager_one, team=team
        ).exists())
        self.assertFalse(TeamLeadGrant.objects.filter(
            profile=manager_two, team=team
        ).exists())

    def test_minority_supervisor_is_invited_as_reporting_manager(self):
        preview = self.preview([
            'Alex,One,alex@example.com,Platform,lead@example.com',
            'Sam,Two,sam@example.com,Platform,lead@example.com',
            'Jo,Three,jo@example.com,Platform,supervisor@example.com',
        ])

        apply_people_import(self.organization, preview, self.admin.user)

        self.assertEqual(
            OrganizationInvitation.objects.get(email='lead@example.com').requested_role,
            'team_leader',
        )
        self.assertEqual(
            OrganizationInvitation.objects.get(
                email='supervisor@example.com'
            ).requested_role,
            'reporting_manager',
        )

    def test_sync_can_deactivate_absent_people_without_deleting_history(self):
        absent = UserProfileFactory(
            organization=self.organization,
            user__email='leave@example.com',
        )
        reviewee = Reviewee.objects.get(
            organization=self.organization, email='leave@example.com'
        )
        reviewee.profile = absent
        reviewee.save(update_fields=['profile', 'updated_at'])
        preview = self.preview(
            ['Current,Person,current@example.com,Platform,'], mode='sync'
        )

        apply_people_import(
            self.organization,
            preview,
            self.admin.user,
            deactivate_missing=True,
        )

        absent.user.refresh_from_db()
        reviewee.refresh_from_db()
        self.assertFalse(absent.user.is_active)
        self.assertFalse(reviewee.is_active)
        self.assertTrue(Reviewee.objects.filter(pk=reviewee.pk).exists())


class PeopleImportViewTests(TestCase):
    def setUp(self):
        self.organization = OrganizationFactory()
        self.admin = UserProfileFactory(organization=self.organization)
        assign_organization_admin(self.admin.user)
        self.admin.user = get_user_model().objects.get(pk=self.admin.user_id)
        self.client.force_login(self.admin.user)

    def test_preview_and_commit_require_admin_and_send_after_confirmation(self):
        data = {
            'file': csv_file(['Alex,Person,alex@example.com,Platform,']),
            'mapping': json.dumps(MAPPING),
            'role_mapping': '{}',
            'mode': 'update',
        }
        response = self.client.post(reverse('people_import_preview'), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(OrganizationInvitation.objects.filter(
            email='alex@example.com'
        ).exists())

        data['file'] = csv_file(['Alex,Person,alex@example.com,Platform,'])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('people_import_commit'), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(OrganizationInvitation.objects.filter(
            email='alex@example.com'
        ).exists())

    def test_member_cannot_preview_import(self):
        member = UserProfileFactory(organization=self.organization)
        self.client.force_login(member.user)
        response = self.client.post(reverse('people_import_headers'), {
            'file': csv_file(['Alex,Person,alex@example.com,Platform,']),
        })
        self.assertEqual(response.status_code, 403)

    @override_settings(PRODUCT_NAME='FindMyPast 360')
    def test_import_invitation_uses_branded_welcome_email(self):
        data = {
            'file': csv_file(['Alex,Person,alex@example.com,Platform,']),
            'mapping': json.dumps(MAPPING),
            'role_mapping': '{}',
            'mode': 'update',
        }

        with self.captureOnCommitCallbacks(execute=True), patch(
            'accounts.import_views.send_email'
        ) as send_email:
            response = self.client.post(reverse('people_import_commit'), data)

        self.assertEqual(response.status_code, 200)
        call = send_email.call_args.kwargs
        self.assertEqual(call['subject'], 'Welcome to FindMyPast 360')
        self.assertIn('our 360-feedback tool', call['html_message'])
        self.assertIn('Role:', call['html_message'])
        self.assertIn('<strong>Member</strong>', call['html_message'])
        self.assertIn('Team:', call['html_message'])
        self.assertIn('<strong>Platform</strong>', call['html_message'])
        self.assertIn('Accept invitation', call['html_message'])
