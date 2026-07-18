#!/usr/bin/env python3
"""Synchronize Onelap FIT activities to the authenticated Strava athlete."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import ssl
import sys
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import download_latest_fit as onelap


STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_CONFIG = ".strava_auth.json"
STRAVA_WEB_SESSION = ".strava_web_session.json"
SYNC_STATE = ".strava_sync_state.json"
REQUIRED_SCOPES = {"activity:read_all", "activity:write"}
FINAL_STATUSES = {"uploaded", "duplicate", "matched"}


class SyncError(RuntimeError):
    pass


class RateLimitError(SyncError):
    pass


class TransientNetworkError(SyncError):
    pass


class WebSessionExpiredError(SyncError):
    pass


def network_error_text(error: BaseException) -> str:
    if isinstance(error, URLError):
        return str(error.reason)
    return str(error)


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.is_file() and default is not None:
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"Cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"JSON file must contain an object: {path}")
    return value


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=True, indent=2)
            file.write("\n")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def http_json(request: Request, timeout: float) -> tuple[Any, Any]:
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            headers = response.headers
    except HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("message") or payload.get("error") or "")
        except (UnicodeError, json.JSONDecodeError):
            pass
        suffix = f": {detail}" if detail else ""
        if exc.code == 429:
            raise RateLimitError(f"Strava rate limit reached; rerun later{suffix}") from exc
        raise SyncError(f"HTTP {exc.code} {exc.reason}{suffix}") from exc
    except URLError as exc:
        raise SyncError(f"Network request failed: {exc.reason}") from exc
    try:
        return json.loads(body.decode("utf-8")), headers
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError("Server returned invalid JSON") from exc


def oauth_token(data: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(
        STRAVA_TOKEN_URL,
        data=urlencode(data).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    result, _ = http_json(request, timeout)
    if not isinstance(result, dict) or not result.get("access_token") or not result.get("refresh_token"):
        raise SyncError("Strava token response is incomplete")
    return result


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, list[str]] | None = None
    expected_state = ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/callback" and params.get("state", [""])[0] == self.expected_state:
            type(self).result = params
            status = 200
            message = "Authorization received. You can close this window."
        else:
            status = 400
            message = "Invalid OAuth callback."
        body = f"<html><body><p>{message}</p></body></html>".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def authorize_strava(config_path: Path, port: int, timeout: float) -> None:
    existing = load_json(config_path, {})
    client_id = os.environ.get("STRAVA_CLIENT_ID") or str(existing.get("client_id", ""))
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET") or str(
        existing.get("client_secret", "")
    )
    if not client_id:
        client_id = input("Strava client ID: ").strip()
    if not client_secret:
        client_secret = getpass.getpass("Strava client secret: ").strip()
    if not client_id or not client_secret:
        raise SyncError("Strava client ID and secret are required")

    redirect_uri = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(24)
    OAuthCallbackHandler.result = None
    OAuthCallbackHandler.expected_state = state
    server = HTTPServer(("127.0.0.1", port), OAuthCallbackHandler)
    server.timeout = 1
    auth_url = STRAVA_AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "force",
            "scope": ",".join(sorted(REQUIRED_SCOPES)),
            "state": state,
        }
    )
    print(f"Open this URL to authorize Strava:\n{auth_url}")
    webbrowser.open(auth_url)

    deadline = time.monotonic() + 300
    while OAuthCallbackHandler.result is None and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    params = OAuthCallbackHandler.result
    if not params:
        raise SyncError("Timed out waiting for Strava authorization")
    if "error" in params:
        raise SyncError(f"Strava authorization failed: {params['error'][0]}")
    code = params.get("code", [""])[0]
    granted = set(params.get("scope", [""])[0].replace(",", " ").split())
    if not code:
        raise SyncError("Strava authorization callback has no code")
    if not REQUIRED_SCOPES.issubset(granted):
        raise SyncError(f"Required Strava scopes were not granted: {sorted(REQUIRED_SCOPES)}")

    tokens = oauth_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout,
    )
    config = {
        "version": 1,
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": int(tokens["expires_at"]),
        "scope": " ".join(sorted(granted)),
    }
    save_json(config_path, config)
    print(f"Strava authorization saved: {config_path.resolve()}")


def capture_strava_web_session(session_path: Path, timeout: float) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SyncError(
            "Playwright is required for web login; run: pip install -r requirements.txt"
        ) from exc

    session_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="msedge", headless=False)
        except PlaywrightError:
            try:
                browser = playwright.chromium.launch(headless=False)
            except PlaywrightError as exc:
                raise SyncError(
                    "No supported browser found; run: python -m playwright install chromium"
                ) from exc
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        try:
            page.goto("https://www.strava.com/login", wait_until="domcontentloaded", timeout=60000)
            print("Complete Strava login in the opened browser window...")
            deadline = time.monotonic() + max(timeout, 300)
            while time.monotonic() < deadline:
                current = urlparse(page.url)
                logged_in_path = current.path.startswith(("/dashboard", "/athlete", "/upload"))
                if current.hostname == "www.strava.com" and logged_in_path:
                    response = context.request.get(
                        "https://www.strava.com/upload/select", timeout=30000
                    )
                    if response.ok and "/login" not in urlparse(response.url).path:
                        context.storage_state(path=str(session_path))
                        try:
                            os.chmod(session_path, 0o600)
                        except OSError:
                            pass
                        print(f"Strava web session saved: {session_path.resolve()}")
                        return
                page.wait_for_timeout(1000)
            raise SyncError("Timed out waiting for Strava web login")
        except PlaywrightError as exc:
            raise SyncError(f"Strava web login failed: {exc}") from exc
        finally:
            context.close()
            browser.close()


def save_cookie_header_session(cookie_header: str, session_path: Path, timeout: float) -> None:
    value = cookie_header.strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    parsed = SimpleCookie()
    try:
        parsed.load(value)
    except Exception as exc:
        raise SyncError("Cannot parse Strava Cookie header") from exc
    if not parsed:
        raise SyncError("Strava Cookie header is empty or invalid")
    state = {
        "cookies": [
            {
                "name": name,
                "value": morsel.value,
                "domain": ".strava.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
            for name, morsel in parsed.items()
        ],
        "origins": [],
    }
    candidate = session_path.with_name(f".{session_path.name}.{os.getpid()}.import")
    try:
        save_json(candidate, state)
        StravaWebClient(candidate, timeout)
        os.replace(candidate, session_path)
        print(f"Strava web session imported: {session_path.resolve()}")
    finally:
        candidate.unlink(missing_ok=True)


def import_strava_har(har_path: Path, session_path: Path, timeout: float) -> None:
    if not har_path.is_file():
        raise SyncError(f"Strava HAR not found: {har_path}")
    try:
        with har_path.open("r", encoding="utf-8-sig") as file:
            har = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"Cannot read Strava HAR: {exc}") from exc
    candidates: list[tuple[int, str, str]] = []
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        parsed_url = urlparse(str(request.get("url", "")))
        if not parsed_url.hostname or not parsed_url.hostname.endswith("strava.com"):
            continue
        cookie_header = next(
            (
                str(item.get("value", ""))
                for item in request.get("headers", [])
                if str(item.get("name", "")).lower() == "cookie"
            ),
            "",
        )
        if not cookie_header:
            cookie_header = "; ".join(
                f"{item.get('name')}={item.get('value')}"
                for item in request.get("cookies", [])
                if item.get("name") and item.get("value") is not None
            )
        if cookie_header:
            priority = 1 if parsed_url.path.startswith("/upload/") else 0
            candidates.append((priority, str(entry.get("startedDateTime", "")), cookie_header))
    if not candidates:
        raise SyncError("No Strava Cookie header found in HAR")
    _, _, cookie_header = max(candidates, key=lambda item: (item[0], item[1]))
    save_cookie_header_session(cookie_header, session_path, timeout)


class CsrfMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "meta" and values.get("name") == "csrf-token":
            self.token = values.get("content")


class StravaWebClient:
    upload_url = "https://www.strava.com/upload/files"
    progress_url = "https://www.strava.com/upload/progress.json"
    select_url = "https://www.strava.com/upload/select"

    def __init__(self, session_path: Path, timeout: float):
        if not session_path.is_file():
            raise SyncError(
                "Strava web session is missing; run: python sync_to_strava.py --web-login"
            )
        self.timeout = timeout
        self.cookie_jar = CookieJar()
        self._load_cookies(session_path)
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self._csrf_token = self.fetch_csrf_token()

    def _load_cookies(self, session_path: Path) -> None:
        state = load_json(session_path)
        cookies = state.get("cookies", [])
        if not isinstance(cookies, list):
            raise SyncError("Strava web session cookie data is invalid")
        now = int(time.time())
        for item in cookies:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain", ""))
            expires = int(float(item.get("expires", -1) or -1))
            if not domain.lstrip(".").endswith("strava.com"):
                continue
            if expires > 0 and expires <= now:
                continue
            cookie = Cookie(
                version=0,
                name=str(item.get("name", "")),
                value=str(item.get("value", "")),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=str(item.get("path", "/")),
                path_specified=True,
                secure=bool(item.get("secure", True)),
                expires=expires if expires > 0 else None,
                discard=expires <= 0,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": bool(item.get("httpOnly", False))},
                rfc2109=False,
            )
            if cookie.name:
                self.cookie_jar.set_cookie(cookie)
        if not list(self.cookie_jar):
            raise WebSessionExpiredError("Strava web session contains no usable cookies")

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        request_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }
        request_headers.update(headers or {})
        attempts = 3
        for attempt in range(1, attempts + 1):
            request = Request(url, data=body, headers=request_headers, method=method)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    content = response.read()
                    final_url = response.geturl()
                break
            except HTTPError as exc:
                if exc.code == 429:
                    raise RateLimitError("Strava web upload rate limited; rerun later") from exc
                if exc.code in (401, 403):
                    raise WebSessionExpiredError(
                        "Strava web session expired; run with --web-login"
                    ) from exc
                if exc.code not in (408, 425, 500, 502, 503, 504) or attempt == attempts:
                    raise SyncError(
                        f"Strava web request failed: HTTP {exc.code} {exc.reason}"
                    ) from exc
                reason = f"HTTP {exc.code} {exc.reason}"
            except (URLError, TimeoutError, ssl.SSLError, ConnectionError) as exc:
                reason = network_error_text(exc)
                if attempt == attempts:
                    raise TransientNetworkError(
                        f"Strava web request failed after {attempts} attempts: {reason}"
                    ) from exc
            delay = min(8, 2 ** (attempt - 1))
            print(
                f"warning: Strava web request failed ({attempt}/{attempts}): "
                f"{reason}; retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
        if urlparse(final_url).path.startswith("/login"):
            raise WebSessionExpiredError("Strava web session expired; run with --web-login")
        return content, final_url

    def fetch_csrf_token(self) -> str:
        content, _ = self._request("GET", self.select_url)
        try:
            page = content.decode("utf-8")
        except UnicodeError as exc:
            raise SyncError("Strava upload page is not UTF-8") from exc
        parser = CsrfMetaParser()
        parser.feed(page)
        if not parser.token:
            raise WebSessionExpiredError(
                "Cannot find Strava CSRF token; refresh login with --web-login"
            )
        return parser.token

    def activities(self, _after: int, _before: int) -> list[dict[str, Any]]:
        return []

    def create_upload_batch(
        self, files: list[tuple[Path, str, str]]
    ) -> list[dict[str, Any]]:
        if not files or len(files) > 15:
            raise SyncError("Strava web batch must contain 1 to 15 files")
        boundary = f"----onelap-{uuid.uuid4().hex}"
        chunks: list[bytes] = []

        def field(name: str, value: str) -> None:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("_method", "POST")
        field("authenticity_token", self._csrf_token)
        for fit_path, _external_id, _name in files:
            safe_name = fit_path.name.replace('"', "_")
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        f'Content-Disposition: form-data; name="files[]"; filename="{safe_name}"\r\n'
                        "Content-Type: application/octet-stream\r\n\r\n"
                    ).encode("utf-8"),
                    fit_path.read_bytes(),
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        content, _ = self._request(
            "POST",
            self.upload_url,
            body=b"".join(chunks),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-CSRF-Token": self._csrf_token,
                "Referer": self.select_url,
            },
        )
        try:
            result = json.loads(content.decode("utf-8"))
            if not isinstance(result, list) or len(result) != len(files):
                raise TypeError("response count does not match file count")
            uploads = [
                {
                    "id": payload["id"],
                    "id_str": str(payload["id"]),
                    "workflow": payload.get("workflow", "pending"),
                }
                for payload in result
            ]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SyncError("Strava web upload response is invalid") from exc
        return uploads

    def create_upload(self, fit_path: Path, external_id: str, name: str) -> dict[str, Any]:
        return self.create_upload_batch([(fit_path, external_id, name)])[0]

    def wait_for_uploads(
        self, upload_ids: list[str], wait_seconds: int = 30
    ) -> dict[str, dict[str, Any]]:
        if not upload_ids or len(upload_ids) > 15:
            raise SyncError("Strava web poll batch must contain 1 to 15 upload IDs")
        upload_ids = [str(value) for value in upload_ids]
        pending = set(upload_ids)
        results: dict[str, dict[str, Any]] = {
            value: {"id": value, "id_str": value} for value in upload_ids
        }
        deadline = time.monotonic() + wait_seconds
        while pending:
            query = urlencode([("ids[]", value) for value in upload_ids])
            content, _ = self._request(
                "GET", self.progress_url + "?" + query, headers={"Referer": self.select_url}
            )
            try:
                payloads = json.loads(content.decode("utf-8"))
                if not isinstance(payloads, list):
                    raise TypeError("response is not a list")
            except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
                raise SyncError("Strava web upload status response is invalid") from exc
            for payload in payloads:
                upload_id = str(payload.get("id", ""))
                if upload_id not in results:
                    continue
                workflow = str(payload.get("workflow", ""))
                current = {
                    "id": upload_id,
                    "id_str": upload_id,
                    "status": workflow,
                    "error": payload.get("error"),
                }
                if workflow in {"complete", "done", "success"}:
                    current["activity_id"] = payload.get("activity_id") or payload.get("id")
                    current["error"] = None
                    pending.discard(upload_id)
                elif workflow == "error" or payload.get("error"):
                    pending.discard(upload_id)
                results[upload_id] = current
            if not pending or time.monotonic() >= deadline:
                return results
            time.sleep(2)
        return results

    def wait_for_upload(self, upload_id: str, wait_seconds: int = 30) -> dict[str, Any]:
        return self.wait_for_uploads([upload_id], wait_seconds)[str(upload_id)]


class StravaClient:
    def __init__(self, config_path: Path, timeout: float):
        self.config_path = config_path
        self.timeout = timeout
        if not config_path.is_file():
            raise SyncError("Strava authorization is missing; run: python sync_to_strava.py --authorize")
        self.config = load_json(config_path)
        self._apply_environment()
        scopes = set(str(self.config.get("scope", "")).replace(",", " ").split())
        if scopes and not REQUIRED_SCOPES.issubset(scopes):
            raise SyncError("Strava token lacks activity:read_all or activity:write; run --authorize")
        self.ensure_token()

    def _apply_environment(self) -> None:
        for field, variable in (
            ("client_id", "STRAVA_CLIENT_ID"),
            ("client_secret", "STRAVA_CLIENT_SECRET"),
            ("refresh_token", "STRAVA_REFRESH_TOKEN"),
        ):
            if os.environ.get(variable):
                self.config[field] = os.environ[variable]

    def ensure_token(self, force: bool = False) -> None:
        access_token = str(self.config.get("access_token", ""))
        expires_at = int(self.config.get("expires_at", 0) or 0)
        if not force and access_token and expires_at > int(time.time()) + 3600:
            return
        required = ("client_id", "client_secret", "refresh_token")
        if any(not self.config.get(field) for field in required):
            raise SyncError("Incomplete Strava config; run with --authorize")
        result = oauth_token(
            {
                "client_id": str(self.config["client_id"]),
                "client_secret": str(self.config["client_secret"]),
                "refresh_token": str(self.config["refresh_token"]),
                "grant_type": "refresh_token",
            },
            self.timeout,
        )
        self.config.update(
            {
                "access_token": result["access_token"],
                "refresh_token": result["refresh_token"],
                "expires_at": int(result["expires_at"]),
            }
        )
        save_json(self.config_path, self.config)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        retry_auth: bool = True,
    ) -> Any:
        url = STRAVA_API + path
        if query:
            url += "?" + urlencode(query)
        headers = {"Authorization": f"Bearer {self.config['access_token']}"}
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(url, data=body, headers=headers, method=method)
        try:
            result, _ = http_json(request, self.timeout)
            return result
        except SyncError as exc:
            if retry_auth and "HTTP 401" in str(exc):
                self.ensure_token(force=True)
                return self.request_json(
                    method,
                    path,
                    query=query,
                    body=body,
                    content_type=content_type,
                    retry_auth=False,
                )
            raise

    def activities(self, after: int, before: int) -> list[dict[str, Any]]:
        activities: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request_json(
                "GET",
                "/athlete/activities",
                query={"after": after, "before": before, "page": page, "per_page": 200},
            )
            if not isinstance(batch, list):
                raise SyncError("Strava activity list response is invalid")
            activities.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 200:
                return activities
            page += 1

    def create_upload(self, fit_path: Path, external_id: str, name: str) -> dict[str, Any]:
        boundary = f"----onelap-{uuid.uuid4().hex}"
        chunks: list[bytes] = []

        def field(field_name: str, value: str) -> None:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode("ascii"),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("data_type", "fit")
        field("external_id", external_id)
        field("name", name)
        safe_name = fit_path.name.replace('"', "_")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n"
                ).encode("utf-8"),
                fit_path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            ]
        )
        result = self.request_json(
            "POST",
            "/uploads",
            body=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        if not isinstance(result, dict) or not (result.get("id_str") or result.get("id")):
            raise SyncError("Strava upload response is invalid")
        return result

    def wait_for_upload(self, upload_id: str, wait_seconds: int = 30) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        while True:
            result = self.request_json("GET", f"/uploads/{upload_id}")
            if not isinstance(result, dict):
                raise SyncError("Strava upload status response is invalid")
            if result.get("activity_id") or result.get("error"):
                return result
            if time.monotonic() >= deadline:
                return result
            time.sleep(1)


def onelap_records(headers: dict[str, str], timeout: float) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    page = 1
    expected = None
    while True:
        query = urlencode(
            {
                "end_time": int(time.time()),
                "page": page,
                "size": 200,
                "source": "all",
                "start_time": 1451577600,
                "time_type": "all",
            }
        )
        result = onelap.api_json(
            f"https://{onelap.API_HOST}{onelap.LIST_PATH}?{query}", headers, timeout
        )
        data = result.get("data", {})
        batch = data.get("list", [])
        expected = int(data.get("count", expected or 0))
        for record in batch:
            record_id = str(record.get("id", ""))
            if record_id:
                records[record_id] = record
        if not batch or len(records) >= expected or len(batch) < 200:
            break
        page += 1
    return list(records.values())


def iso_epoch(value: str) -> int | None:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def matching_activity(
    record: dict[str, Any], activities_by_time: dict[int, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    started = int(record.get("start_time", 0))
    distance = float(record.get("total_distance", 0) or 0)
    duration = int(record.get("total_time", 0) or 0)
    for delta in range(-3, 4):
        for activity in activities_by_time.get(started + delta, []):
            activity_distance = float(activity.get("distance", 0) or 0)
            activity_duration = int(activity.get("moving_time", 0) or 0)
            distance_ok = abs(activity_distance - distance) <= max(100.0, distance * 0.01)
            duration_ok = abs(activity_duration - duration) <= max(60, duration * 0.02)
            if distance_ok and duration_ok:
                return activity
    return None


def external_id_set(activities: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for activity in activities:
        value = str(activity.get("external_id", "")).strip()
        if value:
            result.add(Path(value).name.lower())
    return result


def status_from_upload(result: dict[str, Any]) -> tuple[str, str | None]:
    activity_id = result.get("activity_id")
    if activity_id:
        return "uploaded", str(activity_id)
    error = str(result.get("error", ""))
    if "duplicate" in error.lower():
        match = re.search(r"activity\s+(\d+)", error, re.IGNORECASE)
        return "duplicate", match.group(1) if match else None
    if error:
        raise SyncError(f"Strava rejected upload: {error}")
    return "pending", None


def record_label(record: dict[str, Any]) -> str:
    started_at = time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(int(record.get("start_time", 0)))
    )
    distance_km = float(record.get("total_distance", 0) or 0) / 1000
    duration_seconds = int(record.get("total_time", 0) or 0)
    hours, remainder = divmod(duration_seconds, 3600)
    minutes = remainder // 60
    duration = f"{hours:d}h{minutes:02d}m"
    name = str(record.get("name") or record.get("id") or "unnamed")
    return f"{started_at} | {distance_km:6.1f} km | {duration:>6} | {name}"


def progress_label(index: int, total: int) -> str:
    width = len(str(total))
    percent = index / total * 100 if total else 100
    return f"[{index:>{width}}/{total} {percent:5.1f}%]"


def fetch_onelap_records(
    args: argparse.Namespace,
) -> tuple[dict[str, str] | onelap.OneLapOtmClient, str, list[dict[str, Any]]]:
    direct_path = Path(args.onelap_auth)
    if direct_path.is_file():
        direct = onelap.OneLapOtmClient(direct_path, args.timeout)
        records = direct.records()
        if not records:
            raise SyncError("Onelap returned no activity records")
        return direct, direct.source, records

    headers, auth_source = onelap.obtain_auth(
        Path(args.onelap_token_cache), args.har, args.login_har, args.timeout
    )
    try:
        records = onelap_records(headers, args.timeout)
    except onelap.AuthenticationError:
        headers, auth_source = onelap.obtain_auth(
            Path(args.onelap_token_cache),
            args.har,
            args.login_har,
            args.timeout,
            force_login=True,
        )
        records = onelap_records(headers, args.timeout)
    if not records:
        raise SyncError("Onelap returned no activity records")
    return headers, auth_source, records


def onelap_fit_info(
    source: dict[str, str] | onelap.OneLapOtmClient,
    record: dict[str, Any],
    timeout: float,
) -> tuple[str, str | None]:
    if isinstance(source, onelap.OneLapOtmClient):
        return onelap.otm_fit_filename(record), None
    url, filename = onelap.fit_link(str(record["id"]), source, timeout)
    return filename, url


def download_onelap_fit(
    source: dict[str, str] | onelap.OneLapOtmClient,
    record: dict[str, Any],
    reference: str | None,
    target: Path,
    timeout: float,
) -> str:
    if isinstance(source, onelap.OneLapOtmClient):
        return source.download_record_fit(
            str(record["id"]),
            target,
            force=False,
            fit_reference=str(record.get("fit_reference") or "") or None,
        )
    if not reference:
        raise SyncError("Onelap FIT download URL is missing")
    return onelap.download_fit(reference, target, timeout, force=False)


def mark_uploaded_before(args: argparse.Namespace, date_text: str) -> None:
    try:
        cutoff = datetime.strptime(date_text, "%Y-%m-%d").replace(
            tzinfo=timezone(timedelta(hours=8))
        )
    except ValueError as exc:
        raise SyncError("Upload cutoff must use YYYY-MM-DD format") from exc
    cutoff_epoch = int(cutoff.timestamp())
    _, _, records = fetch_onelap_records(args)
    state_path = Path(args.state)
    state = load_json(state_path, {"version": 1, "records": {}})
    synced = state.setdefault("records", {})
    if not isinstance(synced, dict):
        raise SyncError("Sync state records field is invalid")

    newly_marked = 0
    for record in records:
        if int(record.get("start_time", 0)) >= cutoff_epoch:
            continue
        record_id = str(record["id"])
        current = synced.get(record_id, {})
        if current.get("status") in FINAL_STATUSES:
            continue
        synced[record_id] = {
            "status": "matched",
            "source": "confirmed_before_cutoff",
            "cutoff": date_text,
            "updated_at": int(time.time()),
        }
        newly_marked += 1
    state["confirmed_uploaded_before"] = {
        "date": date_text,
        "timezone": "Asia/Shanghai",
        "updated_at": int(time.time()),
    }
    save_json(state_path, state)
    known_uploaded = sum(
        1
        for record in records
        if synced.get(str(record["id"]), {}).get("status") in FINAL_STATUSES
    )
    print(
        f"marked_now={newly_marked} known_uploaded={known_uploaded} "
        f"remaining={len(records) - known_uploaded} state={state_path.resolve()}"
    )


def sync_web_batches(
    client: StravaWebClient,
    remaining: list[dict[str, Any]],
    records: list[dict[str, Any]],
    synced: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    onelap_source: dict[str, str] | onelap.OneLapOtmClient,
    external_ids: set[str],
    output_dir: Path,
    args: argparse.Namespace,
) -> int:
    index_by_id = {str(record["id"]): index for index, record in enumerate(remaining, 1)}
    submitted = 0
    uploaded_count = 0
    duplicate_count = 0
    skipped_count = 0
    failed = 0

    def prefix_for(record: dict[str, Any]) -> str:
        return progress_label(index_by_id[str(record["id"])], len(remaining))

    def fail_record(record: dict[str, Any], error: Exception) -> None:
        nonlocal failed
        failed += 1
        record_id = str(record["id"])
        synced[record_id] = {
            "status": "error",
            "error": str(error),
            "updated_at": int(time.time()),
        }
        save_json(state_path, state)
        print(f"{prefix_for(record)} ERROR     | {record_label(record)} | {error}", file=sys.stderr)

    def finalize(record: dict[str, Any], result: dict[str, Any]) -> None:
        nonlocal uploaded_count, duplicate_count, failed
        try:
            status, activity_id = status_from_upload(result)
        except SyncError as exc:
            fail_record(record, exc)
            return
        record_id = str(record["id"])
        synced[record_id] = {
            "status": status,
            "activity_id": activity_id,
            "upload_id": str(result.get("id_str") or result.get("id") or ""),
            "updated_at": int(time.time()),
        }
        save_json(state_path, state)
        prefix = prefix_for(record)
        label = record_label(record)
        if status == "uploaded":
            uploaded_count += 1
            print(f"{prefix} UPLOADED  | {label} | Strava id={activity_id or '-'}")
        elif status == "duplicate":
            duplicate_count += 1
            print(f"{prefix} DUPLICATE | {label} | already on Strava")
        else:
            print(f"{prefix} PENDING   | {label} | upload id={result.get('id_str') or '-'}")

    pending_records = [
        record
        for record in remaining
        if synced.get(str(record["id"]), {}).get("status") == "pending"
        and synced.get(str(record["id"]), {}).get("upload_id")
    ]
    for offset in range(0, len(pending_records), 15):
        batch = pending_records[offset : offset + 15]
        upload_ids = [str(synced[str(record["id"])]["upload_id"]) for record in batch]
        results = client.wait_for_uploads(upload_ids)
        for record, upload_id in zip(batch, upload_ids):
            finalize(record, results[upload_id])

    candidates = [
        record
        for record in remaining
        if synced.get(str(record["id"]), {}).get("status") not in FINAL_STATUSES
        and synced.get(str(record["id"]), {}).get("status") != "pending"
    ]
    cursor = 0
    batch_number = 0
    while cursor < len(candidates) and (not args.max_uploads or submitted < args.max_uploads):
        allowance = 15
        if args.max_uploads:
            allowance = min(allowance, args.max_uploads - submitted)
        selected = candidates[cursor : cursor + allowance]
        cursor += len(selected)
        prepared: list[tuple[dict[str, Any], Path, str, str]] = []
        for record in selected:
            record_id = str(record["id"])
            try:
                filename, reference = onelap_fit_info(onelap_source, record, args.timeout)
                expected_external_id = f"onelap-{record_id}.fit"
                if filename.lower() in external_ids or expected_external_id.lower() in external_ids:
                    synced[record_id] = {
                        "status": "matched",
                        "activity_id": None,
                        "updated_at": int(time.time()),
                    }
                    save_json(state_path, state)
                    skipped_count += 1
                    print(
                        f"{prefix_for(record)} SKIPPED   | {record_label(record)} | already exists"
                    )
                    continue
                fit_path = output_dir / filename
                download_onelap_fit(
                    onelap_source, record, reference, fit_path, args.timeout
                )
                prepared.append(
                    (
                        record,
                        fit_path,
                        expected_external_id,
                        str(record.get("name") or fit_path.stem),
                    )
                )
            except (SyncError, onelap.DownloadError) as exc:
                fail_record(record, exc)
        if not prepared:
            continue

        batch_number += 1
        print(f"Batch {batch_number} | uploading {len(prepared)} files in one request")
        upload_pairs: list[tuple[tuple[dict[str, Any], Path, str, str], dict[str, Any]]] = []
        try:
            uploads = client.create_upload_batch(
                [(fit_path, external_id, name) for _, fit_path, external_id, name in prepared]
            )
            submitted += len(prepared)
            upload_pairs = list(zip(prepared, uploads))
        except TransientNetworkError as exc:
            if len(prepared) == 1:
                fail_record(prepared[0][0], exc)
                continue
            print(
                f"Batch {batch_number} | batch upload failed after retries; "
                "retrying files individually",
                file=sys.stderr,
            )
            for item in prepared:
                record, fit_path, external_id, name = item
                try:
                    upload = client.create_upload(fit_path, external_id, name)
                except TransientNetworkError as item_exc:
                    fail_record(record, item_exc)
                    continue
                submitted += 1
                upload_pairs.append((item, upload))
        if not upload_pairs:
            continue
        upload_ids: list[str] = []
        for (record, _fit_path, _external_id, _name), upload in upload_pairs:
            upload_id = str(upload.get("id_str") or upload["id"])
            upload_ids.append(upload_id)
            synced[str(record["id"])] = {
                "status": "pending",
                "upload_id": upload_id,
                "updated_at": int(time.time()),
            }
        save_json(state_path, state)
        results = client.wait_for_uploads(upload_ids)
        for ((record, _fit_path, _external_id, _name), _upload), upload_id in zip(
            upload_pairs, upload_ids
        ):
            finalize(record, results[upload_id])

    remaining_after = sum(
        1
        for record in records
        if synced.get(str(record["id"]), {}).get("status") not in FINAL_STATUSES
    )
    pending_count = sum(
        1
        for record in records
        if synced.get(str(record["id"]), {}).get("status") == "pending"
    )
    print(
        "Run result | "
        f"submitted={submitted} | uploaded={uploaded_count} | duplicates={duplicate_count} | "
        f"skipped={skipped_count} | pending={pending_count} | failed={failed} | "
        f"remaining={remaining_after}"
    )
    return failed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-login", action="store_true", help="log in to Strava web and save cookies")
    parser.add_argument("--import-strava-har", help="import Strava cookies from an Edge HAR")
    parser.add_argument(
        "--import-web-cookie",
        action="store_true",
        help="securely prompt for a Strava Cookie request header",
    )
    parser.add_argument("--web-session", default=STRAVA_WEB_SESSION)
    parser.add_argument(
        "--strava-mode",
        choices=("web", "api"),
        default="web",
        help="Strava upload method (default: web)",
    )
    parser.add_argument("--authorize", action="store_true", help="authorize Strava API mode and exit")
    parser.add_argument("--callback-port", type=int, default=8765)
    parser.add_argument("--strava-config", default=STRAVA_CONFIG)
    parser.add_argument("--state", default=SYNC_STATE)
    parser.add_argument(
        "--mark-uploaded-before",
        metavar="YYYY-MM-DD",
        help="mark all earlier Onelap records as already uploaded and exit",
    )
    parser.add_argument("--max-uploads", type=int, default=15, help="uploads per run; 0 means unlimited")
    parser.add_argument("--newest-first", action="store_true", help="process newest records first")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", default="fits")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--har", help="HAR containing an authenticated Onelap request")
    parser.add_argument("--login-har", help="HAR containing the Onelap login request")
    parser.add_argument(
        "--onelap-auth",
        default=onelap.DIRECT_AUTH_CACHE,
        help=f"OneLap account login file (default: {onelap.DIRECT_AUTH_CACHE})",
    )
    parser.add_argument("--onelap-token-cache", default=onelap.TOKEN_CACHE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.import_strava_har:
            import_strava_har(Path(args.import_strava_har), Path(args.web_session), args.timeout)
            return 0
        if args.import_web_cookie:
            cookie_header = getpass.getpass("Paste Strava Cookie request header: ")
            save_cookie_header_session(cookie_header, Path(args.web_session), args.timeout)
            return 0
        if args.web_login:
            capture_strava_web_session(Path(args.web_session), args.timeout)
            return 0
        strava_config = Path(args.strava_config)
        if args.authorize:
            authorize_strava(strava_config, args.callback_port, args.timeout)
            return 0
        if args.mark_uploaded_before:
            mark_uploaded_before(args, args.mark_uploaded_before)
            return 0

        if args.strava_mode == "web":
            client: StravaClient | StravaWebClient = StravaWebClient(
                Path(args.web_session), args.timeout
            )
        else:
            client = StravaClient(strava_config, args.timeout)
        onelap_source, auth_source, records = fetch_onelap_records(args)

        earliest = min(int(record.get("start_time", 0)) for record in records) - 86400
        activities = client.activities(earliest, int(time.time()) + 86400)
        by_time: dict[int, list[dict[str, Any]]] = {}
        for activity in activities:
            started = iso_epoch(str(activity.get("start_date", "")))
            if started is not None:
                by_time.setdefault(started, []).append(activity)
        external_ids = external_id_set(activities)

        state_path = Path(args.state)
        state = load_json(state_path, {"version": 1, "records": {}})
        synced = state.setdefault("records", {})
        if not isinstance(synced, dict):
            raise SyncError("Sync state records field is invalid")

        matched = 0
        for record in records:
            record_id = str(record["id"])
            current = synced.get(record_id, {})
            if current.get("status") in FINAL_STATUSES:
                continue
            activity = matching_activity(record, by_time)
            if activity:
                synced[record_id] = {
                    "status": "matched",
                    "activity_id": str(activity.get("id", "")),
                    "updated_at": int(time.time()),
                }
                matched += 1
        if matched and not args.dry_run:
            save_json(state_path, state)

        remaining = [
            record
            for record in records
            if synced.get(str(record["id"]), {}).get("status") not in FINAL_STATUSES
        ]
        remaining.sort(
            key=lambda item: int(item.get("start_time", 0)), reverse=args.newest_first
        )
        strava_count = str(len(activities)) if args.strava_mode == "api" else "web-session"
        known_uploaded = len(records) - len(remaining)
        print(
            f"Sync plan | total={len(records)} | known uploaded={known_uploaded} | "
            f"queued={len(remaining)} | batch limit={args.max_uploads or 'unlimited'}"
        )
        print(
            f"Connection | Strava={strava_count} | Onelap auth={auth_source} | "
            f"mode={args.strava_mode} | matched now={matched}"
        )
        if args.dry_run:
            return 0

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(client, StravaWebClient):
            failed = sync_web_batches(
                client,
                remaining,
                records,
                synced,
                state,
                state_path,
                onelap_source,
                external_ids,
                output_dir,
                args,
            )
            return 0 if failed == 0 else 2

        submitted = 0
        uploaded_count = 0
        duplicate_count = 0
        skipped_count = 0
        pending_count = 0
        failed = 0
        for index, record in enumerate(remaining, 1):
            record_id = str(record["id"])
            current = synced.get(record_id, {})
            prefix = progress_label(index, len(remaining))
            label = record_label(record)
            try:
                if current.get("status") == "pending" and current.get("upload_id"):
                    result = client.wait_for_upload(str(current["upload_id"]))
                    status, activity_id = status_from_upload(result)
                else:
                    if args.max_uploads and submitted >= args.max_uploads:
                        break
                    filename, reference = onelap_fit_info(
                        onelap_source, record, args.timeout
                    )
                    expected_external_id = f"onelap-{record_id}.fit"
                    if filename.lower() in external_ids or expected_external_id.lower() in external_ids:
                        synced[record_id] = {
                            "status": "matched",
                            "activity_id": None,
                            "updated_at": int(time.time()),
                        }
                        save_json(state_path, state)
                        skipped_count += 1
                        print(f"{prefix} SKIPPED   | {label} | already exists")
                        continue
                    fit_path = output_dir / filename
                    download_onelap_fit(
                        onelap_source, record, reference, fit_path, args.timeout
                    )
                    upload = client.create_upload(
                        fit_path,
                        expected_external_id,
                        str(record.get("name") or fit_path.stem),
                    )
                    submitted += 1
                    upload_id = str(upload.get("id_str") or upload["id"])
                    synced[record_id] = {
                        "status": "pending",
                        "upload_id": upload_id,
                        "filename": filename,
                        "updated_at": int(time.time()),
                    }
                    save_json(state_path, state)
                    result = client.wait_for_upload(upload_id)
                    status, activity_id = status_from_upload(result)

                synced[record_id] = {
                    "status": status,
                    "activity_id": activity_id,
                    "upload_id": str(result.get("id_str") or result.get("id") or ""),
                    "updated_at": int(time.time()),
                }
                save_json(state_path, state)
                if status == "pending":
                    pending_count += 1
                    print(f"{prefix} PENDING   | {label} | upload id={result.get('id_str') or '-'}")
                elif status == "uploaded":
                    uploaded_count += 1
                    print(f"{prefix} UPLOADED  | {label} | Strava id={activity_id or '-'}")
                elif status == "duplicate":
                    duplicate_count += 1
                    print(f"{prefix} DUPLICATE | {label} | already on Strava")
                else:
                    print(f"{prefix} {status.upper():<10} | {label}")
            except (RateLimitError, WebSessionExpiredError):
                raise
            except (SyncError, onelap.DownloadError) as exc:
                failed += 1
                synced[record_id] = {
                    "status": "error",
                    "error": str(exc),
                    "updated_at": int(time.time()),
                }
                save_json(state_path, state)
                print(f"{prefix} ERROR     | {label} | {exc}", file=sys.stderr)

        remaining_after = sum(
            1
            for record in records
            if synced.get(str(record["id"]), {}).get("status") not in FINAL_STATUSES
        )
        print(
            "Run result | "
            f"submitted={submitted} | uploaded={uploaded_count} | duplicates={duplicate_count} | "
            f"skipped={skipped_count} | pending={pending_count} | failed={failed} | "
            f"remaining={remaining_after}"
        )
        return 0 if failed == 0 else 2
    except (SyncError, onelap.DownloadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
