from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path
from django.views.decorators.cache import never_cache
from django.views.i18n import JavaScriptCatalog

from tracker.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("i18n/", include("django.conf.urls.i18n")),
    path(
        "i18n/js/", never_cache(JavaScriptCatalog.as_view()), name="javascript-catalog"
    ),
    path("accounts/login/", LoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("", include("tracker.urls")),
]
