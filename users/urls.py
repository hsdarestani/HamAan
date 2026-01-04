from django.urls import path

from . import views

urlpatterns = [
    path("create-or-update/", views.UserCreateOrUpdateFromTelegramView, name="user-create-or-update"),
    path("profile/", views.UserProfileView, name="user-profile"),
    path("prefs/", views.UserPrefsView, name="user-prefs"),
    path("delete/", views.UserDeleteDataView, name="user-delete"),
]
