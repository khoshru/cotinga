"""Protect stored provider credentials with the application's secret key."""

import base64

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.views.decorators.debug import sensitive_variables


class CredentialError(Exception):
    pass


@sensitive_variables()
def _cipher() -> MultiFernet:
    keys = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    ciphers = []
    for key in keys:
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"cotinga-provider-credentials-v1",
            info=b"openrouter",
        ).derive(key if isinstance(key, bytes) else key.encode())
        ciphers.append(Fernet(base64.urlsafe_b64encode(derived)))
    return MultiFernet(ciphers)


@sensitive_variables()
def encrypt_key(api_key: str) -> str:
    return _cipher().encrypt(api_key.encode()).decode()


@sensitive_variables()
def decrypt_key(encrypted: str) -> str:
    try:
        return _cipher().decrypt(encrypted.encode()).decode()
    except InvalidToken, UnicodeError, ValueError:
        raise CredentialError(
            "Your saved key could not be read. Save your OpenRouter key again."
        ) from None
