#!/usr/bin/env python3
"""Download the newest cycling FIT file using credentials captured in a HAR."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


API_HOST = "rfs-fitness.rfsvr.net"
LOGIN_PATH = "/api/account/v1/login"
LIST_PATH = "/indoor/v2/app/record/list"
FIT_PATH_PREFIX = "/indoor/v1/app/data/riding/share/"
DROP_HEADERS = {"host", "accept-encoding", "connection", "content-length"}
TOKEN_CACHE = ".onelap_token.json"
DIRECT_AUTH_CACHE = ".onelap_auth.json"
WEB_BASE_URL = "https://www.onelap.cn"
OTM_BASE_URL = "https://otm.onelap.cn"
OTM_FALLBACK_BASE_URL = "https://u.onelap.cn"


class DownloadError(RuntimeError):
    pass


class AuthenticationError(DownloadError):
    pass


class ApiRequestError(DownloadError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _load_direct_auth(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AuthenticationError("OneLap account login is not configured")
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("OneLap account login file is invalid") from exc
    if not isinstance(value, dict):
        raise AuthenticationError("OneLap account login file is invalid")
    if not str(value.get("account") or "").strip() or not re.fullmatch(
        r"[0-9a-f]{32}", str(value.get("password_md5") or "")
    ):
        raise AuthenticationError("OneLap account login file is incomplete")
    return value


def direct_auth_configured(path: Path) -> bool:
    try:
        value = _load_direct_auth(path)
    except AuthenticationError:
        return False
    return bool(value.get("token") and value.get("refresh_token"))


def _multipart_body(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----Magene2Strava{os.getpid():x}{time.time_ns():x}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("msg") or payload.get("message") or payload.get("error") or "")


def _response_cookies(response: Any) -> str:
    headers = getattr(response, "headers", None)
    values = headers.get_all("Set-Cookie") if headers and hasattr(headers, "get_all") else []
    parsed = SimpleCookie()
    for value in values or []:
        try:
            parsed.load(value)
        except Exception:
            continue
    return "; ".join(f"{name}={item.value}" for name, item in parsed.items())


def _open_json(
    request: Request,
    timeout: float,
    context: str,
    cookie_sink: list[str] | None = None,
) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            cookie_header = _response_cookies(response)
            if cookie_sink is not None and cookie_header:
                cookie_sink[:] = [cookie_header]
    except HTTPError as exc:
        detail = _error_detail(exc.read(4096))
        exc.close()
        suffix = f": {detail}" if detail else ""
        if exc.code in (401, 403):
            raise AuthenticationError(f"{context} authentication failed (HTTP {exc.code})") from exc
        raise ApiRequestError(f"{context} failed (HTTP {exc.code}){suffix}", exc.code) from exc
    except URLError as exc:
        raise ApiRequestError(f"{context} failed: {exc.reason}") from exc
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ApiRequestError(f"{context} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ApiRequestError(f"{context} returned an invalid response")
    return value


def _auth_fields(payload: dict[str, Any], *, login: bool) -> tuple[str, str]:
    success_codes = {0, 200} if login else {200}
    if payload.get("code") not in success_codes:
        message = str(
            payload.get("msg") or payload.get("message") or payload.get("error") or "unknown"
        )
        raise AuthenticationError(f"OneLap authentication failed: {message}")
    raw_data = payload.get("data")
    if isinstance(raw_data, list) and raw_data and isinstance(raw_data[0], dict):
        data = raw_data[0]
    elif isinstance(raw_data, dict):
        data = raw_data
    else:
        data = {}
    token = str(data.get("token") or "").strip()
    refresh_token = str(data.get("refresh_token") or "").strip()
    if not token or (login and not refresh_token):
        raise AuthenticationError("OneLap authentication response is missing token fields")
    return token, refresh_token


def configure_direct_auth(
    path: Path, account: str, password: str, timeout: float
) -> "OneLapOtmClient":
    normalized_account = account.strip()
    if not normalized_account or len(normalized_account) > 200:
        raise AuthenticationError("OneLap account is empty or too long")
    if not password or len(password) > 512:
        raise AuthenticationError("OneLap password is empty or too long")
    password_md5 = hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()
    body, content_type = _multipart_body(
        {"account": normalized_account, "password": password_md5}
    )
    captured_cookies: list[str] = []
    payload = _open_json(
        Request(
            f"{WEB_BASE_URL}/api/login",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": content_type,
                "User-Agent": "Magene2Strava/1.0",
            },
            method="POST",
        ),
        timeout,
        "OneLap login",
        captured_cookies,
    )
    token, refresh_token = _auth_fields(payload, login=True)
    _private_json(
        path,
        {
            "version": 1,
            "account": normalized_account,
            "password_md5": password_md5,
            "token": token,
            "refresh_token": refresh_token,
            "cookie_header": captured_cookies[0] if captured_cookies else "",
            "updated_at": int(time.time()),
        },
    )
    return OneLapOtmClient(path, timeout)


def _epoch(value: Any) -> int:
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number / 1000 if number > 10_000_000_000 else number)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        number = float(text)
        return int(number / 1000 if number > 10_000_000_000 else number)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return int(parsed.timestamp())
    except ValueError:
        return 0


def _numeric(record: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = record.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _positive_numeric(record: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = record.get(key)
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


def normalize_otm_record(record: dict[str, Any]) -> dict[str, Any] | None:
    record_id = str(record.get("id") or record.get("activity_id") or "").strip()
    started = _epoch(
        record.get("start_time")
        or record.get("start_riding_time")
        or record.get("created_at")
    )
    if not record_id or not started:
        return None
    distance = _positive_numeric(
        record, "total_distance", "totalDistance", "distance", "distance_m"
    )
    if distance <= 0:
        distance = _positive_numeric(record, "distance_km") * 1000
    return {
        "id": record_id,
        "name": str(record.get("name") or record.get("title") or "骑行训练"),
        "start_time": started,
        "total_distance": distance,
        "total_time": int(
            _positive_numeric(
                record,
                "total_time",
                "totalTime",
                "time",
                "duration",
                "duration_s",
                "time_seconds",
            )
        ),
        "elevation": _numeric(
            record, "elevation", "total_ascent", "totalAscent", "ascent"
        ),
        "cal": _numeric(record, "cal", "calories", "kcal"),
        "TSS": _numeric(record, "TSS", "tss", "load_tss"),
        "source": "onelap-otm",
    }


class OneLapOtmClient:
    def __init__(self, auth_path: Path, timeout: float):
        self.auth_path = auth_path
        self.timeout = timeout
        self.auth = _load_direct_auth(auth_path)

    @property
    def source(self) -> str:
        return "account"

    def _persist(self) -> None:
        self.auth["updated_at"] = int(time.time())
        _private_json(self.auth_path, self.auth)

    def _login(self) -> None:
        body, content_type = _multipart_body(
            {
                "account": str(self.auth["account"]),
                "password": str(self.auth["password_md5"]),
            }
        )
        captured_cookies: list[str] = []
        payload = _open_json(
            Request(
                f"{WEB_BASE_URL}/api/login",
                data=body,
                headers={"Accept": "application/json", "Content-Type": content_type},
                method="POST",
            ),
            self.timeout,
            "OneLap login",
            captured_cookies,
        )
        token, refresh_token = _auth_fields(payload, login=True)
        self.auth["token"] = token
        self.auth["refresh_token"] = refresh_token
        if captured_cookies:
            self.auth["cookie_header"] = captured_cookies[0]
        self._persist()

    def _refresh(self) -> bool:
        refresh_token = str(self.auth.get("refresh_token") or "").strip()
        if not refresh_token:
            return False
        body = json.dumps(
            {"token": refresh_token, "from": "web", "to": "web"}, separators=(",", ":")
        ).encode("utf-8")
        try:
            request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
            cookie_header = str(self.auth.get("cookie_header") or "").strip()
            if cookie_header:
                request_headers["Cookie"] = cookie_header
            captured_cookies: list[str] = []
            payload = _open_json(
                Request(
                    f"{WEB_BASE_URL}/api/token",
                    data=body,
                    headers=request_headers,
                    method="POST",
                ),
                self.timeout,
                "OneLap token refresh",
                captured_cookies,
            )
            token, refreshed = _auth_fields(payload, login=False)
        except (AuthenticationError, ApiRequestError):
            return False
        self.auth["token"] = token
        if refreshed:
            self.auth["refresh_token"] = refreshed
        if captured_cookies:
            self.auth["cookie_header"] = captured_cookies[0]
        self._persist()
        return True

    def _send(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        headers = {
            "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8",
            "Authorization": str(self.auth.get("token") or ""),
            "User-Agent": "Magene2Strava/1.0",
        }
        if content_type:
            headers["Content-Type"] = content_type
        cookie_header = str(self.auth.get("cookie_header") or "").strip()
        if cookie_header:
            headers["Cookie"] = cookie_header
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as exc:
            detail = _error_detail(exc.read(4096))
            exc.close()
            if exc.code in (401, 403):
                raise AuthenticationError(
                    f"OneLap OTM authentication failed (HTTP {exc.code})"
                ) from exc
            suffix = f": {detail}" if detail else ""
            raise ApiRequestError(
                f"OneLap OTM request failed (HTTP {exc.code}){suffix}", exc.code
            ) from exc
        except URLError as exc:
            raise ApiRequestError(f"OneLap OTM request failed: {exc.reason}") from exc
        if body.lstrip().startswith(b"{"):
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("code") in (401, 403):
                raise AuthenticationError("OneLap OTM rejected the saved token")
        return body

    def _authorized(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        try:
            return self._send(method, url, data=data, content_type=content_type)
        except AuthenticationError:
            pass
        if self._refresh():
            try:
                return self._send(method, url, data=data, content_type=content_type)
            except AuthenticationError:
                pass
        self._login()
        return self._send(method, url, data=data, content_type=content_type)

    def _authorized_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        raw = self._authorized(
            method,
            f"{OTM_BASE_URL}{path}",
            data=body,
            content_type="application/json" if body is not None else None,
        )

        def decode(value: bytes) -> dict[str, Any]:
            try:
                decoded = json.loads(value.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ApiRequestError("OneLap OTM returned invalid JSON") from exc
            if not isinstance(decoded, dict):
                raise ApiRequestError("OneLap OTM returned an invalid response")
            return decoded

        value = decode(raw)
        if value.get("code") in (401, 403):
            if self._refresh():
                raw = self._send(
                    method,
                    f"{OTM_BASE_URL}{path}",
                    data=body,
                    content_type="application/json" if body is not None else None,
                )
            else:
                self._login()
                raw = self._send(
                    method,
                    f"{OTM_BASE_URL}{path}",
                    data=body,
                    content_type="application/json" if body is not None else None,
                )
            value = decode(raw)
        try:
            code = int(value.get("code"))
        except (TypeError, ValueError):
            code = -1
        if code != 200:
            message = str(value.get("msg") or value.get("message") or value.get("error") or "unknown")
            normalized_message = message.lower()
            if code == -2 or any(
                marker in normalized_message for marker in ("risk control", "risk_control", "风控")
            ):
                raise ApiRequestError(f"OneLap risk control rejected the request: {message}")
            raise ApiRequestError(f"OneLap OTM returned code={code!r}: {message}")
        return value

    def record_detail(self, record_id: str) -> dict[str, Any]:
        result = self._authorized_json(
            "GET", f"/api/otm/ride_record/analysis/{quote(record_id, safe='')}"
        )
        data = result.get("data")
        if not isinstance(data, dict):
            raise ApiRequestError("OneLap OTM activity detail is invalid")
        riding_record = data.get("ridingRecord")
        if isinstance(riding_record, dict):
            return {**data, **riding_record}
        return data

    def records(
        self,
        page_size: int = 50,
        max_pages: int = 200,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        expected = 0
        for page in range(1, max_pages + 1):
            result = self._authorized_json(
                "POST", "/api/otm/ride_record/list", {"page": page, "limit": page_size}
            )
            data = result.get("data")
            if not isinstance(data, dict):
                raise ApiRequestError("OneLap OTM activity payload is invalid")
            batch = data.get("list")
            if not isinstance(batch, list):
                raise ApiRequestError("OneLap OTM activity list is invalid")
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                normalized = normalize_otm_record(raw)
                if normalized and (
                    normalized["total_distance"] <= 0 or normalized["total_time"] <= 0
                ):
                    try:
                        detail = self.record_detail(str(normalized["id"]))
                        # Detail data contains a device/user numeric `id` which is
                        # not the activity ID used by the list and analysis APIs.
                        # Keep the list record authoritative for identity while
                        # allowing positive detail metrics to replace list zeros.
                        detail_normalized = normalize_otm_record(
                            {
                                **detail,
                                "id": normalized["id"],
                                "start_time": normalized["start_time"],
                            }
                        )
                        if detail_normalized:
                            for key in ("total_distance", "total_time", "elevation", "cal", "TSS"):
                                if detail_normalized[key] > 0:
                                    normalized[key] = detail_normalized[key]
                            fit_reference = str(
                                detail.get("fitUrl") or detail.get("fit_url") or ""
                            ).strip()
                            if fit_reference:
                                normalized["fit_reference"] = fit_reference
                            normalized["details_enriched"] = True
                    except ApiRequestError as exc:
                        if "risk control" in str(exc).lower():
                            raise
                if normalized:
                    records[str(normalized["id"])] = normalized
            for key in ("count", "total"):
                try:
                    expected = max(expected, int(data.get(key) or 0))
                except (TypeError, ValueError):
                    pass
            pagination = data.get("pagination")
            if isinstance(pagination, dict):
                try:
                    expected = max(expected, int(pagination.get("total") or 0))
                except (TypeError, ValueError):
                    pass
            if progress:
                progress(page, len(records), expected)
            # OneLap may cap the response below the requested page size.  A short
            # page therefore does not mean pagination is complete.
            has_more = pagination.get("has_more") if isinstance(pagination, dict) else None
            if not batch or has_more is False or (expected and len(records) >= expected):
                break
        return list(records.values())

    def download_record_fit(
        self,
        record_id: str,
        target: Path,
        force: bool = False,
        fit_reference: str | None = None,
    ) -> str:
        if target.exists() and not force:
            return "exists"
        reference = str(fit_reference or "").strip()
        if not reference:
            detail = self.record_detail(record_id)
            reference = str(
                detail.get("fitUrl") or detail.get("fit_url") or ""
            ).strip()
        if not reference:
            raise ApiRequestError("OneLap activity detail has no FIT reference")
        encoded_reference = quote(
            base64.b64encode(reference.encode("utf-8")).decode("ascii"), safe=""
        )
        path = f"/api/otm/ride_record/analysis/fit_content/{encoded_reference}"
        raw: bytes | None = None
        last_error: ApiRequestError | None = None
        for base_url in (OTM_BASE_URL, OTM_FALLBACK_BASE_URL):
            try:
                raw = self._authorized("GET", f"{base_url}{path}")
                break
            except ApiRequestError as exc:
                last_error = exc
                if base_url != OTM_BASE_URL or not exc.status or exc.status < 500:
                    raise
        if raw is None:
            raise last_error or ApiRequestError("OneLap FIT download failed")
        if raw.lstrip().startswith(b"{"):
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                try:
                    code = int(payload.get("code"))
                except (TypeError, ValueError):
                    code = -1
                message = str(
                    payload.get("msg")
                    or payload.get("message")
                    or payload.get("error")
                    or "unknown"
                )
                normalized_message = message.lower()
                if code == -2 or any(
                    marker in normalized_message
                    for marker in ("risk control", "risk_control", "风控")
                ):
                    raise ApiRequestError(
                        f"OneLap risk control rejected the FIT request: {message}"
                    )
        if len(raw) < 12 or raw[8:12] != b".FIT":
            raise ApiRequestError("OneLap FIT endpoint returned invalid content")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as file:
                file.write(raw)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return "downloaded"


def otm_fit_filename(record: dict[str, Any]) -> str:
    started = int(record.get("start_time") or 0)
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(started))
    record_id = (safe_filename(str(record.get("id") or "activity")) or "activity")[:80]
    return f"ONELAP_{stamp}_{record_id}.fit"


def read_har(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Cannot read {path} as UTF-8 HAR JSON: {exc}") from exc


def har_paths(explicit_path: str | None) -> list[Path]:
    paths: list[Path] = []
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise DownloadError(f"HAR not found: {path}")
        paths.append(path)
    paths.extend(sorted(Path.cwd().glob("*.har"), key=lambda p: p.stat().st_mtime, reverse=True))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def find_har_entry(paths: list[Path], request_path: str) -> tuple[Path, dict[str, Any]] | None:
    matches: list[tuple[str, Path, dict[str, Any]]] = []
    for path in paths:
        har = read_har(path)
        for entry in har.get("log", {}).get("entries", []):
            url = entry.get("request", {}).get("url", "")
            if urlparse(url).path == request_path:
                matches.append((str(entry.get("startedDateTime", "")), path, entry))
    if not matches:
        return None
    _, path, entry = max(matches, key=lambda item: item[0])
    return path, entry


def request_headers(request: dict[str, Any]) -> dict[str, str]:
    return {
        str(item["name"]): str(item["value"])
        for item in request.get("headers", [])
        if item.get("name") and item.get("value") is not None
        and str(item["name"]).lower() not in DROP_HEADERS
    }


def header_value(headers: dict[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def set_header(headers: dict[str, str], name: str, value: str) -> None:
    existing = next((key for key in headers if key.lower() == name.lower()), None)
    if existing:
        del headers[existing]
    headers[name] = value


def jwt_expiry(token: str) -> int | None:
    raw_token = token.removeprefix("Bearer ").strip()
    parts = raw_token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return int(claims["exp"])
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None


def token_is_valid(token: str, leeway: int = 60) -> bool:
    expires_at = jwt_expiry(token)
    return expires_at is not None and expires_at > int(time.time()) + leeway


def authenticated_headers(token: str, uid: str, client_headers: dict[str, str]) -> dict[str, str]:
    headers = dict(client_headers)
    set_header(headers, "Authorization", token)
    set_header(headers, "UserId", uid)
    return headers


def load_cached_auth(cache_path: Path) -> dict[str, str] | None:
    if not cache_path.is_file():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as file:
            cache = json.load(file)
        token = str(cache["token"])
        uid = str(cache["uid"])
        headers = {str(key): str(value) for key, value in cache["client_headers"].items()}
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Invalid token cache {cache_path}: {exc}") from exc
    if not token_is_valid(token):
        return None
    return authenticated_headers(token, uid, headers)


def save_cached_auth(
    cache_path: Path, token: str, uid: str, client_headers: dict[str, str]
) -> None:
    stored_headers = {
        key: value
        for key, value in client_headers.items()
        if key.lower() not in {"authorization", "userid"}
    }
    payload = {
        "version": 1,
        "token": token,
        "uid": uid,
        "expires_at": jwt_expiry(token),
        "client_headers": stored_headers,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=True, indent=2)
            file.write("\n")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, cache_path)
    finally:
        temp.unlink(missing_ok=True)


def response_json(entry: dict[str, Any]) -> dict[str, Any]:
    content = entry.get("response", {}).get("content", {})
    text = str(content.get("text", ""))
    if content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise DownloadError("Cannot decode HAR response body") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DownloadError("HAR response body is not valid JSON") from exc


def auth_from_entry(entry: dict[str, Any]) -> tuple[str, str, dict[str, str]] | None:
    headers = request_headers(entry["request"])
    token = header_value(headers, "Authorization")
    uid = header_value(headers, "UserId")
    if not token or not uid or not token_is_valid(token):
        return None
    return token, uid, headers


def auth_from_login_capture(entry: dict[str, Any]) -> tuple[str, str, dict[str, str]] | None:
    result = response_json(entry)
    data = result.get("data", {})
    token = str(data.get("token", ""))
    uid = str(data.get("uid", ""))
    if result.get("code") != 200 or not token or not uid or not token_is_valid(token):
        return None
    return token, uid, request_headers(entry["request"])


def perform_login(entry: dict[str, Any], timeout: float) -> tuple[str, str, dict[str, str]]:
    captured_request = entry["request"]
    body = str(captured_request.get("postData", {}).get("text", ""))
    if not body:
        raise AuthenticationError("Captured login request has no body")
    url = str(captured_request.get("url", ""))
    parsed = urlparse(url)
    if parsed.hostname != API_HOST or parsed.path != LOGIN_PATH or parsed.scheme != "https":
        raise AuthenticationError("Captured login URL is unexpected")

    headers = request_headers(captured_request)
    request = Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise AuthenticationError(f"Login failed: HTTP {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise AuthenticationError(f"Login failed: {exc.reason}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("Login returned invalid JSON") from exc

    data = result.get("data", {})
    token = str(data.get("token", ""))
    uid = str(data.get("uid", ""))
    if result.get("code") != 200 or not token or not uid:
        raise AuthenticationError(
            f"Login returned code={result.get('code')!r}, error={result.get('error')!r}"
        )
    if not token_is_valid(token):
        raise AuthenticationError("Login returned an expired or invalid JWT")
    return token, uid, headers


def obtain_auth(
    cache_path: Path,
    har_path: str | None,
    login_har_path: str | None,
    timeout: float,
    force_login: bool = False,
) -> tuple[dict[str, str], str]:
    if not force_login:
        cached = load_cached_auth(cache_path)
        if cached:
            return cached, "cache"

    login_match = find_har_entry(har_paths(login_har_path or har_path), LOGIN_PATH)
    if not force_login and login_match:
        try:
            captured = auth_from_login_capture(login_match[1])
        except DownloadError:
            # Sanitized imported HARs intentionally retain only the login
            # request. Replay that request below when the cached token expires.
            captured = None
        if captured:
            token, uid, client_headers = captured
            save_cached_auth(cache_path, token, uid, client_headers)
            return authenticated_headers(token, uid, client_headers), "login HAR"

    if not force_login:
        auth_match = find_har_entry(har_paths(har_path), LIST_PATH)
        if auth_match:
            captured = auth_from_entry(auth_match[1])
            if captured:
                token, uid, client_headers = captured
                save_cached_auth(cache_path, token, uid, client_headers)
                return authenticated_headers(token, uid, client_headers), "authenticated HAR"

    if not login_match:
        raise AuthenticationError(
            f"No captured request to {LOGIN_PATH}; pass --login-har PATH"
        )
    token, uid, client_headers = perform_login(login_match[1], timeout)
    save_cached_auth(cache_path, token, uid, client_headers)
    return authenticated_headers(token, uid, client_headers), "login"


def api_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthenticationError(f"API authentication failed (HTTP {exc.code})") from exc
        raise DownloadError(f"API request failed: HTTP {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise DownloadError(f"API request failed: {exc.reason}") from exc

    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DownloadError("API returned invalid JSON") from exc
    if result.get("code") in (401, 403):
        raise AuthenticationError(f"API authentication failed (code {result.get('code')})")
    if result.get("code") != 200:
        raise DownloadError(f"API returned code={result.get('code')!r}, error={result.get('error')!r}")
    return result


def newest_record(headers: dict[str, str], timeout: float) -> dict[str, Any]:
    query = urlencode(
        {
            "end_time": int(time.time()),
            "page": 1,
            "size": 20,
            "source": "all",
            "start_time": 1451577600,
            "time_type": "all",
        }
    )
    result = api_json(f"https://{API_HOST}{LIST_PATH}?{query}", headers, timeout)
    records = result.get("data", {}).get("list", [])
    if not records:
        raise DownloadError("No activity records returned by the API")
    return max(records, key=lambda item: int(item.get("start_time", 0)))


def fit_link(record_id: str, headers: dict[str, str], timeout: float) -> tuple[str, str]:
    url = f"https://{API_HOST}{FIT_PATH_PREFIX}{quote(record_id, safe='')}/fit"
    data = api_json(url, headers, timeout).get("data", {})
    download_url = str(data.get("url", ""))
    filename = safe_filename(str(data.get("name", "")))
    parsed = urlparse(download_url)
    if parsed.hostname != "fits.rfsvr.net" or parsed.scheme not in ("http", "https"):
        raise DownloadError("API returned an unexpected FIT download URL")
    if not filename.lower().endswith(".fit"):
        raise DownloadError("API returned an invalid FIT filename")
    return download_url, filename


def safe_filename(value: str) -> str:
    value = Path(value).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")


def download_fit(url: str, target: Path, timeout: float, force: bool) -> str:
    if target.exists() and not force:
        return "exists"

    request = Request(url, headers={"Accept": "*/*"}, method="GET")
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with urlopen(request, timeout=timeout) as response, temp.open("wb") as file:
            while chunk := response.read(1024 * 1024):
                file.write(chunk)
        with temp.open("rb") as file:
            header = file.read(12)
        if len(header) < 12 or header[8:12] != b".FIT":
            raise DownloadError("Downloaded content is not a valid FIT file")
        os.replace(temp, target)
    except HTTPError as exc:
        raise DownloadError(f"FIT download failed: HTTP {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise DownloadError(f"FIT download failed: {exc.reason}") from exc
    finally:
        temp.unlink(missing_ok=True)
    return "downloaded"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--har", help="HAR containing an authenticated record/list request")
    parser.add_argument("--login-har", help="HAR containing the captured login request")
    parser.add_argument(
        "--token-cache",
        default=TOKEN_CACHE,
        help=f"JWT cache path (default: {TOKEN_CACHE})",
    )
    parser.add_argument("--relogin", action="store_true", help="ignore cached JWT and log in again")
    parser.add_argument("--output-dir", default="fits", help="destination directory (default: fits)")
    parser.add_argument("--force", action="store_true", help="replace an existing FIT file")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cache_path = Path(args.token_cache)
        headers, auth_source = obtain_auth(
            cache_path,
            args.har,
            args.login_har,
            args.timeout,
            force_login=args.relogin,
        )

        def resolve_latest() -> tuple[dict[str, Any], str, str]:
            record = newest_record(headers, args.timeout)
            record_id = str(record.get("id", ""))
            if not record_id:
                raise DownloadError("Newest record has no id")
            url, filename = fit_link(record_id, headers, args.timeout)
            return record, url, filename

        try:
            record, url, filename = resolve_latest()
        except AuthenticationError:
            headers, auth_source = obtain_auth(
                cache_path,
                args.har,
                args.login_har,
                args.timeout,
                force_login=True,
            )
            record, url, filename = resolve_latest()

        record_id = str(record["id"])
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / filename
        status = download_fit(url, target, args.timeout, args.force)
        started = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(int(record.get("start_time", 0)))
        )
        print(f"auth: {auth_source}")
        print(f"{status}: {target.resolve()}")
        print(f"activity: {started} | id={record_id}")
        return 0
    except DownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
