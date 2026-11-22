"""自動更新（版の比較・最新版情報の確認・ダウンロードの検証）のテスト。"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client import updater  # noqa: E402


class _Resp:
    def __init__(self, data=None, body=b""):
        self._data = data
        self._body = body
        self.headers = {"Content-Length": str(len(body))}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestUpdater(unittest.TestCase):
    def test_version_compare(self):
        self.assertTrue(updater.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(updater.is_newer("1.10.0", "1.9.9"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(updater.is_newer("0.9", "1.0.0"))

    def test_fetch_rejects_bad_info(self):
        bad = [
            {"version": "1.0.1", "url": "http://x/a.exe", "sha256": "a" * 64},   # https でない
            {"version": "1.0.1", "url": "https://x/a.exe", "sha256": "short"},  # 確認用の値が変
            {"version": "", "url": "https://x/a.exe", "sha256": "a" * 64},
        ]
        for data in bad:
            with mock.patch("client.updater.requests.get", return_value=_Resp(data)):
                with self.assertRaises(ValueError):
                    updater.fetch_latest("https://example/latest.json")

    def test_fetch_ok_and_empty_url(self):
        data = {"version": "1.2.0", "url": "https://x/OnseiMan-Setup-1.2.0.exe", "sha256": "A" * 64, "notes": "改善"}
        with mock.patch("client.updater.requests.get", return_value=_Resp(data)):
            info = updater.fetch_latest("https://example/latest.json")
        self.assertEqual(info.version, "1.2.0")
        self.assertEqual(info.sha256, "a" * 64)
        self.assertIsNone(updater.fetch_latest(""))

    def test_download_checks_hash(self):
        body = b"installer-bytes" * 1000
        good = updater.UpdateInfo("9.9.9", "https://x/i.exe", hashlib.sha256(body).hexdigest())
        with mock.patch("client.updater.requests.get", return_value=_Resp(body=body)):
            path = updater.download_installer(good)
        self.assertEqual(path.read_bytes(), body)
        path.unlink()

        bad = updater.UpdateInfo("9.9.8", "https://x/i.exe", "0" * 64)
        with mock.patch("client.updater.requests.get", return_value=_Resp(body=body)):
            with self.assertRaises(ValueError):
                updater.download_installer(bad)
        self.assertFalse((Path(updater.tempfile.gettempdir()) / "OnseiManUpdate" / "OnseiMan-Setup-9.9.8.exe").exists())


if __name__ == "__main__":
    unittest.main()
