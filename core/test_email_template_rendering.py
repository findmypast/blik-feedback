"""Static invariants for the HTML email templates.

Catches rendering bugs that don't surface in unit tests because the
templates are only fully exercised by real email clients.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


EMAIL_TEMPLATE_DIR = Path(settings.BASE_DIR) / 'templates' / 'emails'


def _declaration_blocks(template_text):
    """Yield (kind, body) for each CSS declaration block in `template_text`.

    Covers both shapes used in our templates:
      - 'rule'   — body between `{ ... }` in a <style> block
      - 'inline' — value of a `style="..."` attribute
    """
    for match in re.finditer(r'\{([^{}]*)\}', template_text):
        yield 'rule', match.group(1)
    for match in re.finditer(r'style="([^"]*)"', template_text):
        yield 'inline', match.group(1)


class EmailTemplateOutlookCompatibilityTests(SimpleTestCase):
    """Outlook (desktop + Office dark mode) discards any `background:`
    shorthand whose value is `linear-gradient(...)`. Without a solid
    `background-color:` declared earlier in the same block, headers and
    call-to-action buttons render with no background — invisible against
    the body color.
    """

    def test_every_linear_gradient_has_preceding_background_color(self):
        violations = []
        for template_path in sorted(EMAIL_TEMPLATE_DIR.glob('*.html')):
            for kind, block in _declaration_blocks(template_path.read_text()):
                gradient_at = block.find('linear-gradient')
                if gradient_at == -1:
                    continue
                if 'background-color:' not in block[:gradient_at]:
                    violations.append(
                        f'{template_path.name} [{kind}]: linear-gradient '
                        f'without a preceding background-color fallback'
                    )
        self.assertEqual(violations, [], '\n'.join(violations))
