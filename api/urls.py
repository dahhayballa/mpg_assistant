from django.urls import path

from .views import ChatView, WhatsAppWebhookView

urlpatterns = [
    path('chat/', ChatView.as_view(), name='chat'),
    path('whatsapp/webhook/', WhatsAppWebhookView.as_view(), name='whatsapp-webhook'),
]
