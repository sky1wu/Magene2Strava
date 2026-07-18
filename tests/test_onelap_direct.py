from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import download_latest_fit as onelap


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class OneLapDirectTests(unittest.TestCase):
    def test_sanitized_login_har_replays_request_after_cache_expiry(self) -> None:
        login_entry = {
            "request": {
                "url": f"https://{onelap.API_HOST}{onelap.LOGIN_PATH}",
                "headers": [],
                "postData": {"text": '{"account":"encrypted","password":"hash"}'},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            har_path = root / "login.har"
            cache_path = root / "token.json"
            har_path.write_text(
                json.dumps({"log": {"entries": [login_entry]}}), encoding="utf-8"
            )
            with (
                patch.object(onelap, "har_paths", return_value=[har_path]),
                patch.object(onelap, "perform_login", return_value=("token", "uid", {})) as login,
            ):
                headers, source = onelap.obtain_auth(
                    cache_path, str(har_path), str(har_path), 10
                )

        self.assertEqual(source, "login")
        self.assertEqual(headers["Authorization"], "token")
        login.assert_called_once_with(login_entry, 10)

    def test_configure_direct_auth_hashes_password_and_saves_tokens(self) -> None:
        requests = []

        def fake_open(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(
                json.dumps(
                    {
                        "code": 200,
                        "data": {"token": "access", "refresh_token": "refresh"},
                    }
                ).encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / onelap.DIRECT_AUTH_CACHE
            with patch.object(onelap, "urlopen", side_effect=fake_open):
                onelap.configure_direct_auth(path, "rider@example.com", "plain-secret", 12)

            stored = json.loads(path.read_text(encoding="utf-8"))

        request, timeout = requests[0]
        body = request.data
        expected_hash = hashlib.md5(
            b"plain-secret", usedforsecurity=False
        ).hexdigest().encode("ascii")
        self.assertEqual(request.full_url, "https://www.onelap.cn/api/login")
        self.assertEqual(timeout, 12)
        self.assertIn(b"rider@example.com", body)
        self.assertIn(expected_hash, body)
        self.assertNotIn(b"plain-secret", body)
        self.assertEqual(stored["password_md5"], expected_hash.decode("ascii"))
        self.assertEqual(stored["token"], "access")
        self.assertEqual(stored["refresh_token"], "refresh")
        self.assertNotIn("password", stored)

    def test_otm_records_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / onelap.DIRECT_AUTH_CACHE
            auth_path.write_text(
                json.dumps(
                    {
                        "account": "rider",
                        "password_md5": "a" * 32,
                        "token": "access",
                        "refresh_token": "refresh",
                    }
                ),
                encoding="utf-8",
            )
            response = {
                "code": 200,
                "data": {
                    "count": 1,
                    "list": [
                        {
                            "id": 123,
                            "name": "晨骑",
                            "start_time": "2026-07-18T06:30:00+08:00",
                            "totalDistance": 32500,
                            "time": 3600,
                        }
                    ],
                },
            }
            captured = []

            def fake_open(request, timeout):
                captured.append(request)
                return FakeResponse(json.dumps(response).encode("utf-8"))

            with patch.object(onelap, "urlopen", side_effect=fake_open):
                records = onelap.OneLapOtmClient(auth_path, 10).records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "123")
        self.assertEqual(records[0]["total_distance"], 32500)
        self.assertEqual(records[0]["total_time"], 3600)
        self.assertEqual(captured[0].get_header("Authorization"), "access")

    def test_otm_records_use_detail_for_missing_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / onelap.DIRECT_AUTH_CACHE
            auth_path.write_text(
                json.dumps(
                    {
                        "account": "rider",
                        "password_md5": "c" * 32,
                        "token": "access",
                        "refresh_token": "refresh",
                    }
                ),
                encoding="utf-8",
            )

            def fake_open(request, timeout):
                if request.full_url.endswith("/analysis/456"):
                    payload = {
                        "code": 200,
                        "data": {
                            "ridingRecord": {
                                "id": 336072,
                                "totalDistance": 42000,
                                "time": 5400,
                            }
                        },
                    }
                else:
                    payload = {
                        "code": 200,
                        "data": {
                            "list": [
                                {
                                    "id": 456,
                                    "start_riding_time": "2026-07-18T06:30:00+08:00",
                                }
                            ]
                        },
                    }
                return FakeResponse(json.dumps(payload).encode("utf-8"))

            with patch.object(onelap, "urlopen", side_effect=fake_open):
                records = onelap.OneLapOtmClient(auth_path, 10).records()

        self.assertEqual(records[0]["total_distance"], 42000)
        self.assertEqual(records[0]["total_time"], 5400)
        self.assertEqual(records[0]["id"], "456")

    def test_otm_records_supports_current_list_metrics_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / onelap.DIRECT_AUTH_CACHE
            auth_path.write_text(
                json.dumps({"account": "rider", "password_md5": "e" * 32, "token": "access"}),
                encoding="utf-8",
            )
            response = {
                "code": 200,
                "data": {
                    "pagination": {"total": 1, "has_more": False},
                    "list": [{
                        "id": "activity-1",
                        "start_riding_time": "2026-07-18T06:30:00+08:00",
                        "distance_km": 32.5,
                        "time_seconds": 3600,
                        "load_tss": 42.5,
                    }],
                },
            }

            with patch.object(onelap, "urlopen", return_value=FakeResponse(json.dumps(response).encode("utf-8"))) as opened:
                records = onelap.OneLapOtmClient(auth_path, 10).records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["total_distance"], 32500)
        self.assertEqual(records[0]["total_time"], 3600)
        self.assertEqual(records[0]["TSS"], 42.5)
        self.assertEqual(opened.call_count, 1)

    def test_otm_records_continues_after_server_capped_short_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / onelap.DIRECT_AUTH_CACHE
            auth_path.write_text(
                json.dumps({"account": "rider", "password_md5": "d" * 32, "token": "access"}),
                encoding="utf-8",
            )
            pages = []

            def fake_open(request, timeout):
                body = json.loads(request.data.decode("utf-8"))
                pages.append(body["page"])
                items = [
                    {
                        "id": body["page"],
                        "start_riding_time": "2026-07-18T06:30:00+08:00",
                        "distance": 1000,
                        "time": 60,
                    }
                ] if body["page"] < 3 else []
                return FakeResponse(json.dumps({"code": 200, "data": {"list": items}}).encode("utf-8"))

            with patch.object(onelap, "urlopen", side_effect=fake_open):
                records = onelap.OneLapOtmClient(auth_path, 10).records(page_size=50)

        self.assertEqual(len(records), 2)
        self.assertEqual(pages, [1, 2, 3])

    def test_otm_client_refreshes_after_unauthorized_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / onelap.DIRECT_AUTH_CACHE
            auth_path.write_text(
                json.dumps(
                    {
                        "account": "rider",
                        "password_md5": "b" * 32,
                        "token": "stale",
                        "refresh_token": "refresh",
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            def fake_open(request, timeout):
                calls.append((request.full_url, request.get_header("Authorization")))
                if request.full_url.endswith("/api/token"):
                    return FakeResponse(
                        json.dumps(
                            {
                                "code": 200,
                                "data": {"token": "fresh", "refresh_token": "new-refresh"},
                            }
                        ).encode("utf-8")
                    )
                if request.get_header("Authorization") == "stale":
                    raise HTTPError(
                        request.full_url,
                        401,
                        "Unauthorized",
                        {},
                        io.BytesIO(b'{"message":"expired"}'),
                    )
                return FakeResponse(b'{"code":200,"data":{"count":0,"list":[]}}')

            with patch.object(onelap, "urlopen", side_effect=fake_open):
                records = onelap.OneLapOtmClient(auth_path, 10).records()
            stored = json.loads(auth_path.read_text(encoding="utf-8"))

        self.assertEqual(records, [])
        self.assertEqual(stored["token"], "fresh")
        self.assertEqual(stored["refresh_token"], "new-refresh")
        self.assertEqual(calls[-1][1], "fresh")


if __name__ == "__main__":
    unittest.main()
