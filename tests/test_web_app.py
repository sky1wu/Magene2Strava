from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_app


class WebAppTests(unittest.TestCase):
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
            ):
                self.assertEqual(web_app.preferred_strava_mode(), "web")
                session_path.unlink()
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
