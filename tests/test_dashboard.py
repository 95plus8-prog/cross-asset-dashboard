import copy
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cross_asset_dashboard as app  # noqa: E402

DAY = "2026-11-27"


def quote(key, latest=100.0, day=DAY):
    return {**app.QUOTE_SYMBOLS[key], "latest": latest, "previous": latest, "five_ago": None,
            "one_day_pct": 0.0, "five_day_pct": None, "one_day_change": 0.0,
            "five_day_change": None, "date": f"{day}T16:00:00-0500", "source": "CNBC quote"}


def quotes(vix=18.0):
    data = {key: quote(key) for key in app.QUOTE_SYMBOLS}
    data["vix"]["latest"] = vix
    return data


FEAR = {"latest": 50.0, "source": "CNN Fear & Greed"}
SPREAD = {"latest": 3.0, "five_day_change": 0.0, "date": DAY, "source": "FRED"}


def boom(*_args, **_kwargs):
    raise RuntimeError("simulated outage")


class BuildSnapshotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name, value in {
            "CACHE_PATH": self.dir / "cache.json",
            "PUBLISHED_PATH": self.dir / "published.json",
            "HISTORY_PATH": self.dir / "history.json",
        }.items():
            patcher = mock.patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, cnbc=None, fear=None, fred=None):
        with mock.patch.object(app, "fetch_cnbc_quotes", cnbc or (lambda: (quotes(), []))), \
             mock.patch.object(app, "fetch_fear_greed", fear or (lambda: dict(FEAR))), \
             mock.patch.object(app, "fetch_fred", fred or (lambda _s: dict(SPREAD))):
            return app.build_snapshot()

    def test_cnn_outage_without_previous_data_does_not_crash(self):
        snapshot = self.build(fear=boom)
        self.assertNotIn("fear_greed", snapshot["metrics"])
        self.assertEqual(["fear_greed"], [e["key"] for e in snapshot["errors"]])
        self.assertEqual("中性运行", snapshot["reading"]["regime"])

    def test_failed_source_reuses_last_value_and_marks_it(self):
        first = self.build()
        second = self.build(fred=boom)
        spread = second["metrics"]["hy_spread"]
        self.assertTrue(spread["stale"])
        self.assertFalse(spread["expired"])
        self.assertEqual(first["generated_at"], spread["fetched_at"])
        third = self.build(fred=boom)
        self.assertEqual(first["generated_at"], third["metrics"]["hy_spread"]["fetched_at"])

    def test_old_value_expires_and_blocks_conclusion(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=app.EXPIRE_HOURS + 1)).isoformat()
        app.write_json(app.PUBLISHED_PATH, {"generated_at": old, "metrics": {"hy_spread": dict(SPREAD)}})
        snapshot = self.build(fred=boom)
        self.assertTrue(snapshot["metrics"]["hy_spread"]["expired"])
        self.assertEqual("数据不足", snapshot["reading"]["regime"])

    def test_legacy_hardcoded_fallback_is_not_reused(self):
        app.write_json(app.PUBLISHED_PATH, {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "metrics": {"hy_spread": {**SPREAD, "source": "FRED fallback snapshot"}}})
        self.assertNotIn("hy_spread", self.build(fred=boom)["metrics"])

    def test_total_outage_keeps_previous_file(self):
        self.build()
        before = app.CACHE_PATH.read_text(encoding="utf-8")
        with self.assertRaises(app.NoDataError):
            self.build(cnbc=boom, fear=boom, fred=boom)
        self.assertEqual(before, app.CACHE_PATH.read_text(encoding="utf-8"))

    def test_stale_ratio_from_different_days_is_expired(self):
        self.build()

        def without_rsp():
            data = {k: quote(k, day="2026-11-30") for k in app.QUOTE_SYMBOLS if k != "rsp"}
            return data, [{"key": "rsp", "error": "missing"}]

        snapshot = self.build(cnbc=without_rsp)
        self.assertTrue(snapshot["metrics"]["rsp_spy"]["expired"])
        self.assertNotIn("stale", snapshot["metrics"]["iwm_spy"])


class ParsingTest(unittest.TestCase):
    def test_one_missing_symbol_keeps_the_others(self):
        rows = [{"symbol": c["symbol"], "last": "1,234.5", "previous_day_closing": "1,200"}
                for k, c in app.QUOTE_SYMBOLS.items() if k != "gold"]
        body = json.dumps({"QuickQuoteResult": {"QuickQuote": rows}}).encode()
        with mock.patch.object(app, "http_get", return_value=body):
            metrics, errors = app.fetch_cnbc_quotes()
        self.assertEqual(8, len(metrics))
        self.assertEqual(["gold"], [e["key"] for e in errors])

    def test_retry(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("timeout")
            return "ok"

        with mock.patch.object(app.time, "sleep"):
            self.assertEqual("ok", app.with_retry(flaky))
        self.assertEqual(3, len(calls))

    def test_non_finite_numbers_are_rejected(self):
        self.assertIsNone(app.to_float("nan"))
        self.assertIsNone(app.to_float("inf"))


class HistoryTest(unittest.TestCase):
    days = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]

    def test_five_day_change(self):
        history = {"spy": {d: 100.0 + i for i, d in enumerate(self.days)}}
        metrics = {"spy": quote("spy", 110.0, "2026-11-23")}
        app.apply_history(metrics, history)
        self.assertEqual(100.0, metrics["spy"]["five_ago"])
        self.assertAlmostEqual(10.0, metrics["spy"]["five_day_pct"])
        self.assertEqual(110.0, history["spy"]["2026-11-23"])

    def test_large_gap_leaves_blank(self):
        history = {"spy": {d: 100.0 for d in self.days}}
        metrics = {"spy": quote("spy", 110.0, "2026-12-07")}
        app.apply_history(metrics, history)
        self.assertIsNone(metrics["spy"]["five_day_pct"])

    def test_weekend_is_not_recorded(self):
        history = {}
        app.apply_history({"gold": quote("gold", day="2026-11-29")}, history)
        self.assertEqual({}, history["gold"])


def scenarios():
    def base(**changes):
        data = {**quotes(), "fear_greed": dict(FEAR), "hy_spread": dict(SPREAD),
                "rsp_spy": {"one_day_pct": 0.0, "five_day_pct": None},
                "iwm_spy": {"one_day_pct": 0.0, "five_day_pct": None}}
        for path, value in changes.items():
            key, field = path.split("__")
            data[key][field] = value
        return data

    missing = base()
    del missing["hy_spread"]
    return [
        ("中性运行", base()),
        ("中性运行", base(fear_greed__latest=10)),
        ("可分批进攻", base(fear_greed__latest=10, rsp_spy__one_day_pct=0.5)),
        ("中性运行", base(fear_greed__latest=25)),
        ("谨慎观察", base(fear_greed__latest=76)),
        ("谨慎观察", base(vix__latest=30)),
        ("防守优先", base(vix__latest=30, dxy__one_day_pct=1.2)),
        ("防守优先", base(vix__latest=45, fear_greed__latest=5)),
        ("防守优先", base(hy_spread__latest=5)),
        ("谨慎观察", base(hyg__five_day_pct=-1.5)),
        ("中性运行", base(dxy__one_day_pct=2, dxy__five_day_pct=0.0)),
        ("数据不足", missing),
        ("数据不足", base(vix__expired=True)),
        ("中性运行", base(gold__latest=None)),
    ]


class RiskReadingTest(unittest.TestCase):
    def test_python_rules(self):
        for index, (expected, metrics) in enumerate(scenarios()):
            with self.subTest(index=index):
                self.assertEqual(expected, app.risk_reading(metrics)["regime"])

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js")
    def test_netlify_matches_python(self):
        cases = [metrics for _, metrics in scenarios()]
        history = {"spy": {d: 100.0 + i for i, d in enumerate(HistoryTest.days)}}
        spy = {"spy": quote("spy", 110.0, "2026-11-23")}
        module = (ROOT / "netlify" / "functions" / "markets.mjs").as_uri()
        script = f"""
          const m = await import({json.dumps(module)});
          let text = ""; for await (const chunk of process.stdin) text += chunk;
          const {{ cases, spy, history }} = JSON.parse(text);
          m.applyHistory(spy, history);
          console.log(JSON.stringify({{ readings: cases.map(m.riskReading), spy }}));
        """
        done = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=json.dumps({"cases": cases, "spy": spy, "history": history}),
            capture_output=True, text=True, check=True, timeout=30)
        result = json.loads(done.stdout)
        self.assertEqual([app.risk_reading(copy.deepcopy(c)) for c in cases], result["readings"])
        expected = copy.deepcopy(spy)
        app.apply_history(expected, copy.deepcopy(history))
        self.assertEqual(expected["spy"]["five_ago"], result["spy"]["spy"]["five_ago"])
        self.assertAlmostEqual(expected["spy"]["five_day_pct"], result["spy"]["spy"]["five_day_pct"])


if __name__ == "__main__":
    unittest.main()
