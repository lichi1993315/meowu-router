from encrypted_saver import decrypt_string_only


def decrypt_payload(encrypted_str: str) -> str | None:
    return decrypt_string_only(encrypted_str)
