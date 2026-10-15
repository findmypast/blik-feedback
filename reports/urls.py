from django.urls import path
from . import views

app_name = 'reports'

urlpatterns = [
    # Admin views (require staff authentication)
    path('report/<uuid:cycle_uuid>/', views.view_report, name='view_report'),
    path('report/<uuid:cycle_uuid>/regenerate/', views.regenerate_report, name='regenerate_report'),
    path('report/<uuid:cycle_uuid>/release/', views.set_peer_report_release, name='set_peer_report_release'),

    # Reviewee views (require secure token)
    path('my-report/<uuid:access_token>/', views.reviewee_report, name='reviewee_report'),
]
