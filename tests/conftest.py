import pytest

from accounts.models import User


@pytest.fixture
def owner() -> User:
    return User.objects.create_user("owner@example.com", "correct-horse-battery-staple")
