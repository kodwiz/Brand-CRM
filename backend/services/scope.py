"""Location scoping: the single place that decides which locations a user may touch."""
from __future__ import annotations

from backend.models import User


def visible_location_ids(user: User) -> set[int] | None:
    """None means every location; otherwise the explicit set of assigned location ids."""
    if user.role.scope_all_locations:
        return None
    return set(user.assigned_location_ids)


def can_access_location(user: User, location_id: int) -> bool:
    ids = visible_location_ids(user)
    return ids is None or location_id in ids
