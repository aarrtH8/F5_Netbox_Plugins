from django.urls import path
from . import views

app_name = 'netbox_device_cleaner'

urlpatterns = [
    path('', views.PurgeView.as_view(), name='purge'),
]
