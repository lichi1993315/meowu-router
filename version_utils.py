from __future__ import annotations


def _is_int_segment(value: str) -> bool:
    return value.isdigit()


def _has_release_marker(parts: list[str]) -> bool:
    return any(not part.isdigit() for part in parts)


def _looks_like_build_suffix(value: str) -> bool:
    return not value.isdigit() or len(value) >= 7


def release_version_from_client_version(client_version: object) -> str:
    if not isinstance(client_version, str):
        return ""
    value = client_version.strip()
    if not value:
        return ""

    unity_prefix = "unity-"
    if not value.lower().startswith(unity_prefix):
        return value

    suffix = value[len(unity_prefix) :]
    parts = suffix.split(".")
    if len(parts) >= 5 and _looks_like_build_suffix(parts[-1]):
        child_semver = parts[-4:-1]
        release_parts = parts[:-4]
        if (
            release_parts
            and _has_release_marker(release_parts)
            and all(_is_int_segment(part) for part in child_semver)
        ):
            return ".".join(release_parts)

    if len(parts) >= 4:
        child_semver = parts[-3:]
        release_parts = parts[:-3]
        if (
            release_parts
            and _has_release_marker(release_parts)
            and all(_is_int_segment(part) for part in child_semver)
        ):
            return ".".join(release_parts)

    return value
