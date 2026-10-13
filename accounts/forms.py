from django.contrib.auth.forms import UserChangeForm as DjangoUserChangeForm
from django.contrib.auth.forms import UserCreationForm as DjangoUserCreationForm

from .models import User


class UserCreationForm(DjangoUserCreationForm):  # type: ignore[type-arg]
    """Admin form for creating users, identified by email."""

    class Meta(DjangoUserCreationForm.Meta):
        model = User
        fields = ("email", "first_name", "last_name")


class UserChangeForm(DjangoUserChangeForm):  # type: ignore[type-arg]
    """Admin form for editing users, identified by email."""

    class Meta(DjangoUserChangeForm.Meta):
        model = User
        fields = ("email", "first_name", "last_name", "password")  # type: ignore[assignment]
