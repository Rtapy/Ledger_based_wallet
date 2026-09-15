"""Read-only reconciliation using the same wallet lock as posting."""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, Count, DecimalField, F, Sum, When

from wallet.exceptions import WalletError
from wallet.models import MAX_BALANCE, LedgerEntry, Wallet


@dataclass(frozen=True)
class ReconciliationResult:
    wallet_id: int
    user_id: int
    stored_balance: Decimal
    ledger_balance: Decimal
    entry_count: int
    first_invalid_entry_id: int | None

    @property
    def balance_matches(self) -> bool:
        return self.stored_balance == self.ledger_balance

    @property
    def chain_valid(self) -> bool:
        return self.first_invalid_entry_id is None

    @property
    def is_consistent(self) -> bool:
        return self.balance_matches and self.chain_valid


def _first_invalid_entry(entries) -> int | None:
    expected_before = Decimal("0")
    # Row arithmetic can need one extra digit. Never accumulate a lifetime's
    # credits in the process-wide Decimal context; SQL computes the net sum.
    with localcontext() as context:
        context.prec = settings.LEDGER_DECIMAL_MAX_DIGITS + 1
        for entry_id, entry_type, amount, before, after in entries.values_list(
            "id", "entry_type", "amount", "balance_before", "balance_after"
        ).iterator(chunk_size=1000):
            if (
                not all(value.is_finite() for value in (amount, before, after))
                or not 0 < amount <= MAX_BALANCE
                or not 0 <= before <= MAX_BALANCE
                or not 0 <= after <= MAX_BALANCE
                or before != expected_before
            ):
                return entry_id
            if entry_type == LedgerEntry.EntryType.CREDIT:
                expected_after = before + amount
            elif entry_type == LedgerEntry.EntryType.DEBIT:
                expected_after = before - amount
            else:
                return entry_id
            if after != expected_after:
                return entry_id
            expected_before = after
    return None


def reconcile_wallet(*, user_id: int) -> ReconciliationResult:
    """Compare the stored balance with signed ledger amounts and audit the chain.

    Mismatches are returned as data, never repaired. Missing users/wallets raise
    WalletError. Consistency assumes PostgreSQL READ COMMITTED and all writers
    acquiring the wallet lock, as post_entry does. In an outer atomic block,
    the lock remains held until that outer block ends.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise WalletError("invalid_request", "user_id must be a positive integer.")

    with transaction.atomic():
        try:
            wallet = Wallet.objects.select_for_update().get(user_id=user_id)
        except Wallet.DoesNotExist:
            if not get_user_model().objects.filter(pk=user_id).exists():
                raise WalletError("user_not_found", "User does not exist.") from None
            raise WalletError("wallet_not_found", "User has no wallet.") from None

        entries = wallet.entries.order_by("id")
        signed_amount = Case(
            When(entry_type=LedgerEntry.EntryType.CREDIT, then=F("amount")),
            When(entry_type=LedgerEntry.EntryType.DEBIT, then=-F("amount")),
            output_field=DecimalField(
                max_digits=settings.LEDGER_DECIMAL_MAX_DIGITS,
                decimal_places=settings.LEDGER_DECIMAL_PLACES,
            ),
        )
        totals = entries.aggregate(
            ledger_balance=Sum(signed_amount, default=Decimal("0")),
            entry_count=Count("pk"),
        )
        return ReconciliationResult(
            wallet_id=wallet.pk,
            user_id=wallet.user_id,
            stored_balance=wallet.balance,
            ledger_balance=totals["ledger_balance"],
            entry_count=totals["entry_count"],
            first_invalid_entry_id=_first_invalid_entry(entries),
        )
