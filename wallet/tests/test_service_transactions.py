"""PostgreSQL transaction and concurrency contracts for the posting service.

Fault injection uses the normal LedgerEntry.save() persistence boundary.
The write order is: lock wallet, update balance, insert ledger entry.
Unexpected persistence failures must propagate after rollback; HTTP translation
belongs to the API.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TransactionTestCase

from wallet.exceptions import WalletError
from wallet.models import LedgerEntry, Wallet
from wallet.services import post_entry


User = get_user_model()


class InjectedPersistenceFailure(RuntimeError):
    pass


class _PostingDatabaseHelpers:
    def setUp(self):
        super().setUp()
        if connection.vendor != "postgresql":
            raise RuntimeError("These wallet transaction tests require PostgreSQL.")
        self.user = User.objects.create_user(username="transaction-alice")
        self.wallet = Wallet.objects.create(user=self.user)

    def _post(self, **overrides):
        arguments = {
            "user_id": self.user.pk,
            "entry_type": "credit",
            "amount": Decimal("10"),
            "idempotency_key": uuid4(),
        }
        arguments.update(overrides)
        return post_entry(**arguments)

    def _snapshot(self):
        return (
            list(Wallet.objects.order_by("pk").values()),
            list(LedgerEntry.objects.order_by("pk").values()),
        )

    def _assert_balanced(self, expected_balance, expected_count):
        self.wallet.refresh_from_db()
        entries = list(self.wallet.entries.order_by("id"))
        self.assertEqual(len(entries), expected_count)
        running = Decimal("0")
        for entry in entries:
            self.assertEqual(entry.balance_before, running)
            self.assertGreater(entry.amount, 0)
            self.assertIn(entry.entry_type, ("credit", "debit"))
            running += entry.amount if entry.entry_type == "credit" else -entry.amount
            self.assertGreaterEqual(running, 0)
            self.assertEqual(entry.balance_after, running)
        self.assertEqual(running, expected_balance)
        self.assertEqual(self.wallet.balance, expected_balance)


class PostingTransactionTests(_PostingDatabaseHelpers, TransactionTestCase):
    def test_service_opens_its_own_transaction_from_autocommit(self):
        self.assertTrue(connection.get_autocommit())
        self.assertFalse(connection.in_atomic_block)

        result = self._post()

        self.assertIs(result.created, True)
        self.assertFalse(connection.in_atomic_block)
        self._assert_balanced(Decimal("10"), 1)

    def _exercise_injected_failure(self, *, after_insert):
        self._post(amount=Decimal("40"))
        original_save = LedgerEntry.save

        for entry_type in ("credit", "debit"):
            with self.subTest(entry_type=entry_type, after_insert=after_insert):
                self.wallet.refresh_from_db()
                old_balance = self.wallet.balance
                expected_pending = old_balance + (
                    Decimal("10") if entry_type == "credit" else Decimal("-10")
                )
                before = self._snapshot()
                key = uuid4()

                def fail_at_persistence(entry, *args, **kwargs):
                    self.assertTrue(connection.in_atomic_block)
                    self.assertEqual(
                        Wallet.objects.get(pk=self.wallet.pk).balance,
                        expected_pending,
                    )
                    if after_insert:
                        original_save(entry, *args, **kwargs)
                        self.assertTrue(LedgerEntry.objects.filter(pk=entry.pk).exists())
                    raise InjectedPersistenceFailure("Injected at ledger persistence")

                with patch.object(LedgerEntry, "save", autospec=True) as mocked_save:
                    mocked_save.side_effect = fail_at_persistence
                    with self.assertRaises(InjectedPersistenceFailure):
                        self._post(entry_type=entry_type, idempotency_key=key)
                    mocked_save.assert_called_once()

                self.assertFalse(connection.in_atomic_block)
                self.assertEqual(self._snapshot(), before)

                # A rolled-back write must not reserve the idempotency key.
                successful = self._post(entry_type=entry_type, idempotency_key=key)
                after_success = self._snapshot()
                replay = self._post(entry_type=entry_type, idempotency_key=key)
                self.assertIs(successful.created, True)
                self.assertIs(replay.created, False)
                self.assertEqual(replay.entry.pk, successful.entry.pk)
                self.assertEqual(self._snapshot(), after_success)

        self._assert_balanced(Decimal("40"), 3)

    def test_failure_after_balance_update_rolls_back_credit_and_debit(self):
        self._exercise_injected_failure(after_insert=False)

    def test_failure_after_ledger_insert_rolls_back_credit_and_debit(self):
        self._exercise_injected_failure(after_insert=True)

    def test_real_database_error_rolls_back_balance_and_preserves_connection(self):
        self._post(amount=Decimal("40"))
        before = self._snapshot()
        original_save = LedgerEntry.save
        key = uuid4()

        def insert_invalid_entry(entry, *args, **kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(Wallet.objects.get(pk=self.wallet.pk).balance, Decimal("50"))
            # Violate only amount positivity: before == after keeps arithmetic valid.
            entry.amount = Decimal("0")
            entry.balance_after = entry.balance_before
            return original_save(entry, *args, **kwargs)

        with patch.object(LedgerEntry, "save", autospec=True) as mocked_save:
            mocked_save.side_effect = insert_invalid_entry
            with self.assertRaises(IntegrityError) as raised:
                self._post(idempotency_key=key)
            mocked_save.assert_called_once()

        self.assertEqual(
            raised.exception.__cause__.diag.constraint_name, "ledger_amount_range"
        )
        self.assertEqual(self._snapshot(), before)
        result = self._post(idempotency_key=key)
        self.assertIs(result.created, True)
        self._assert_balanced(Decimal("50"), 2)

    def test_outer_transaction_rollback_undoes_a_successful_service_call(self):
        before = self._snapshot()
        key = uuid4()

        with self.assertRaises(InjectedPersistenceFailure):
            with transaction.atomic():
                result = self._post(idempotency_key=key)
                self.assertIs(result.created, True)
                self._assert_balanced(Decimal("10"), 1)
                raise InjectedPersistenceFailure("Caller rolled back its transaction")

        self.assertEqual(self._snapshot(), before)
        retry = self._post(idempotency_key=key)
        self.assertIs(retry.created, True)
        self._assert_balanced(Decimal("10"), 1)

    def test_domain_rejection_does_not_break_the_callers_transaction(self):
        before = self._snapshot()

        with transaction.atomic():
            with self.assertRaises(WalletError) as raised:
                self._post(entry_type="debit")
            self.assertEqual(raised.exception.code, "insufficient_funds")
            self.assertEqual(self._snapshot(), before)
            successful = self._post()
            self.assertIs(successful.created, True)

        self._assert_balanced(Decimal("10"), 1)


class PostingConcurrencyTests(_PostingDatabaseHelpers, TransactionTestCase):
    def _worker(self, arguments, *, barrier=None, pid_queue=None):
        # Each thread owns and closes its own Django database connection.
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                # Bound blocked SQL so a failed test can release its workers.
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SET statement_timeout = '12s'")
                if pid_queue is not None:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid_queue.put(cursor.fetchone()[0])
            if barrier is not None:
                barrier.wait(timeout=5)
            try:
                result = self._post(**arguments)
            except WalletError as exc:
                return {"error": exc.code}
            return {"entry_id": result.entry.pk, "created": result.created}
        finally:
            connections.close_all()

    def _concurrent(self, *requests):
        barrier = Barrier(len(requests))
        with ThreadPoolExecutor(max_workers=len(requests)) as executor:
            futures = [
                executor.submit(self._worker, arguments, barrier=barrier)
                for arguments in requests
            ]
            # Unexpected exceptions propagate and fail the test.
            return [future.result(timeout=20) for future in futures]

    def _assert_one_success_one_rejection(self, outcomes, error_code):
        successes = [outcome for outcome in outcomes if "entry_id" in outcome]
        errors = [outcome for outcome in outcomes if "error" in outcome]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertIs(successes[0]["created"], True)
        self.assertEqual(errors, [{"error": error_code}], outcomes)
        return successes[0]

    def test_concurrent_distinct_credits_do_not_lose_an_update(self):
        outcomes = self._concurrent(
            {"amount": Decimal("10"), "idempotency_key": uuid4()},
            {"amount": Decimal("20"), "idempotency_key": uuid4()},
        )
        self.assertTrue(all(outcome.get("created") is True for outcome in outcomes), outcomes)
        self.assertEqual(len({outcome["entry_id"] for outcome in outcomes}), 2)
        self._assert_balanced(Decimal("30"), 2)

    def test_two_withdrawals_of_eighty_from_one_hundred_allow_only_one(self):
        self._post(amount=Decimal("100"))
        outcomes = self._concurrent(
            {"entry_type": "debit", "amount": Decimal("80"), "idempotency_key": uuid4()},
            {"entry_type": "debit", "amount": Decimal("80"), "idempotency_key": uuid4()},
        )
        self._assert_one_success_one_rejection(outcomes, "insufficient_funds")
        self._assert_balanced(Decimal("20"), 2)  # Initial credit plus one debit.

    def test_concurrent_same_key_credits_create_one_entry_and_one_replay(self):
        request = {"idempotency_key": uuid4()}
        outcomes = self._concurrent(request, request.copy())
        self.assertTrue(all("entry_id" in outcome for outcome in outcomes), outcomes)
        self.assertEqual(sorted(outcome["created"] for outcome in outcomes), [False, True])
        self.assertEqual(outcomes[0]["entry_id"], outcomes[1]["entry_id"])
        self._assert_balanced(Decimal("10"), 1)

    def test_concurrent_same_key_debits_replay_instead_of_rechecking_funds(self):
        self._post(amount=Decimal("100"))
        request = {"entry_type": "debit", "amount": Decimal("80"), "idempotency_key": uuid4()}
        outcomes = self._concurrent(request, request.copy())
        self.assertTrue(all("entry_id" in outcome for outcome in outcomes), outcomes)
        self.assertEqual(sorted(outcome["created"] for outcome in outcomes), [False, True])
        self.assertEqual(outcomes[0]["entry_id"], outcomes[1]["entry_id"])
        self._assert_balanced(Decimal("20"), 2)

    def test_concurrent_same_key_with_different_amounts_has_one_conflict(self):
        key = uuid4()
        outcomes = self._concurrent(
            {"amount": Decimal("10"), "idempotency_key": key},
            {"amount": Decimal("20"), "idempotency_key": key},
        )
        winner = self._assert_one_success_one_rejection(outcomes, "idempotency_conflict")
        entry = LedgerEntry.objects.get(pk=winner["entry_id"])
        self.assertIn(entry.amount, (Decimal("10"), Decimal("20")))
        self._assert_balanced(entry.amount, 1)

    def test_concurrent_same_key_with_different_types_has_one_conflict(self):
        self._post(amount=Decimal("100"))
        key = uuid4()
        outcomes = self._concurrent(
            {"entry_type": "credit", "idempotency_key": key},
            {"entry_type": "debit", "idempotency_key": key},
        )
        winner = self._assert_one_success_one_rejection(outcomes, "idempotency_conflict")
        entry = LedgerEntry.objects.get(pk=winner["entry_id"])
        expected = Decimal("110") if entry.entry_type == "credit" else Decimal("90")
        self._assert_balanced(expected, 2)

    def test_concurrent_credits_cannot_exceed_the_balance_ceiling(self):
        self._post(amount=Decimal("999999999999.99999998"))
        outcomes = self._concurrent(
            {"amount": Decimal("0.00000001"), "idempotency_key": uuid4()},
            {"amount": Decimal("0.00000001"), "idempotency_key": uuid4()},
        )
        self._assert_one_success_one_rejection(outcomes, "balance_limit_exceeded")
        self._assert_balanced(Decimal("999999999999.99999999"), 2)

    def _wait_until_blocked_by(self, future, worker_pid, owner_pid):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if future.done():
                outcome = future.result()
                self.fail(f"Posting completed before the wallet lock was released: {outcome}")
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT %s = ANY(pg_blocking_pids(%s))", [owner_pid, worker_pid]
                )
                if cursor.fetchone()[0]:
                    return
            # Poll actual PostgreSQL lock state, rather than assume a sleep made a race.
            sleep(0.01)
        self.fail("The posting connection did not wait for the held wallet lock")

    def test_waiting_debit_uses_the_balance_committed_by_the_lock_owner(self):
        self._post(amount=Decimal("100"))
        pid_queue = Queue()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                Wallet.objects.select_for_update().get(pk=self.wallet.pk)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    owner_pid = cursor.fetchone()[0]
                future = executor.submit(
                    self._worker,
                    {"entry_type": "debit", "amount": Decimal("80"), "idempotency_key": uuid4()},
                    pid_queue=pid_queue,
                )
                worker_pid = pid_queue.get(timeout=5)
                self._wait_until_blocked_by(future, worker_pid, owner_pid)
                # Commit 150 while the other transaction waits; it must then use 150.
                self._post(amount=Decimal("50"))

            outcome = future.result(timeout=20)

        self.assertIs(outcome.get("created"), True, outcome)
        self._assert_balanced(Decimal("70"), 3)
