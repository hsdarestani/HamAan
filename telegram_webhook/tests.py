import os
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from persona.models import Bot, BotUserState

from telegram_webhook.views import (
    _ensure_conversation,
    _get_ai_latency_budget_seconds,
    _get_ai_retry_timeout_seconds,
    _get_ai_timeout_seconds,
    _handle_message,
    _is_ai_timeout_retry_enabled,
    _partner_setup_completed,
)
from users.models import User


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
        with patch.dict(
            os.environ, {"AI_REPLY_RETRY_TIMEOUT_SECONDS": "0.1"}, clear=True
        ):
            self.assertEqual(_get_ai_retry_timeout_seconds(), 0.5)
        with patch.dict(
            os.environ, {"AI_REPLY_RETRY_TIMEOUT_SECONDS": "100"}, clear=True
        ):
            self.assertEqual(_get_ai_retry_timeout_seconds(), 12.0)


class AiLatencyBudgetConfigTests(SimpleTestCase):
    def test_default_budget(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_ai_latency_budget_seconds(), 2.0)

    def test_budget_clamp(self):
        with patch.dict(
            os.environ, {"AI_REPLY_TOTAL_BUDGET_SECONDS": "0.1"}, clear=True
        ):
            self.assertEqual(_get_ai_latency_budget_seconds(), 1.0)
        with patch.dict(
            os.environ, {"AI_REPLY_TOTAL_BUDGET_SECONDS": "100"}, clear=True
        ):
            self.assertEqual(_get_ai_latency_budget_seconds(), 5.0)


class AiTimeoutRetryFlagTests(SimpleTestCase):
    def test_retry_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_is_ai_timeout_retry_enabled())

    def test_retry_enabled_with_env(self):
        with patch.dict(os.environ, {"AI_TIMEOUT_RETRY_ENABLED": "true"}, clear=True):
            self.assertTrue(_is_ai_timeout_retry_enabled())


class PartnerSetupFlowTests(TestCase):
    def setUp(self):
        self.template = Bot.objects.create(
            code="hamdam", display_name="همدم", is_active=True
        )
        self.user = User.objects.create_user(telegram_id=12345, first_name="Ali")
        self.bot = self.template
        self.user.assigned_bot = self.bot
        self.user.save(update_fields=["assigned_bot", "updated_at"])
        self.conversation = _ensure_conversation(self.user, self.bot)

    def test_start_prompts_partner_setup_before_chat(self):
        replies = _handle_message(
            self.user, self.bot, "/start", {"message": {"message_id": 1}}
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("پارتنر دیجیتالت", replies[0]["text"])
        self.assertIn("قابل تغییر نیست", replies[0]["text"])
        self.assertIn("inline_keyboard", replies[0]["reply_markup"])

    def test_partner_setup_callbacks_lock_profile_and_update_bot(self):
        for text in [
            "psetup:gender:FEMALE",
            "psetup:name:آوا",
            "psetup:age:26",
            "psetup:interest:music",
            "psetup:interest:books",
        ]:
            _handle_message(
                self.user,
                self.bot,
                text,
                {"callback_query": {"message": {"message_id": 1}}},
            )

        replies = _handle_message(
            self.user,
            self.bot,
            "psetup:done",
            {"callback_query": {"message": {"message_id": 1}}},
        )

        self.bot.refresh_from_db()
        state = BotUserState.objects.get(user=self.user, bot=self.bot)
        profile = state.relationship_memory["digital_partner_profile"]
        self.assertTrue(_partner_setup_completed(state))
        self.assertEqual(self.bot.display_name, "آوا")
        self.assertEqual(self.bot.gender, "FEMALE")
        self.assertEqual(profile["age"], "26")
        self.assertEqual(profile["interests"], ["موسیقی", "کتاب"])
        self.assertIn("قفل", replies[0]["text"])
