"""Language changes are explicit; first visits always start in English."""

from django.conf import settings
from django.http import HttpRequest
from django.middleware.locale import LocaleMiddleware
from django.utils import translation


class ExplicitLanguageMiddleware(LocaleMiddleware):
    def process_request(self, request: HttpRequest) -> None:
        selected = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME, "en")
        language = selected if selected in dict(settings.LANGUAGES) else "en"
        translation.activate(language)
        request.LANGUAGE_CODE = language
