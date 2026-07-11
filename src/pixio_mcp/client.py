"""Async HTTP layer for the Pixio media-generation gateway.

Every REST call the MCP tools need goes through :class:`PixioClient`:
catalog/params discovery, cost estimation, generation submit/poll, credits,
media uploads, and output downloads.  All HTTP-response -> error mapping lives
in one place (:meth:`PixioClient._raise_for_response`) so the tool layer only
ever sees :class:`~pixio_mcp.errors.PixioError`.

Spend safety: ``POST /generate`` is submitted exactly once and is NEVER
auto-retried under any circumstances.  Idempotent GETs and the estimate call
retry up to 3 times on httpx transport errors and 5xx responses (backoff
0.5s / 1s / 2s); uploads retry once, on connect errors only.

Secrets hygiene: the API key is attached per request, is never logged, is
never embedded in error messages, and is only ever sent to Pixio hosts —
output downloads from third-party hosts (CDNs / DigitalOcean Spaces) carry no
Authorization header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from pixio_mcp.config import Settings
from pixio_mcp.errors import ErrorCode, PixioError

logger = logging.getLogger("pixio_mcp.client")

#: Backoff schedule for retryable calls: up to 3 retries after the first
#: attempt, sleeping 0.5s / 1s / 2s before each retry.
_RETRY_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0)

#: Uploads get a single retry (connect errors only).
_UPLOAD_BACKOFF_S: tuple[float, ...] = (0.5,)

#: Hosts that may receive the Authorization header on downloads.
_PIXIO_HOST_SUFFIX = "pixio.myapps.ai"

_RetryPolicy = Literal["none", "retry", "upload"]

#: 402 body fields surfaced as INSUFFICIENT_CREDITS details when present.
_CREDIT_DETAIL_KEYS = ("availableCredits", "requiredCredits", "shortfall")

#: 429 body fields surfaced as CONCURRENCY details when present.
_CONCURRENCY_DETAIL_KEYS = ("concurrencyLimit", "generationId", "status")


def _is_pixio_host(url: str) -> bool:
    """Return True when ``url``'s host is pixio.myapps.ai or a subdomain of it."""
    host = (urlsplit(url).hostname or "").lower()
    return host == _PIXIO_HOST_SUFFIX or host.endswith("." + _PIXIO_HOST_SUFFIX)


class PixioClient:
    """Thin async client for the Pixio REST API.

    Parameters
    ----------
    settings:
        Resolved server settings (base URL, API key, ...).
    transport:
        Optional ``httpx`` transport, injectable so tests can use
        ``httpx.MockTransport`` without touching the network.
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Public API methods
    # ------------------------------------------------------------------ #

    async def get_models(self) -> list[dict[str, Any]]:
        """Fetch the model catalog (``GET /models``), unwrapping ``{"models": [...]}``."""
        response = await self._request("GET", "/models", policy="retry")
        body = self._json(response)
        models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(models, list):
            raise PixioError(
                ErrorCode.UPSTREAM_ERROR,
                "GET /models returned an unexpected shape (no 'models' list)",
            )
        return models

    async def get_params(self, model_id: str) -> dict[str, Any]:
        """Fetch one model's input schema (``GET /params?modelId=...``), verbatim."""
        response = await self._request(
            "GET", "/params", query={"modelId": model_id}, policy="retry"
        )
        return self._json_dict(response)

    async def estimate(self, model_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Price a job (``POST /generations/estimate``) and return the body verbatim.

        Retried like an idempotent GET — estimating never spends credits.
        """
        logger.debug(
            "estimate params",
            extra={"param_keys": sorted(str(key) for key in params)},
        )
        response = await self._request(
            "POST",
            "/generations/estimate",
            json_body={"providerId": "pixio", "modelId": model_id, "params": params},
            policy="retry",
        )
        return self._json_dict(response)

    async def generate(self, model_id: str, params: dict[str, Any]) -> str:
        """Submit a generation (``POST /generate``) and return its ``contentId``.

        This call spends credits and is therefore submitted exactly once —
        it is NEVER auto-retried, not on transport errors and not on 5xx.
        The gateway names the new generation's id ``contentId`` in this
        response (``GET /generations/{id}`` later calls the same value ``id``).
        """
        logger.debug(
            "generate params",
            extra={"param_keys": sorted(str(key) for key in params)},
        )
        response = await self._request(
            "POST",
            "/generate",
            json_body={"providerId": "pixio", "modelId": model_id, "params": params},
            policy="none",
        )
        body = self._json_dict(response)
        content_id = body.get("contentId")
        if not isinstance(content_id, str) or not content_id:
            raise PixioError(
                ErrorCode.UPSTREAM_ERROR,
                "generate succeeded (HTTP 2xx) but the response has no 'contentId'",
                details={"response_keys": sorted(str(key) for key in body)},
            )
        return content_id

    async def get_generation(self, generation_id: str) -> dict[str, Any]:
        """Fetch one generation's status record (``GET /generations/{id}``), verbatim."""
        response = await self._request(
            "GET", f"/generations/{quote(generation_id, safe='')}", policy="retry"
        )
        return self._json_dict(response)

    async def get_credits(self) -> dict[str, Any]:
        """Fetch the account credit balance (``GET /credits``), verbatim."""
        response = await self._request("GET", "/credits", policy="retry")
        return self._json_dict(response)

    async def get_ledger(self, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch the most recent credit-ledger entries (``GET /credits/ledger``).

        Returns at most ``limit`` entries (the head of the ``entries`` list).
        """
        response = await self._request("GET", "/credits/ledger", policy="retry")
        body = self._json(response)
        entries = body.get("entries") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise PixioError(
                ErrorCode.UPSTREAM_ERROR,
                "GET /credits/ledger returned an unexpected shape (no 'entries' list)",
            )
        return entries[: max(0, limit)]

    async def upload_file(self, path: Path) -> str:
        """Upload a local file (multipart ``POST /media``) and return its public URL.

        The returned URL is a permanent, public
        ``pixiomedia.nyc3.digitaloceanspaces.com`` location.  Retried once on
        connect errors only (never on HTTP errors).
        """
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PixioError(
                ErrorCode.VALIDATION,
                f"cannot read file for upload: {path} ({exc.strerror or exc})",
            ) from exc
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        response = await self._request(
            "POST",
            "/media",
            files={"file": (path.name, data, content_type)},
            policy="upload",
        )
        return self._extract_upload_url(response)

    async def upload_url(self, url: str) -> str:
        """Mirror a remote URL into Pixio media (JSON ``POST /media``) and return the new URL.

        The returned URL is a permanent, public
        ``pixiomedia.nyc3.digitaloceanspaces.com`` location.  Retried once on
        connect errors only (never on HTTP errors).
        """
        response = await self._request(
            "POST", "/media", json_body={"url": url}, policy="upload"
        )
        return self._extract_upload_url(response)

    async def download(self, url: str, dest: Path) -> int:
        """Stream ``url`` to ``dest`` atomically and return the bytes written.

        Follows redirects.  The Authorization header is sent ONLY when the URL
        host is ``pixio.myapps.ai`` (or a subdomain) — generation outputs live
        on DigitalOcean Spaces / CDNs and must never see the bearer token.
        The file is written to a temp path in the destination directory and
        renamed into place, so a failed transfer never leaves a partial
        ``dest`` behind.
        """
        headers: dict[str, str] = {}
        if _is_pixio_host(url):
            self._require_key()
            headers["Authorization"] = f"Bearer {self._settings.api_key}"

        log_path = urlsplit(url).path  # never log the query (signed URLs)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.with_name(f".{dest.name}.{uuid4().hex}.part")
        bytes_written = 0

        self._log_start("GET", log_path)
        started = time.monotonic()
        try:
            async with self._http.stream(
                "GET", url, headers=headers, follow_redirects=True
            ) as response:
                if not response.is_success:
                    await response.aread()
                    self._log_finish(
                        "GET", log_path, response.status_code, started
                    )
                    self._raise_for_response(response)
                try:
                    with open(tmp_path, "wb") as fh:
                        async for chunk in response.aiter_bytes():
                            fh.write(chunk)
                            bytes_written += len(chunk)
                    os.replace(tmp_path, dest)
                except OSError as exc:
                    raise PixioError(
                        ErrorCode.VALIDATION,
                        f"cannot write download to {dest} ({exc.strerror or exc})",
                    ) from exc
                self._log_finish("GET", log_path, response.status_code, started)
        except httpx.RequestError as exc:
            self._log_finish("GET", log_path, None, started)
            raise PixioError(
                ErrorCode.UPSTREAM_ERROR,
                f"network error downloading {log_path}: {type(exc).__name__}",
            ) from exc
        finally:
            tmp_path.unlink(missing_ok=True)
        return bytes_written

    # ------------------------------------------------------------------ #
    # Request core
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        query: Mapping[str, str] | None = None,
        files: Any | None = None,
        policy: _RetryPolicy = "none",
    ) -> httpx.Response:
        """Issue one API request under the given retry policy.

        ``retry``  — up to 3 retries on httpx transport errors and 5xx
        (backoff 0.5s/1s/2s).  ``upload`` — one retry, on
        ``httpx.ConnectError`` only.  ``none`` — exactly one attempt
        (``POST /generate``: spend safety).  Raises :class:`PixioError` for
        every failure via :meth:`_raise_for_response`.
        """
        self._require_key()
        backoff: tuple[float, ...]
        if policy == "retry":
            backoff = _RETRY_BACKOFF_S
        elif policy == "upload":
            backoff = _UPLOAD_BACKOFF_S
        else:
            backoff = ()

        attempt = 0
        while True:
            self._log_start(method, path)
            started = time.monotonic()
            try:
                response = await self._http.request(
                    method,
                    path,
                    json=json_body,
                    params=query,
                    files=files,
                    headers={"Authorization": f"Bearer {self._settings.api_key}"},
                )
            except httpx.RequestError as exc:
                self._log_finish(method, path, None, started)
                retryable = (
                    policy == "retry" and isinstance(exc, httpx.TransportError)
                ) or (policy == "upload" and isinstance(exc, httpx.ConnectError))
                if retryable and attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    attempt += 1
                    continue
                raise PixioError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"network error calling {method} {path}: {type(exc).__name__}",
                ) from exc

            self._log_finish(method, path, response.status_code, started)
            if (
                response.status_code >= 500
                and policy == "retry"
                and attempt < len(backoff)
            ):
                await asyncio.sleep(backoff[attempt])
                attempt += 1
                continue
            self._raise_for_response(response)
            return response

    def _require_key(self) -> None:
        """Raise AUTH before any request goes out when no API key is configured."""
        if not self._settings.api_key:
            raise PixioError(ErrorCode.AUTH, "PIXIO_API_KEY is not set")

    # ------------------------------------------------------------------ #
    # Response handling
    # ------------------------------------------------------------------ #

    def _raise_for_response(self, response: httpx.Response) -> None:
        """Central HTTP -> :class:`PixioError` mapping; no-op on 2xx.

        401 -> AUTH; 402 -> INSUFFICIENT_CREDITS (+credit details);
        404 or a body mentioning "model not found" -> NOT_FOUND;
        429 or a body mentioning "concurrency limit" -> CONCURRENCY
        (+concurrency details); other 4xx -> VALIDATION with the body's
        error/message text passed through verbatim (the gateway hides the
        real reason in 400 bodies); 5xx -> UPSTREAM_ERROR.
        """
        if response.is_success:
            return
        status = response.status_code
        try:
            body: Any = response.json()
        except Exception:
            body = None
        message = self._sanitize(self._extract_error_message(body, response))
        lowered = message.lower()

        if status == 401:
            raise PixioError(ErrorCode.AUTH, message)
        if status == 402:
            details = self._pick_details(body, _CREDIT_DETAIL_KEYS)
            raise PixioError(ErrorCode.INSUFFICIENT_CREDITS, message, details=details)
        if status == 404 or (status < 500 and "model not found" in lowered):
            raise PixioError(ErrorCode.NOT_FOUND, message)
        if status == 429 or (status < 500 and "concurrency limit" in lowered):
            details = self._pick_details(body, _CONCURRENCY_DETAIL_KEYS)
            raise PixioError(ErrorCode.CONCURRENCY, message, details=details)
        if status < 500:
            raise PixioError(ErrorCode.VALIDATION, message)
        raise PixioError(ErrorCode.UPSTREAM_ERROR, message)

    @staticmethod
    def _extract_error_message(body: Any, response: httpx.Response) -> str:
        """Pull the most useful human-readable text out of an error body.

        Handles all observed Pixio error shapes: ``{"error": "..."}``,
        ``{"error": {...}}``, and ``{"error": "...", "message": "..."}``.
        Falls back to the raw body text, then to ``HTTP <status>``.
        """
        parts: list[str] = []
        if isinstance(body, Mapping):
            err = body.get("error")
            if isinstance(err, str) and err.strip():
                parts.append(err.strip())
            elif isinstance(err, Mapping):
                inner = err.get("message")
                if isinstance(inner, str) and inner.strip():
                    parts.append(inner.strip())
                else:
                    parts.append(json.dumps(err, default=str))
            msg = body.get("message")
            if isinstance(msg, str) and msg.strip() and msg.strip() not in parts:
                parts.append(msg.strip())
        if not parts:
            text = (response.text or "").strip()
            if text:
                parts.append(text[:500])
        return ": ".join(parts) or f"HTTP {response.status_code}"

    @staticmethod
    def _pick_details(body: Any, keys: tuple[str, ...]) -> dict[str, Any] | None:
        """Return the subset of ``keys`` present in a dict body, or None."""
        if not isinstance(body, Mapping):
            return None
        details = {key: body[key] for key in keys if key in body}
        return details or None

    def _json(self, response: httpx.Response) -> Any:
        """Decode a 2xx response body as JSON; decode failure -> UPSTREAM_ERROR."""
        try:
            return response.json()
        except Exception as exc:
            raise PixioError(
                ErrorCode.UPSTREAM_ERROR,
                f"invalid JSON in response from {response.url.path}",
            ) from exc

    def _json_dict(self, response: httpx.Response) -> dict[str, Any]:
        """Decode a 2xx response body as a JSON object; other shapes -> UPSTREAM_ERROR."""
        body = self._json(response)
        if not isinstance(body, dict):
            raise PixioError(
                ErrorCode.UPSTREAM_ERROR,
                f"expected a JSON object from {response.url.path}, "
                f"got {type(body).__name__}",
            )
        return body

    def _extract_upload_url(self, response: httpx.Response) -> str:
        """Return the ``url`` field of a media-upload response; missing -> UPSTREAM_ERROR."""
        body = self._json_dict(response)
        url = body.get("url")
        if not isinstance(url, str) or not url:
            raise PixioError(
                ErrorCode.UPSTREAM_ERROR,
                "media upload succeeded (HTTP 2xx) but the response has no 'url'",
                details={"response_keys": sorted(str(key) for key in body)},
            )
        return url

    def _sanitize(self, text: str) -> str:
        """Strip the API key from text destined for error messages, defensively."""
        if self._settings.api_key and self._settings.api_key in text:
            return text.replace(self._settings.api_key, "[redacted]")
        return text

    # ------------------------------------------------------------------ #
    # Logging (method/path/status/elapsed_ms only — never headers, params,
    # query strings, or the key)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _log_start(method: str, path: str) -> None:
        logger.info("http request start", extra={"method": method, "path": path})

    @staticmethod
    def _log_finish(method: str, path: str, status: int | None, started: float) -> None:
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        logger.info(
            "http request finish",
            extra={
                "method": method,
                "path": path,
                "status": status,
                "elapsed_ms": elapsed_ms,
            },
        )
