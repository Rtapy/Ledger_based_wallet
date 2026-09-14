from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q


MAX_BALANCE = Decimal("999999999999.99999999")


class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet",
    )
    balance = models.DecimalField(
        max_digits=settings.LEDGER_DECIMAL_MAX_DIGITS,
        decimal_places=settings.LEDGER_DECIMAL_PLACES,
        default=Decimal("0"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(balance__gte=0, balance__lte=MAX_BALANCE),
                name="wallet_balance_range",
            ),
        ]


class LedgerEntry(models.Model):

    class EntryType(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="entries",
    )
    entry_type = models.CharField(max_length=6, choices=EntryType.choices)
    amount = models.DecimalField(
        max_digits=settings.LEDGER_DECIMAL_MAX_DIGITS,
        decimal_places=settings.LEDGER_DECIMAL_PLACES,
    )
    balance_before = models.DecimalField(
        max_digits=settings.LEDGER_DECIMAL_MAX_DIGITS,
        decimal_places=settings.LEDGER_DECIMAL_PLACES,
    )
    balance_after = models.DecimalField(
        max_digits=settings.LEDGER_DECIMAL_MAX_DIGITS,
        decimal_places=settings.LEDGER_DECIMAL_PLACES,
    )
    # Supplied by the caller, not generated on the server for each retry.
    idempotency_key = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["wallet", "id"], name="ledger_wallet_id_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0, amount__lte=MAX_BALANCE),
                name="ledger_amount_range",
            ),
            models.CheckConstraint(
                condition=Q(balance_before__gte=0, balance_before__lte=MAX_BALANCE),
                name="ledger_before_range",
            ),
            models.CheckConstraint(
                condition=Q(balance_after__gte=0, balance_after__lte=MAX_BALANCE),
                name="ledger_after_range",
            ),
            models.CheckConstraint(
                condition=Q(entry_type__in=["credit", "debit"]),
                name="ledger_known_type",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(entry_type="credit")
                    | Q(balance_after=F("balance_before") + F("amount"))
                ),
                name="ledger_credit_arithmetic",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(entry_type="debit")
                    | Q(balance_after=F("balance_before") - F("amount"))
                ),
                name="ledger_debit_arithmetic",
            ),
            models.UniqueConstraint(
                fields=["wallet", "idempotency_key"],
                name="ledger_wallet_key_unique",
            ),
        ]
