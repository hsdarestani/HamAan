from django.urls import path

from . import views

urlpatterns = [
    path("initiation/rule/", views.InitiationRuleView, name="initiation-rule"),
    path("initiation/status/", views.InitiationStatusView, name="initiation-status"),
    path("initiation/trigger/", views.InitiationTriggerView, name="initiation-trigger"),
    path("initiation/events/", views.InitiationEventListView, name="initiation-event-list"),
    path("jobs/", views.ScheduledJobListView, name="scheduled-job-list"),
    path("jobs/run/", views.ScheduledJobRunView, name="scheduled-job-run"),
]
