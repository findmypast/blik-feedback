"""
Custom email utilities that use Organization SMTP settings
"""
import logging
import re
from email.mime.image import MIMEImage
from pathlib import Path

from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.mail.backends.smtp import EmailBackend
from django.conf import settings
from .models import Organization

logger = logging.getLogger(__name__)
EMAIL_LOGO_CID = 'findmypast-logo'


def get_email_brand_name():
    """Return the public product name used to identify application email."""
    return getattr(settings, 'PRODUCT_NAME', None) or getattr(
        settings, 'SITE_NAME', '360 Feedback'
    )


def brand_email_subject(subject):
    """Prefix a subject consistently without duplicating an existing brand."""
    brand = get_email_brand_name().strip()
    subject = str(subject).strip()
    if subject.casefold().startswith(brand.casefold()):
        return subject
    if brand.casefold().endswith('360') and subject.casefold().startswith('360 '):
        return f'{brand} {subject[4:]}'
    return f'{brand}: {subject}'


def add_email_footer(message, html=False):
    """Add a branded header and unambiguous notice to application email."""
    notice = f'This is an automated message from {get_email_brand_name()} Feedback system.'
    if notice in (message or '') and (
        not html or 'role="banner"' in (message or '')
    ):
        return message
    if not html:
        return f'{(message or "").rstrip()}\n\n---\n{notice}\n'

    brand = get_email_brand_name()
    if brand.casefold().startswith('findmypast'):
        identity = (
            f'<img src="cid:{EMAIL_LOGO_CID}" width="180" '
            f'alt="{brand}" style="display:block;width:180px;max-width:100%;'
            'height:auto;border:0;margin:0 auto 10px;">'
            '<div style="color:#232147;font-size:16px;font-weight:700;">'
            '360 Feedback</div>'
        )
    else:
        identity = (
            f'<div style="color:#232147;font-size:20px;font-weight:700;">{brand}</div>'
        )
    header = (
        '<div role="banner" style="padding:22px 24px 18px;text-align:center;'
        'font-family:Arial,sans-serif;background:#ffffff;">'
        f'{identity}</div>'
    )
    footer = (
        '<div role="contentinfo" style="margin:28px auto 0;padding:18px 24px;'
        'border-top:1px solid #d8dee8;color:#5f6b7a;font-family:Arial,sans-serif;'
        'font-size:12px;line-height:1.5;text-align:center;">'
        f'{notice}</div>'
    )
    html_message = message or ''
    body_open = re.search(r'<body\b[^>]*>', html_message, flags=re.IGNORECASE)
    if body_open:
        html_message = (
            f'{html_message[:body_open.end()]}{header}{html_message[body_open.end():]}'
        )
    else:
        html_message = f'{header}{html_message}'
    lower_message = html_message.lower()
    body_end = lower_message.rfind('</body>')
    if body_end >= 0:
        return f'{html_message[:body_end]}{footer}{html_message[body_end:]}'
    return f'{html_message}{footer}'


def get_email_backend():
    """
    Get email backend configured with Organization SMTP settings.
    Falls back to the backend in Django settings if no organization SMTP host
    is configured — that fallback must honour EMAIL_BACKEND, otherwise console
    and locmem (test) backends are bypassed in favour of a real SMTP connection.
    """
    try:
        org = Organization.objects.filter(is_active=True).first()

        if org and org.smtp_host:
            # Use organization's SMTP settings
            return EmailBackend(
                host=org.smtp_host,
                port=org.smtp_port,
                username=org.smtp_username,
                password=org.smtp_password,
                use_tls=org.smtp_use_tls,
                fail_silently=False,
            )
    except Exception:
        logger.exception('Error loading organization email settings')

    return get_connection(fail_silently=False)


def get_from_email():
    """Get the from_email from Organization or fall back to Django settings"""
    try:
        org = Organization.objects.filter(is_active=True).first()
        if org and org.from_email:
            return org.from_email
    except Exception:
        pass

    return settings.DEFAULT_FROM_EMAIL


def send_email(subject, message, recipient_list, html_message=None, from_email=None):
    """
    Send email using Organization SMTP settings.

    Args:
        subject: Email subject
        message: Plain text message
        recipient_list: List of recipient email addresses
        html_message: Optional HTML version of message
        from_email: Optional from email (defaults to organization setting)

    Returns:
        Number of emails sent (0 or 1)
    """
    if from_email is None:
        from_email = get_from_email()

    backend = get_email_backend()

    email = EmailMultiAlternatives(
        subject=brand_email_subject(subject),
        body=add_email_footer(message),
        from_email=from_email,
        to=recipient_list,
        connection=backend,
    )

    if html_message:
        email.attach_alternative(add_email_footer(html_message, html=True), 'text/html')
        if get_email_brand_name().casefold().startswith('findmypast'):
            logo_path = Path(settings.BASE_DIR) / 'static' / 'img' / 'findmypast-logo-email.png'
            if logo_path.exists():
                logo = MIMEImage(logo_path.read_bytes(), _subtype='png')
                logo.add_header('Content-ID', f'<{EMAIL_LOGO_CID}>')
                logo.add_header(
                    'Content-Disposition', 'inline', filename='findmypast-logo.png'
                )
                email.attach(logo)

    return email.send()


def send_password_reset_email(user, token):
    """
    Send password reset email to user.

    Args:
        user: User object
        token: PasswordResetToken object

    Returns:
        Number of emails sent (0 or 1)
    """
    from django.template.loader import render_to_string
    from django.urls import reverse

    # Build reset URL
    reset_url = f'{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}{reverse("reset_password", args=[token.token])}'

    subject = f'Reset your {settings.SITE_NAME} password'

    context = {
        'user': user,
        'reset_url': reset_url,
        'site_name': settings.SITE_NAME,
    }

    html_message = render_to_string('emails/password_reset.html', context)
    text_message = render_to_string('emails/password_reset.txt', context)

    return send_email(
        subject=subject,
        message=text_message,
        recipient_list=[user.email],
        html_message=html_message
    )


def send_welcome_email(user, organization, password=None):
    """
    Send welcome email to newly registered user.

    Args:
        user: User object
        organization: Organization object
        password: Optional password to include in email (for admin-created accounts)

    Returns:
        Number of emails sent (0 or 1)
    """
    import random
    from django.template.loader import render_to_string
    from django.urls import reverse
    from .models import WelcomeEmailFact

    # Build login URL
    login_url = f'{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}{reverse("login")}'

    # Build Dreyfus model URL
    dreyfus_url = f'{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}/landing/dreyfus-model/'

    # Get active facts from database
    active_facts = list(WelcomeEmailFact.objects.filter(is_active=True))

    if active_facts:
        # Select a random fact from the database
        selected_fact_obj = random.choice(active_facts)
        selected_fact = {
            'title': selected_fact_obj.title,
            'content': selected_fact_obj.content
        }
    else:
        # Fallback if no facts in database
        selected_fact = {
            'title': 'The Power of 360 Feedback',
            'content': 'Research shows that <strong>360-degree feedback increases self-awareness by up to 30%</strong> and significantly improves leadership effectiveness.'
        }

    # Use different templates based on whether password is provided
    if password:
        # Admin-created account with credentials
        subject = f'Welcome to {settings.SITE_NAME} - Your Account is Ready'
        html_template = 'emails/welcome.html'
        text_template = 'emails/welcome.txt'
    else:
        # Invited member (already set their own password)
        subject = f'Welcome to {organization.name}!'
        html_template = 'emails/welcome_member.html'
        text_template = 'emails/welcome_member.txt'

    # Render email templates
    context = {
        'organization': organization,
        'user': user,
        'password': password,
        'login_url': login_url,
        'dreyfus_url': dreyfus_url,
        'site_name': settings.SITE_NAME,
        'fact_title': selected_fact['title'],
        'fact_content': selected_fact['content'],
    }

    html_message = render_to_string(html_template, context)
    text_message = render_to_string(text_template, context)

    return send_email(
        subject=subject,
        message=text_message,
        recipient_list=[user.email],
        html_message=html_message,
        from_email=organization.from_email if organization.from_email else None
    )
