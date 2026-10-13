from django.urls import path

from . import views

app_name = "tracker"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("meals/new/", views.meal_create, name="meal-create"),
    path("meals/<int:pk>/delete/", views.meal_delete, name="meal-delete"),
    path("targets/", views.targets_edit, name="targets-edit"),
]
