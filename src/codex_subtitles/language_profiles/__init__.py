from __future__ import annotations

from .base import LanguageProfile
from .zh_hans import ZH_HANS


PROFILES: dict[str, LanguageProfile] = {ZH_HANS.id: ZH_HANS}
DEFAULT_PROFILE = ZH_HANS


def profile_ids() -> tuple[str, ...]:
    return tuple(sorted(PROFILES))


def get_profile(profile_id: str) -> LanguageProfile:
    try:
        return PROFILES[profile_id.casefold()]
    except KeyError as exc:
        raise ValueError(
            f"unknown language profile {profile_id!r}; available: {', '.join(profile_ids())}"
        ) from exc


__all__ = ["DEFAULT_PROFILE", "LanguageProfile", "PROFILES", "get_profile", "profile_ids"]
