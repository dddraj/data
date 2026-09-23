"""Minimal Meta Graph (Marketing) API client over urllib.

Everything Ads Manager does is a Graph API call against an ad account and the
campaign -> ad set -> ad tree beneath it, so a thin client is all the server
needs. No SDK, no dependencies outside the standard library.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

DEFAULT_API_VERSION = "v24.0"
GRAPH_HOST = "https://graph.facebook.com"

# Graph error codes that mean "slow down", not "your request is wrong".
RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80003, 80004, 80014}


class GraphError(RuntimeError):
    def __init__(self, message: str, *, code: Optional[int] = None, subcode: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


def normalize_account_id(account_id: str) -> str:
    """Accept '123', 'act_123' or ' act_123 ' and return 'act_123'."""
    account_id = str(account_id).strip()
    return account_id if account_id.startswith("act_") else f"act_{account_id}"


def _encode(params: Dict[str, Any]) -> Dict[str, str]:
    """Graph takes form fields; nested values (targeting, specs) go as JSON."""
    out = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            out[key] = json.dumps(value, separators=(",", ":"))
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


class GraphClient:
    def __init__(
        self,
        access_token: str,
        *,
        app_secret: Optional[str] = None,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 60.0,
        max_retries: int = 3,
        opener: Optional[Callable] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not access_token:
            raise GraphError("META_ACCESS_TOKEN is not set")
        self._token = access_token
        self._proof = (
            hmac.new(app_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()
            if app_secret
            else None
        )
        self.base = f"{GRAPH_HOST}/{api_version}"
        self.timeout = timeout
        self.max_retries = max_retries
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    # -- transport ------------------------------------------------------
    def _auth(self) -> Dict[str, str]:
        auth = {"access_token": self._token}
        if self._proof:
            auth["appsecret_proof"] = self._proof
        return auth

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    def _send(self, method: str, url: str, params: Dict[str, Any]) -> Any:
        fields = {**_encode(params), **self._auth()}
        data = None
        if method == "GET":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(fields)}"
        else:
            data = urllib.parse.urlencode(fields).encode()

        for attempt in range(self.max_retries):
            request = urllib.request.Request(url, data=data, method=method)
            try:
                with self._open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                error = self._parse_error(exc)
                last = attempt == self.max_retries - 1
                if last or not (exc.code >= 500 or error.code in RATE_LIMIT_CODES):
                    raise error from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == self.max_retries - 1:
                    raise GraphError(self._redact(f"{method} request failed: {exc}")) from None
            self._sleep(2 ** (attempt + 1))
        raise GraphError("unreachable")  # pragma: no cover

    def _parse_error(self, exc: urllib.error.HTTPError) -> GraphError:
        try:
            err = json.loads(exc.read().decode()).get("error", {})
        except (ValueError, AttributeError):
            err = {}
        parts = [err.get("message") or f"HTTP {exc.code}"]
        if err.get("error_user_msg"):
            parts.append(err["error_user_msg"])
        if err.get("error_subcode"):
            parts.append(f"(code {err.get('code')}, subcode {err['error_subcode']})")
        elif err.get("code"):
            parts.append(f"(code {err['code']})")
        return GraphError(
            self._redact(" ".join(parts)), code=err.get("code"), subcode=err.get("error_subcode")
        )

    # -- public API -----------------------------------------------------
    def get(self, path: str, **params: Any) -> Any:
        return self._send("GET", f"{self.base}/{path.lstrip('/')}", params)

    def post(self, path: str, **params: Any) -> Any:
        return self._send("POST", f"{self.base}/{path.lstrip('/')}", params)

    def get_all(self, path: str, *, max_items: int = 200, **params: Any) -> List[dict]:
        """GET an edge and follow `paging.next` until `max_items` rows."""
        params.setdefault("limit", min(max_items, 100))
        page = self.get(path, **params)
        rows: List[dict] = []
        while True:
            rows.extend(page.get("data", []))
            nxt = page.get("paging", {}).get("next")
            if len(rows) >= max_items or not nxt:
                return rows[:max_items]
            # `next` already carries every query param, token included; strip
            # it so _send re-adds exactly one copy.
            parsed = urllib.parse.urlsplit(nxt)
            query = [
                (k, v)
                for k, v in urllib.parse.parse_qsl(parsed.query)
                if k not in ("access_token", "appsecret_proof")
            ]
            url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
            page = self._send("GET", url, {})
