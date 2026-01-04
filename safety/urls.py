from django.urls import path

from . import views

urlpatterns = [
    path("events/report/", views.SafetyEventReportView, name="safety-event-report"),
    path("events/", views.SafetyEventListView, name="safety-event-list"),
    path("restriction/", views.UserRestrictionView, name="user-restriction"),
    path("blocked-phrases/", views.BlockedPhraseListView, name="blocked-phrase-list"),
    path("blocked-phrases/upsert/", views.BlockedPhraseUpsertView, name="blocked-phrase-upsert"),
]
