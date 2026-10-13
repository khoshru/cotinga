from typing import Any

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower


class UserManager(BaseUserManager["User"]):
    """Manager where email is the unique identifier."""

    use_in_migrations = True

    def _create_user(
        self,
        email: str,
        password: str | None,
        **extra_fields: Any,
    ) -> User:
        """Create and save a user with the given email and hashed password."""
        if not email:
            raise ValueError("An email address must be provided")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.password = make_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> User:
        """Create a regular user with staff/superuser flags unset."""
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> User:
        """Create a superuser, enforcing both staff and superuser flags."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)

    def get_by_natural_key(self, email: str | None) -> User:
        """Allow case-insensitive lookup of users by email."""
        return self.get(**{f"{self.model.USERNAME_FIELD}__iexact": email})


class LowercaseEmailFields(models.EmailField):  # type: ignore[type-arg]
    """EmailField that normalizes stored values to lowercase."""

    def get_prep_value(self, value: str | None) -> str | None:
        """Lowercase the value before it reaches the database."""
        value = super().get_prep_value(value)
        if value is not None:
            return value.lower()
        return value

    def clean(
        self,
        value: str | None,
        model_instance: models.Model | None,
    ) -> str | None:
        """Lowercase the value before running the parent validation."""
        value = super().clean(value, model_instance)
        return value.lower() if value else value


class User(AbstractUser):
    """User model that uses email as the unique login identifier."""

    username = None  # type: ignore[assignment]
    email = LowercaseEmailFields("email address", unique=True)
    first_name = models.CharField("first name", max_length=150, blank=True)
    last_name = models.CharField("last name", max_length=150, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()  # type: ignore[assignment,misc]

    class Meta(AbstractUser.Meta):
        """Model metadata with a database-level lowercase email guard."""

        constraints = [
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")),
                name="%(app_label)s_%(class)s_email_lowercase",
            ),
        ]

    def __str__(self) -> str:
        """Return the email address as the user's representation."""
        return str(self.email)
