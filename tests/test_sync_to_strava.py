from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import download_latest_fit as onelap
import sync_to_strava as sync


class SyncRiskControlTests(unittest.TestCase):
    def test_web_batch_propagates_fit_risk_control(self) -> None:
        record = {"id": "ride", "name": "测试骑行", "start_time": 100}
        synced: dict[str, object] = {}
        state = {"version": 1, "records": synced}
        error = onelap.ApiRequestError(
            "OneLap risk control rejected the FIT request"
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(sync, "onelap_fit_info", return_value=("ride.fit", None)),
                patch.object(sync, "download_onelap_fit", side_effect=error),
            ):
                with self.assertRaisesRegex(onelap.ApiRequestError, "risk control"):
                    sync.sync_web_batches(
                        object(),
                        [record],
                        [record],
                        synced,
                        state,
                        root / "state.json",
                        {},
                        set(),
                        root,
                        SimpleNamespace(timeout=10, max_uploads=0),
                    )

        self.assertEqual(synced, {})

    def test_only_onelap_risk_control_is_propagated(self) -> None:
        sync.raise_onelap_risk_control(onelap.ApiRequestError("network"))
        sync.raise_onelap_risk_control(sync.SyncError("risk control elsewhere"))

        with self.assertRaisesRegex(onelap.ApiRequestError, "风控"):
            sync.raise_onelap_risk_control(onelap.ApiRequestError("顽鹿风控"))


if __name__ == "__main__":
    unittest.main()
