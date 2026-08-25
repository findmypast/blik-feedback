"""
Core views for Blik application
"""
from django.http import JsonResponse
from django.shortcuts import redirect, render


def health_check(request):
    """Health check endpoint for monitoring"""
    return JsonResponse({
        'status': 'healthy',
        'service': 'blik',
        'version': '0.1.0'
    })


def home(request):
    """Home page - redirect authenticated users to dashboard, others to login"""
    if request.user.is_authenticated:
        return redirect('admin_dashboard')
    return redirect('login')


def _render_error(request, status_code, title, message):
    """Keep the HTTP status while giving users a safe route back into Blik."""
    return render(request, 'errors/error.html', {
        'status_code': status_code,
        'error_title': title,
        'error_message': message,
    }, status=status_code)


def handler400(request, exception):
    return _render_error(
        request, 400, 'Bad request',
        'We could not process that request. Please return to the application and try again.',
    )


def handler403(request, exception):
    return _render_error(
        request, 403, 'Access denied',
        'You do not have permission to view or change this page.',
    )


def handler404(request, exception):
    return _render_error(
        request, 404, 'Page not found',
        'The page you requested does not exist or is no longer available.',
    )


def handler500(request):
    return _render_error(
        request, 500, 'Something went wrong',
        'An unexpected error occurred. Please return to the application and try again.',
    )
