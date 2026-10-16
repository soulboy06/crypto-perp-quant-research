from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from perpquant.archive import ArchiveSpec, csv_bytes, download_one, preserve, verify_checksum
from perpquant.config import load
from perpquant.quality import KLINE_HEADER, candles, causal_mark_proxy, crosscheck_funding, funding

START = 1672531200000000


def candle_csv(unit="ms", duplicate=False):
    lines = [",".join(KLINE_HEADER)]
    for index in range(2):
        opening = START + (0 if duplicate else index) * 60_000_000
        closing = opening + 60_000_000 - (1000 if unit == "ms" else 1)
        if unit == "ms":
            opening, closing = opening // 1000, closing // 1000
        lines.append(f"{opening},100,101,99,100,1,{closing},100,1,0.5,50,0")
    return ("\n".join(lines) + "\n").encode()


class DataBehaviorTests(unittest.TestCase):
    def test_funding_price_uses_closed_history_and_fails_on_stale_boundary(self):
        closed = [{"available_time_us": START, "close": Decimal(100)},
                  {"available_time_us": START + 60_000_000, "close": Decimal(999)}]
        proxy = causal_mark_proxy(START + 8000, closed)
        self.assertEqual(proxy["proxy_mark"], 100)
        self.assertEqual(proxy["age_us"], 8000)
        with self.assertRaises(ValueError):
            causal_mark_proxy(START - 1, closed)
        with self.assertRaises(ValueError):
            causal_mark_proxy(START + 120_000_001, closed)

    def test_holdout_rejected_before_any_network_request(self):
        config = load(ROOT / "configs/first_round.toml")
        with patch("perpquant.archive.get_bytes") as network:
            with self.assertRaises(ValueError):
                download_one(ROOT, config, ArchiveSpec("BTCUSDT", "klines", "2026-01-01"))
            with self.assertRaises(ValueError):
                download_one(ROOT, config, ArchiveSpec("BTCUSDT", "fundingRate", "2026-01"))
            network.assert_not_called()

    def test_checksum_and_member_name_prevent_wrong_data(self):
        body = b"archive"
        checksum = (hashlib.sha256(body).hexdigest() + "  sample.zip\n").encode()
        verify_checksum(body, checksum, "sample.zip")
        for value, name in [(b"corrupt", "sample.zip"), (body, "other.zip")]:
            with self.assertRaises(ValueError):
                verify_checksum(value, checksum, name)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../sample.csv", "untrusted")
        with self.assertRaises(ValueError):
            csv_bytes(output.getvalue(), "sample.csv")

    def test_versions_never_overwrite_preserved_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.zip"
            preserve(path, b"first")
            preserve(path, b"first")
            with self.assertRaises(ValueError):
                preserve(path, b"changed")
            self.assertEqual(path.read_bytes(), b"first")

    def test_timestamp_units_preserve_precision_and_close_availability(self):
        for unit in ("ms", "us"):
            parsed, detected = candles(candle_csv(unit), START, START + 120_000_000)
            self.assertEqual(detected, unit)
            self.assertEqual(parsed[0]["available_time_us"], START + 60_000_000)
            self.assertEqual(parsed[1]["open_time_us"], START + 60_000_000)
        with self.assertRaises(ValueError):
            candles(candle_csv(duplicate=True), START, START + 120_000_000)
        with self.assertRaises(ValueError):
            candles(candle_csv().replace(b",101,99,", b",98,99,"), START, START + 120_000_000)

    def test_funding_jitter_is_preserved_and_empty_marks_are_not_zero(self):
        body = b"calc_time,funding_interval_hours,last_funding_rate\n1672531200000,8,0.0001\n1672560000008,8,-0.0002\n"
        parsed, unit = funding(body, START, START + 16 * 3_600_000_000)
        self.assertEqual(parsed[1]["event_time_us"], 1672560000008000)
        api = [{"symbol": "BTCUSDT", "fundingTime": 1672531200000, "fundingRate": "0.0001", "markPrice": ""},
               {"symbol": "BTCUSDT", "fundingTime": 1672560000008, "fundingRate": "-0.0002", "markPrice": "100"}]
        summary = crosscheck_funding(parsed, api, "BTCUSDT", START, START + 16 * 3_600_000_000)
        self.assertEqual(summary["missing_exact_settlement_marks"], 1)
        api[1]["fundingTime"] += 1
        with self.assertRaises(ValueError):
            crosscheck_funding(parsed, api, "BTCUSDT", START, START + 16 * 3_600_000_000)
