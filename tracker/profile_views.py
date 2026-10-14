from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from accounts.models import User

from .forms import BodyProfileForm
from .models import BodyProfile
from .profiles import profile_initial, save_profile


@login_required
@never_cache
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def profile_view(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    destination = request.POST.get("next", request.GET.get("next", ""))
    if not url_has_allowed_host_and_scheme(
        destination, {request.get_host()}, require_https=request.is_secure()
    ):
        destination = reverse("tracker:profile")
    profile = BodyProfile.objects.filter(user=user).first()
    form = BodyProfileForm(
        request.POST if request.method == "POST" else None,
        instance=profile,
        initial=profile_initial(user),
        prefix="profile",
    )
    if request.method == "POST" and form.is_valid():
        save_profile(user, form.cleaned_data)
        messages.success(request, gettext("Your details are saved and ready to reuse."))
        return redirect(destination)
    return render(
        request, "tracker/profile.html", {"form": form, "return_to": destination}
    )
