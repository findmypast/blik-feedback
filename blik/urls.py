"""
URL configuration for blik project.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from . import views, admin_views, superuser_views

# Error handlers
handler400 = 'blik.views.handler400'
handler403 = 'blik.views.handler403'
handler404 = 'blik.views.handler404'
handler500 = 'blik.views.handler500'

urlpatterns = [
    path('', views.home, name='home'),
    path('health/', views.health_check, name='health_check'),
    path('dashboard/revision/', views.dashboard_revision, name='dashboard_revision'),
    path('admin/', admin.site.urls),

    # Superuser tools
    path('superuser/create-org/', superuser_views.create_organization, name='superuser_create_organization'),

    # Admin dashboard
    path('dashboard/', admin_views.dashboard, name='admin_dashboard'),
    path('dashboard/settings/', admin_views.settings_view, name='settings'),
    path('dashboard/settings/people/manage/', admin_views.manage_organization_person, name='manage_organization_person'),
    path('dashboard/settings/roles/manage/', admin_views.manage_organization_role, name='manage_organization_role'),
    path('dashboard/settings/api-tokens/create/', admin_views.create_api_token, name='create_api_token'),
    path('dashboard/settings/api-tokens/<int:token_id>/update/', admin_views.update_api_token, name='update_api_token'),
    path('dashboard/settings/api-tokens/<int:token_id>/delete/', admin_views.delete_api_token, name='delete_api_token'),
    path('dashboard/settings/webhooks/create/', admin_views.create_webhook, name='create_webhook'),
    path('dashboard/settings/webhooks/<int:webhook_id>/update/', admin_views.update_webhook, name='update_webhook'),
    path('dashboard/settings/webhooks/<int:webhook_id>/delete/', admin_views.delete_webhook, name='delete_webhook'),
    path('dashboard/team/', admin_views.team_list, name='team_list'),
    path('dashboard/team/update-permissions/', admin_views.update_user_permissions, name='update_user_permissions'),
    path('dashboard/team/structure/', admin_views.manage_team_structure, name='manage_team_structure'),
    path('dashboard/team/gdpr/', admin_views.gdpr_management, name='gdpr_management'),
    path('dashboard/team/gdpr/user/<int:user_id>/delete/', admin_views.gdpr_delete_user_view, name='gdpr_delete_user'),
    path('dashboard/team/gdpr/reviewee/<int:reviewee_id>/delete/', admin_views.gdpr_delete_reviewee_view, name='gdpr_delete_reviewee'),
    path('dashboard/reviewees/', admin_views.reviewee_list, name='reviewee_list'),
    path('dashboard/reviewees/create/', admin_views.reviewee_create, name='reviewee_create'),
    path('dashboard/reviewees/<int:reviewee_id>/edit/', admin_views.reviewee_edit, name='reviewee_edit'),
    path('dashboard/reviewees/<int:reviewee_id>/delete/', admin_views.reviewee_delete, name='reviewee_delete'),
    path('dashboard/reviewees/<int:reviewee_id>/quick-cycle/', admin_views.quick_cycle_create, name='quick_cycle_create'),
    path('dashboard/questionnaires/', admin_views.questionnaire_list, name='questionnaire_list'),
    path('dashboard/questionnaires/create/', admin_views.questionnaire_create, name='questionnaire_create'),
    path('dashboard/questionnaires/<int:questionnaire_id>/edit/', admin_views.questionnaire_edit, name='questionnaire_edit'),
    path('dashboard/questionnaires/<int:questionnaire_id>/preview/', admin_views.questionnaire_preview, name='questionnaire_preview'),
    path('dashboard/questionnaires/<int:questionnaire_id>/sample-report/', admin_views.questionnaire_sample_report, name='questionnaire_sample_report'),
    path('api/questions/<int:question_id>/dreyfus-config/', admin_views.question_dreyfus_config_api, name='question_dreyfus_config_api'),
    path('dashboard/cycles/', admin_views.review_cycle_list, name='review_cycle_list'),
    path('dashboard/reports/', admin_views.report_list, name='report_list'),
    path('dashboard/cycles/create/', admin_views.review_cycle_create, name='review_cycle_create'),
    path('dashboard/cycles/bulk/send-invitations/', admin_views.bulk_send_invitations, name='bulk_send_invitations'),
    path('dashboard/cycles/<uuid:cycle_uuid>/', admin_views.review_cycle_detail, name='review_cycle_detail'),
    path('dashboard/cycles/<uuid:cycle_uuid>/renew/', admin_views.renew_review_cycle, name='renew_review_cycle'),
    path('dashboard/cycles/<uuid:cycle_uuid>/nominate-peers/', admin_views.nominate_peer_reviewers, name='nominate_peer_reviewers'),
    path('dashboard/campaigns/<uuid:campaign_uuid>/', admin_views.review_campaign_detail, name='review_campaign_detail'),
    path('dashboard/organisation-cycles/<uuid:cycle_uuid>/', admin_views.organisation_cycle_detail, name='organisation_cycle_detail'),
    path('dashboard/campaigns/<uuid:campaign_uuid>/renew/', admin_views.renew_review_campaign, name='renew_review_campaign'),
    path('dashboard/campaigns/<uuid:campaign_uuid>/close/', admin_views.close_review_campaign, name='close_review_campaign'),
    path('dashboard/organizational-cycles/<uuid:cycle_uuid>/close-scope/', admin_views.close_organizational_cycle_scope, name='close_organizational_cycle_scope'),
    path('dashboard/campaigns/<uuid:campaign_uuid>/participants/add/', admin_views.add_campaign_participant, name='add_campaign_participant'),
    path('dashboard/campaigns/<uuid:campaign_uuid>/cycles/<uuid:cycle_uuid>/remind/', admin_views.send_campaign_cycle_reminder, name='send_campaign_cycle_reminder'),
    path('dashboard/campaigns/<uuid:campaign_uuid>/cycles/<uuid:cycle_uuid>/reviewers/<int:token_id>/remind/', admin_views.send_campaign_reviewer_reminder, name='send_campaign_reviewer_reminder'),
    path('dashboard/cycles/<uuid:cycle_uuid>/invitations/', admin_views.manage_invitations, name='manage_invitations'),
    path('dashboard/cycles/<uuid:cycle_uuid>/invitations/assign/', admin_views.assign_invitations, name='assign_invitations'),
    path('dashboard/cycles/<uuid:cycle_uuid>/invitations/send/', admin_views.send_invitations, name='send_invitations'),
    path('dashboard/cycles/<uuid:cycle_uuid>/generate-report/', admin_views.generate_report_view, name='generate_report'),
    path('dashboard/cycles/<uuid:cycle_uuid>/close/', admin_views.close_cycle, name='close_cycle'),
    path('dashboard/cycles/<uuid:cycle_uuid>/delete/', admin_views.delete_review_cycle, name='delete_review_cycle'),
    path('dashboard/cycles/<uuid:cycle_uuid>/send-reminder/', admin_views.send_reminder_form, name='send_reminder_form'),
    path('dashboard/cycles/<uuid:cycle_uuid>/send-reminder/send/', admin_views.send_reminder, name='send_reminder'),
    path('dashboard/cycles/<uuid:cycle_uuid>/reminder/<int:token_id>/', admin_views.send_individual_reminder, name='send_individual_reminder'),
    path('dashboard/cycles/<uuid:cycle_uuid>/remove-reviewer/<int:token_id>/', admin_views.remove_reviewer_token, name='remove_reviewer_token'),
    path('dashboard/cycles/<uuid:cycle_uuid>/send-report-email/', admin_views.send_report_email, name='send_report_email'),
    path('dashboard/product-reviews/', admin_views.product_review_list, name='product_review_list'),
    path('dashboard/product-reviews/create/', admin_views.product_review_create, name='product_review_create'),
    path('dashboard/product-reviews/quick-submit/', admin_views.quick_product_review, name='quick_product_review'),
    path('dashboard/product-reviews/<int:review_id>/', admin_views.product_review_detail, name='product_review_detail'),
    path('dashboard/product-reviews/<int:review_id>/edit/', admin_views.product_review_edit, name='product_review_edit'),
    path('dashboard/product-reviews/<int:review_id>/delete/', admin_views.product_review_delete, name='product_review_delete'),
    path('dashboard/product-reviews/<int:review_id>/approve/', admin_views.product_review_approve, name='product_review_approve'),
    path('dashboard/product-reviews/<int:review_id>/reject/', admin_views.product_review_reject, name='product_review_reject'),

    # Other apps
    path('setup/', include('core.urls')),
    path('accounts/', include('accounts.urls')),
    path('accounts/', include('accounts.sso_urls')),
    path('accounts/', include('allauth.urls')),
    path('account/', include('blik.account_urls')),
    path('', include('reviews.urls')),
    path('', include('reports.urls')),
    path('api/', include('subscriptions.urls')),

    # REST API v1
    path('api/v1/', include(('api.urls', 'api'), namespace='api')),

]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
