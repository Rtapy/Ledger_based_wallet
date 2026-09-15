from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from wallet.models import Wallet


class Command(BaseCommand):
    help = "Create a demo user with an unusable password and a zero-balance wallet; print user ID."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)

    def handle(self, *args, **options):
        User = get_user_model()
        username = User.normalize_username(options["username"])
        try:
            with transaction.atomic():
                user = User(username=username)
                user.set_unusable_password()
                user.full_clean(validate_unique=False)
                if User.objects.filter(username=username).exists():
                    raise CommandError("user_exists: Choose a new demo username.")
                user.save()
                Wallet.objects.create(user=user)
        except ValidationError as exc:
            raise CommandError(f"invalid_username: {'; '.join(exc.messages)}") from exc
        except IntegrityError as exc:
            # A concurrent invocation may have created the same username.
            # Inspect only after the failed atomic block has rolled back.
            if User.objects.filter(username=username).exists():
                raise CommandError("user_exists: Choose a new demo username.") from exc
            raise

        self.stdout.write(str(user.pk))
