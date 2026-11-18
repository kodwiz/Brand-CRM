import httpx
import pytest

from desktop.api.client import ApiClient, ApiError, _detail


def test_unreachable_server_is_reported_as_offline():
    c = ApiClient("http://127.0.0.1:9")  # nothing listens on the discard port
    with pytest.raises(ApiError) as e:
        c.get("/health")
    assert e.value.offline is True
    assert "Cannot reach the server" in e.value.message


def test_error_detail_string_and_validation_list_are_readable():
    r = httpx.Response(422, json={"detail": "Password must contain a digit."})
    assert _detail(r) == "Password must contain a digit."
    r = httpx.Response(422, json={"detail": [{"loc": ["body", "username"], "msg": "too short"}]})
    assert _detail(r) == "username: too short"
    assert "500" in _detail(httpx.Response(500, text="boom"))


def test_logout_clears_token_even_if_server_down():
    c = ApiClient("http://127.0.0.1:9")
    c._token = "abc"
    c.logout()
    assert c.authenticated is False
