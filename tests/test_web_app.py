from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_app


class WebAppTests(unittest.TestCase):
    def test_download_all_fits_reauthenticates_legacy_har_mid_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fit_dir = root / "fits"
            har_path = root / "onelap.har"
            har_path.write_text("{}", encoding="utf-8")
            records = [{"id": "ride", "name": "测试骑行", "start_time": 100}]
            with (
                patch.object(web_app, "DATA_ROOT", root),
                patch.object(web_app, "FIT_DIR", fit_dir),
                patch.object(web_app, "ONELAP_DIRECT_AUTH_PATH", root / "missing.json"),
                patch.object(web_app, "ONELAP_HAR_PATH", har_path),
                patch.object(
                    web_app.onelap,
                    "obtain_auth",
                    side_effect=[({"Authorization": "old"}, "cache"), ({"Authorization": "new"}, "login")],
                ) as obtain_auth,
                patch.object(web_app.sync, "onelap_records", return_value=records),
                patch.object(
                    web_app.onelap,
                    "fit_link",
                    side_effect=[
                        web_app.onelap.AuthenticationError("expired"),
                        ("https://fits.rfsvr.net/ride.fit", "ride.fit"),
                    ],
                ) as fit_link,
                patch.object(web_app.onelap, "download_fit", return_value="downloaded"),
            ):
                result = web_app.DashboardService().download_all_fits()

        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(fit_link.call_count, 2)
        self.assertEqual(obtain_auth.call_count, 2)
        self.assertTrue(obtain_auth.call_args_list[1].kwargs["force_login"])

    def test_enrich_direct_records_reuses_cache_and_fetches_missing_details(self) -> None:
        class FakeClient:
            def __init__(self):
                self.requested = []

            def record_detail(self, record_id):
                self.requested.append(record_id)
                return {
                    "id": 336072,
                    "name": "详细骑行",
                    "elevation": 88,
                    "cal": 520,
                    "totalDistance": 42000,
                    "time": 3600,
                }

        records = [
            {"id": "cached", "name": "骑行训练", "start_time": 100, "elevation": 0, "cal": 0, "TSS": 0},
            {"id": "missing", "name": "骑行训练", "start_time": 200, "elevation": 0, "cal": 100, "TSS": 0},
        ]
        previous = {
            "details_enriched": True,
            "activities": [{"id": "cached", "name": "缓存骑行", "elevation_m": 42, "calories": 300, "tss": 18}],
        }
        client = FakeClient()

        complete = web_app.DashboardService().enrich_direct_records(
            client, records, previous
        )

        self.assertTrue(complete)
        self.assertEqual(client.requested, ["missing"])
        self.assertEqual(records[0]["elevation"], 42)
        self.assertEqual(records[0]["name"], "缓存骑行")
        self.assertEqual(records[1]["id"], "missing")
        self.assertEqual(records[1]["elevation"], 88)
        self.assertEqual(records[1]["cal"], 520)

    def test_enrich_direct_records_refetches_partial_account_cache(self) -> None:
        class FakeClient:
            def __init__(self):
                self.requested = []

            def record_detail(self, record_id):
                self.requested.append(record_id)
                return {
                    "elevation": 88,
                    "cal": 520,
                    "TSS": 35,
                    "totalDistance": 42000,
                    "time": 3600,
                }

        records = [{
            "id": "partial",
            "name": "列表活动名",
            "start_time": 200,
            "elevation": 0,
            "cal": 100,
            "TSS": 12,
        }]
        previous = {
            "auth_source": "account",
            "details_enriched": False,
            "activities": [{
                "id": "partial",
                "name": "旧缓存名",
                "elevation_m": 42,
                "calories": 0,
                "tss": 0,
            }],
        }
        client = FakeClient()

        complete = web_app.DashboardService().enrich_direct_records(
            client, records, previous
        )

        self.assertTrue(complete)
        self.assertEqual(client.requested, ["partial"])
        self.assertEqual(records[0]["elevation"], 88)
        self.assertEqual(records[0]["cal"], 520)
        self.assertEqual(records[0]["TSS"], 35)
        self.assertEqual(records[0]["name"], "列表活动名")

    def test_download_all_fits_skips_existing_and_continues_after_error(self) -> None:
        class FakeClient:
            def records(self, progress=None):
                if progress:
                    progress(1, 3, 3)
                return [
                    {"id": "one", "name": "活动一", "start_time": 1_720_000_001},
                    {"id": "two", "name": "活动二", "start_time": 1_720_000_002},
                    {"id": "three", "name": "活动三", "start_time": 1_720_000_003},
                ]

            def download_record_fit(self, record_id, target, force=False):
                if record_id == "three":
                    raise web_app.onelap.DownloadError("network")
                return "exists" if record_id == "two" else "downloaded"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth_path = root / "auth.json"
            auth_path.write_text("{}", encoding="utf-8")
            fit_dir = root / "fits"
            lines = []
            with (
                patch.object(web_app, "ONELAP_DIRECT_AUTH_PATH", auth_path),
                patch.object(web_app, "FIT_DIR", fit_dir),
                patch.object(web_app.onelap, "OneLapOtmClient", return_value=FakeClient()),
            ):
                result = web_app.DashboardService().download_all_fits(
                    lambda line, progress: lines.append((line, progress))
                )

        self.assertEqual(
            result, {"total": 3, "downloaded": 1, "existing": 1, "failed": 1}
        )
        self.assertEqual(lines[-1][1], 100)
        self.assertIn("新增 1", lines[-1][0])

    def test_local_jobs_skip_strava_setup(self) -> None:
        for mode in ("refresh", "download"):
            with self.subTest(mode=mode):
                manager = web_app.JobManager()
                with (
                    patch.object(
                        web_app, "preferred_strava_mode", side_effect=AssertionError("unused")
                    ),
                    patch.object(web_app.threading, "Thread") as thread,
                ):
                    job = manager.start(mode, 15)

                self.assertEqual(job["mode"], mode)
                self.assertEqual(job["strava_mode"], "")
                thread.assert_called_once()

    def test_job_manager_current_prefers_running_job(self) -> None:
        manager = web_app.JobManager()
        manager.jobs = {
            "done": {"id": "done", "status": "completed"},
            "running": {"id": "running", "status": "running"},
        }

        self.assertEqual(manager.current()["id"], "running")

    def test_preferred_strava_mode_prioritizes_web_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = root / "web.json"
            config_path = root / "api.json"
            session_path.write_text(
                json.dumps({"cookies": [{"name": "session", "value": "secret"}]}),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "client_id": "1",
                        "client_secret": "secret",
                        "refresh_token": "refresh",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "STRAVA_WEB_SESSION_PATH", session_path),
                patch.object(web_app, "STRAVA_CONFIG_PATH", config_path),
                patch.object(web_app.sync, "StravaWebClient"),
            ):
                self.assertEqual(web_app.preferred_strava_mode(), "web")
                session_path.unlink()
                self.assertEqual(web_app.preferred_strava_mode(), "api")

    def test_preferred_strava_mode_falls_back_from_expired_web_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = root / "web.json"
            config_path = root / "api.json"
            session_path.write_text(
                json.dumps({"cookies": [{"name": "session", "value": "expired"}]}),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps({"client_id": "1", "client_secret": "secret", "refresh_token": "refresh"}),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "STRAVA_WEB_SESSION_PATH", session_path),
                patch.object(web_app, "STRAVA_CONFIG_PATH", config_path),
                patch.object(
                    web_app.sync,
                    "StravaWebClient",
                    side_effect=web_app.sync.WebSessionExpiredError("expired"),
                ),
            ):
                self.assertEqual(web_app.preferred_strava_mode(), "api")

    def test_onelap_har_import_retains_only_login_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retained_path = root / "onelap_auth.har"
            login_request = {
                "url": f"https://{web_app.onelap.API_HOST}{web_app.onelap.LOGIN_PATH}",
                "method": "POST",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "postData": {"text": '{"account":"encrypted","password":"hash"}'},
            }
            har = {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "2026-07-18T00:00:00Z",
                            "request": login_request,
                            "response": {"content": {"text": "sensitive response"}},
                        },
                        {"request": {"url": "https://example.com/private"}},
                    ]
                }
            }

            def fake_auth(cache_path, *_args, **_kwargs):
                web_app.sync.save_json(
                    cache_path,
                    {"token": "token", "uid": "uid", "expires_at": 9_999_999_999},
                )
                return {"Authorization": "token"}, "login"

            with (
                patch.object(web_app, "DATA_ROOT", root),
                patch.object(web_app, "ONELAP_HAR_PATH", retained_path),
                patch.object(web_app.onelap, "obtain_auth", side_effect=fake_auth),
                patch.object(web_app.onelap, "newest_record", return_value={"id": "ride"}),
            ):
                source = web_app.AuthService().import_onelap_har(har)

            retained = json.loads(retained_path.read_text(encoding="utf-8"))
            entries = retained["log"]["entries"]
            self.assertEqual(source, "login")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["request"], login_request)
            self.assertNotIn("response", entries[0])

    def test_onelap_har_import_requires_login_request(self) -> None:
        har = {"log": {"entries": [{"request": {"url": "https://example.com"}}]}}

        with self.assertRaisesRegex(ValueError, "登录请求"):
            web_app.AuthService().import_onelap_har(har)

    def test_clean_activity_merges_sync_state(self) -> None:
        record = {
            "id": "ride-1",
            "name": "晚间恢复骑",
            "start_time": 1_720_000_000,
            "total_distance": 25_432.5,
            "total_time": 3_601,
            "elevation": 122.4,
            "cal": 530,
            "TSS": 46.8,
        }
        states = {"ride-1": {"status": "uploaded", "activity_id": "987"}}

        result = web_app.clean_activity(record, states)

        self.assertEqual(result["name"], "晚间恢复骑")
        self.assertEqual(result["distance_m"], 25_432.5)
        self.assertEqual(result["duration_s"], 3_601)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["activity_id"], "987")

    def test_local_activities_reads_supported_fit_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fit_dir = Path(directory)
            (fit_dir / "MAGENE_C406_2026-07-03_194804_336072.fit").write_bytes(b"FIT")
            (fit_dir / "unrelated.fit").write_bytes(b"FIT")

            with patch.object(web_app, "FIT_DIR", fit_dir):
                activities = web_app.local_activities({})

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["device"], "MAGENE C406")
        self.assertEqual(activities[0]["status"], "local")

    def test_dashboard_uses_sanitized_cache_and_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            cache_path = root / "cache.json"
            fit_dir = root / "fits"
            fit_dir.mkdir()
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "records": {
                            "ride-1": {"status": "error", "error": "temporary", "updated_at": 10},
                            "ride-2": {"status": "matched", "updated_at": 20},
                        },
                    }
                ),
                encoding="utf-8",
            )
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "fetched_at": 30,
                        "activities": [
                            {
                                "id": "ride-1",
                                "name": "测试骑行",
                                "start_time": 1_720_000_000,
                                "distance_m": 10_000,
                                "duration_s": 1_800,
                                "elevation_m": 50,
                                "calories": 100,
                                "tss": 20,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(web_app, "STATE_PATH", state_path),
                patch.object(web_app, "CACHE_PATH", cache_path),
                patch.object(web_app, "FIT_DIR", fit_dir),
                patch.object(web_app, "connection_status", return_value=[]),
            ):
                result = web_app.DashboardService().dashboard()

        self.assertEqual(result["data_source"], "onelap")
        self.assertEqual(result["activities"][0]["status"], "error")
        self.assertEqual(result["summary"]["total_rides"], 1)
        self.assertEqual(result["summary"]["total_distance_km"], 10)
        self.assertEqual(result["summary"]["total_duration_s"], 1_800)
        self.assertEqual(result["summary"]["total_elevation_m"], 50)
        self.assertEqual(result["summary"]["synced_records"], 1)
        self.assertEqual(result["summary"]["error_records"], 1)

    def test_number_helpers_fail_closed(self) -> None:
        self.assertEqual(web_app.number("not-a-number"), 0)
        self.assertEqual(web_app.integer(None), 0)
        self.assertEqual(web_app.integer(None, 15), 15)

    def test_project_route_preserves_shape_without_absolute_location(self) -> None:
        coordinates = [
            (22.0 + index * 0.00001, 114.0 + (index % 40) * 0.00002)
            for index in range(1_000)
        ]

        route = web_app.project_route(coordinates, max_points=120)

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(len(route["points"]), 120)
        self.assertEqual(route["source_points"], 1_000)
        self.assertGreater(route["width"], 0)
        self.assertGreater(route["height"], 0)
        self.assertNotIn("latitude", route)
        self.assertNotIn("longitude", route)

    def test_project_route_rejects_stationary_coordinates(self) -> None:
        self.assertIsNone(web_app.project_route([(22.0, 114.0)] * 20))


if __name__ == "__main__":
    unittest.main()
