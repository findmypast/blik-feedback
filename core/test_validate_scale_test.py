from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from core.models import Organization
from questionnaires.models import Questionnaire
from reviews.campaign_services import launch_organizational_cycle


@override_settings(DEBUG=True)
class ValidateScaleTestCommandTests(TestCase):
    def setUp(self):
        call_command(
            'seed_scale_test',
            organization='Blik Scale Test',
            users=10,
            teams=5,
            password='test-password',
            seed=1183,
            stdout=StringIO(),
        )
        self.organization = Organization.objects.get(name='Blik Scale Test')

    def validate(self):
        output = StringIO()
        call_command(
            'validate_scale_test',
            organization=self.organization.name,
            stdout=output,
        )
        return output.getvalue()

    def test_structural_validation_passes_before_cycle_creation(self):
        output = self.validate()

        self.assertIn('PASS  10 user profiles have linked reviewees', output)
        self.assertIn('WARN  No organisation cycle exists yet', output)
        self.assertIn('PASSED:', output)

    def test_assignment_validation_passes_after_organisation_cycle_creation(self):
        questionnaires = Questionnaire.objects.for_organization(self.organization)
        launch_organizational_cycle(
            organization=self.organization,
            created_by=self.organization.users.get(
                user__email='scale-admin@scale-test.invalid'
            ).user,
            questionnaires={
                'self': questionnaires.filter(allow_self_assessment=True).first(),
                'peer': questionnaires.filter(allow_peer_review=True).first(),
                'manager': questionnaires.filter(allow_manager_assessment=True).first(),
            },
            minimum_peer_reviewers=2,
        )

        output = self.validate()

        self.assertIn('one shared self-assessment campaign exists', output)
        self.assertIn('every active participant has one shared self-assessment', output)
        self.assertIn('manager assessments target each assigned team manager', output)
        self.assertIn('team managers can see members’ shared self-assessments', output)
        self.assertNotIn('FAIL ', output)
