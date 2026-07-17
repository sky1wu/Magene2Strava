from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_app


class WebAppTests(unittest.TestCase):
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
