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
from typing import Any
from urllib.parse import unquote, urlparse

import fitdecode
import download_latest_fit as onelap
import sync_to_strava as sync


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
CACHE_PATH = ROOT / ".dashboard_cache.json"
STATE_PATH = ROOT / sync.SYNC_STATE
FIT_DIR = ROOT / "fits"
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
    try:
        return int(value or 0)
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
    token = read_json(ROOT / onelap.TOKEN_CACHE, {})
    expiry = integer(token.get("expires_at"))
    onelap_ok = expiry > int(time.time()) + 60

    web_session = read_json(ROOT / sync.STRAVA_WEB_SESSION, {})
    cookie_count = len(web_session.get("cookies", [])) if isinstance(web_session.get("cookies"), list) else 0
    strava_ok = cookie_count > 0

    return [
        {
            "id": "onelap",
            "name": "顽鹿运动",
            "detail": "授权有效" if onelap_ok else "需要刷新授权",
            "status": "connected" if onelap_ok else "attention",
        },
        {
            "id": "strava",
            "name": "Strava",
            "detail": "Web 会话已保存" if strava_ok else "尚未连接",
            "status": "connected" if strava_ok else "attention",
        },
        {
            "id": "storage",
            "name": "本地 FIT",
            "detail": f"{len(list(FIT_DIR.glob('*.fit'))) if FIT_DIR.is_dir() else 0} 个文件",
            "status": "connected",
        },
    ]


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

    def refresh(self) -> dict[str, Any]:
        if not self.refresh_lock.acquire(blocking=False):
            raise RuntimeError("数据刷新正在进行，请稍候")
        try:
            headers, auth_source = onelap.obtain_auth(
                ROOT / onelap.TOKEN_CACHE, None, None, 30.0
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
            write_json(CACHE_PATH, payload)
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
            command = [sys.executable, str(ROOT / "sync_to_strava.py")]
            if mode == "preview":
                command.append("--dry-run")
            else:
                command.extend(["--max-uploads", str(max_uploads), "--newest-first"])
            job = {
                "id": job_id,
                "mode": mode,
                "status": "running",
                "started_at": int(time.time()),
                "finished_at": 0,
                "exit_code": None,
                "lines": [],
            }
            self.jobs[job_id] = job
            threading.Thread(target=self._run, args=(job_id, command), daemon=True).start()
            return dict(job)

    def _run(self, job_id: str, command: list[str]) -> None:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        try:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
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


SERVICE = DashboardService()
JOBS = JobManager()


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

    def read_body(self) -> dict[str, Any]:
        length = integer(self.headers.get("Content-Length"))
        if length < 0 or length > 16_384:
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

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"status": "ok", "time": int(time.time())})
            return
        if path == "/api/dashboard":
            self.send_json(SERVICE.dashboard())
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
            body = self.read_body()
            if path == "/api/refresh":
                self.send_json(SERVICE.refresh())
                return
            if path == "/api/jobs":
                mode = str(body.get("mode") or "sync")
                if mode not in {"sync", "preview"}:
                    raise ValueError("不支持的任务类型")
                max_uploads = integer(body.get("max_uploads"), 15)
                if not 1 <= max_uploads <= 100:
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
