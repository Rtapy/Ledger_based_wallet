from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from wallet.models import LedgerEntry, Wallet


User = get_user_model()


class WalletModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="alice")

    def test_wallet_created_with_zero_balance_by_default(self):
        wallet = Wallet.objects.create(user=self.user)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0"))

    def test_wallet_requires_a_user(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Wallet.objects.create(user=None)

    def test_only_one_wallet_per_user_is_allowed(self):
        Wallet.objects.create(user=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Wallet.objects.create(user=self.user)
        self.assertEqual(Wallet.objects.filter(user=self.user).count(), 1)

    def test_wallet_balance_cannot_be_negative_at_db_level(self):
        wallet = Wallet.objects.create(user=self.user)
        # update() bypasses Model.save() and its signals.
        with self.assertRaises(IntegrityError) as raised:
            with transaction.atomic():
                Wallet.objects.filter(pk=wallet.pk).update(balance=Decimal("-1"))
        self.assertEqual(
            raised.exception.__cause__.diag.constraint_name,
            "wallet_balance_range",
        )
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0"))

    def test_wallet_preserves_decimal_precision_and_boundary_values(self):
        wallet = Wallet.objects.create(user=self.user)
        for amount in (
            Decimal("0.00000001"),
            Decimal("1.12345678"),
            Decimal("999999999999.99999999"),
        ):
            with self.subTest(amount=amount):
                Wallet.objects.filter(pk=wallet.pk).update(balance=amount)
                wallet.refresh_from_db()
                self.assertEqual(wallet.balance, amount)

    def test_deleting_a_user_with_a_wallet_is_protected(self):
        wallet = Wallet.objects.create(user=self.user)
        with self.assertRaises(ProtectedError):
            self.user.delete()
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Wallet.objects.filter(pk=wallet.pk).exists())


class LedgerEntryModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="bob")
        cls.wallet = Wallet.objects.create(user=cls.user)

    def _make_entry(self, **overrides):
        # These fixtures exercise individual rows, not the posting service.
        defaults = dict(
            wallet=self.wallet,
            entry_type=LedgerEntry.EntryType.CREDIT,
            amount=Decimal("10.00"),
            balance_before=Decimal("0.00"),
            balance_after=Decimal("10.00"),
            idempotency_key=uuid4(),
        )
        defaults.update(overrides)
        return LedgerEntry.objects.create(**defaults)

    def _assert_constraint_rejected(self, constraint_name, **overrides):
        count_before = LedgerEntry.objects.count()
        with self.assertRaises(IntegrityError) as raised:
            with transaction.atomic():
                self._make_entry(**overrides)
        # PostgreSQL evidence: failure must come from the intended constraint.
        self.assertEqual(
            raised.exception.__cause__.diag.constraint_name,
            constraint_name,
        )
        self.assertEqual(LedgerEntry.objects.count(), count_before)

    def test_entry_requires_a_wallet(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._make_entry(wallet=None)

    def test_amount_must_be_positive_at_db_level(self):
        # The balances and credit arithmetic are otherwise valid.
        self._assert_constraint_rejected(
            "ledger_amount_range",
            amount=Decimal("-10"),
            balance_before=Decimal("20"),
            balance_after=Decimal("10"),
        )

    def test_zero_amount_is_rejected_at_db_level(self):
        self._assert_constraint_rejected(
            "ledger_amount_range",
            amount=Decimal("0"),
            balance_after=Decimal("0"),
        )

    def test_balance_before_cannot_be_negative_at_db_level(self):
        self._assert_constraint_rejected(
            "ledger_before_range",
            amount=Decimal("10"),
            balance_before=Decimal("-5"),
            balance_after=Decimal("5"),
        )

    def test_balance_after_cannot_be_negative_at_db_level(self):
        self._assert_constraint_rejected(
            "ledger_after_range",
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("10"),
            balance_before=Decimal("5"),
            balance_after=Decimal("-5"),
        )

    def test_entry_type_is_restricted_to_known_values_at_db_level(self):
        # Invalid but within max_length, so this reaches the CHECK constraint.
        self._assert_constraint_rejected(
            "ledger_known_type", entry_type="refund"
        )

    def test_credit_arithmetic_is_checked_at_db_level(self):
        self._assert_constraint_rejected(
            "ledger_credit_arithmetic",
            amount=Decimal("5"),
            balance_before=Decimal("10"),
            balance_after=Decimal("14"),
        )

    def test_debit_arithmetic_is_checked_at_db_level(self):
        self._assert_constraint_rejected(
            "ledger_debit_arithmetic",
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("5"),
            balance_before=Decimal("10"),
            balance_after=Decimal("6"),
        )

    def test_valid_debit_can_reach_zero(self):
        entry = self._make_entry(
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("10"),
            balance_before=Decimal("10"),
            balance_after=Decimal("0"),
        )
        entry.refresh_from_db()
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.DEBIT)
        self.assertEqual(entry.balance_after, Decimal("0"))

    def test_ledger_preserves_decimal_precision_and_boundary_values(self):
        for amount in (
            Decimal("0.00000001"),
            Decimal("1.12345678"),
            Decimal("999999999999.99999999"),
        ):
            with self.subTest(amount=amount):
                credit = self._make_entry(amount=amount, balance_after=amount)
                debit = self._make_entry(
                    entry_type=LedgerEntry.EntryType.DEBIT,
                    amount=amount,
                    balance_before=amount,
                    balance_after=Decimal("0"),
                )
                credit.refresh_from_db()
                debit.refresh_from_db()
                self.assertEqual(credit.amount, amount)
                self.assertEqual(credit.balance_before, Decimal("0"))
                self.assertEqual(credit.balance_after, amount)
                self.assertEqual(debit.amount, amount)
                self.assertEqual(debit.balance_before, amount)
                self.assertEqual(debit.balance_after, Decimal("0"))

    def test_idempotency_key_is_required(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._make_entry(idempotency_key=None)

    def test_idempotency_key_must_be_a_valid_uuid(self):
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                self._make_entry(idempotency_key="not-a-uuid")
        self.assertFalse(LedgerEntry.objects.exists())

    def test_idempotency_key_round_trips_as_uuid(self):
        key = uuid4()
        entry = self._make_entry(idempotency_key=key)
        entry.refresh_from_db()
        self.assertEqual(entry.idempotency_key, key)

    def test_idempotency_key_is_unique_within_a_wallet(self):
        key = uuid4()
        original = self._make_entry(idempotency_key=key)
        self._assert_constraint_rejected(
            "ledger_wallet_key_unique",
            amount=Decimal("20"),
            balance_before=Decimal("10"),
            balance_after=Decimal("30"),
            idempotency_key=key,
        )
        self.assertEqual(self.wallet.entries.get().pk, original.pk)

    def test_idempotency_key_is_shared_across_credit_and_debit(self):
        key = uuid4()
        self._make_entry(idempotency_key=key)
        self._assert_constraint_rejected(
            "ledger_wallet_key_unique",
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("10"),
            balance_before=Decimal("10"),
            balance_after=Decimal("0"),
            idempotency_key=key,
        )

    def test_idempotency_key_may_repeat_across_different_wallets(self):
        other_user = User.objects.create_user(username="carol")
        other_wallet = Wallet.objects.create(user=other_user)
        key = uuid4()
        first = self._make_entry(idempotency_key=key)
        second = self._make_entry(wallet=other_wallet, idempotency_key=key)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(self.wallet.entries.get().pk, first.pk)
        self.assertEqual(other_wallet.entries.get().pk, second.pk)

    def test_deleting_a_wallet_with_entries_is_protected(self):
        entry = self._make_entry()
        with self.assertRaises(ProtectedError):
            self.wallet.delete()
        self.assertTrue(Wallet.objects.filter(pk=self.wallet.pk).exists())
        self.assertTrue(LedgerEntry.objects.filter(pk=entry.pk).exists())

    def test_entries_are_ordered_by_newest_created_at_then_highest_id(self):
        # Equal timestamps verify ID is the deterministic tie-breaker.
        # Row fixtures intentionally do not represent a complete wallet history.
        third = self._make_entry(id=300)
        first = self._make_entry(id=100)
        second = self._make_entry(id=200)
        timestamp = third.created_at
        LedgerEntry.objects.filter(pk__in=[first.pk, second.pk]).update(
            created_at=timestamp
        )
        history = self.wallet.entries.all()
        self.assertTrue(history.ordered)
        self.assertEqual(list(history), [third, second, first])
