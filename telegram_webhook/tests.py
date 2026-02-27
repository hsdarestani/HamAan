import os
from unittest.mock import patch

from django.test import SimpleTestCase

from telegram_webhook.views import (
    _get_ai_latency_budget_seconds,
    _get_ai_retry_timeout_seconds,
    _get_ai_timeout_seconds,
    _is_ai_timeout_retry_enabled,
)


class AiTimeoutConfigTests(SimpleTestCase):
    def test_default_timeout(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_ai_timeout_seconds(), 2.0)

    def test_timeout_override_and_clamp(self):
        with patch.dict(os.environ, {"AI_REPLY_TIMEOUT_SECONDS": "1.5"}, clear=True):
            self.assertEqual(_get_ai_timeout_seconds(), 1.5)
        with patch.dict(os.environ, {"AI_REPLY_TIMEOUT_SECONDS": "0.1"}, clear=True):
            self.assertEqual(_get_ai_timeout_seconds(), 0.6)
        with patch.dict(os.environ, {"AI_REPLY_TIMEOUT_SECONDS": "100"}, clear=True):
            self.assertEqual(_get_ai_timeout_seconds(), 10.0)


class AiRetryTimeoutConfigTests(SimpleTestCase):
    def test_default_retry_timeout(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_ai_retry_timeout_seconds(), 4.0)

    def test_retry_timeout_clamp(self):
        with patch.dict(os.environ, {"AI_REPLY_RETRY_TIMEOUT_SECONDS": "0.1"}, clear=True):
            self.assertEqual(_get_ai_retry_timeout_seconds(), 0.5)
        with patch.dict(os.environ, {"AI_REPLY_RETRY_TIMEOUT_SECONDS": "100"}, clear=True):
            self.assertEqual(_get_ai_retry_timeout_seconds(), 12.0)


class AiLatencyBudgetConfigTests(SimpleTestCase):
    def test_default_budget(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_ai_latency_budget_seconds(), 2.0)

    def test_budget_clamp(self):
        with patch.dict(os.environ, {"AI_REPLY_TOTAL_BUDGET_SECONDS": "0.1"}, clear=True):
            self.assertEqual(_get_ai_latency_budget_seconds(), 1.0)
        with patch.dict(os.environ, {"AI_REPLY_TOTAL_BUDGET_SECONDS": "100"}, clear=True):
            self.assertEqual(_get_ai_latency_budget_seconds(), 5.0)


class AiTimeoutRetryFlagTests(SimpleTestCase):
    def test_retry_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_is_ai_timeout_retry_enabled())

    def test_retry_enabled_with_env(self):
        with patch.dict(os.environ, {"AI_TIMEOUT_RETRY_ENABLED": "true"}, clear=True):
            self.assertTrue(_is_ai_timeout_retry_enabled())
