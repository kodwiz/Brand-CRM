"""Client-side session state. The permission list comes from the server (/auth/me); it is used only to
decide what to show. The backend independently enforces every permission on every call."""
from __future__ import annotations

from dataclasses import dataclass, field

from desktop.api.client import ApiClient


@dataclass
class Session:
    client: ApiClient
    me: dict = field(default_factory=dict)
    current_location_id: int | None = None  # None = all locations in scope

    def load_me(self) -> None:
        self.me = self.client.get("/auth/me")

    @property
    def permissions(self) -> set[str]:
        return set(self.me.get("permissions", []))

    def can(self, *perms: str) -> bool:
        return any(p in self.permissions for p in perms)

    @property
    def user(self) -> dict:
        return self.me["user"]

    @property
    def locations(self) -> list[dict]:
        return self.me.get("locations", [])

    @property
    def currency(self) -> str:
        return self.me.get("currency", "PKR")

    @property
    def environment(self) -> str:
        return self.me.get("environment", "development")
