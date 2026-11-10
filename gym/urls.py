from django.urls import path

from . import ai_views, record_views, views

app_name = "gym"
urlpatterns = [
    path("", views.home, name="home"),
    path("exercises/", views.library, name="library"),
    path("exercises/new/", views.exercise_edit, name="exercise-new"),
    path("exercises/<int:pk>/", views.exercise_progress, name="exercise"),
    path("exercises/<int:pk>/edit/", views.exercise_edit, name="exercise-edit"),
    path("ai/", ai_views.generate, name="ai-generate"),
    path("ai/<uuid:pk>/", ai_views.review, name="ai-review"),
    path("routines/new/", views.routine_edit, name="routine-new"),
    path("routines/<int:pk>/", views.routine_edit, name="routine"),
    path("routines/<int:pk>/delete/", views.routine_delete, name="routine-delete"),
    path(
        "routines/<int:pk>/<str:action>/", views.routine_action, name="routine-action"
    ),
    path("start/", views.start, name="start"),
    path("workouts/<uuid:pk>/", views.workout, name="workout"),
    path("workouts/<uuid:pk>/action/", views.workout_action, name="workout-action"),
    path("workouts/<uuid:pk>/delete/", views.workout_delete, name="workout-delete"),
    path("history/", views.history, name="history"),
    path("progress/", views.progress, name="progress"),
    path("records/", record_views.records, name="records"),
    path("export/", views.export, name="export"),
]
