"""Microsoft SSO endpoints using Blik's tenant-validating OAuth adapter."""

from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView
from django.urls import path

from accounts.social_adapter import BlikMicrosoftOAuth2Adapter

urlpatterns = [
    path(
        'microsoft/login/',
        OAuth2LoginView.adapter_view(BlikMicrosoftOAuth2Adapter),
        name='microsoft_login',
    ),
    path(
        'microsoft/login/callback/',
        OAuth2CallbackView.adapter_view(BlikMicrosoftOAuth2Adapter),
        name='microsoft_callback',
    ),
]
