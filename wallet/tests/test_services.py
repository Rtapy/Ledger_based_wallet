"""Posting-service contract.

post_entry(*, user_id, entry_type, amount: Decimal, idempotency_key: UUID)
returns an object with .entry (persisted LedgerEntry) and .created (bool).
Domain rejections raise WalletError with a stable .code.

The API parses JSON/header strings. This service accepts Decimal and UUID
objects, rejects non-finite amounts and more than eight fractional places
(including trailing zeros), and compares valid retry amounts numerically.
"""

from decimal import Decimal
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase

from wallet.exceptions import WalletError
from wallet.models import LedgerEntry, Wallet
from wallet.services import post_entry


User = get_user_model()


class PostingServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="posting-alice")
        cls.wallet = Wallet.objects.create(user=cls.user)
        cls.other_user = User.objects.create_user(username="posting-bob")
        cls.other_wallet = Wallet.objects.create(user=cls.other_user)

    def _post(self, **overrides):
        arguments = {
            "user_id": self.user.pk,
            "entry_type": LedgerEntry.EntryType.CREDIT,
            "amount": Decimal("10"),
            "idempotency_key": uuid4(),
        }
        arguments.update(overrides)
        return post_entry(**arguments)

    def _snapshot(self):
        # Include timestamps and all wallets, so replays/rejections must be inert.
        return (
            list(Wallet.objects.order_by("pk").values()),
            list(LedgerEntry.objects.order_by("pk").values()),
        )

    def _assert_rejected(self, code, **arguments):
        before = self._snapshot()
        with self.assertRaises(WalletError) as raised:
            self._post(**arguments)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self._snapshot(), before)

    def _assert_history(self, wallet, expected_balance, expected_count):
        wallet.refresh_from_db()
        entries = list(wallet.entries.order_by("id"))
        self.assertEqual(len(entries), expected_count)
        running_balance = Decimal("0")
        for entry in entries:
            self.assertEqual(entry.balance_before, running_balance)
            self.assertGreater(entry.amount, 0)
            self.assertIn(entry.entry_type, ("credit", "debit"))
            if entry.entry_type == "credit":
                running_balance += entry.amount
            else:
                running_balance -= entry.amount
            self.assertGreaterEqual(running_balance, 0)
            self.assertEqual(entry.balance_after, running_balance)
        self.assertEqual(running_balance, expected_balance)
        self.assertEqual(wallet.balance, expected_balance)

    def test_credit_returns_the_persisted_entry_and_created_flag(self):
        key = uuid4()
        result = self._post(amount=Decimal("12.34567890"), idempotency_key=key)

        self.assertIs(result.created, True)
        self.assertIsInstance(result.entry, LedgerEntry)
        self.assertIsNotNone(result.entry.pk)
        result.entry.refresh_from_db()
        self.assertEqual(result.entry.wallet_id, self.wallet.pk)
        self.assertEqual(result.entry.entry_type, "credit")
        self.assertEqual(result.entry.amount, Decimal("12.34567890"))
        self.assertEqual(result.entry.idempotency_key, key)
        self._assert_history(self.wallet, Decimal("12.34567890"), 1)
        self._assert_history(self.other_wallet, Decimal("0"), 0)

    def test_sequential_credits_and_debits_preserve_the_complete_chain(self):
        self._post(amount=Decimal("10.12345678"))
        self._post(amount=Decimal("2.00000002"))
        debit = self._post(entry_type="debit", amount=Decimal("1.00000001"))

        self.assertIs(debit.created, True)
        self.assertEqual(debit.entry.entry_type, "debit")
        self._assert_history(self.wallet, Decimal("11.12345679"), 3)

    def test_decimal_addition_and_subtraction_are_exact(self):
        self._post(amount=Decimal("0.1"))
        self._post(amount=Decimal("0.2"))
        self._assert_history(self.wallet, Decimal("0.3"), 2)
        self._post(entry_type="debit", amount=Decimal("0.3"))
        self._assert_history(self.wallet, Decimal("0"), 3)

    def test_debit_from_an_empty_wallet_is_rejected(self):
        self._assert_rejected("insufficient_funds", entry_type="debit")

    def test_debit_one_smallest_unit_above_balance_is_rejected(self):
        self._post(amount=Decimal("100"))
        self._assert_rejected(
            "insufficient_funds",
            entry_type="debit",
            amount=Decimal("100.00000001"),
        )
        self._assert_history(self.wallet, Decimal("100"), 1)

    def test_debit_may_leave_exactly_zero(self):
        self._post(amount=Decimal("100"))
        self._post(entry_type="debit", amount=Decimal("100"))
        self._assert_history(self.wallet, Decimal("0"), 2)

    def test_smallest_unit_can_be_credited_and_debited(self):
        self._post(amount=Decimal("0.00000001"))
        self._assert_history(self.wallet, Decimal("0.00000001"), 1)
        self._post(entry_type="debit", amount=Decimal("0.00000001"))
        self._assert_history(self.wallet, Decimal("0"), 2)

    def test_maximum_amount_can_be_credited_and_debited(self):
        maximum = Decimal("999999999999.99999999")
        self._post(amount=maximum)
        self._assert_history(self.wallet, maximum, 1)
        self._post(entry_type="debit", amount=maximum)
        self._assert_history(self.wallet, Decimal("0"), 2)

    def test_credit_may_reach_the_balance_ceiling_exactly(self):
        self._post(amount=Decimal("999999999999.99999998"))
        self._post(amount=Decimal("0.00000001"))
        self._assert_history(self.wallet, Decimal("999999999999.99999999"), 2)

    def test_credit_above_the_balance_ceiling_is_rejected(self):
        self._post(amount=Decimal("999999999999.99999999"))
        self._assert_rejected("balance_limit_exceeded", amount=Decimal("0.00000001"))

    def test_invalid_amounts_are_rejected_without_writes(self):
        cases = (
            ("zero", Decimal("0")),
            ("negative_zero", Decimal("-0")),
            ("negative", Decimal("-1")),
            ("nan", Decimal("NaN")),
            ("signaling_nan", Decimal("sNaN")),
            ("infinity", Decimal("Infinity")),
            ("negative_infinity", Decimal("-Infinity")),
            ("below_smallest_unit", Decimal("0.000000001")),
            ("excess_fraction", Decimal("1.123456789")),
            ("excess_trailing_zeros", Decimal("1.000000000")),
            ("above_maximum", Decimal("1000000000000")),
            ("float", 1.0),
            ("integer", 1),
            ("string", "1.00"),
            ("boolean_true", True),
            ("boolean_false", False),
            ("none", None),
        )
        for label, amount in cases:
            for entry_type in ("credit", "debit"):
                with self.subTest(case=label, entry_type=entry_type):
                    self._assert_rejected(
                        "invalid_amount", entry_type=entry_type, amount=amount
                    )

    def test_invalid_entry_types_are_rejected_without_writes(self):
        for entry_type in ("refund", "CREDIT", "", None, 1):
            with self.subTest(entry_type=entry_type):
                self._assert_rejected("invalid_request", entry_type=entry_type)

    def test_service_requires_a_uuid_object(self):
        for key in (None, "not-a-uuid", str(uuid4()), b"uuid", True, 1):
            with self.subTest(key=key):
                self._assert_rejected("invalid_idempotency_key", idempotency_key=key)

    def test_missing_user_is_rejected_without_creating_a_wallet(self):
        missing_id = User.objects.order_by("-pk").values_list("pk", flat=True).first() + 1
        self._assert_rejected("user_not_found", user_id=missing_id)

    def test_existing_user_without_wallet_is_rejected_without_provisioning(self):
        user = User.objects.create_user(username="posting-without-wallet")
        self._assert_rejected("wallet_not_found", user_id=user.pk)
        self.assertFalse(Wallet.objects.filter(user=user).exists())

    def test_credit_retry_returns_original_entry_without_any_writes(self):
        key = uuid4()
        original = self._post(idempotency_key=key)
        before = self._snapshot()

        replay = self._post(idempotency_key=key)

        self.assertIs(replay.created, False)
        self.assertEqual(replay.entry.pk, original.entry.pk)
        self.assertEqual(self._snapshot(), before)
        self._assert_history(self.wallet, Decimal("10"), 1)

    def test_debit_retry_succeeds_even_when_current_balance_is_zero(self):
        self._post(amount=Decimal("10"))
        key = uuid4()
        original = self._post(entry_type="debit", idempotency_key=key)
        before = self._snapshot()

        replay = self._post(entry_type="debit", idempotency_key=key)

        self.assertIs(replay.created, False)
        self.assertEqual(replay.entry.pk, original.entry.pk)
        self.assertEqual(self._snapshot(), before)
        self._assert_history(self.wallet, Decimal("0"), 2)

    def test_credit_retry_succeeds_even_when_balance_is_at_the_ceiling(self):
        key = uuid4()
        original = self._post(idempotency_key=key)
        self._post(amount=Decimal("999999999989.99999999"))
        before = self._snapshot()

        replay = self._post(idempotency_key=key)

        self.assertIs(replay.created, False)
        self.assertEqual(replay.entry.pk, original.entry.pk)
        self.assertEqual(replay.entry.balance_after, Decimal("10"))
        self.assertEqual(self._snapshot(), before)

    def test_retry_after_later_entries_preserves_the_original_snapshot(self):
        key = uuid4()
        original = self._post(idempotency_key=key)
        self._post(amount=Decimal("20"))
        before = self._snapshot()

        replay = self._post(idempotency_key=key)

        self.assertEqual(replay.entry.pk, original.entry.pk)
        self.assertEqual(replay.entry.balance_before, Decimal("0"))
        self.assertEqual(replay.entry.balance_after, Decimal("10"))
        self.assertIs(replay.created, False)
        self.assertEqual(self._snapshot(), before)
        self._assert_history(self.wallet, Decimal("30"), 2)

    def test_retry_compares_amounts_numerically_and_uuids_by_value(self):
        key = uuid4()
        original = self._post(amount=Decimal("10.0"), idempotency_key=key)
        before = self._snapshot()

        replay = self._post(
            amount=Decimal("10.00000000"),
            idempotency_key=UUID(str(key).upper()),
        )

        self.assertEqual(replay.entry.pk, original.entry.pk)
        self.assertIs(replay.created, False)
        self.assertEqual(self._snapshot(), before)

    def test_same_key_with_changed_amount_is_a_conflict(self):
        key = uuid4()
        self._post(idempotency_key=key)
        self._assert_rejected(
            "idempotency_conflict", amount=Decimal("11"), idempotency_key=key
        )

    def test_same_key_with_changed_type_is_a_conflict(self):
        key = uuid4()
        self._post(idempotency_key=key)
        self._assert_rejected(
            "idempotency_conflict", entry_type="debit", idempotency_key=key
        )

    def test_conflict_is_checked_before_current_funds(self):
        self._post()
        key = uuid4()
        self._post(entry_type="debit", idempotency_key=key)
        self._assert_rejected(
            "idempotency_conflict",
            entry_type="debit",
            amount=Decimal("11"),
            idempotency_key=key,
        )

    def test_same_key_is_independent_for_different_wallets(self):
        key = uuid4()
        first = self._post(idempotency_key=key)
        second = self._post(
            user_id=self.other_user.pk, amount=Decimal("20"), idempotency_key=key
        )
        self.assertIs(first.created, True)
        self.assertIs(second.created, True)
        self.assertNotEqual(first.entry.pk, second.entry.pk)
        self._assert_history(self.wallet, Decimal("10"), 1)
        self._assert_history(self.other_wallet, Decimal("20"), 1)

    def test_conflict_is_checked_before_the_balance_ceiling(self):
        key = uuid4()
        self._post(amount=Decimal("999999999999.99999999"), idempotency_key=key)
        self._assert_rejected(
            "idempotency_conflict", amount=Decimal("1"), idempotency_key=key
        )

    def test_distinct_keys_allow_identical_operations(self):
        first = self._post()
        second = self._post()
        self.assertIs(first.created, True)
        self.assertIs(second.created, True)
        self.assertNotEqual(first.entry.pk, second.entry.pk)
        self._assert_history(self.wallet, Decimal("20"), 2)

    def test_insufficient_funds_does_not_reserve_the_key(self):
        key = uuid4()
        self._assert_rejected(
            "insufficient_funds", entry_type="debit", idempotency_key=key
        )
        self._post()
        successful = self._post(entry_type="debit", idempotency_key=key)
        replay = self._post(entry_type="debit", idempotency_key=key)
        self.assertIs(successful.created, True)
        self.assertIs(replay.created, False)
        self.assertEqual(successful.entry.pk, replay.entry.pk)
        self._assert_history(self.wallet, Decimal("0"), 2)

    def test_balance_ceiling_rejection_does_not_reserve_the_key(self):
        self._post(amount=Decimal("999999999999.99999999"))
        key = uuid4()
        self._assert_rejected("balance_limit_exceeded", idempotency_key=key)
        self._post(entry_type="debit")
        result = self._post(idempotency_key=key)
        self.assertIs(result.created, True)
        self._assert_history(self.wallet, Decimal("999999999999.99999999"), 3)

    def test_invalid_amount_does_not_reserve_a_valid_key(self):
        key = uuid4()
        self._assert_rejected("invalid_amount", amount=Decimal("0"), idempotency_key=key)
        result = self._post(idempotency_key=key)
        self.assertIs(result.created, True)
        self._assert_history(self.wallet, Decimal("10"), 1)
