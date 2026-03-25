from django.urls import path
from . import views

app_name = 'netbox_f5_bigip'

urlpatterns = [
    path('',
         views.HomeView.as_view(),
         name='home'),

    path('<str:kind>/<int:device_id>/connect/',
         views.ConnectView.as_view(),
         name='connect'),

    path('<str:kind>/<int:device_id>/preview/',
         views.PreviewView.as_view(),
         name='preview'),

    path('<str:kind>/<int:device_id>/import/',
         views.ImportView.as_view(),
         name='import'),

    # Polling du statut du job RQ
    path('job/<str:job_id>/status/',
         views.JobStatusView.as_view(),
         name='job_status'),
]
