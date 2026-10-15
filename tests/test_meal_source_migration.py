import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from accounts.models import User


@pytest.mark.django_db(transaction=True)
def test_existing_meal_sources_and_text_survive_migration_and_rollback(
    owner: User,
) -> None:
    old = [("tracker", "0008_georgian_and_original_inputs")]
    new = [("tracker", "0009_meal_additions_and_sources")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(old)
        apps = executor.loader.project_state(old).apps
        Estimate = apps.get_model("tracker", "MealEstimate")
        Meal = apps.get_model("tracker", "Meal")
        source = Estimate.objects.create(
            user_id=owner.pk,
            date="2026-10-15",
            model="test/model",
            status="saved",
            original_input="Original text\nwith lines.",
        )
        meal = Meal.objects.create(
            user_id=owner.pk,
            date="2026-10-15",
            kind="dinner",
            description="Rice",
            description_fa="برنج",
            description_ka="ბრინჯი",
            portion="1 bowl",
            calories=300,
            estimate_id=source.pk,
        )
        manual = Meal.objects.create(
            user_id=owner.pk,
            date="2026-10-15",
            kind="lunch",
            description="Bread",
            calories=100,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(new)
        Meal = executor.loader.project_state(new).apps.get_model("tracker", "Meal")
        updated = Meal.objects.get(pk=meal.pk)
        assert list(updated.source_estimates.values_list("pk", flat=True)) == [
            source.pk
        ]
        assert not Meal.objects.get(pk=manual.pk).source_estimates.exists()
        assert updated.description == "Rice" and updated.description_ka == "ბრინჯი"
        assert updated.estimate.original_input == "Original text\nwith lines."
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        Meal = executor.loader.project_state(old).apps.get_model("tracker", "Meal")
        restored = Meal.objects.get(pk=meal.pk)
        assert restored.estimate_id == source.pk and restored.description_fa == "برنج"
        assert restored.calories == 300
    finally:
        MigrationExecutor(connection).migrate(latest)
