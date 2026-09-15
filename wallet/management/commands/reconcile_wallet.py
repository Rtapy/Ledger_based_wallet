import json

from django.core.management.base import BaseCommand, CommandError

from wallet.exceptions import WalletError
from wallet.reconciliation import reconcile_wallet


class Command(BaseCommand):
    help = "Check a wallet balance and ledger chain without modifying financial data."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            result = reconcile_wallet(user_id=options["user_id"])
        except WalletError as exc:
            raise CommandError(f"{exc.code}: {exc.message}") from exc

        self.stdout.write(json.dumps({
            "wallet_id": result.wallet_id,
            "user_id": result.user_id,
            "stored_balance": format(result.stored_balance, ".8f"),
            "ledger_balance": format(result.ledger_balance, ".8f"),
            "entry_count": result.entry_count,
            "balance_matches": result.balance_matches,
            "chain_valid": result.chain_valid,
            "first_invalid_entry_id": result.first_invalid_entry_id,
            "is_consistent": result.is_consistent,
        }, sort_keys=True))

        if not result.is_consistent:
            raise CommandError("wallet_mismatch: balance or ledger chain is inconsistent.")
