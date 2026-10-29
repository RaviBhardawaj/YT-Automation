"""Photorealistic Film Studio — an isolated, opt-in workflow (feature flag + per-account permission).

Off by default. When it is off, nothing here runs and every existing workflow behaves exactly as before; a request
that asks for a realistic film is answered with how to switch the studio on, never quietly made as a cartoon.
"""
from __future__ import annotations

from .. import plans, settings


class StudioOff(ValueError):
    pass


def enabled(workspace_id: int | None = None) -> bool:
    """The platform switch is on and (when an account is given) the account may use the studio."""
    if not settings.get("film_studio.enabled"):
        return False
    return workspace_id is None or plans.can(workspace_id, "film_studio")


def require(workspace_id: int) -> None:
    if not settings.get("film_studio.enabled"):
        raise StudioOff("Realistic films are made in the Photorealistic Film Studio, which is switched off. "
                        "The platform admin can switch it on under Settings → Film Studio.")
    if not plans.can(workspace_id, "film_studio"):
        raise StudioOff(f"“{plans.FEATURES['film_studio']}” isn't enabled for your account. Ask the administrator.")
