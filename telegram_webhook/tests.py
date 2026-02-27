import os
from unittest.mock import patch

from django.test import SimpleTestCase

from telegram_webhook.views import _get_ai_timeout_seconds


class AiTimeoutConfigTests(SimpleTestCase):
    def test_default_timeout_is_more_tolerant(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_ai_timeout_seconds(), 8.0)

    def test_timeout_can_be_overridden(self):
        with patch.dict(os.environ, {"AI_REPLY_TIMEOUT_SECONDS": "12"}, clear=True):
            self.assertEqual(_get_ai_timeout_seconds(), 12.0)

    def test_timeout_is_clamped(self):
        with patch.dict(os.environ, {"AI_REPLY_TIMEOUT_SECONDS": "0.2"}, clear=True):
            self.assertEqual(_get_ai_timeout_seconds(), 1.0)
        with patch.dict(os.environ, {"AI_REPLY_TIMEOUT_SECONDS": "999"}, clear=True):
            self.assertEqual(_get_ai_timeout_seconds(), 30.0)
