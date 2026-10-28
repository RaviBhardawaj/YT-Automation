"""Compliance-module tests. Run: .venv/bin/python -m unittest discover -s tests -v

All prices here are SYNTHETIC TEST FIXTURES (source 'test_fixture', status PROHIBITED) — they
are not NIFTY 50 market data and the tests assert they can never be published."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
ROOT = Path(__file__).resolve().parent.parent

from studio import config, db, settings  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels as _channels  # noqa: E402
_channels.ensure_default()
# never read the owner's real Pivot Reversal files from tests
settings.set("nifty.pivot_project_dir", tempfile.mkdtemp(prefix="pivot-project-empty-"))
for f in (ROOT / "data" / "brand" / "fonts").glob("*"):          # OFL fonts for rendering checks
    if not (config.FONTS_DIR / f.name).exists():
        shutil.copy(f, config.FONTS_DIR / f.name)

from studio.finance import analysis, audit, gate, lint, marketdata, pivots, rules  # noqa: E402
from studio.finance import calendar as fin_calendar  # noqa: E402

rules.seed()
marketdata.seed_sources()


def fixture_csv(start: date, n: int, base: float = 24000.0, header: str = "Index Name,Date,Open,High,Low,Close") -> str:
    """Deterministic synthetic weekday bars (NOT market data)."""
    rows, d, price = [header], start, base
    while len(rows) <= n:
        if fin_calendar.is_trading_day(d):
            o = round(price, 2)
            c = round(price + ((len(rows) * 37) % 120 - 60), 2)
            h = round(max(o, c) + 40 + (len(rows) * 13) % 30, 2)
            l = round(min(o, c) - 35 - (len(rows) * 7) % 25, 2)
            rows.append(f"NIFTY 50,{d.strftime('%d %b %Y')},{o},{h},{l},{c}")
            price = c
        d += timedelta(days=1)
    return "\n".join(rows)


def import_fixture(start: date = date(2026, 7, 1), n: int = 45, source: str = "test_fixture") -> dict:
    return marketdata.import_file(None, text=fixture_csv(start, n), original_name="fixture.csv", source_id=source,
                                  timeframe="1d", data_nature="end_of_day",
                                  retrieved_at_local="2026-10-22T19:00", retrieved_by="unit test")


class PivotTests(unittest.TestCase):
    def test_known_values(self):
        lv = pivots.fibonacci_pivots(24812.35, 24601.90, 24750.15)
        self.assertEqual(lv["levels"], {"P": 24721.47, "R1": 24801.86, "S1": 24641.07, "R2": 24851.52, "S2": 24591.41,
                                        "R3": 24931.92, "S3": 24511.02})
        self.assertEqual(lv["range"], 210.45)

    def test_round_numbers_and_symmetry(self):
        lv = pivots.fibonacci_pivots(25000, 24800, 24900)["levels"]
        self.assertEqual((lv["P"], lv["R2"], lv["S2"], lv["R3"], lv["S3"]), (24900.0, 25023.6, 24776.4, 25100.0, 24700.0))

    def test_inconsistent_session_rejected(self):
        with self.assertRaises(ValueError):
            pivots.fibonacci_pivots(24800, 24900, 24850)


class ParsingAndValidationTests(unittest.TestCase):
    RET = datetime(2026, 10, 22, 19, 0, tzinfo=marketdata.IST)

    def test_parses_nse_website_layout(self):
        text = ('Date ,Open ,High ,Low ,Close ,Shares Traded ,Turnover (₹ Cr)\n'
                '"01-SEP-2026","24,700.10","24,812.35","24,601.90","24,750.15","312345678","25000.12"\n')
        bars = marketdata.parse_csv(text, "1d")
        self.assertEqual(bars[0]["trade_date"], "2026-09-01")
        self.assertEqual((bars[0]["high"], bars[0]["close"]), (24812.35, 24750.15))

    def test_rejects_other_index(self):
        with self.assertRaises(marketdata.DataError):
            marketdata.parse_csv("Index Name,Date,Open,High,Low,Close\nNIFTY BANK,01 Sep 2026,1,2,1,1\n", "1d")

    def test_missing_value_is_not_filled(self):
        with self.assertRaises(marketdata.DataError):
            marketdata.parse_csv("Date,Open,High,Low,Close\n01 Sep 2026,1,2,,1\n", "1d")

    def test_validation_rules(self):
        bars = [
            {"trade_date": "2026-09-01", "ts": None, "open": 100, "high": 90, "low": 80, "close": 85},   # high < open
            {"trade_date": "2026-09-02", "ts": None, "open": 100, "high": 110, "low": 95, "close": 105},
            {"trade_date": "2026-09-02", "ts": None, "open": 101, "high": 110, "low": 95, "close": 105},  # conflicting dup
            {"trade_date": "2026-09-05", "ts": None, "open": 100, "high": 110, "low": 95, "close": 105},  # Saturday
            {"trade_date": "2026-09-20", "ts": None, "open": 100, "high": 110, "low": 95, "close": 105},  # gap
            {"trade_date": "2026-10-30", "ts": None, "open": 100, "high": 110, "low": 95, "close": 105},  # future
        ]
        rep = marketdata.validate(bars, "1d", retrieved_at=self.RET)
        joined = " ".join(rep["errors"])
        self.assertIn("inconsistent OHLC", joined)
        self.assertIn("conflicting duplicate", joined)
        self.assertIn("after the retrieval time", joined)
        self.assertIn("weekend", joined)
        self.assertTrue(any("missing session" in w for w in rep["warnings"]))

    def test_holiday_bar_is_rejected(self):
        bar = {"ts": None, "open": 100, "high": 110, "low": 95, "close": 105}
        rep = marketdata.validate([{"trade_date": "2026-10-20", **bar}], "1d", retrieved_at=self.RET)
        self.assertTrue(any("Dussehra" in e for e in rep["errors"]))

    def test_incomplete_intraday_session_warned(self):
        bars = [{"trade_date": "2026-09-01", "ts": f"2026-09-01T{9 + i // 4:02d}:{15 * (i % 4):02d}:00+05:30",
                 "open": 100, "high": 110, "low": 95, "close": 105} for i in range(1, 11)]
        rep = marketdata.validate(bars, "15m", retrieved_at=self.RET)
        self.assertTrue(any("10 of 25" in w for w in rep["warnings"]))

    def test_incomplete_session_rejected(self):
        early = datetime(2026, 10, 22, 11, 0, tzinfo=marketdata.IST)
        rep = marketdata.validate([{"trade_date": "2026-10-22", "ts": None, "open": 1, "high": 2, "low": 1, "close": 1.5}],
                                  "1d", retrieved_at=early)
        self.assertTrue(any("still open" in e for e in rep["errors"]))

    def test_import_stores_immutable_copy_and_audits(self):
        res = import_fixture()
        ds = marketdata.dataset(res["dataset_id"])
        self.assertIn(ds["status"], ("valid", "valid_with_warnings"))
        self.assertEqual(audit.file_sha256(Path(ds["stored_path"])), ds["sha256"])
        self.assertFalse(os.access(ds["stored_path"], os.W_OK))
        self.assertTrue(any(e["event"] == "dataset_imported" for e in audit.entries()))

    def test_invalid_dataset_has_no_bars(self):
        bad = "Date,Open,High,Low,Close\n01 Sep 2026,100,90,80,85\n"
        res = marketdata.import_file(None, text=bad, original_name="bad.csv", source_id="test_fixture", timeframe="1d",
                                     data_nature="end_of_day", retrieved_at_local="2026-10-22T19:00", retrieved_by="t")
        self.assertEqual(res["status"], "invalid")
        self.assertEqual(marketdata.daily_bars(res["dataset_id"]), [])

    def test_daily_bars_cannot_be_called_intraday(self):
        with self.assertRaises(marketdata.DataError):
            marketdata.import_file(None, text=fixture_csv(date(2026, 7, 1), 5), original_name="x.csv",
                                   source_id="test_fixture", timeframe="1d", data_nature="intraday",
                                   retrieved_at_local="2026-10-22T19:00", retrieved_by="t")


class FactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = import_fixture()["dataset_id"]
        cls.bars = marketdata.daily_bars(cls.ds)

    def test_historical_case_study_facts(self):
        basis = self.bars[30]["trade_date"]
        f = analysis.build_facts(self.ds, basis, "historical_case_study", "English")
        self.assertEqual(f["review_date"], self.bars[31]["trade_date"])
        ids = {o["id"] for o in f["observations"]}
        self.assertTrue({"change", "close_location", "range_vs_avg", "level_spacing", "gap", "s2_review"} <= ids)
        self.assertTrue(any("No verified 15-minute or 1-hour" in x for x in f["limitations"]))

    def test_no_invented_session(self):
        with self.assertRaises(marketdata.DataError):
            analysis.build_facts(self.ds, "2026-07-04", "historical_case_study")      # a Saturday not in the data

    def test_thirty_day_lag_enforced_for_historical_mode(self):
        recent = import_fixture(start=date.today() - timedelta(days=20), n=8)["dataset_id"]
        bars = marketdata.daily_bars(recent)
        with self.assertRaises(marketdata.DataError) as ctx:
            analysis.build_facts(recent, bars[2]["trade_date"], "historical_case_study", "English")
        self.assertIn("30 days", str(ctx.exception))

    def test_hindi_facts_are_in_devanagari(self):
        from studio.finance import episode
        f = analysis.build_facts(self.ds, self.bars[30]["trade_date"], "historical_case_study", "Hindi")
        self.assertEqual(f["lang"], "hi")
        self.assertIn("अगस्त", f["basis_label"])
        for o in f["observations"]:
            self.assertGreater(episode.devanagari_ratio(o["text"]), 0.5, o["text"])
        self.assertEqual(lint.prohibited_language(" ".join(o["text"] for o in f["observations"]) + " ".join(f["limitations"])), [])

    def test_daily_mode_states_levels_are_not_predictions(self):
        f = analysis.build_facts(self.ds, self.bars[-1]["trade_date"], "daily_levels", "English")
        self.assertNotIn("review", f)
        self.assertTrue(any("not predictions" in x for x in f["limitations"]))


class LintTests(unittest.TestCase):
    def assertFlagged(self, text, category):
        cats = {f["category"] for f in lint.prohibited_language(text)}
        self.assertIn(category, cats, text)

    def test_prohibited_english(self):
        self.assertFlagged("Buy now above 24,800 for a quick move", "trade_instruction")
        self.assertFlagged("Keep a stop loss below S2", "target_stoploss")
        self.assertFlagged("Our next target is R2", "target_stoploss")
        self.assertFlagged("This setup is sure-shot and 100% accurate", "guarantee")
        self.assertFlagged("Our levels have an 85% win rate", "performance_claim")
        self.assertFlagged("Nifty will definitely reach R3", "certain_prediction")
        self.assertFlagged("Hurry, last chance to join our Telegram", "urgency")
        self.assertFlagged("A SEBI registered analyst explains", "registration_claim")
        self.assertFlagged("Buy the 24800 CE today", "options_call")

    def test_prohibited_hindi_and_hinglish(self):
        self.assertFlagged("अभी खरीदें और टारगेट 25000", "trade_instruction")
        self.assertFlagged("स्टॉप लॉस S2 के नीचे", "target_stoploss")
        self.assertFlagged("पक्का मुनाफा गारंटी", "guarantee")
        self.assertFlagged("abhi kharido, pakka profit", "trade_instruction")

    def test_educational_language_not_flagged(self):
        for ok in ["S2 is a reference level, not a target.",
                   "Technical levels are analytical reference points, not guarantees of future price movements.",
                   "Selling pressure pushed the index toward the pivot, and buyers returned near S1.",
                   "Index level pe S2 tak pahuncha, phir close upar hua.",
                   "These levels are not trade signals."]:
            self.assertEqual(lint.prohibited_language(ok), [], ok)

    def test_numbers_must_come_from_fact_sheet(self):
        facts = {"numbers": {"level_S2": 24591.41, "basis_close": 24750.15}, "date_numbers": [1, 2026]}
        self.assertEqual(lint.unverified_numbers("S2 sits at 24,591.41 after a close of 24,750.15 on 1 Sep 2026", facts), [])
        self.assertEqual(lint.unverified_numbers("R2 near 24,900", facts), ["24,900"])
        self.assertEqual(lint.unverified_numbers("S2 and R3 are labels, not numbers", facts), [])
        self.assertEqual(lint.unverified_numbers("S2 २४,५९१.४१ पर है", facts), [])          # Devanagari digits verified
        self.assertEqual(lint.unverified_numbers("स्तर २५,०००", facts), ["25,000"])           # …and caught when wrong


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds_fixture = import_fixture()["dataset_id"]
        # same synthetic numbers attributed to the manual NSE Indices source (status REVIEW_REQUIRED)
        cls.ds_manual = import_fixture(source="manual_niftyindices")["dataset_id"]
        cls.ds_fixture = import_fixture(source="test_fixture")["dataset_id"]

    def make_production(self, ds_id: int, mutate=None, language: str = "English") -> dict:
        from studio.finance import episode, i18n
        basis = marketdata.daily_bars(ds_id)[30]["trade_date"]
        facts = analysis.build_facts(ds_id, basis, "historical_case_study", language)
        src = marketdata.source(facts["source_id"])
        disclosure = lint.disclosure_text(facts, src["name"])
        lv, fmt = facts["pivots"]["levels"], analysis.fmt
        obs = {o["id"]: o["text"] for o in facts["observations"]}
        scenes = [
            {"purpose": "intro", "narration": episode.intro_text(facts), "on_screen_text": "", "observation_ids": []},
            {"purpose": "context", "narration": obs["change"] + " " + obs["close_location"], "on_screen_text": "",
             "observation_ids": ["change", "close_location"]},
            {"purpose": "calculation", "narration": f"The pivot is the average of the high, low and close, giving "
             f"{fmt(lv['P'])}. R2 adds sixty-one point eight percent of the range and S2 subtracts it: S2 is "
             f"{fmt(lv['S2'])} and R2 is {fmt(lv['R2'])}. " + obs["level_spacing"], "on_screen_text": f"S2 {fmt(lv['S2'])}",
             "observation_ids": ["level_spacing"]},
            {"purpose": "review", "narration": obs["gap"] + " " + obs["s2_review"] + " " + obs["r2_review"],
             "on_screen_text": "", "observation_ids": ["gap", "s2_review", "r2_review"]},
            {"purpose": "concept", "narration": "A wider daily range spreads the levels further apart, so the same "
             "ratio describes a bigger zone. That is why the spacing of S2 and R2 changes from session to session and "
             "should be read alongside the size of the day that produced it, rather than as fixed lines.",
             "on_screen_text": "Range sets the spacing", "observation_ids": ["range_vs_avg"]},
            {"purpose": "limitations", "narration": " ".join(facts["limitations"]), "on_screen_text": "",
             "observation_ids": []},
            {"purpose": "disclosure", "narration": disclosure, "on_screen_text": "", "observation_ids": []},
        ]
        for s in scenes:
            s["caption_text"] = s["narration"]
        pkg = {"title": i18n.t("title", facts["lang"], date=facts["basis_label"]), "tags": episode.TAGS,
               "scenes": scenes, "thumbnail": {"generated_by": "deterministic", "lines": [f"S2 {fmt(lv['S2'])}"]},
               "final_description": episode.compose_description(facts, disclosure)}
        p = {"id": 999000 + ds_id, "kind": "nifty_pivot", "package": pkg, "episode": {"facts": facts, "disclosure": disclosure},
             "timeline": {"voice": {"voice_id": "af_alloy", "commercial": True}, "disclosure_ribbon": True}}
        if mutate:
            mutate(p)
        return p

    def assets(self):
        return [{"scene_index": 0, "license": "Original — drawn by this app from dataset #1"}]

    def check(self, result, no):
        return next(c for c in result["checks"] if c["no"] == no)

    def test_clean_episode_with_unlicensed_source_is_private_only(self):
        r = gate.run(self.make_production(self.ds_manual), self.assets())
        self.assertEqual(r["critical_failures"], [], r)
        self.assertEqual(r["status"], "PRIVATE_REVIEW_ONLY")
        self.assertEqual(r["allowed_privacy"], ["private"])
        self.assertFalse(self.check(r, 3)["ok"])           # data licence not recorded
        self.assertFalse(self.check(r, 10)["ok"])          # legal questions open
        self.assertEqual(gate.permits(r, "public")[0], False)
        self.assertEqual(gate.permits(r, "private")[0], True)

    def test_hindi_episode_title_and_disclosure_review(self):
        def concept_hi(p):     # replace the English free-text scene with Hindi
            p["package"]["scenes"][4]["narration"] = ("रेंज जितनी चौड़ी होती है, संदर्भ स्तर उतने दूर होते हैं। इसलिए हर सत्र में "
                                                      "S2 और R2 की दूरी बदलती है, और इन्हें उस दिन की रेंज के साथ पढ़ना चाहिए।")
            p["package"]["scenes"][2]["narration"] = p["episode"]["facts"]["observations"][3]["text"]
        r = gate.run(self.make_production(self.ds_manual, concept_hi, language="Hindi"), self.assets())
        self.assertEqual(r["critical_failures"], [], [c for c in r["checks"] if not c["ok"]])
        self.assertIn("DISCLOSURE-HI", self.check(r, 1)["detail"])
        self.assertEqual(r["allowed_privacy"], ["private"])
        wrong = self.make_production(self.ds_manual, concept_hi, language="Hindi")
        wrong["package"]["title"] = "NIFTY 50 Fibonacci Pivot Analysis | 12 Aug 2026"
        self.assertFalse(self.check(gate.run(wrong, self.assets()), 6)["ok"])

    def test_fixture_source_can_never_publish(self):
        r = gate.run(self.make_production(self.ds_fixture), self.assets())
        self.assertEqual(r["status"], "NEEDS_REVIEW")
        self.assertEqual(r["allowed_privacy"], [])

    def test_recommendation_language_blocks_everything(self):
        def bad(p):
            p["package"]["scenes"][3]["narration"] += " Traders should buy now near S2 with a stop loss below S3."
        r = gate.run(self.make_production(self.ds_manual, bad), self.assets())
        self.assertEqual(r["status"], "NEEDS_REVIEW")
        self.assertFalse(self.check(r, 5)["ok"])

    def test_altered_number_blocks(self):
        def bad(p):
            p["package"]["scenes"][2]["narration"] += " Some say 25,555.55 matters."
        r = gate.run(self.make_production(self.ds_manual, bad), self.assets())
        self.assertFalse(self.check(r, 2)["ok"])
        self.assertEqual(r["status"], "NEEDS_REVIEW")

    def test_missing_disclosure_blocks(self):
        def bad(p):
            p["package"]["scenes"] = p["package"]["scenes"][:-1]
            p["timeline"]["disclosure_ribbon"] = False
        r = gate.run(self.make_production(self.ds_manual, bad), self.assets())
        self.assertFalse(self.check(r, 8)["ok"])

    def test_live_claim_and_wrong_title_block(self):
        def bad(p):
            p["package"]["title"] = "NIFTY Guaranteed Target"
            p["package"]["scenes"][1]["narration"] += " This is live data."
        r = gate.run(self.make_production(self.ds_manual, bad), self.assets())
        self.assertFalse(self.check(r, 6)["ok"])

    def test_intraday_dataset_from_prohibited_source_is_critical(self):
        def with_intraday(p):
            p["episode"]["facts"]["intraday"] = {"timeframes": ["15m"], "dataset_ids": {"15m": self.ds_fixture}}
        r = gate.run(self.make_production(self.ds_manual, with_intraday), self.assets())
        c3 = self.check(r, 3)
        self.assertFalse(c3["ok"])
        self.assertEqual(c3["severity"], gate.CRITICAL)
        self.assertIn(f"intraday dataset #{self.ds_fixture}", c3["detail"])

    def test_resolving_reviews_and_licence_clears(self):
        for rule in ("LEGAL-RA-STATUS", "DATA-NSE-INDICES-LICENCE", "SEBI-RA-MASTER-CIRCULAR", "DISCLOSURE-HI"):
            rules.add_review(rule, "Test Reviewer", "resolved", qualification="test", notes="unit test")
        marketdata.record_licence("manual_niftyindices", reference="TEST-LIC-1", holder="Test", expiry="2099-12-31",
                                  permits_public_video=True, permits_monetization=True, evidence="unit test",
                                  actor="test")
        try:
            r = gate.run(self.make_production(self.ds_manual), self.assets())
            self.assertEqual(r["critical_failures"], [])
            self.assertIn(r["status"], ("CLEARED_PRIVATE_UNTIL_ENABLED", "PRIVATE_REVIEW_ONLY"), r["review_items"])
            settings.set("compliance.public_publishing_enabled", True)
            r2 = gate.run(self.make_production(self.ds_manual), self.assets())
            if not r2["review_items"]:
                self.assertEqual(r2["status"], "CLEARED")
                self.assertTrue(gate.permits(r2, "public")[0])
        finally:
            settings.set("compliance.public_publishing_enabled", False)
            db.ex("DELETE FROM compliance_reviews")
            db.ex("UPDATE data_sources SET status='REVIEW_REQUIRED', permits_public_video=NULL, permits_monetization=NULL, "
                  "licence_reference=NULL WHERE id='manual_niftyindices'")


class CalendarTests(unittest.TestCase):
    def test_sessions_skip_weekends_and_holidays(self):
        self.assertEqual(fin_calendar.next_session(date(2026, 10, 19)), date(2026, 10, 21))     # Dussehra Tue 20 Oct
        self.assertEqual(fin_calendar.previous_session(date(2026, 10, 5)), date(2026, 10, 1))   # Gandhi Jayanti Fri 2 Oct
        self.assertEqual(fin_calendar.next_session(date(2026, 10, 23)), date(2026, 10, 26))     # weekend
        self.assertFalse(fin_calendar.is_trading_day(date(2026, 11, 8)))                        # Muhurat only
        self.assertTrue(fin_calendar.consecutive("2026-10-19", "2026-10-21"))
        self.assertFalse(fin_calendar.consecutive("2026-10-19", "2026-10-22"))

    def test_session_status_before_and_after_close(self):
        fri = date(2026, 10, 23)
        self.assertEqual(fin_calendar.session_status(fri, after_close=False)["basis_session"], "2026-10-22")
        st = fin_calendar.session_status(fri, after_close=True)
        self.assertEqual((st["basis_session"], st["next_session"]), ("2026-10-23", "2026-10-26"))
        self.assertEqual(fin_calendar.session_status(date(2026, 10, 25), after_close=True)["basis_session"], "2026-10-23")
        self.assertFalse(fin_calendar.year_verified(2027))


def _bars(rows, day="2026-08-03", step=15, start=(9, 15)):
    out, h, m = [], *start
    for o, hi, lo, c in rows:
        out.append({"ts": f"{day}T{h:02d}:{m:02d}:00+05:30", "trade_date": day, "open": o, "high": hi, "low": lo, "close": c})
        m += step
        h, m = h + m // 60, m % 60
    return out


class IntradayTests(unittest.TestCase):
    def test_level_states(self):
        from studio.finance import intraday
        bars = _bars([(110, 112, 104, 106), (106, 107, 98, 99), (99, 104, 97, 103), (103, 108, 102, 107)])
        self.assertEqual(intraday.level_behaviour(bars, "S2", 100)["state"], "failed_breakout")
        fb = intraday.level_behaviour(bars, "S2", 100)
        self.assertEqual((fb["first_touch"], fb["first_close_beyond"], fb["returned_at"]), ("09:30", "09:30", "09:45"))
        self.assertEqual(intraday.level_behaviour(bars, "S3", 90)["state"], "not_tested")
        self.assertEqual(intraday.level_behaviour(bars, "S3", 90)["distance"], 7)
        held = _bars([(110, 112, 104, 106), (106, 107, 100.5, 104), (104, 108, 102, 107)])
        self.assertEqual(intraday.level_behaviour(held, "S1", 101)["state"], "tested_held")
        down = _bars([(110, 112, 104, 106), (106, 107, 96, 97), (97, 99, 94, 95)])
        self.assertEqual(intraday.level_behaviour(down, "S2", 100)["state"], "closed_beyond")

    def test_structure_and_comparison(self):
        from studio.finance import intraday
        up = _bars([(100, 105, 99, 104), (104, 108, 103, 107), (107, 111, 106, 110), (110, 114, 109, 113)])
        self.assertEqual(intraday.structure(up)["label"], "rising")
        self.assertEqual(intraday.structure(up)["high_time"], "10:00")
        m15 = _bars([(110, 112, 104, 106), (106, 107, 98, 99), (99, 104, 97, 103)])
        h1 = _bars([(110, 112, 97, 103)], step=60)
        res = intraday.analyse(m15, h1, {"R2": 130, "R1": 120, "P": 111, "S1": 105, "S2": 100})
        self.assertIn("S2", [x["level"] for x in res["comparison"]["disagree"]])

    def test_mtf_levels_use_last_n_bars(self):
        from studio.finance import intraday
        bars = _bars([(100, 101, 99, 100)] * 30)
        bars[0]["high"] = 500                     # outside the 25-bar window: must be ignored
        m = intraday.mtf_levels(bars, "15m")
        self.assertEqual(m["high"], 101)
        self.assertEqual(m["levels"], pivots.fibonacci_pivots(101, 99, 100)["levels"])
        self.assertIsNone(intraday.mtf_levels(bars[:10], "15m"))


class ShortsTests(unittest.TestCase):
    def _parent(self, facts, scenes, durations):
        return {"id": 7001, "kind": "nifty_pivot", "format": "long", "package": {"title": "NIFTY 50 Fibonacci Pivot Analysis | X",
                "scenes": scenes, "chapters": [{"title": "The session", "first_scene": 2},
                                                {"title": "Calculating the pivots", "first_scene": 3}]},
                "episode": {"facts": facts, "disclosure": "FULL DISCLOSURE"},
                "timeline": {"scenes": [{"duration": d} for d in durations]}}

    def _facts(self):
        res = import_fixture(date(2026, 7, 1), 45)
        bars = marketdata.daily_bars(res["dataset_id"])
        return analysis.build_facts(res["dataset_id"], bars[-3]["trade_date"], "historical_case_study", "English")

    def test_segments_are_whole_scenes_and_fit(self):
        from studio.finance import shorts
        facts = self._facts()
        words = lambda n: " ".join(["word"] * n)  # noqa: E731
        scenes = [{"purpose": "intro", "narration": words(30), "observation_ids": []},
                  {"purpose": "context", "narration": words(80), "observation_ids": ["change"]},
                  {"purpose": "calculation", "narration": words(70), "observation_ids": []},
                  {"purpose": "calculation", "narration": words(60), "observation_ids": []},
                  {"purpose": "levels", "narration": words(20), "observation_ids": []},          # too short alone
                  {"purpose": "disclosure", "narration": words(40), "observation_ids": []}]
        parent = self._parent(facts, scenes, [10, 30, 25, 25, 8, 15])
        segs = {c["purpose"]: c for c in shorts.candidates(parent)}
        self.assertIn("calculation", segs)
        self.assertEqual(segs["calculation"]["scenes"], [2])              # 25 s + 25 s > 44 s: keep the first whole scene
        self.assertNotIn("levels", segs)
        self.assertEqual(segs["calculation"]["start"], 40.0)

    def test_long_segment_is_trimmed_to_whole_scenes(self):
        from studio.finance import shorts
        facts = self._facts()
        scenes = [{"purpose": "calculation", "narration": " ".join(["w"] * 45), "observation_ids": []} for _ in range(3)]
        each = shorts.MAX_SEGMENT_SECONDS * 0.45                          # two scenes fit the budget, three do not
        parent = self._parent(facts, scenes, [each] * 3)
        seg = next(c for c in shorts.candidates(parent) if c["purpose"] == "calculation")
        self.assertEqual(seg["scenes"], [0, 1])

    def test_built_short_keeps_parent_numbers_and_ends_with_disclosure(self):
        from studio.finance import shorts
        facts = self._facts()
        scenes = [{"purpose": "calculation", "narration": " ".join(["w"] * 60), "on_screen_text": "P", "visual": "formula",
                   "observation_ids": []}]
        parent = self._parent(facts, scenes, [25])
        built = shorts.build(parent, shorts.candidates(parent)[0])
        sc = built["package"]["scenes"]
        self.assertEqual([x["purpose"] for x in sc], ["hook", "calculation", "outro", "disclosure", "signoff"])
        self.assertEqual(sc[-2]["narration"], built["episode"]["disclosure"])
        self.assertIs(built["episode"]["facts"], facts)
        self.assertIn("FULL DISCLOSURE", built["package"]["description"])
        self.assertTrue(built["package"]["title"].endswith("#Shorts"))
        self.assertEqual(lint.unverified_numbers(" ".join(x["narration"] for x in sc), facts), [])
        # nothing the app itself adds to a Short may trip the gate's language or number checks
        for L in ("English", "Hindi"):
            f2 = analysis.build_facts(facts["dataset_id"], facts["basis_date"], facts["mode"], L)
            parent = self._parent(f2, scenes, [25])
            b2 = shorts.build(parent, shorts.candidates(parent)[0])
            texts = gate._texts({"package": b2["package"]})
            everything = "\n".join(texts.values())
            self.assertEqual(lint.prohibited_language(everything), [], L)
            self.assertEqual(lint.unverified_numbers(texts["description"] + texts["narration"], f2), [], L)


class DailyTriggerTests(unittest.TestCase):
    def test_waits_for_todays_data_and_skips_holidays(self):
        from unittest import mock
        from studio import tasks
        with mock.patch.object(marketdata, "ist_now", return_value=datetime(2026, 10, 20, 16, 0, tzinfo=marketdata.IST)):
            self.assertIn("Dussehra", tasks._nifty_daily({}, {})["skipped"])
        with mock.patch.object(marketdata, "ist_now", return_value=datetime(2026, 10, 23, 14, 0, tzinfo=marketdata.IST)):
            self.assertEqual(tasks._nifty_daily({}, {}), {"skipped": "market still open"})
        from studio import worker
        with mock.patch.object(marketdata, "ist_now", return_value=datetime(2026, 10, 23, 15, 40, tzinfo=marketdata.IST)), \
                mock.patch.object(worker, "enqueue") as enq:
            res = tasks._nifty_daily({}, {})
        self.assertIn("today's (2026-10-23) daily bar", res["waiting_for_data"])
        self.assertEqual(enq.call_args.kwargs["delay"], 600)                # re-check every 10 minutes
        self.assertEqual(enq.call_args.args[1], {"recheck": True})

    def test_recheck_is_queued_while_the_job_itself_is_running(self):
        from unittest import mock
        from studio import tasks, worker
        running = worker.enqueue("nifty_daily_episode", {"recheck": True})          # a re-check itself running
        db.ex("UPDATE tasks SET status='running' WHERE id=?", (running,))
        try:
            with mock.patch.object(marketdata, "ist_now", return_value=datetime(2026, 10, 23, 15, 40, tzinfo=marketdata.IST)):
                tasks._nifty_daily({}, {})
            follow = db.q1("SELECT id, payload FROM tasks WHERE type='nifty_daily_episode' AND status='pending' "
                           "ORDER BY id DESC LIMIT 1")
            self.assertIsNotNone(follow)
            self.assertNotEqual(follow["id"], running)
        finally:
            db.ex("DELETE FROM tasks WHERE type='nifty_daily_episode'")
        from studio import scheduler
        job = next(s for s in scheduler.DEFAULT_SCHEDULES if s["task_type"] == "nifty_daily_episode")
        self.assertEqual(job["time_of_day"], "15:40")


class DailyReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        res = import_fixture(date(2026, 7, 1), 45)
        cls.ds = res["dataset_id"]
        cls.bars = marketdata.daily_bars(cls.ds)

    def test_next_session_levels_come_from_the_reviewed_session(self):
        from studio.finance import episode
        basis, today = self.bars[-2], self.bars[-1]
        f = analysis.build_facts(self.ds, basis["trade_date"], "daily_review", "English")
        self.assertEqual(f["review_date"], today["trade_date"])
        self.assertEqual(f["next"]["levels"], pivots.fibonacci_pivots(today["high"], today["low"], today["close"])["levels"])
        self.assertEqual(f["next"]["date"], fin_calendar.next_session(date.fromisoformat(today["trade_date"])).isoformat())
        self.assertIn("next_levels", [o["id"] for o in f["observations"]])
        secs = episode.required_sections(f)
        self.assertEqual(secs[:2], ["levels", "review"])
        self.assertLess(secs.index("calculation"), secs.index("next_levels"))
        from studio.finance import i18n
        title = episode.title_for(f)
        self.assertIn(i18n.short_date(f["next"]["date"], "en"), title)
        self.assertIn(i18n.short_date(f["review_date"], "en"), title)
        self.assertLessEqual(len(title), 100)

    def test_daily_review_needs_the_session_after_basis(self):
        with self.assertRaises(marketdata.DataError):
            analysis.build_facts(self.ds, self.bars[-1]["trade_date"], "daily_review", "English")

    def test_old_data_is_blocked_as_critical(self):
        from studio.finance import episode
        f = analysis.build_facts(self.ds, self.bars[-2]["trade_date"], "daily_review", "English")
        p = {"id": 990001, "kind": "nifty_pivot", "created_at": "2026-10-23T11:00:00Z",
             "package": {"title": episode.title_for(f), "scenes": [], "thumbnail": {"generated_by": "deterministic"}},
             "episode": {"facts": f, "disclosure": "x"}, "timeline": {}}
        r = gate.run(p, [])
        c2 = next(c for c in r["checks"] if c["no"] == 2)
        self.assertFalse(c2["ok"])
        self.assertIn("old data must not be shown as today's", c2["detail"])
        self.assertEqual(r["status"], "NEEDS_REVIEW")

    def test_hindi_description_fits_youtube_limit(self):
        from studio.finance import episode
        f = analysis.build_facts(self.ds, self.bars[-2]["trade_date"], "daily_review", "Hindi")
        f["observations"] = f["observations"] * 6                     # far more text than fits
        d = episode.compose_description(f, "डिस्क्लोज़र " * 60, [("0:00", "अध्याय")] * 12)
        self.assertLessEqual(len(d.encode()), episode.DESCRIPTION_BYTES)
        for needle in (f["review_label"], f["next"]["label"], "डिस्क्लोज़र", f"P {analysis.fmt(f['next']['levels']['P'])}"):
            self.assertIn(needle, d)

    def test_every_video_ends_with_disclosure_then_anchor_signoff(self):
        from studio.finance import episode, i18n, shorts
        for L in ("English", "Hindi"):
            f = analysis.build_facts(self.ds, self.bars[-2]["trade_date"], "daily_review", L)
            so = episode.signoff_scene(f)
            self.assertEqual(so["purpose"], "signoff")
            self.assertIn(i18n.short_date(f["next"]["date"], f["lang"]), so["narration"])
            for text in (so["narration"], episode.signoff_scene(f, short=True)["narration"]):
                self.assertEqual(lint.prohibited_language(text), [])
                self.assertEqual(lint.unverified_numbers(text, f), [])
                self.assertIsNone(episode.FORECAST.search(text))
            scenes = [{"purpose": "levels", "narration": " ".join(["w"] * 60), "on_screen_text": "", "visual": "levels",
                       "observation_ids": ["level_spacing"]}]
            parent = {"id": 7002, "kind": "nifty_pivot", "format": "long",
                      "package": {"title": "T", "scenes": scenes, "chapters": []},
                      "episode": {"facts": f, "disclosure": "D"}, "timeline": {"scenes": [{"duration": 25}]}}
            built = shorts.build(parent, shorts.candidates(parent)[-1])
            self.assertEqual([s["purpose"] for s in built["package"]["scenes"]][-2:], ["disclosure", "signoff"])
            self.assertIn("#NIFTY50", built["package"]["description"])
            self.assertIn("#NIFTY50", episode.compose_description(f, "D"))

    def test_forecast_wording_is_rejected(self):
        from studio.finance import episode
        self.assertTrue(episode.FORECAST.search("NIFTY will likely rise to R2 tomorrow"))
        self.assertTrue(episode.FORECAST.search("कल बाज़ार गिरेगा"))
        self.assertFalse(episode.FORECAST.search("If price holds above the pivot, analysts often watch R1."))


class PivotProjectImportTests(unittest.TestCase):
    def _write(self, d: Path, date_s="2026-10-23", basis="post_close", calc="2026-10-23 09:09:19", tamper=False):
        import json as _json
        def block(h, lo, c):
            b = {"H": h, "L": lo, "C": c, **pivots.fibonacci_pivots(h, lo, c)["levels"]}
            if tamper:
                b["P"] = b["P"] + 5
            return b
        (d / "tv_pivot_cache.json").write_text(_json.dumps({"NIFTY": {
            "date": date_s, "basis": basis, "Daily": block(22787.25, 22372.0, 22600.0),
            "15-Min": block(22733.0, 22372.15, 22600.0), "1-Hour": block(23175.1, 22372.15, 22600.0)}}))
        (d / "multi_symbol_levels.json").write_text(_json.dumps({"NIFTY": {
            "calculated_at": calc, "mtf_pivots": {"Daily": block(23175.25, 22845.0, 22888.95)}}}))
        late = datetime(2026, 10, 23, 15, 46, tzinfo=marketdata.IST).timestamp()
        os.utime(d / "tv_pivot_cache.json", (late, late))

    def test_import_builds_review_with_unknown_open(self):
        from studio.finance import pivot_project
        d = Path(tempfile.mkdtemp())
        self._write(d)
        old = settings.get("nifty.pivot_project_dir")
        settings.set("nifty.pivot_project_dir", str(d))
        try:
            ds = pivot_project.import_today(date(2026, 10, 23), actor="test")
            bars = marketdata.daily_bars(ds)
            self.assertEqual([b["trade_date"] for b in bars], ["2026-10-22", "2026-10-23"])
            self.assertIsNone(bars[1]["open"])
            f = analysis.build_facts(ds, "2026-10-22", "daily_review", "Hindi")
            self.assertEqual(f["touched"], ["S2", "S3"])
            self.assertNotIn("gap", [o["id"] for o in f["observations"]])
            self.assertEqual(f["mtf"]["15m"]["levels"], pivots.fibonacci_pivots(22733.0, 22372.15, 22600.0)["levels"])
            self.assertEqual(f["next"]["levels"], pivots.fibonacci_pivots(22787.25, 22372.0, 22600.0)["levels"])
            from studio.finance import episode
            fb = episode.fallback_script(f)                 # the no-AI script must pass every content check
            self.assertEqual([x for x in episode._lint_script(fb, f) if "too short" not in x], [])
            text = " ".join(s.narration for s in fb.scenes)
            self.assertEqual(lint.unverified_numbers(text, f), [])
            self.assertIsNone(episode.FORECAST.search(text))
        finally:
            settings.set("nifty.pivot_project_dir", old)

    def test_stale_or_mismatched_project_files_are_refused(self):
        from studio.finance import pivot_project
        for kwargs in ({"date_s": "2026-10-22"}, {"basis": "pre_close"}, {"calc": "2026-10-22 09:09:19"}, {"tamper": True}):
            d = Path(tempfile.mkdtemp())
            self._write(d, **kwargs)
            old = settings.get("nifty.pivot_project_dir")
            settings.set("nifty.pivot_project_dir", str(d))
            try:
                with self.assertRaises(pivot_project.ProjectDataError, msg=str(kwargs)):
                    pivot_project.read_today(date(2026, 10, 23))
            finally:
                settings.set("nifty.pivot_project_dir", old)


class SelfHealingTests(unittest.TestCase):
    def test_failed_check_on_ai_script_remakes_with_fallback_instead_of_stopping(self):
        from unittest import mock
        from studio import worker
        from studio.finance import episode
        ep = {"facts": {"mode": "daily_review"}, "request": {}}
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "compliance gate", "stage": "compliance",
                                        "kind": "nifty_pivot", "package": db.dumps({"generated_by": {"provider": "ollama_cloud"}}),
                                        "episode": db.dumps(ep), "test_mode": 0,
                                        "created_at": db.iso(), "updated_at": db.iso()})
        result = {"status": "NEEDS_REVIEW", "checks": [{"no": 5, "name": "x", "ok": False, "severity": gate.CRITICAL,
                                                       "detail": "trade call"}], "review_items": [], "allowed_privacy": [],
                  "critical_failures": ["x"]}
        from studio import pipeline
        with mock.patch.object(worker, "enqueue"):
            episode._compliance_outcome(pipeline.load(pid), result)
        p = pipeline.load(pid)
        self.assertTrue(p["episode"]["force_fallback"])
        self.assertEqual((p["status"], p["stage"]), ("queued", "script"))
        # a second failure after the fallback is a real (data) problem: it stops
        db.update("productions", pid, {"package": db.dumps({"generated_by": {"provider": "app"}})})
        with mock.patch.object(worker, "enqueue"):
            episode._compliance_outcome(pipeline.load(pid), result)
        self.assertEqual(pipeline.load(pid)["status"], "needs_review")


class SignalWordTests(unittest.TestCase):
    def test_only_trading_call_sense_is_flagged(self):
        flagged = ["join our free signals group", "buy signal at 23,000", "intraday tips for tomorrow", "BTST calls daily",
                   "premium signals service"]
        allowed = ["What today's GMP signals for the IPO", "5 tips to understand an IPO", "market signals were mixed"]
        for t in flagged:
            self.assertTrue([f for f in lint.prohibited_language(t) if f["category"] == "paid_signal"], t)
        for t in allowed:
            self.assertFalse([f for f in lint.prohibited_language(t) if f["category"] == "paid_signal"], t)


class SarvamTests(unittest.TestCase):
    def test_request_format_and_wav_decoding(self):
        import base64
        import io
        import wave
        from unittest import mock
        import numpy as np
        from studio.media import tts
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(np.arange(100, dtype=np.int16).tobytes())
        resp = mock.Mock(status_code=200, headers={"content-type": "application/json"})
        resp.json.return_value = {"request_id": "x", "audios": [base64.b64encode(buf.getvalue()).decode()]}
        with mock.patch.object(settings, "secret", return_value="test-key"), \
                mock.patch("httpx.post", return_value=resp) as post:
            audio, sr = tts._synth_sentence("आज NIFTY 50 का क्लोज़ 23,382.48 रहा।", "sarvam", "shubh", "hi", 1.0)
        self.assertEqual(sr, 24000)
        self.assertEqual(len(audio), 100)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.sarvam.ai/text-to-speech")
        self.assertEqual(kwargs["headers"], {"api-subscription-key": "test-key"})
        body = kwargs["json"]
        self.assertEqual((body["language_code"], body["speaker"], body["model"]), ("hi-IN", "shubh", "bulbul:v3"))
        self.assertIn("23382.48", body["text"])                    # thousands comma removed before speech
        self.assertNotIn("test-key", str(body))
        with mock.patch.object(settings, "secret", return_value="test-key"), \
                mock.patch("httpx.post", return_value=resp) as post:
            tts._synth_sentence("NIFTY 50 closed at 23,382.48 today.", "sarvam", "ritu", "hi", 1.0)
        self.assertEqual(post.call_args.kwargs["json"]["language_code"], "en-IN")    # English text read as English


class SpeechTests(unittest.TestCase):
    def test_voice_gets_numbers_without_separators(self):
        from studio.media import tts
        self.assertEqual(tts.spoken_numbers("S2 24,591.41 और R2 1,23,456.78"), "S2 24591.41 और R2 123456.78")
        self.assertEqual(tts.spoken_numbers("one, two, 3, 4"), "one, two, 3, 4")


class AuditSnapshotTests(unittest.TestCase):
    def test_rerendered_working_file_does_not_alter_audited_copy(self):
        work = config.PRODUCTIONS_DIR / "99999"
        work.mkdir(parents=True, exist_ok=True)
        thumb = work / "thumbnail.jpg"
        thumb.write_bytes(b"first render")
        audit.record("thumbnail_rendered", {}, production_id=99999, artifact=thumb)
        thumb.write_bytes(b"second render")                     # re-render in place
        row = db.q1("SELECT artifact_path FROM audit_log ORDER BY id DESC LIMIT 1")
        self.assertNotEqual(row["artifact_path"], str(thumb))
        self.assertEqual(Path(row["artifact_path"]).read_bytes(), b"first render")
        self.assertTrue(audit.verify_chain()["ok"])


class AuditTests(unittest.TestCase):
    def test_chain_detects_tampering(self):
        audit.record("unit_test_event", {"x": 1})
        self.assertTrue(audit.verify_chain()["ok"])
        last = db.q1("SELECT id FROM audit_log ORDER BY id DESC LIMIT 1")["id"]
        db.ex("UPDATE audit_log SET details=? WHERE id=?", ('{"x": 2}', last))
        res = audit.verify_chain()
        self.assertFalse(res["ok"])
        self.assertTrue(any("changed" in pr for pr in res["problems"]))
        db.ex("DELETE FROM audit_log WHERE id=?", (last,))


class UploadBlockTests(unittest.TestCase):
    def test_approve_refuses_public_when_gate_disallows(self):
        from studio import pipeline
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "awaiting_approval", "stage": "compliance",
                                        "kind": "nifty_pivot", "package": "{}", "episode": "{}", "test_mode": 0,
                                        "qa": db.dumps({"passed": True}),
                                        "compliance": db.dumps({"status": "PRIVATE_REVIEW_ONLY", "allowed_privacy": ["private"],
                                                                "critical_failures": []}),
                                        "created_at": db.iso(), "updated_at": db.iso()})
        with self.assertRaises(pipeline.NeedsAttention):
            pipeline.approve(pid, privacy="public", publish_at=None)
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "awaiting_approval")

    def test_already_uploaded_production_is_never_uploaded_again(self):
        from unittest import mock
        from studio import pipeline
        from studio.youtube import oauth, upload
        pid = db.insert("productions", {"format": "short", "title": "t", "status": "approved", "stage": "qa", "kind": "general",
                                        "package": "{}", "test_mode": 0, "privacy": "private", "youtube_video_id": "abc123",
                                        "qa": db.dumps({"passed": True}), "created_at": db.iso(), "updated_at": db.iso()})
        with mock.patch.object(oauth, "connected", return_value=True), mock.patch.object(upload, "upload") as up:
            res = pipeline.run_upload({"production_id": pid}, {})
        up.assert_not_called()
        self.assertIn("already uploaded", res["skipped"])
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "uploaded")

    def test_short_waits_for_its_full_episode(self):
        from unittest import mock
        from studio import pipeline, worker
        from studio.youtube import oauth, upload
        parent = db.insert("productions", {"format": "long", "title": "full", "status": "approved", "stage": "compliance",
                                           "kind": "nifty_pivot", "package": "{}", "test_mode": 0,
                                           "created_at": db.iso(), "updated_at": db.iso()})
        short = db.insert("productions", {"format": "short", "title": "s", "status": "approved", "stage": "compliance",
                                          "kind": "nifty_short", "parent_id": parent, "package": "{}", "episode": "{}",
                                          "privacy": "public", "approved_at": db.iso(), "test_mode": 0,
                                          "created_at": db.iso(), "updated_at": db.iso()})
        with mock.patch.object(oauth, "connected", return_value=True), mock.patch.object(upload, "upload") as up, \
                mock.patch.object(worker, "enqueue") as enq, \
                mock.patch.object(oauth, "verify_destination", return_value={"id": "UC_test", "title": "Test"}):
            res = pipeline.run_upload({"production_id": short}, {})
            up.assert_not_called()
            self.assertEqual(res, {"waiting_for_parent": parent})
            self.assertEqual(enq.call_args.kwargs["delay"], 600)
            db.ex("UPDATE productions SET status='needs_review' WHERE id=?", (parent,))
            res = pipeline.run_upload({"production_id": short}, {})
            up.assert_not_called()
        self.assertIn("held", res)
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (short,))["status"], "awaiting_approval")

    def test_rejected_production_stops_running(self):
        from studio import pipeline
        pid = db.insert("productions", {"format": "short", "title": "t", "status": "rejected", "stage": "voice",
                                        "kind": "general", "package": "{}", "test_mode": 0,
                                        "created_at": db.iso(), "updated_at": db.iso()})
        pipeline._run_stages(pid, pipeline.load(pid))
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "rejected")

    def test_non_commercial_voice_is_private_only_for_any_kind(self):
        from studio import pipeline
        tl = {"voice": {"voice_id": "hi_IN-pratham-medium", "commercial": False}}
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "awaiting_approval", "stage": "qa",
                                        "kind": "general", "package": "{}", "test_mode": 0, "timeline": db.dumps(tl),
                                        "qa": db.dumps({"passed": True}), "created_at": db.iso(), "updated_at": db.iso()})
        for privacy in ("public", "unlisted", "scheduled"):
            with self.assertRaises(pipeline.NeedsAttention):
                pipeline.approve(pid, privacy=privacy, publish_at=None)
        self.assertTrue(pipeline.nc_voice(tl))
        self.assertFalse(pipeline.nc_voice({"voice": {"commercial": True}}))
        settings.set("compliance.owner_override", {"active": True, "statement": "accepted"})
        try:
            pipeline._nc_voice_guard(pipeline.load(pid), "public")          # owner override: no exception
        finally:
            settings.set("compliance.owner_override", None)
        with self.assertRaises(pipeline.NeedsAttention):
            pipeline._nc_voice_guard(pipeline.load(pid), "public")


if __name__ == "__main__":
    unittest.main()


class LessonTests(unittest.TestCase):
    def make_lesson(self, mutate=None) -> dict:
        from studio.finance import lessons
        facts = lessons.facts_for(3, "Hindi")
        L = facts["lang"]
        scenes = [
            {"purpose": "hook", "narration": "क्या आपने कभी सोचा है कि चार्ट पर ये रेखाएँ कहाँ से आती हैं? आज हम इन्हें खुद निकालेंगे।",
             "on_screen_text": "", "observation_ids": []},
            {"purpose": "teach", "narration": "सबसे पहले रेंज निकालिए: हाई में से लो घटाइए। काल्पनिक उदाहरण में 1,200 में से 1,100 घटाने पर "
             "रेंज 100 बनती है।", "on_screen_text": "रेंज = H − L", "observation_ids": ["range"]},
            {"purpose": "teach", "narration": "R1 के लिए पिवट 1,150 में रेंज का 0.382 गुना जोड़िए, R2 के लिए 0.618 गुना, और R3 के लिए "
             "पूरी रेंज।", "on_screen_text": "R स्तर", "observation_ids": ["r_levels"]},
            {"purpose": "teach", "narration": "S स्तरों के लिए यही हिस्से पिवट से घटाइए। इस तरह ऊपर और नीचे बराबर दूरी पर संदर्भ स्तर बनते हैं।",
             "on_screen_text": "S स्तर", "observation_ids": ["s_levels"]},
            {"purpose": "example", "narration": "काल्पनिक उदाहरण में R1 1,188.2, R2 1,211.8 और R3 1,250 बनते हैं; S1 1,111.8, S2 1,088.2 "
             "और S3 1,050 बनते हैं।", "on_screen_text": "उदाहरण", "observation_ids": ["example"]},
            {"purpose": "recap", "narration": "याद रखिए: पहले रेंज, फिर पिवट, फिर अनुपात। ये संदर्भ स्तर हैं, भविष्यवाणी नहीं।",
             "on_screen_text": "सार", "observation_ids": []},
            {"purpose": "outro", "narration": "कमेंट में बताइए, आपको कौन सा स्तर सबसे दिलचस्प लगा? सीरीज़ के अगले भाग के लिए चैनल को "
             "सब्सक्राइब कर सकते हैं।", "on_screen_text": "", "observation_ids": []},
            {"purpose": "disclosure", "narration": lessons.DISCLOSURE[L], "on_screen_text": "", "observation_ids": []},
        ]
        for s in scenes:
            s["caption_text"] = s["narration"]
        pkg = {"title": facts["title"], "tags": lessons.TAGS_HI, "scenes": scenes,
               "thumbnail": {"generated_by": "deterministic", "lines": [facts["lesson_title"]]},
               "final_description": lessons.describe(facts)}
        p = {"id": 888001, "kind": "fib_lesson", "language": "Hindi", "package": pkg,
             "episode": {"facts": facts, "disclosure": lessons.DISCLOSURE[L]},
             "timeline": {"voice": {"voice_id": "hf_alpha", "commercial": True}, "disclosure_ribbon": True}}
        if mutate:
            mutate(p)
        return p

    def test_curriculum_numbers_are_computed(self):
        from studio.finance import lessons
        f = lessons.facts_for(6, "Hindi")
        self.assertEqual(f["numbers"]["narrow_spacing"], 61.8)
        self.assertEqual(f["numbers"]["wide_spacing"], 185.4)
        self.assertEqual(len(lessons.LESSONS), 7)
        self.assertIn("भाग 6/7", f["title"])

    def test_clean_lesson_is_private_until_hindi_disclosure_reviewed(self):
        r = gate.run(self.make_lesson(), [{"scene_index": 0, "license": "Original — drawn by this app"}])
        self.assertEqual(r["critical_failures"], [], [c for c in r["checks"] if not c["ok"]])
        self.assertTrue(next(c for c in r["checks"] if c["no"] == 3)["ok"])      # no market data used
        self.assertIn("DISCLOSURE-HI", next(c for c in r["checks"] if c["no"] == 1)["detail"])
        self.assertNotIn("LEGAL-RA-STATUS", next(c for c in r["checks"] if c["no"] == 10)["detail"])
        self.assertEqual(r["allowed_privacy"], ["private"])

    def test_non_commercial_voice_is_review_level_and_never_public(self):
        def nc(p):
            p["timeline"]["voice"] = {"voice_id": "hi_IN-pratham-medium", "commercial": False}
        assets = [{"scene_index": 0, "license": "Original — drawn by this app"}]
        settings.set("compliance.public_publishing_enabled", True)
        try:
            r = gate.run(self.make_lesson(nc), assets)
        finally:
            settings.set("compliance.public_publishing_enabled", False)
        c7 = next(c for c in r["checks"] if c["no"] == 7)
        self.assertFalse(c7["ok"])
        self.assertEqual(c7["severity"], gate.REVIEW)
        self.assertEqual(r["critical_failures"], [])
        self.assertEqual(r["allowed_privacy"], ["private"])
        self.assertTrue(gate.permits(r, "private")[0])
        self.assertFalse(gate.permits(r, "public")[0])

    def test_owner_override_allows_public_but_never_over_critical_failures(self):
        def nc(p):
            p["timeline"]["voice"] = {"voice_id": "hi_IN-pratham-medium", "commercial": False}
        assets = [{"scene_index": 0, "license": "Original — drawn by this app"}]
        settings.set("compliance.owner_override", {"active": True, "accepted_at": "2026-10-23T12:00:00Z",
                                                   "accepted_by": "channel owner", "statement": "I accept the risk"})
        try:
            r = gate.run(self.make_lesson(nc), assets)
            self.assertEqual(r["status"], "OWNER_OVERRIDE")
            self.assertTrue(gate.permits(r, "public")[0])
            self.assertEqual(r["owner_override"]["statement"], "I accept the risk")

            def bad(p):
                nc(p)
                p["package"]["scenes"][1]["narration"] += " Buy at 1,234.56 for a sure profit."
            r2 = gate.run(self.make_lesson(bad), assets)
            self.assertEqual(r2["status"], "NEEDS_REVIEW")
            self.assertFalse(gate.permits(r2, "public")[0])
            self.assertFalse(gate.permits(r2, "private")[0])
        finally:
            settings.set("compliance.owner_override", None)
        self.assertNotEqual(gate.run(self.make_lesson(nc), assets)["status"], "OWNER_OVERRIDE")

    def test_lesson_with_invented_number_or_call_is_blocked(self):
        def bad(p):
            p["package"]["scenes"][4]["narration"] += " असली बाज़ार में 24,700 पर खरीदें।"
        r = gate.run(self.make_lesson(bad), [])
        self.assertEqual(r["status"], "NEEDS_REVIEW")

    def test_next_lesson_numbering_and_schedule(self):
        from studio import scheduler
        from studio.finance import lessons
        scheduler.ensure_defaults()
        self.assertTrue(db.q1("SELECT enabled, time_of_day FROM schedules WHERE task_type='fib_lesson_daily'")["time_of_day"] == "16:00")
        before = lessons.next_lesson_number("india")
        db.insert("productions", {"format": "long", "title": "x", "status": "awaiting_approval", "stage": "compliance",
                                  "kind": "fib_lesson", "audience": "india", "language": "Hindi", "test_mode": 0,
                                  "episode": db.dumps({"facts": {"lesson_no": before}}), "created_at": db.iso(),
                                  "updated_at": db.iso()})
        self.assertEqual(lessons.next_lesson_number("india"), before + 1)


class ChannelOwnershipTests(unittest.TestCase):
    """NIFTY videos belong to the NIFTY studio channel (its library, analytics and upload switch)."""

    def test_lesson_and_episode_are_saved_on_the_nifty_channel(self):
        from unittest import mock
        from studio import worker
        from studio.finance import episode, lessons
        nifty = _channels.finance_channel()
        res = import_fixture(date(2026, 7, 1), 45)
        bars = marketdata.daily_bars(res["dataset_id"])
        with mock.patch.object(worker, "enqueue", return_value=1):
            pids = [lessons.create(1, audience="india"),
                    episode.create(res["dataset_id"], bars[-3]["trade_date"], "historical_case_study")]
        for pid in pids:
            self.assertEqual(db.q1("SELECT channel_id FROM productions WHERE id=?", (pid,))["channel_id"], nifty["id"])

    def test_orphaned_nifty_videos_are_adopted(self):
        pid = db.insert("productions", {"format": "short", "title": "orphan", "status": "uploaded", "stage": "compliance",
                                        "kind": "nifty_short", "test_mode": 0, "created_at": db.iso(), "updated_at": db.iso()})
        self.assertGreaterEqual(_channels.adopt_finance_orphans(), 1)
        self.assertEqual(db.q1("SELECT channel_id FROM productions WHERE id=?", (pid,))["channel_id"],
                         _channels.finance_channel()["id"])

    def test_lesson_report_needs_no_market_data(self):
        from studio.finance import episode
        pid = db.insert("productions", {"format": "long", "title": "Lesson", "status": "approved", "stage": "compliance",
                                        "kind": "fib_lesson", "test_mode": 0, "package": db.dumps({"scenes": []}),
                                        "episode": db.dumps({"facts": {"lesson_no": 2, "title": "Lesson"}}),
                                        "created_at": db.iso(), "updated_at": db.iso()})
        self.assertTrue(episode.write_report(pid).exists())
