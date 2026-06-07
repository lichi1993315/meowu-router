import os

from cryptography.fernet import Fernet, InvalidToken


PAW_FERNET_KEY_ENV = "PAW_FERNET_KEY"

_fernet_key: str | None = None
_fernet: Fernet | None = None


class FernetConfigError(RuntimeError):
    pass


def _get_fernet() -> Fernet:
    global _fernet, _fernet_key

    key = os.getenv(PAW_FERNET_KEY_ENV, "").strip()
    if not key:
        raise FernetConfigError(f"{PAW_FERNET_KEY_ENV} is not configured")

    if _fernet is not None and _fernet_key == key:
        return _fernet

    try:
        _fernet = Fernet(key.encode("utf-8"))
    except ValueError as exc:
        raise FernetConfigError(
            f"{PAW_FERNET_KEY_ENV} must be a urlsafe base64 encoded 32-byte Fernet key"
        ) from exc
    _fernet_key = key
    return _fernet


def decrypt_payload(encrypted_body: str | bytes) -> str | None:
    token = encrypted_body if isinstance(encrypted_body, bytes) else encrypted_body.encode("utf-8")
    try:
        return _get_fernet().decrypt(token).decode("utf-8")
    except InvalidToken:
        return None
