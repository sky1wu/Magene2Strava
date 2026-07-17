#!/usr/bin/env python3
"""Download the newest cycling FIT file using credentials captured in a HAR."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


API_HOST = "rfs-fitness.rfsvr.net"
LOGIN_PATH = "/api/account/v1/login"
LIST_PATH = "/indoor/v2/app/record/list"
FIT_PATH_PREFIX = "/indoor/v1/app/data/riding/share/"
DROP_HEADERS = {"host", "accept-encoding", "connection", "content-length"}
TOKEN_CACHE = ".onelap_token.json"


class DownloadError(RuntimeError):
    pass


class AuthenticationError(DownloadError):
    pass


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
        captured = auth_from_login_capture(login_match[1])
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
