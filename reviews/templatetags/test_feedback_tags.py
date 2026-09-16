from django.test import SimpleTestCase

from .feedback_tags import personalize


class PersonalizeFilterTests(SimpleTestCase):
    def test_person_name_placeholder_uses_full_reviewee_name(self):
        self.assertEqual(
            personalize("What did <personName> accomplish?", "Alex Morgan"),
            "What did Alex Morgan accomplish?",
        )

    def test_legacy_this_person_wording_still_uses_first_name(self):
        self.assertEqual(
            personalize("This person communicates clearly.", "Alex Morgan"),
            "Alex communicates clearly.",
        )
