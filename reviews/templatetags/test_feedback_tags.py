from django.test import SimpleTestCase

from .feedback_tags import has_gap_chart_data, has_section_chart_data, personalize


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


class ChartDataFilterTests(SimpleTestCase):
    def test_empty_or_metadata_only_sections_are_not_plottable(self):
        self.assertFalse(has_section_chart_data({}))
        self.assertFalse(has_section_chart_data({'section_scores': {}}))
        self.assertFalse(has_section_chart_data({
            'section_scores': {'Communication': {'others_avg': 4.2}},
        }))

    def test_section_chart_requires_an_assessment_category(self):
        self.assertFalse(has_section_chart_data({
            'section_scores': {'Communication': {'peer': 4.2}},
        }))
        self.assertTrue(has_section_chart_data({
            'section_scores': {
                'Communication': {'peer': 4.2},
                'Leadership': {'manager': 3.8},
            },
        }))

    def test_gap_chart_requires_self_and_others_in_the_same_section(self):
        self.assertFalse(has_gap_chart_data({
            'section_scores': {'Communication': {'peer': 4.2, 'others_avg': 4.2}},
        }))
        self.assertFalse(has_gap_chart_data({
            'section_scores': {
                'Communication': {'self': 3.8},
                'Leadership': {'others_avg': 4.2},
            },
        }))
        self.assertTrue(has_gap_chart_data({
            'section_scores': {
                'Communication': {'self': 3.8, 'peer': 4.2, 'others_avg': 4.2},
            },
        }))
