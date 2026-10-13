import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User


class Command(BaseCommand):
    help = "Create an owner account from environment variables, once."

    def handle(self, *args: object, **options: object) -> None:
        email = os.environ.get("COTINGA_OWNER_EMAIL", "").strip().lower()
        password = os.environ.get("COTINGA_OWNER_PASSWORD", "")
        if not email and not password:
            return
        if not email or not password:
            raise CommandError(
                "Set both COTINGA_OWNER_EMAIL and COTINGA_OWNER_PASSWORD"
            )
        if User.objects.filter(email__iexact=email).exists():
            self.stdout.write("Owner account already exists; left unchanged.")
            return
        user = User(email=email)
        try:
            user.full_clean(exclude=["password"])
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        User.objects.create_user(email=email, password=password)
        self.stdout.write(self.style.SUCCESS("Owner account created."))
