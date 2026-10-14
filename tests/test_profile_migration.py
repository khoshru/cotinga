import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from accounts.models import User


@pytest.mark.django_db(transaction=True)
def test_legacy_body_details_survive_migration_and_rollback(owner: User) -> None:
    old = [("tracker", "0005_progressplan")]
    new = [("tracker", "0006_shared_body_profile")]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(old)
        apps = executor.loader.project_state(old).apps
        Plan = apps.get_model("tracker", "ProgressPlan")
        Plan.objects.create(
            user_id=owner.pk,
            start_weight=85,
            age=32,
            height=180,
            sex="female",
            activity="moderate",
            mode="gain",
        )
        unknown = User.objects.create_user("legacy-unknown@example.test")
        Plan.objects.create(user_id=unknown.pk, start_weight=90, sex="unspecified")
        executor = MigrationExecutor(connection)
        executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        Profile = apps.get_model("tracker", "BodyProfile")
        profile = Profile.objects.get(user_id=owner.pk)
        assert (
            profile.age,
            profile.height,
            profile.sex,
            profile.activity,
            profile.goal,
        ) == (32, 180, "female", "moderate", "gain")
        assert Profile.objects.get(user_id=unknown.pk).sex == ""
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        restored = (
            executor.loader.project_state(old)
            .apps.get_model("tracker", "ProgressPlan")
            .objects.get(user_id=owner.pk)
        )
        assert (restored.age, restored.height, restored.sex, restored.activity) == (
            32,
            180,
            "female",
            "moderate",
        )
    finally:
        MigrationExecutor(connection).migrate(new)
