"""
Core views for Blik application
"""
from django.http import JsonResponse
from django.shortcuts import render, redirect
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


def handler404(request, exception):
    """Return users to the application instead of the marketing error page."""
    if request.user.is_authenticated:
        return redirect('admin_dashboard')
    return redirect(f'{reverse("login")}?next={request.path}')


def handler500(request):
    """Custom 500 error handler"""
    return render(request, 'landing/500.html', status=500)
