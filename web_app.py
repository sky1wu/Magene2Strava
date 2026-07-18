#!/usr/bin/env python3
"""Local web dashboard for reviewing and syncing Onelap rides to Strava."""

from __future__ import annotations

import argparse
import functools
import json
import math
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import fitdecode
import download_latest_fit as onelap
import sync_to_strava as sync


APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MAGENE2STRAVA_DATA_DIR", APP_ROOT)).expanduser().resolve()
WEB_ROOT = APP_ROOT / "web"
CACHE_PATH = DATA_ROOT / ".dashboard_cache.json"
STATE_PATH = DATA_ROOT / sync.SYNC_STATE
FIT_DIR = DATA_ROOT / "fits"
ONELAP_HAR_PATH = DATA_ROOT / "onelap_auth.har"
ONELAP_DIRECT_AUTH_PATH = DATA_ROOT / onelap.DIRECT_AUTH_CACHE
STRAVA_CONFIG_PATH = DATA_ROOT / sync.STRAVA_CONFIG
STRAVA_WEB_SESSION_PATH = DATA_ROOT / sync.STRAVA_WEB_SESSION
FINAL_STATUSES = sync.FINAL_STATUSES
FIT_NAME = re.compile(
    r"^(?P<device>.+?)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{6})(?:_.+)?\.fit$",
    re.IGNORECASE,
)


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else default
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_activity(record: dict[str, Any], states: dict[str, Any]) -> dict[str, Any]:
    record_id = str(record.get("id", ""))
    state = states.get(record_id, {}) if isinstance(states, dict) else {}
    status = str(state.get("status") or "queued")
    return {
        "id": record_id,
        "name": str(record.get("name") or "室内骑行"),
        "start_time": integer(record.get("start_time")),
        "distance_m": round(number(record.get("total_distance")), 1),
        "duration_s": integer(record.get("total_time")),
        "elevation_m": round(number(record.get("elevation")), 1),
        "calories": round(number(record.get("cal")), 0),
        "tss": round(number(record.get("TSS")), 1),
        "status": status,
        "activity_id": str(state.get("activity_id") or ""),
        "error": str(state.get("error") or ""),
    }


def local_activities(states: dict[str, Any]) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    if not FIT_DIR.is_dir():
        return activities
    for path in FIT_DIR.glob("*.fit"):
        match = FIT_NAME.match(path.name)
        if not match:
            continue
        try:
            stamp = datetime.strptime(
                f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H%M%S"
            )
            started = int(stamp.timestamp())
        except ValueError:
            continue
        activities.append(
            {
                "id": path.name,
                "name": "室内骑行",
                "start_time": started,
                "distance_m": 0,
                "duration_s": 0,
                "elevation_m": 0,
                "calories": 0,
                "tss": 0,
                "status": "local",
                "activity_id": "",
                "error": "",
                "device": match.group("device").replace("_", " "),
            }
        )
    return sorted(activities, key=lambda item: item["start_time"], reverse=True)


def project_route(
    coordinates: list[tuple[float, float]], max_points: int = 420
) -> dict[str, Any] | None:
    """Project latitude/longitude coordinates to local, privacy-preserving route points."""
    if len(coordinates) < 2:
        return None
    latitude_origin = sum(item[0] for item in coordinates) / len(coordinates)
    longitude_origin = coordinates[0][1]
    longitude_scale = max(0.01, math.cos(math.radians(latitude_origin)))
    projected: list[tuple[float, float]] = []
    for latitude, longitude in coordinates:
        x = (longitude - longitude_origin) * longitude_scale
        y = latitude - latitude_origin
        if not projected or abs(x - projected[-1][0]) + abs(y - projected[-1][1]) > 1e-10:
            projected.append((x, y))
    if len(projected) < 2:
        return None

    source_points = len(projected)
    if len(projected) > max_points:
        step = (len(projected) - 1) / (max_points - 1)
        projected = [projected[round(index * step)] for index in range(max_points - 1)] + [
            projected[-1]
        ]

    minimum_x = min(item[0] for item in projected)
    maximum_x = max(item[0] for item in projected)
    minimum_y = min(item[1] for item in projected)
    maximum_y = max(item[1] for item in projected)
    width = maximum_x - minimum_x
    height = maximum_y - minimum_y
    if width < 1e-9 and height < 1e-9:
        return None
    return {
        "points": [
            [round(x - minimum_x, 8), round(maximum_y - y, 8)] for x, y in projected
        ],
        "width": round(max(width, 1e-9), 8),
        "height": round(max(height, 1e-9), 8),
        "source_points": source_points,
    }


@functools.lru_cache(maxsize=24)
def decode_fit_route(path_text: str, modified_ns: int, size: int) -> dict[str, Any] | None:
    """Decode and cache one FIT route; stat arguments invalidate changed files."""
    del modified_ns, size
    coordinates: list[tuple[float, float]] = []
    try:
        with fitdecode.FitReader(
            path_text,
            check_crc=fitdecode.CrcCheck.DISABLED,
            error_handling=fitdecode.ErrorHandling.WARN,
        ) as fit_file:
            for frame in fit_file:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA or frame.name != "record":
                    continue
                if not frame.has_field("position_lat") or not frame.has_field("position_long"):
                    continue
                latitude_raw = frame.get_value("position_lat")
                longitude_raw = frame.get_value("position_long")
                if not isinstance(latitude_raw, (int, float)) or not isinstance(
                    longitude_raw, (int, float)
                ):
                    continue
                latitude = float(latitude_raw) * 180.0 / 2**31
                longitude = float(longitude_raw) * 180.0 / 2**31
                if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    coordinates.append((latitude, longitude))
    except (OSError, fitdecode.FitError):
        return None
    return project_route(coordinates)


def latest_fit_route(activities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest cached activity that has a matching local FIT route."""
    if not FIT_DIR.is_dir():
        return None
    files_by_time: dict[int, Path] = {}
    for path in FIT_DIR.glob("*.fit"):
        match = FIT_NAME.match(path.name)
        if not match:
            continue
        try:
            stamp = datetime.strptime(
                f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H%M%S"
            )
            files_by_time[int(stamp.timestamp())] = path
        except ValueError:
            continue

    for activity in activities:
        started = integer(activity.get("start_time"))
        candidates = [
            files_by_time.get(started + offset) for offset in range(-3, 4) if started + offset in files_by_time
        ]
        for path in candidates:
            if path is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            route = decode_fit_route(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
            if route:
                return {
                    **route,
                    "activity_id": str(activity.get("id") or ""),
                    "start_time": started,
                }
    return None


def connection_status() -> list[dict[str, Any]]:
    token = read_json(DATA_ROOT / onelap.TOKEN_CACHE, {})
    expiry = integer(token.get("expires_at"))
    onelap_ok = expiry > int(time.time()) + 60
    onelap_direct = onelap.direct_auth_configured(ONELAP_DIRECT_AUTH_PATH)
    onelap_har = ONELAP_HAR_PATH.is_file()

    web_session = read_json(STRAVA_WEB_SESSION_PATH, {})
    cookie_count = len(web_session.get("cookies", [])) if isinstance(web_session.get("cookies"), list) else 0
    strava_config = read_json(STRAVA_CONFIG_PATH, {})
    strava_api = all(
        strava_config.get(field) for field in ("client_id", "client_secret", "refresh_token")
    )
    strava_web = cookie_count > 0
    strava_ok = strava_web or strava_api

    return [
        {
            "id": "onelap",
            "name": "顽鹿运动",
            "detail": (
                "账号登录已配置"
                if onelap_direct
                else "授权有效"
                if onelap_ok
                else "授权文件已保存，可刷新令牌"
                if onelap_har
                else "尚未授权"
            ),
            "status": "connected" if onelap_direct or onelap_ok or onelap_har else "attention",
            "action": "更新登录" if onelap_direct or onelap_ok or onelap_har else "登录授权",
        },
        {
            "id": "strava",
            "name": "Strava",
            "detail": (
                "Web 会话已保存"
                if strava_web
                else "API OAuth 已授权（备用）"
                if strava_api
                else "尚未授权"
            ),
            "status": "connected" if strava_ok else "attention",
            "action": "更新会话" if strava_web else "Web 授权",
        },
        {
            "id": "storage",
            "name": "本地 FIT",
            "detail": f"{len(list(FIT_DIR.glob('*.fit'))) if FIT_DIR.is_dir() else 0} 个文件",
            "status": "connected",
        },
    ]


def preferred_strava_mode() -> str:
    config = read_json(STRAVA_CONFIG_PATH, {})
    api_available = all(
        config.get(field) for field in ("client_id", "client_secret", "refresh_token")
    )
    session = read_json(STRAVA_WEB_SESSION_PATH, {})
    if isinstance(session.get("cookies"), list) and session["cookies"]:
        try:
            sync.StravaWebClient(STRAVA_WEB_SESSION_PATH, 15.0)
            return "web"
        except sync.SyncError:
            if not api_available:
                return "web"
    if api_available:
        return "api"
    return "web"


class AuthService:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending_strava: dict[str, dict[str, Any]] = {}

    def import_onelap_har(self, har: dict[str, Any]) -> str:
        entries = har.get("log", {}).get("entries", [])
        if not isinstance(entries, list) or not entries:
            raise ValueError("HAR 中没有可用的网络请求")
        login_entry = next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict)
                and urlparse(str(entry.get("request", {}).get("url", ""))).path
                == onelap.LOGIN_PATH
            ),
            None,
        )
        if not login_entry:
            raise ValueError("HAR 未包含顽鹿登录请求，请从登录前开始录制")

        suffix = uuid.uuid4().hex
        temporary_har = DATA_ROOT / f".onelap_auth.{suffix}.har"
        temporary_cache = DATA_ROOT / f".onelap_token.{suffix}.json"
        try:
            sync.save_json(temporary_har, har)
            headers, source = onelap.obtain_auth(
                temporary_cache,
                str(temporary_har),
                str(temporary_har),
                30.0,
            )
            onelap.newest_record(headers, 30.0)
            retained = {
                "log": {
                    "version": "1.2",
                    "creator": {"name": "Magene2Strava", "version": "1"},
                    "entries": [
                        {
                            "startedDateTime": login_entry.get("startedDateTime", ""),
                            "request": login_entry.get("request", {}),
                        }
                    ],
                }
            }
            sync.save_json(temporary_har, retained)
            os.replace(temporary_har, ONELAP_HAR_PATH)
            os.replace(temporary_cache, DATA_ROOT / onelap.TOKEN_CACHE)
            for path in (ONELAP_HAR_PATH, DATA_ROOT / onelap.TOKEN_CACHE):
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            return source
        finally:
            temporary_har.unlink(missing_ok=True)
            temporary_cache.unlink(missing_ok=True)

    def login_onelap(self, account: str, password: str) -> str:
        temporary = DATA_ROOT / f".onelap_auth.{uuid.uuid4().hex}.json"
        try:
            client = onelap.configure_direct_auth(temporary, account, password, 30.0)
            client.records(page_size=1, max_pages=1)
            os.replace(temporary, ONELAP_DIRECT_AUTH_PATH)
            try:
                os.chmod(ONELAP_DIRECT_AUTH_PATH, 0o600)
            except OSError:
                pass
            return client.source
        finally:
            temporary.unlink(missing_ok=True)

    def import_strava_cookie(self, cookie_header: str) -> None:
        if not cookie_header.strip():
            raise ValueError("请粘贴 Strava Cookie 请求头")
        sync.save_cookie_header_session(cookie_header, STRAVA_WEB_SESSION_PATH, 30.0)

    def import_strava_har(self, har: dict[str, Any]) -> None:
        entries = har.get("log", {}).get("entries", [])
        if not isinstance(entries, list) or not entries:
            raise ValueError("HAR 中没有可用的网络请求")
        temporary_har = DATA_ROOT / f".strava_auth.{uuid.uuid4().hex}.har"
        try:
            sync.save_json(temporary_har, har)
            sync.import_strava_har(temporary_har, STRAVA_WEB_SESSION_PATH, 30.0)
        finally:
            temporary_har.unlink(missing_ok=True)

    def start_strava(
        self, client_id: str, client_secret: str, redirect_uri: str
    ) -> dict[str, str]:
        if not client_id.isdigit() or len(client_id) > 20:
            raise ValueError("Strava Client ID 格式无效")
        if len(client_secret) < 8 or len(client_secret) > 200:
            raise ValueError("Strava Client Secret 格式无效")
        parsed = urlparse(redirect_uri)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path != "/api/auth/strava/callback"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Strava 回调地址无效")

        state = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.lock:
            self.pending_strava = {
                key: value
                for key, value in self.pending_strava.items()
                if integer(value.get("expires_at")) > now
            }
            self.pending_strava[state] = {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "expires_at": now + 600,
            }
        authorization_url = sync.STRAVA_AUTHORIZE_URL + "?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "approval_prompt": "force",
                "scope": ",".join(sorted(sync.REQUIRED_SCOPES)),
                "state": state,
            }
        )
        return {"authorization_url": authorization_url, "redirect_uri": redirect_uri}

    def complete_strava(self, state: str, code: str, scope_text: str) -> None:
        with self.lock:
            pending = self.pending_strava.pop(state, None)
        if not pending or integer(pending.get("expires_at")) <= int(time.time()):
            raise ValueError("Strava 授权请求已失效，请重新开始")
        if not code:
            raise ValueError("Strava 未返回授权码")
        tokens = sync.oauth_token(
            {
                "client_id": str(pending["client_id"]),
                "client_secret": str(pending["client_secret"]),
                "code": code,
                "grant_type": "authorization_code",
            },
            30.0,
        )
        granted_text = str(tokens.get("scope") or scope_text)
        granted = set(granted_text.replace(",", " ").split())
        if not sync.REQUIRED_SCOPES.issubset(granted):
            raise ValueError("未授予 activity:read_all 和 activity:write 权限")
        sync.save_json(
            STRAVA_CONFIG_PATH,
            {
                "version": 1,
                "client_id": str(pending["client_id"]),
                "client_secret": str(pending["client_secret"]),
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "expires_at": integer(tokens.get("expires_at")),
                "scope": " ".join(sorted(granted)),
            },
        )


class DashboardService:
    def __init__(self) -> None:
        self.refresh_lock = threading.Lock()

    def dashboard(self) -> dict[str, Any]:
        state = read_json(STATE_PATH, {"version": 1, "records": {}})
        states = state.get("records", {})
        if not isinstance(states, dict):
            states = {}
        cache = read_json(CACHE_PATH, {})
        cached = cache.get("activities", [])
        if isinstance(cached, list) and cached:
            activities = []
            for item in cached:
                if not isinstance(item, dict):
                    continue
                current = dict(item)
                record_state = states.get(str(current.get("id", "")), {})
                current["status"] = str(record_state.get("status") or "queued")
                current["activity_id"] = str(record_state.get("activity_id") or "")
                current["error"] = str(record_state.get("error") or "")
                activities.append(current)
            source = "onelap"
        else:
            activities = local_activities(states)
            source = "local"

        activities.sort(key=lambda item: integer(item.get("start_time")), reverse=True)
        status_counts: dict[str, int] = {}
        for value in states.values():
            if isinstance(value, dict):
                key = str(value.get("status") or "unknown")
                status_counts[key] = status_counts.get(key, 0) + 1

        synced_count = sum(status_counts.get(key, 0) for key in FINAL_STATUSES)
        state_total = len(states)
        updated_times = [
            integer(value.get("updated_at"))
            for value in states.values()
            if isinstance(value, dict) and value.get("updated_at")
        ]
        return {
            "generated_at": int(time.time()),
            "data_source": source,
            "cache_updated_at": integer(cache.get("fetched_at")),
            "summary": {
                "total_records": len(activities) if source == "onelap" else max(len(activities), state_total),
                "total_rides": len(activities),
                "total_distance_km": round(sum(number(item.get("distance_m")) for item in activities) / 1000, 1),
                "total_duration_s": sum(integer(item.get("duration_s")) for item in activities),
                "total_elevation_m": round(sum(number(item.get("elevation_m")) for item in activities)),
                "synced_records": synced_count,
                "pending_records": status_counts.get("pending", 0),
                "error_records": status_counts.get("error", 0),
                "sync_rate": round(synced_count / state_total * 100, 1) if state_total else 0,
                "last_sync_at": max(updated_times, default=0),
            },
            "status_counts": status_counts,
            "connections": connection_status(),
            "activities": activities[:2000],
            "latest_route": latest_fit_route(activities),
        }

    def refresh(self, progress: Callable[[str, int], None] | None = None) -> dict[str, Any]:
        if not self.refresh_lock.acquire(blocking=False):
            raise RuntimeError("数据刷新正在进行，请稍候")
        try:
            if ONELAP_DIRECT_AUTH_PATH.is_file():
                if progress:
                    progress("正在连接顽鹿运动…", 8)
                direct = onelap.OneLapOtmClient(ONELAP_DIRECT_AUTH_PATH, 30.0)
                records = direct.records(
                    progress=(
                        lambda page, count, total: progress(
                            f"已读取第 {page} 页，共获取 {count} / {total} 条活动"
                            if total
                            else f"已读取第 {page} 页，共获取 {count} 条活动",
                            min(90, 10 + round((count / total) * 80)) if total else min(88, 10 + page * 4),
                        )
                        if progress
                        else None
                    )
                )
                auth_source = direct.source
            else:
                if progress:
                    progress("正在读取顽鹿活动…", 12)
                login_har = str(ONELAP_HAR_PATH) if ONELAP_HAR_PATH.is_file() else None
                headers, auth_source = onelap.obtain_auth(
                    DATA_ROOT / onelap.TOKEN_CACHE, login_har, login_har, 30.0
                )
                try:
                    records = sync.onelap_records(headers, 30.0)
                except onelap.AuthenticationError:
                    headers, auth_source = onelap.obtain_auth(
                        DATA_ROOT / onelap.TOKEN_CACHE,
                        login_har,
                        login_har,
                        30.0,
                        force_login=True,
                    )
                    records = sync.onelap_records(headers, 30.0)
            state = read_json(STATE_PATH, {"version": 1, "records": {}})
            states = state.get("records", {})
            if not isinstance(states, dict):
                states = {}
            payload = {
                "version": 1,
                "fetched_at": int(time.time()),
                "auth_source": auth_source,
                "activities": [clean_activity(item, states) for item in records],
            }
            if progress:
                progress(f"正在保存 {len(records)} 条活动…", 94)
            write_json(CACHE_PATH, payload)
            if progress:
                progress(f"刷新完成，共获取 {len(records)} 条活动", 100)
            return self.dashboard()
        finally:
            self.refresh_lock.release()


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}

    def start(self, mode: str, max_uploads: int) -> dict[str, Any]:
        with self.lock:
            active = next((job for job in self.jobs.values() if job["status"] == "running"), None)
            if active:
                raise RuntimeError("已有同步任务正在运行")
            job_id = uuid.uuid4().hex[:12]
            command = [sys.executable, str(APP_ROOT / "sync_to_strava.py")]
            strava_mode = preferred_strava_mode()
            command.extend(["--strava-mode", strava_mode])
            if ONELAP_DIRECT_AUTH_PATH.is_file():
                command.extend(["--onelap-auth", str(ONELAP_DIRECT_AUTH_PATH)])
            elif ONELAP_HAR_PATH.is_file():
                command.extend(
                    ["--har", str(ONELAP_HAR_PATH), "--login-har", str(ONELAP_HAR_PATH)]
                )
            if mode == "preview":
                command.append("--dry-run")
            else:
                command.extend(["--max-uploads", str(max_uploads), "--newest-first"])
            job = {
                "id": job_id,
                "mode": mode,
                "strava_mode": strava_mode,
                "status": "running",
                "started_at": int(time.time()),
                "finished_at": 0,
                "exit_code": None,
                "lines": [],
            }
            self.jobs[job_id] = job
            target = self._run_refresh if mode == "refresh" else self._run
            args = (job_id,) if mode == "refresh" else (job_id, command)
            threading.Thread(target=target, args=args, daemon=True).start()
            return dict(job)

    def _update_progress(self, job_id: str, line: str, progress: int) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job["lines"].append(line)
            job["lines"] = job["lines"][-500:]
            job["progress"] = max(0, min(100, progress))

    def _run_refresh(self, job_id: str) -> None:
        try:
            result = SERVICE.refresh(
                lambda line, progress: self._update_progress(job_id, line, progress)
            )
            with self.lock:
                job = self.jobs[job_id]
                job["result"] = {"activities": result["summary"]["total_rides"]}
                job["status"] = "completed"
                job["exit_code"] = 0
                job["finished_at"] = int(time.time())
        except Exception as exc:
            with self.lock:
                job = self.jobs[job_id]
                job["status"] = "failed"
                job["exit_code"] = -1
                job["finished_at"] = int(time.time())
                job["lines"].append(f"刷新失败：{exc}")

    def _run(self, job_id: str, command: list[str]) -> None:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        try:
            process = subprocess.Popen(
                command,
                cwd=DATA_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                with self.lock:
                    self.jobs[job_id]["lines"].append(line)
                    self.jobs[job_id]["lines"] = self.jobs[job_id]["lines"][-500:]
            exit_code = process.wait()
            with self.lock:
                job = self.jobs[job_id]
                job["status"] = "completed" if exit_code == 0 else "failed"
                job["exit_code"] = exit_code
                job["finished_at"] = int(time.time())
        except Exception as exc:  # pragma: no cover - defensive process boundary
            with self.lock:
                job = self.jobs[job_id]
                job["status"] = "failed"
                job["exit_code"] = -1
                job["finished_at"] = int(time.time())
                job["lines"].append(f"error: {exc}")

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def current(self) -> dict[str, Any] | None:
        with self.lock:
            jobs = list(self.jobs.values())
            running = next((job for job in reversed(jobs) if job["status"] == "running"), None)
            job = running or (jobs[-1] if jobs else None)
            return json.loads(json.dumps(job)) if job else None


SERVICE = DashboardService()
JOBS = JobManager()
AUTHS = AuthService()


class AppHandler(BaseHTTPRequestHandler):
    server_version = "Magene2Strava/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'",
        )

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def read_body(self, max_bytes: int = 16_384) -> dict[str, Any]:
        length = integer(self.headers.get("Content-Length"))
        if length < 0 or length > max_bytes:
            raise ValueError("请求内容过大")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求 JSON 无效") from exc
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    def send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()

    def handle_strava_callback(self, query: str) -> None:
        params = parse_qs(query)
        try:
            if params.get("error"):
                raise ValueError("Strava 授权已取消")
            AUTHS.complete_strava(
                params.get("state", [""])[0],
                params.get("code", [""])[0],
                params.get("scope", [""])[0],
            )
            self.send_redirect("/?auth=strava-success")
        except (ValueError, sync.SyncError) as exc:
            message = quote(str(exc)[:180], safe="")
            self.send_redirect(f"/?auth=strava-error&message={message}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/auth/strava/callback":
            self.handle_strava_callback(parsed.query)
            return
        if path == "/api/health":
            self.send_json({"status": "ok", "time": int(time.time())})
            return
        if path == "/api/dashboard":
            self.send_json(SERVICE.dashboard())
            return
        if path == "/api/jobs/current":
            self.send_json({"job": JOBS.current()})
            return
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if job is None:
                self.send_error_json("任务不存在", HTTPStatus.NOT_FOUND)
            else:
                self.send_json(job)
            return
        if path.startswith("/api/"):
            self.send_error_json("接口不存在", HTTPStatus.NOT_FOUND)
            return
        self.serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body_limit = (
                12 * 1024 * 1024
                if path in {"/api/auth/onelap/har", "/api/auth/strava/har"}
                else 128 * 1024
                if path == "/api/auth/strava/web-session"
                else 16_384
            )
            body = self.read_body(body_limit)
            if path == "/api/auth/onelap/login":
                source = AUTHS.login_onelap(
                    str(body.get("account") or "").strip(),
                    str(body.get("password") or ""),
                )
                self.send_json({"status": "connected", "source": source})
                return
            if path == "/api/auth/onelap/har":
                har = body.get("har")
                if not isinstance(har, dict):
                    raise ValueError("请选择有效的 HAR 文件")
                source = AUTHS.import_onelap_har(har)
                self.send_json({"status": "connected", "source": source})
                return
            if path == "/api/auth/strava/web-session":
                AUTHS.import_strava_cookie(str(body.get("cookie_header") or ""))
                self.send_json({"status": "connected", "mode": "web"})
                return
            if path == "/api/auth/strava/har":
                har = body.get("har")
                if not isinstance(har, dict):
                    raise ValueError("请选择有效的 HAR 文件")
                AUTHS.import_strava_har(har)
                self.send_json({"status": "connected", "mode": "web"})
                return
            if path == "/api/auth/strava/start":
                redirect_uri = str(body.get("redirect_uri") or "")
                origin = urlparse(str(self.headers.get("Origin") or ""))
                callback = urlparse(redirect_uri)
                if origin.netloc and (
                    origin.scheme != callback.scheme or origin.netloc != callback.netloc
                ):
                    raise ValueError("回调地址必须与当前页面同源")
                self.send_json(
                    AUTHS.start_strava(
                        str(body.get("client_id") or "").strip(),
                        str(body.get("client_secret") or "").strip(),
                        redirect_uri,
                    )
                )
                return
            if path == "/api/refresh":
                self.send_json(SERVICE.refresh())
                return
            if path == "/api/jobs":
                mode = str(body.get("mode") or "sync")
                if mode not in {"sync", "preview", "refresh"}:
                    raise ValueError("不支持的任务类型")
                max_uploads = integer(body.get("max_uploads"), 15)
                if mode != "refresh" and not 1 <= max_uploads <= 100:
                    raise ValueError("单次同步数量必须在 1 到 100 之间")
                self.send_json(JOBS.start(mode, max_uploads), HTTPStatus.ACCEPTED)
                return
            self.send_error_json("接口不存在", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except (RuntimeError, onelap.DownloadError, sync.SyncError) as exc:
            self.send_error_json(str(exc), HTTPStatus.CONFLICT)
        except Exception as exc:  # pragma: no cover - HTTP safety boundary
            self.send_error_json(f"操作失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else unquote(request_path.lstrip("/"))
        target = (WEB_ROOT / relative).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            target = WEB_ROOT / "index.html"
        try:
            body = target.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8848, help="bind port (default: 8848)")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    parser.add_argument("--quiet", action="store_true", help="suppress request logs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not WEB_ROOT.is_dir():
        print(f"error: web assets not found: {WEB_ROOT}", file=sys.stderr)
        return 1
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        os.chdir(DATA_ROOT)
    except OSError as exc:
        print(f"error: cannot use data directory {DATA_ROOT}: {exc}", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    server.quiet = args.quiet  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}"
    print(f"Magene2Strava 已启动：{url}")
    print("按 Ctrl+C 停止服务")
    if args.open:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
