"""Shared mock helpers for tests that need httpx stand-ins.

Provides a unified :class:`MockResponse` and :func:`install_mock_client`
factory so that test modules don't each redefine the same boilerplate.
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest


class MockResponse:
    """Minimal httpx.Response stand-in for testing.

    Accepts either plain *text* (via the ``text=`` keyword) or
    *json_data* (any JSON-serialisable object, as the first positional
    argument).  When *json_data* is given, ``.text`` returns its JSON
    representation and ``.json()`` returns the original object.
    """

    def __init__(
        self,
        json_data: Any = None,
        *,
        text: str | None = None,
        status_code: int = 200,
    ) -> None:
        """Create a mock response with optional *text* or *json_data* body."""
        if text is not None:
            self._text = text
            self._json_data = None
        else:
            self._text = None
            self._json_data = json_data
        self.status_code = status_code

    @property
    def text(self) -> str:
        """Response body as a string (JSON-serialised when *json_data* was given)."""
        if self._text is not None:
            return self._text
        if self._json_data is not None:
            return _json.dumps(self._json_data)
        return ""

    def json(self) -> Any:
        """Return the original *json_data* (or parse ``.text`` if text was given)."""
        if self._json_data is not None:
            return self._json_data
        return _json.loads(self._text or "")

    def raise_for_status(self) -> None:
        """Raise :class:`httpx.HTTPStatusError` when status >= 400."""
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=object(),  # type: ignore[arg-type]
                response=self,  # type: ignore[arg-type]
            )


def install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    response: MockResponse,
    *,
    counter: list[int] | None = None,
    capture_kwargs: bool = False,
) -> dict[str, Any]:
    """Replace ``httpx.AsyncClient`` with a factory returning *response*.

    Returns a ``captured`` dict that receives ``url``, ``headers``,
    ``params`` (GET), ``json`` (POST), and ``method`` from each call
    for later inspection.

    If *counter* is a list, its first element is incremented on every
    ``get`` or ``post`` call.

    If *capture_kwargs* is True, the ``__init__`` kwargs passed to the
    mock client are stored under ``captured["client_kwargs"]``.
    """
    captured: dict[str, Any] = {}

    class _BoundClient:
        def __init__(self, **kwargs: Any) -> None:
            if capture_kwargs:
                captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> _BoundClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            params: dict[str, str] | None = None,
        ) -> MockResponse:
            captured["method"] = "GET"
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            if counter is not None:
                counter[0] += 1
            return response

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            json: dict[str, Any] | None = None,
        ) -> MockResponse:
            captured["method"] = "POST"
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            if counter is not None:
                counter[0] += 1
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _BoundClient)
    return captured
