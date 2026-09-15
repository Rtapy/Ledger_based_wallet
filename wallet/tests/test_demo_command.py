from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from wallet.models import LedgerEntry, Wallet
from wallet.services import post_entry


User = get_user_model()


class DemoUserCommandTests(TestCase):
    def _create(self, username):
        stdout = StringIO()
        call_command("create_demo_user", "--username", username, stdout=stdout)
        return int(stdout.getvalue().strip())

    def test_creates_user_and_empty_wallet_and_prints_id(self):
        user_id = self._create("demo-alice")
        user = User.objects.get(pk=user_id)
        self.assertEqual(user.username, "demo-alice")
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.wallet.balance, Decimal("0"))
        self.assertFalse(LedgerEntry.objects.exists())

    def test_existing_user_with_history_is_rejected_without_resetting_data(self):
        user_id = self._create("demo-alice")
        post_entry(user_id=user_id, entry_type="credit", amount=Decimal("10"),
                   idempotency_key=uuid4())
        before = (
            list(User.objects.values()), list(Wallet.objects.values()),
            list(LedgerEntry.objects.values()),
        )
        with self.assertRaisesMessage(CommandError, "user_exists"):
            self._create("demo-alice")
        self.assertEqual(before, (
            list(User.objects.values()), list(Wallet.objects.values()),
            list(LedgerEntry.objects.values()),
        ))

    def test_existing_user_without_wallet_is_rejected_without_provisioning(self):
        user = User.objects.create_user(username="already-exists")
        with self.assertRaisesMessage(CommandError, "user_exists"):
            self._create(user.username)
        self.assertFalse(Wallet.objects.filter(user=user).exists())

    def test_invalid_usernames_do_not_create_partial_data(self):
        for username in ("", "with space", "bad/name", "x" * 151):
            with self.subTest(username=username):
                with self.assertRaisesMessage(CommandError, "invalid_username"):
                    self._create(username)
                self.assertFalse(User.objects.exists())
                self.assertFalse(Wallet.objects.exists())

    def test_username_is_normalized_before_duplicate_detection(self):
        user_id = self._create("Ａlice")
        self.assertEqual(User.objects.get(pk=user_id).username, "Alice")
        with self.assertRaisesMessage(CommandError, "user_exists"):
            self._create("Alice")

    def test_wallet_creation_failure_rolls_back_the_user(self):
        stdout = StringIO()
        with patch.object(Wallet.objects, "create", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesMessage(RuntimeError, "injected failure"):
                call_command("create_demo_user", username="rollback-demo", stdout=stdout)
        self.assertFalse(User.objects.exists())
        self.assertFalse(Wallet.objects.exists())
        self.assertEqual(stdout.getvalue(), "")
