"""Encrypts integration tokens at rest (Fernet). The key comes from BULBUL_SECRET_KEY."""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            # Works, but tokens become unreadable after a restart (users must reconnect).
            log.warning("BULBUL_SECRET_KEY is not set: using a temporary key; integrations must be reconnected after restart")
            key = Fernet.generate_key().decode()
        self._f = Fernet(key.encode())

    def encrypt(self, value: str | None) -> str | None:
        return self._f.encrypt(value.encode()).decode() if value else None

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._f.decrypt(value.encode()).decode()
        except InvalidToken:
            return None  # different key (e.g. rotated): treat as disconnected


def new_key() -> str:
    return Fernet.generate_key().decode()
