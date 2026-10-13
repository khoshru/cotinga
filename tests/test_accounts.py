import pytest
from django.contrib.auth import authenticate, get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import Client

User = get_user_model()


@pytest.mark.django_db
class TestUserManager:
    def test_create_user_requires_email(self) -> None:
        """Verify empty email raises ValueError."""
        with pytest.raises(ValueError, match="email"):
            User.objects.create_user(email="", password="irrelevant")

    def test_create_user_hashes_password(self) -> None:
        """Verify passwords are stored hashed, never in plaintext."""
        user = User.objects.create_user("userexample@gmail.com", "q3de-2")
        assert user.check_password("q3de-2")
        assert not user.password.startswith("q3de-2")
        assert "$" in user.password

    def test_create_user_defaults(self) -> None:
        """Verify regular users default to non-staff, non-superuser, active."""
        user = User.objects.create_user("userexample@gmail.com", "q3de-2")
        assert user.is_staff is False
        assert user.is_superuser is False
        assert user.is_active is True

    def test_email_is_normalized(self) -> None:
        """Verify emails are stored fully lowercased."""
        user = User.objects.create_user("UsereXample@Gmail.com", "q3de-2")
        assert user.email == "userexample@gmail.com"

    def test_create_superuser_flag(self) -> None:
        """Verify superusers get both staff and superuser flags."""
        user = User.objects.create_superuser(
            email="admin@example.com", password="s3cret-pw!"
        )
        assert user.is_staff is True
        assert user.is_superuser is True

    @pytest.mark.parametrize("kwargs", [{"is_staff": False}, {"is_superuser": False}])
    def test_create_superuser_rejects_false_flags(
        self, kwargs: dict[str, bool]
    ) -> None:
        """Verify create_superuser rejects False staff/superuser flags."""
        with pytest.raises(ValueError, match="Superuser"):
            User.objects.create_superuser(
                email="admin@example.com", password="s3cret-pw!", **kwargs
            )


@pytest.mark.django_db
class TestEmailUniqueness:
    def test_duplicate_email_rejected(self) -> None:
        """Verify the database unique constraint rejects duplicate emails."""
        User.objects.create_superuser(email="admin@example.com", password="s3cret-pw!")
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_superuser(
                email="admin@example.com", password="s3cret-pw!"
            )

    def test_case_variant_email_rejected(self) -> None:
        """Verify case-variant emails are normalized and rejected as duplicates."""
        User.objects.create_user(email="user@example.com", password="s3cret-pw!")
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_user(email="USER@EXAMPLE.COM", password="other-pw-9")

    def test_direct_create_normalizes_email(self) -> None:
        """Verify objects.create stores the email lowercased in the database."""
        user = User.objects.create(email="DIRECT@EXAMPLE.COM")
        stored = User.objects.get(pk=user.pk)
        assert stored.email == "direct@example.com"

    def test_bulk_create_normalizes_email(self) -> None:
        """Verify bulk_create lowercases emails (path that skips clean())."""
        User.objects.bulk_create([User(email="BULK@EXAMPLE.COM")])
        user = User.objects.get(email="bulk@example.com")
        assert user.email == "bulk@example.com"

    def test_constraint_rejects_uppercase_email(self) -> None:
        """Verify the database constraint rejects uppercase emails via raw SQL."""
        User.objects.create_user(email="user@example.com", password="s3cret-pw!")
        with (
            transaction.atomic(),
            connection.cursor() as cursor,
            pytest.raises(IntegrityError),
        ):
            cursor.execute(
                f"UPDATE {User._meta.db_table} SET email = %s",
                ["USER@EXAMPLE.COM"],
            )


@pytest.mark.django_db
class TestCaseInsensitiveLogin:
    def test_get_by_natural_key_is_case_insensitive(self) -> None:
        """Verify users can be looked up by email regardless of case."""
        User.objects.create_user(email="user@example.com", password="s3cret-pw!")
        user = User.objects.get_by_natural_key("USER@EXAMPLE.COM")
        assert user.email == "user@example.com"

    def test_authenticate_with_mixed_case_email(self) -> None:
        """Verify authenticate() succeeds with different email casing."""
        User.objects.create_user(email="user@example.com", password="s3cret-pw!")
        user = authenticate(username="User@Example.Com", password="s3cret-pw!")
        assert user is not None
        assert user.email == "user@example.com"

    def test_authenticate_with_wrong_password_fails(self) -> None:
        """Verify mixed-case login still requires the correct password."""
        User.objects.create_user(email="user@example.com", password="s3cret-pw!")
        user = authenticate(username="User@Example.Com", password="wrong-pw-12")
        assert user is None

    def test_authenticate_with_unknown_email_fails(self) -> None:
        """Verify authenticate() returns None for unknown mixed-case emails."""
        user = authenticate(username="Nobody@Example.Com", password="s3cret-pw")
        assert user is None


@pytest.mark.django_db
class TestProfileFields:
    def test_name_is_optional(self) -> None:
        """Verify first_name and last_name default to empty strings."""
        user = User.objects.create_user(
            email="admin@example.com", password="s3cret-pw!"
        )
        assert user.first_name == ""
        assert user.last_name == ""

    def test_names_saved(self) -> None:
        """Verify first_name and last_name persist on the user."""
        user = User.objects.create_user(
            email="admin@example.com",
            password="s3cret-pw!",
            first_name="Dina",
            last_name="Khoshrou",
        )
        assert user.first_name == "Dina"
        assert user.last_name == "Khoshrou"


@pytest.mark.django_db
class TestPasswordValidation:
    def test_numeric_password_rejected_by_form(self) -> None:
        """Verify the configured NumericPasswordValidator rejects numeric passwords."""
        from accounts.forms import UserCreationForm

        form = UserCreationForm(
            data={
                "email": "user@example.com",
                "password1": "12345678",
                "password2": "12345678",
            }
        )
        assert not form.is_valid()
        assert "password2" in form.errors

    def test_common_password_rejected_by_form(self) -> None:
        """Verify the configured CommonPasswordValidator rejects common passwords."""
        from accounts.forms import UserCreationForm

        form = UserCreationForm(
            data={
                "email": "user@example.com",
                "password1": "password1234",
                "password2": "password1234",
            }
        )
        assert not form.is_valid()
        assert "password2" in form.errors

    def test_valid_password_accepted_by_form(self) -> None:
        """Verify a strong password passes all configured validators."""
        from accounts.forms import UserCreationForm

        form = UserCreationForm(
            data={
                "email": "user@example.com",
                "first_name": "",
                "last_name": "",
                "password1": "correct-horse-battery-staple",
                "password2": "correct-horse-battery-staple",
            }
        )
        assert form.is_valid(), form.errors


class TestAuthModelConfig:
    def test_settings_point_to_custom_user(self) -> None:
        """Verify AUTH_USER_MODEL points to accounts.User."""
        from django.conf import settings

        assert settings.AUTH_USER_MODEL == "accounts.User"

    def test_email_is_username_field(self) -> None:
        """Verify email is the login identifier and no extra fields are required."""
        assert User.USERNAME_FIELD == "email"
        assert User.REQUIRED_FIELDS == []


@pytest.mark.django_db
def test_admin_search_uses_email(admin_client: Client) -> None:
    User.objects.create_user(email="findme@example.com", password="s3cret-pw!")
    response = admin_client.get("/admin/accounts/user/", {"q": "findme@example.com"})
    assert response.status_code == 200
    content = response.content.decode()
    assert "findme@example.com" in content
    assert "username" not in content.lower()
