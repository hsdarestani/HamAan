from django.urls import path

from . import views

urlpatterns = [
    path("", views.BotListView, name="bot-list"),
    path("select/", views.BotSelectView, name="bot-select"),
    path("profile/", views.BotProfileView, name="bot-profile"),
    path("state/", views.BotUserStateView, name="bot-user-state"),
    path("memory/", views.MemoryFragmentsListView, name="memory-fragments"),
    path("memory/upsert/", views.MemoryFragmentUpsertView, name="memory-fragment-upsert"),
    path("memory/deactivate/", views.MemoryFragmentDeactivateView, name="memory-fragment-deactivate"),
]
