from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Apply migrations and provision the owner before the single app instance starts."

    def handle(self, *args: object, **options: object) -> None:
        # The EC2 deployment script serializes releases and stops the old
        # container first. Gunicorn starts only after this command completes.
        call_command("migrate", interactive=False)
        call_command("bootstrap_owner")
