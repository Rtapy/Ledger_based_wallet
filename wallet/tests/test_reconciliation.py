import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from io import StringIO
from queue import Queue
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from wallet.exceptions import WalletError
from wallet.models import LedgerEntry, Wallet
from wallet.reconciliation import reconcile_wallet
from wallet.services import post_entry


User = get_user_model()


class _ReconciliationHelpers:
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="reconciliation-alice")
        self.wallet = Wallet.objects.create(user=self.user)

    def _post(self, amount="10", entry_type="credit", user_id=None):
        return post_entry(
            user_id=user_id or self.user.pk,
            entry_type=entry_type,
            amount=Decimal(amount),
            idempotency_key=uuid4(),
        ).entry

    def _snapshot(self):
        return (
            list(Wallet.objects.order_by("pk").values()),
            list(LedgerEntry.objects.order_by("pk").values()),
        )

    def _assert_consistent(self, result, balance, count):
        self.assertTrue(result.is_consistent, result)
        self.assertTrue(result.balance_matches)
        self.assertTrue(result.chain_valid)
        self.assertIsNone(result.first_invalid_entry_id)
        self.assertEqual(result.wallet_id, self.wallet.pk)
        self.assertEqual(result.user_id, self.user.pk)
        self.assertEqual(result.stored_balance, Decimal(balance))
        self.assertEqual(result.ledger_balance, Decimal(balance))
        self.assertEqual(result.entry_count, count)


class ReconciliationTests(_ReconciliationHelpers, TestCase):
    def test_empty_zero_wallet_is_consistent(self):
        self._assert_consistent(reconcile_wallet(user_id=self.user.pk), "0", 0)

    def test_sequential_credit_debit_history_is_consistent(self):
        self._post("10.12345678")
        self._post("2.00000002")
        self._post("1.00000001", "debit")
        self._assert_consistent(
            reconcile_wallet(user_id=self.user.pk), "11.12345679", 3
        )

    def test_maximum_credit_and_debit_cancel_exactly_before_smallest_credit(self):
        self._post("999999999999.99999999")
        self._post("999999999999.99999999", "debit")
        self._assert_consistent(reconcile_wallet(user_id=self.user.pk), "0", 2)
        self._post("0.00000001")
        self._assert_consistent(reconcile_wallet(user_id=self.user.pk), "0.00000001", 3)

    def test_low_caller_decimal_precision_does_not_round_the_audit(self):
        self._post("123456789012.12345678")
        self._post("0.00000001", "debit")
        with localcontext() as context:
            context.prec = 4
            result = reconcile_wallet(user_id=self.user.pk)
        self._assert_consistent(result, "123456789012.12345677", 2)

    def test_changed_cached_balance_is_detected_without_repair(self):
        self._post("10")
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("11"))
        before = self._snapshot()
        result = reconcile_wallet(user_id=self.user.pk)
        self.assertFalse(result.is_consistent)
        self.assertFalse(result.balance_matches)
        self.assertTrue(result.chain_valid)
        self.assertEqual(result.stored_balance, Decimal("11"))
        self.assertEqual(result.ledger_balance, Decimal("10"))
        self.assertEqual(self._snapshot(), before)

    def test_nonzero_balance_without_entries_is_detected(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("1"))
        result = reconcile_wallet(user_id=self.user.pk)
        self.assertFalse(result.is_consistent)
        self.assertFalse(result.balance_matches)
        self.assertTrue(result.chain_valid)
        self.assertEqual(result.ledger_balance, Decimal("0"))
        self.assertEqual(result.entry_count, 0)

    def test_chain_corruption_is_detected_even_when_signed_sum_matches(self):
        self._post("10")
        second = self._post("20")
        # Preserve per-row arithmetic and the signed sum, but break the chain.
        LedgerEntry.objects.filter(pk=second.pk).update(
            balance_before=Decimal("12"), balance_after=Decimal("32")
        )
        before = self._snapshot()
        result = reconcile_wallet(user_id=self.user.pk)
        self.assertTrue(result.balance_matches)
        self.assertFalse(result.chain_valid)
        self.assertFalse(result.is_consistent)
        self.assertEqual(result.first_invalid_entry_id, second.pk)
        self.assertEqual(result.ledger_balance, Decimal("30"))
        self.assertEqual(self._snapshot(), before)

    def test_first_entry_must_start_from_zero(self):
        first = self._post()
        LedgerEntry.objects.filter(pk=first.pk).update(
            balance_before=Decimal("5"), balance_after=Decimal("15")
        )
        result = reconcile_wallet(user_id=self.user.pk)
        self.assertTrue(result.balance_matches)
        self.assertFalse(result.is_consistent)
        self.assertEqual(result.first_invalid_entry_id, first.pk)

    def test_deleted_middle_entry_is_detected(self):
        self._post("10")
        middle = self._post("20")
        last = self._post("30")
        LedgerEntry.objects.filter(pk=middle.pk).delete()
        result = reconcile_wallet(user_id=self.user.pk)
        self.assertFalse(result.balance_matches)
        self.assertFalse(result.chain_valid)
        self.assertEqual(result.ledger_balance, Decimal("40"))
        self.assertEqual(result.first_invalid_entry_id, last.pk)
        self.assertEqual(result.entry_count, 2)

    def test_changed_amount_and_snapshot_do_not_fool_balance_comparison(self):
        entry = self._post()
        LedgerEntry.objects.filter(pk=entry.pk).update(
            amount=Decimal("11"), balance_after=Decimal("11")
        )
        result = reconcile_wallet(user_id=self.user.pk)
        self.assertTrue(result.chain_valid)
        self.assertFalse(result.balance_matches)
        self.assertEqual(result.ledger_balance, Decimal("11"))

    def test_inconsistent_sum_beyond_storage_precision_is_reported_exactly(self):
        maximum = Decimal("999999999999.99999999")
        # Each row is valid, but the sequence and cached balance are corrupt.
        entries = [LedgerEntry.objects.create(
            wallet=self.wallet, entry_type="credit", amount=maximum,
            balance_before=Decimal("0"), balance_after=maximum,
            idempotency_key=uuid4(),
        ) for _ in range(3)]
        result = reconcile_wallet(user_id=self.user.pk)
        self.assertFalse(result.is_consistent)
        self.assertEqual(result.ledger_balance, Decimal("2999999999999.99999997"))
        self.assertEqual(result.first_invalid_entry_id, entries[1].pk)

    def test_other_wallet_entries_and_corruption_are_excluded(self):
        self._post("10")
        other = User.objects.create_user(username="reconciliation-bob")
        other_wallet = Wallet.objects.create(user=other)
        self._post("20", user_id=other.pk)
        Wallet.objects.filter(pk=other_wallet.pk).update(balance=Decimal("21"))
        self._assert_consistent(reconcile_wallet(user_id=self.user.pk), "10", 1)
        self.assertFalse(reconcile_wallet(user_id=other.pk).is_consistent)

    def test_successful_reconciliation_does_not_change_data_or_timestamps(self):
        self._post()
        before = self._snapshot()
        self._assert_consistent(reconcile_wallet(user_id=self.user.pk), "10", 1)
        self.assertEqual(self._snapshot(), before)

    def test_missing_user_and_missing_wallet_are_distinguished_without_creation(self):
        without_wallet = User.objects.create_user(username="reconciliation-no-wallet")
        for user_id, code in (
            (without_wallet.pk, "wallet_not_found"),
            (without_wallet.pk + 1, "user_not_found"),
        ):
            with self.subTest(code=code):
                before = self._snapshot()
                with self.assertRaises(WalletError) as raised:
                    reconcile_wallet(user_id=user_id)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self._snapshot(), before)

    def test_invalid_user_ids_are_rejected(self):
        for user_id in (None, True, False, "1", 1.0, 0, -1):
            with self.subTest(user_id=user_id):
                with self.assertRaises(WalletError) as raised:
                    reconcile_wallet(user_id=user_id)
                self.assertEqual(raised.exception.code, "invalid_request")


class ReconciliationCommandTests(_ReconciliationHelpers, TestCase):
    def test_command_reports_success_as_json(self):
        self._post("10.12345678")
        stdout, stderr = StringIO(), StringIO()
        before = self._snapshot()
        call_command("reconcile_wallet", "--user-id", str(self.user.pk),
                     stdout=stdout, stderr=stderr)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["is_consistent"])
        self.assertTrue(report["balance_matches"])
        self.assertTrue(report["chain_valid"])
        self.assertIsNone(report["first_invalid_entry_id"])
        self.assertEqual(report["stored_balance"], "10.12345678")
        self.assertEqual(report["ledger_balance"], "10.12345678")
        self.assertEqual(report["wallet_id"], self.wallet.pk)
        self.assertEqual(report["user_id"], self.user.pk)
        self.assertEqual(report["entry_count"], 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self._snapshot(), before)

    def test_command_reports_balance_mismatch_and_requests_exit_one(self):
        self._post()
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("11"))
        before = self._snapshot()
        stdout = StringIO()
        with self.assertRaises(CommandError) as raised:
            call_command("reconcile_wallet", user_id=self.user.pk, stdout=stdout)
        self.assertEqual(raised.exception.returncode, 1)
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["is_consistent"])
        self.assertFalse(report["balance_matches"])
        self.assertTrue(report["chain_valid"])
        self.assertEqual(report["stored_balance"], "11.00000000")
        self.assertEqual(report["ledger_balance"], "10.00000000")
        self.assertEqual(self._snapshot(), before)

    def test_command_fails_on_chain_corruption_even_with_matching_balance(self):
        entry = self._post()
        LedgerEntry.objects.filter(pk=entry.pk).update(
            balance_before=Decimal("1"), balance_after=Decimal("11")
        )
        stdout = StringIO()
        with self.assertRaises(CommandError):
            call_command("reconcile_wallet", user_id=self.user.pk, stdout=stdout)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["balance_matches"])
        self.assertFalse(report["chain_valid"])
        self.assertFalse(report["is_consistent"])
        self.assertEqual(report["first_invalid_entry_id"], entry.pk)

    def test_command_converts_missing_wallet_to_command_error(self):
        user = User.objects.create_user(username="command-no-wallet")
        stdout = StringIO()
        with self.assertRaisesMessage(CommandError, "wallet_not_found"):
            call_command("reconcile_wallet", user_id=user.pk, stdout=stdout)
        self.assertEqual(stdout.getvalue(), "")

    def test_command_requires_a_valid_user_id_argument(self):
        for arguments in ((), ("--user-id", "abc"), ("--user-id", "0")):
            with self.subTest(arguments=arguments):
                with self.assertRaises(CommandError):
                    call_command("reconcile_wallet", *arguments, stdout=StringIO())


class ReconciliationConcurrencyTests(_ReconciliationHelpers, TransactionTestCase):
    def setUp(self):
        super().setUp()
        if connection.vendor != "postgresql":
            raise RuntimeError("Reconciliation concurrency tests require PostgreSQL.")
        self._post("40")

    def _worker(self, operation, pid_queue):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SET statement_timeout = '12s'")
                cursor.execute("SELECT pg_backend_pid()")
                pid_queue.put(cursor.fetchone()[0])
            return operation()
        finally:
            connections.close_all()

    def _wait_for_wallet_lock(self, future, worker_pid):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if future.done():
                self.fail(f"Worker completed before the wallet lock was released: {future.result()}")
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [worker_pid]
                )
                if cursor.fetchone()[0]:
                    return
            sleep(0.01)
        self.fail("Worker did not wait on the wallet lock")

    def _audit_during_posting(self, *, commit):
        pid_queue = Queue()
        futures = []
        original_save = LedgerEntry.save

        with ThreadPoolExecutor(max_workers=1) as executor:
            def paused_insert(entry, *args, **kwargs):
                # Actual posting has changed the balance but has not inserted
                # its ledger row yet. Reconciliation must not inspect this gap.
                self.assertEqual(Wallet.objects.get(pk=self.wallet.pk).balance, Decimal("50"))
                self.assertEqual(self.wallet.entries.count(), 1)
                future = executor.submit(
                    self._worker, lambda: reconcile_wallet(user_id=self.user.pk), pid_queue
                )
                futures.append(future)
                self._wait_for_wallet_lock(future, pid_queue.get(timeout=5))
                if not commit:
                    raise RuntimeError("Injected posting rollback")
                return original_save(entry, *args, **kwargs)

            with patch.object(LedgerEntry, "save", autospec=True, side_effect=paused_insert):
                if commit:
                    self._post("10")
                else:
                    with self.assertRaisesMessage(RuntimeError, "Injected posting rollback"):
                        self._post("10")
            result = futures[0].result(timeout=20)

        self._assert_consistent(result, "50" if commit else "40", 2 if commit else 1)

    def test_reconciliation_waits_for_posting_commit_and_reads_the_new_state(self):
        self.assertFalse(connection.in_atomic_block)
        self._audit_during_posting(commit=True)

    def test_reconciliation_waits_for_posting_rollback_and_reads_the_original_state(self):
        self._audit_during_posting(commit=False)

    def test_posting_waits_for_reconciliation_outer_transaction_to_finish(self):
        pid_queue = Queue()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                report = reconcile_wallet(user_id=self.user.pk)
                self._assert_consistent(report, "40", 1)
                future = executor.submit(self._worker, lambda: self._post("10"), pid_queue)
                self._wait_for_wallet_lock(future, pid_queue.get(timeout=5))
                self.assertEqual(Wallet.objects.get(pk=self.wallet.pk).balance, Decimal("40"))
            future.result(timeout=20)
        self._assert_consistent(reconcile_wallet(user_id=self.user.pk), "50", 2)

    def test_reconciliation_does_not_block_posting_to_another_wallet(self):
        other = User.objects.create_user(username="reconciliation-parallel")
        Wallet.objects.create(user=other)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                self._assert_consistent(reconcile_wallet(user_id=self.user.pk), "40", 1)
                future = executor.submit(
                    self._worker, lambda: self._post("10", user_id=other.pk), Queue()
                )
                entry = future.result(timeout=20)
                self.assertEqual(entry.wallet.user_id, other.pk)
        self.assertTrue(reconcile_wallet(user_id=other.pk).is_consistent)
