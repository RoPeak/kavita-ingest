from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .base import ProviderError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        try:
            return json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"provider returned invalid JSON: {exc}") from exc


class Transport(Protocol):
    def get(
        self,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    def get(
        self,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: float,
    ) -> HttpResponse:
        request_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        request = urllib.request.Request(request_url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    response.status,
                    {key.casefold(): value for key, value in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                exc.code,
                {key.casefold(): value for key, value in exc.headers.items()},
                exc.read(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"network request failed: {exc}") from exc
