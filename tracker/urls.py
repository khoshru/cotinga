from django.urls import path

from . import ai_views, outlook_views, profile_views, progress_views, views

app_name = "tracker"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("meals/new/", views.meal_create, name="meal-create"),
    path("meals/<int:pk>/edit/", views.meal_edit, name="meal-edit"),
    path("meals/<int:pk>/delete/", views.meal_delete, name="meal-delete"),
    path("targets/", views.targets_edit, name="targets-edit"),
    path("profile/", profile_views.profile_view, name="profile"),
    path("ai/", ai_views.estimate_view, name="ai-estimate"),
    path("ai/settings/", ai_views.settings_view, name="ai-settings"),
    path("ai/models/", ai_views.model_search, name="ai-models"),
    path("ai/review/<uuid:pk>/", ai_views.review_view, name="ai-review"),
    path("ai/source/<uuid:pk>/", views.original_input, name="ai-source"),
    path("ai/targets/", ai_views.targets_estimate_view, name="ai-targets"),
    path(
        "ai/targets/<uuid:pk>/", ai_views.targets_review_view, name="ai-targets-review"
    ),
    path("outlook/", outlook_views.outlook_view, name="outlook"),
    path("statistics/", progress_views.statistics_view, name="statistics"),
    path(
        "statistics/export/", progress_views.statistics_export, name="statistics-export"
    ),
    path("weight/", progress_views.weight_view, name="weight"),
    path("weight/<int:pk>/delete/", progress_views.weight_delete, name="weight-delete"),
    path("weight/goal/", progress_views.weight_goal, name="weight-goal"),
]
