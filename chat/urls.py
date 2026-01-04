from django.urls import path

from . import views

urlpatterns = [
    path("conversations/create-or-get/", views.ConversationCreateOrGetView, name="conversation-create-or-get"),
    path("conversations/", views.ConversationListView, name="conversation-list"),
    path("conversations/detail/", views.ConversationDetailView, name="conversation-detail"),
    path("messages/", views.MessageListView, name="message-list"),
    path("messages/user/", views.MessageCreateUserView, name="message-create-user"),
    path("messages/bot/", views.MessageCreateBotView, name="message-create-bot"),
    path("llm-calls/", views.LLMCallLogListView, name="llmcall-list"),
]
