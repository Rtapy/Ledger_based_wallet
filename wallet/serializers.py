import re
from decimal import Decimal, localcontext

from django.conf import settings
from rest_framework import serializers

from wallet.models import MAX_BALANCE, LedgerEntry, Wallet


class AmountField(serializers.Field):
    """Validate the original string before normalizing its decimal scale."""

    default_error_messages = {
        "invalid": "Use a positive decimal string with at most 8 fractional digits.",
    }

    def to_internal_value(self, data):
        places = settings.LEDGER_DECIMAL_PLACES
        if not isinstance(data, str) or not re.fullmatch(
            rf"[0-9]+(?:\.[0-9]{{1,{places}}})?", data
        ):
            self.fail("invalid")

        integer, _, fraction = data.partition(".")
        integer = integer.lstrip("0") or "0"
        if len(integer) > settings.LEDGER_DECIMAL_MAX_DIGITS - places:
            self.fail("invalid")
        amount = Decimal(integer + ("." + fraction if fraction else ""))
        if amount <= 0 or amount > MAX_BALANCE:
            self.fail("invalid")

        with localcontext() as context:
            context.prec = settings.LEDGER_DECIMAL_MAX_DIGITS
            return amount.quantize(Decimal("1").scaleb(-places))


class PostingSerializer(serializers.Serializer):
    amount = AmountField()

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) != {"amount"}:
            raise serializers.ValidationError(
                {"non_field_errors": ["Body must be an object containing only amount."]},
                code="invalid_request",
            )
        return super().to_internal_value(data)


class IdempotencyKeySerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField()


class MoneyOutputField(serializers.DecimalField):
    def __init__(self):
        super().__init__(
            max_digits=settings.LEDGER_DECIMAL_MAX_DIGITS,
            decimal_places=settings.LEDGER_DECIMAL_PLACES,
            coerce_to_string=True,
            read_only=True,
        )


class WalletSerializer(serializers.ModelSerializer):
    balance = MoneyOutputField()

    class Meta:
        model = Wallet
        fields = ("id", "user_id", "balance", "updated_at")
        read_only_fields = fields


class LedgerEntrySerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="entry_type", read_only=True)
    amount = MoneyOutputField()
    balance_before = MoneyOutputField()
    balance_after = MoneyOutputField()

    class Meta:
        model = LedgerEntry
        fields = (
            "id", "wallet_id", "type", "amount", "balance_before",
            "balance_after", "idempotency_key", "created_at",
        )
        read_only_fields = fields
