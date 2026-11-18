"""HTTP client for the backend. No business logic and no UI code lives here."""
from __future__ import annotations

import os
from typing import Any

import httpx


class ApiError(Exception):
    """Raised for any failed call. `offline` is True when the server could not be reached at all."""

    def __init__(self, message: str, status: int | None = None, offline: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.offline = offline


class ApiClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("COSMETICS_API_URL") or "http://127.0.0.1:8000").rstrip("/")
        self._token: str | None = None
        self._http = httpx.Client(base_url=self.base_url, timeout=15.0)

    # -- session ---------------------------------------------------------------------------
    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def login(self, username: str, password: str) -> dict:
        data = self._request("POST", "/auth/login", json={"username": username, "password": password}, auth=False)
        self._token = data["access_token"]
        return data

    def logout(self) -> None:
        try:
            if self._token:
                self._request("POST", "/auth/logout")
        except ApiError:
            pass
        finally:
            self._token = None

    # -- verbs -----------------------------------------------------------------------------
    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, json: Any = None) -> Any:
        return self._request("POST", path, json=json)

    def patch(self, path: str, json: Any = None) -> Any:
        return self._request("PATCH", path, json=json)

    def put(self, path: str, json: Any = None) -> Any:
        return self._request("PUT", path, json=json)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def get_bytes(self, path: str, **params: Any) -> bytes:
        """Download a binary response (template, image)."""
        return self._request("GET", path, raw=True, params={k: v for k, v in params.items() if v is not None})

    def post_file(self, path: str, file_path: str, **params: Any) -> Any:
        """Send a file's bytes as the raw request body (used for attendance exports)."""
        with open(file_path, "rb") as fh:
            body = fh.read()
        return self._request("POST", path, params={k: v for k, v in params.items() if v is not None}, content=body)

    def upload(self, path: str, file_path: str, data: dict | None = None, method: str = "POST") -> Any:
        import mimetypes
        import os
        with open(file_path, "rb") as fh:
            files = {"file": (os.path.basename(file_path), fh.read(), mimetypes.guess_type(file_path)[0] or "application/octet-stream")}
        return self._request(method, path, files=files, data=data or {})

    def _request(self, method: str, path: str, auth: bool = True, raw: bool = False, **kw: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if auth and self._token else {}
        try:
            r = self._http.request(method, path, headers=headers, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError):
            raise ApiError(f"Cannot reach the server at {self.base_url}. Check your connection and try again.", offline=True)
        if r.status_code == 204:
            return None
        if r.status_code >= 400:
            raise ApiError(_detail(r), status=r.status_code)
        return r.content if raw else r.json()


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
    except Exception:
        d = None
    if isinstance(d, list):  # pydantic validation errors
        parts = []
        for e in d:
            loc = ".".join(str(x) for x in e.get("loc", []) if x not in ("body",))
            parts.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        return "; ".join(parts)
    return str(d) if d else f"Request failed ({r.status_code})"
