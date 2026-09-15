
from dataclasses import dataclass
from decimal import Decimal, localcontext
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from wallet.exceptions import WalletError
from wallet.models import MAX_BALANCE, LedgerEntry, Wallet


@dataclass(frozen=True)
class PostingResult:
    entry: LedgerEntry
    created: bool


def _validate_request(
    *, user_id: int, entry_type: str, amount: Decimal, idempotency_key: UUID
) -> None:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise WalletError("invalid_request", "user_id must be a positive integer.")

    if not isinstance(entry_type, str) or entry_type not in LedgerEntry.EntryType.values:
        raise WalletError("invalid_request", "entry_type must be credit or debit.")

    # is_finite() must precede comparisons: even comparing sNaN can raise.
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise WalletError("invalid_amount", "amount must be a finite Decimal.")

    if amount.as_tuple().exponent < -settings.LEDGER_DECIMAL_PLACES:
        raise WalletError("invalid_amount", "amount must have at most 8 decimal places.")

    if amount <= 0 or amount > MAX_BALANCE:
        raise WalletError("invalid_amount", "amount is outside the supported range.")

    if not isinstance(idempotency_key, UUID):
        raise WalletError("invalid_idempotency_key", "idempotency_key must be a UUID.")


def post_entry(
    *, user_id: int, entry_type: str, amount: Decimal, idempotency_key: UUID
) -> PostingResult:
    """Post once or replay an existing entry with the same key and payload.

    The API validates the original input and quantizes valid amounts to eight
    places before calling. Internal callers may supply fewer decimal places;
    this service rejects excess places and never rounds an invalid amount.

    Domain rejections raise WalletError. Unexpected persistence failures
    propagate after rollback. When called inside an outer atomic block, success
    remains subject to that caller's eventual commit or rollback.
    """
    _validate_request(
        user_id=user_id,
        entry_type=entry_type,
        amount=amount,
        idempotency_key=idempotency_key,
    )

    with transaction.atomic():
        try:
            # Even a new key has a stable wallet row to lock. Read the balance
            # only after acquiring that lock, never from an earlier snapshot.
            wallet = Wallet.objects.select_for_update().get(user_id=user_id)
        except Wallet.DoesNotExist:
            if not get_user_model().objects.filter(pk=user_id).exists():
                raise WalletError("user_not_found", "User does not exist.") from None
            raise WalletError("wallet_not_found", "User has no wallet.") from None

        existing = wallet.entries.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.entry_type != entry_type or existing.amount != amount:
                raise WalletError(
                    "idempotency_conflict",
                    "This key was already used with a different type or amount.",
                )
            # Replay the historical entry without checking today's funds or
            # saving the wallet (which would change updated_at).
            return PostingResult(entry=existing, created=False)

        balance_before = wallet.balance
        # One extra digit permits an exact sum above the storage ceiling, so
        # it can be rejected explicitly rather than rounded or sent to the DB.
        with localcontext() as decimal_context:
            decimal_context.prec = settings.LEDGER_DECIMAL_MAX_DIGITS + 1
            if entry_type == LedgerEntry.EntryType.DEBIT:
                if amount > balance_before:
                    raise WalletError("insufficient_funds", "Wallet balance is insufficient.")
                balance_after = balance_before - amount
            else:
                balance_after = balance_before + amount
                if balance_after > MAX_BALANCE:
                    raise WalletError(
                        "balance_limit_exceeded", "Credit would exceed the balance limit."
                    )

        wallet.balance = balance_after
        wallet.save(update_fields=["balance", "updated_at"])
        entry = LedgerEntry.objects.create(
            wallet=wallet,
            entry_type=entry_type,
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            idempotency_key=idempotency_key,
        )
        return PostingResult(entry=entry, created=True)
