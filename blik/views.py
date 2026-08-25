"""
Core views for Blik application
"""
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse


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


def _redirect_from_error(request):
    """Send errors back to the appropriate application entry point."""
    if request.user.is_authenticated:
        return redirect('admin_dashboard')
    return redirect(f'{reverse("login")}?next={request.path}')


def handler400(request, exception):
    return _redirect_from_error(request)


def handler403(request, exception):
    return _redirect_from_error(request)


def handler404(request, exception):
    return _redirect_from_error(request)


def handler500(request):
    return _redirect_from_error(request)
