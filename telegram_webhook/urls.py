from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.HealthCheckView, name="health"),
    path("tg/webhook/", views.TelegramWebhookView, name="telegram-webhook"),
    path("tg/set-webhook/", views.TelegramSetWebhookView, name="telegram-set-webhook"),
    path("tg/diagnostics/", views.TelegramWebhookDiagnosticsView, name="telegram-webhook-diagnostics"),
]
