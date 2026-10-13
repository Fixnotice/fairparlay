"""py -m unittest discover tests -v"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect as C                                       # noqa: E402

EV = {"id": "ev1", "home_team": "Baltimore Ravens", "away_team": "Atlanta Falcons", "commence_time": "2026-10-18T17:00:00Z",
      "bookmakers": [
          {"key": "pinnacle", "markets": [
              {"key": "h2h", "outcomes": [{"name": "Baltimore Ravens", "price": -300}, {"name": "Atlanta Falcons", "price": 260}]},
              {"key": "spreads", "outcomes": [{"name": "Baltimore Ravens", "price": -108, "point": -6.5}, {"name": "Atlanta Falcons", "price": -102, "point": 6.5}]},
              {"key": "totals", "outcomes": [{"name": "Over", "price": -105, "point": 47.5}, {"name": "Under", "price": -105, "point": 47.5}]}]},
          {"key": "draftkings", "markets": [
              {"key": "h2h", "outcomes": [{"name": "Baltimore Ravens", "price": -330}, {"name": "Atlanta Falcons", "price": 265}]},
              {"key": "spreads", "outcomes": [{"name": "Baltimore Ravens", "price": -110, "point": -7}, {"name": "Atlanta Falcons", "price": -110, "point": 7}]},
              {"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 47.5}, {"name": "Under", "price": -110, "point": 47.5}]}]}]}

PROPS = {"id": "ev1", "home_team": "Baltimore Ravens", "away_team": "Atlanta Falcons", "bookmakers": [
    {"key": "draftkings", "markets": [
        {"key": "player_reception_yds", "outcomes": [{"name": "Over", "description": "Zay Flowers", "price": -115, "point": 68.5},
                                                     {"name": "Under", "description": "Zay Flowers", "price": -105, "point": 68.5}]},
        {"key": "player_anytime_td", "outcomes": [{"name": "Yes", "description": "Derrick Henry", "price": -150}]},
        {"key": "player_pass_yds", "outcomes": [{"name": "Over", "description": "Lamar Jackson", "price": -110, "point": 245.5}]}]},
    {"key": "fanduel", "markets": [
        {"key": "player_reception_yds", "outcomes": [{"name": "Over", "description": "Zay Flowers", "price": -110, "point": 68.5},
                                                     {"name": "Under", "description": "Zay Flowers", "price": -110, "point": 68.5}]},
        {"key": "player_anytime_td", "outcomes": [{"name": "Yes", "description": "Derrick Henry", "price": -140},
                                                  {"name": "No", "description": "Derrick Henry", "price": 110}]}]}]}
ROSTER = {"zay flowers": ("BAL", "WR"), "derrick henry": ("BAL", "RB"), "lamar jackson": ("BAL", "QB")}


class TestOddsMath(unittest.TestCase):
    def test_round_trip(self):
        for am in (-300, -110, 100, 150, 450):
            self.assertAlmostEqual(C.implied(am), 1 / C.decimal(am), places=9)
            self.assertEqual(C.american(C.implied(am)), am)

    def test_devig_removes_the_vig(self):
        self.assertAlmostEqual(C.devig(-110, -110), 0.5)
        p = C.devig(-300, 260)
        self.assertAlmostEqual(p + C.devig(260, -300), 1.0)
        self.assertTrue(0.72 < p < 0.75)

    def test_td_fair_below_implied(self):
        for am in (-200, -110, 150, 400):
            self.assertLess(C.td_fair(am), C.implied(am))
        self.assertAlmostEqual(C.td_fair(-150), 0.536, places=2)   # DK -150 (60% implied) hit ~54% historically

    def test_consensus_weights_pinnacle(self):
        fair, nb, sharp = C.consensus({"pinnacle": 0.60, "draftkings": 0.50})
        self.assertAlmostEqual(fair, (0.6 * 3 + 0.5) / 4)
        self.assertEqual((nb, sharp), (2, True))
        self.assertEqual(C.consensus({}), (None, 0, False))


class TestLegs(unittest.TestCase):
    def test_featured(self):
        legs = {l["id"]: l for l in C.featured_legs(EV)}
        self.assertIn("ev1:h2h:BAL", legs)
        self.assertEqual(legs["ev1:h2h:BAL"]["pair"], "ev1:h2h:ATL")
        self.assertAlmostEqual(legs["ev1:h2h:BAL"]["fair"] + legs["ev1:h2h:ATL"]["fair"], 1.0, places=3)
        self.assertEqual(legs["ev1:h2h:BAL"]["px"], {"pinnacle": -300, "draftkings": -330})
        self.assertTrue(legs["ev1:h2h:BAL"]["sharp"])
        # spreads at different points are different legs, each with its own books
        self.assertEqual(set(legs["ev1:spreads:BAL:-6.5"]["px"]), {"pinnacle"})
        self.assertEqual(set(legs["ev1:spreads:ATL:7"]["px"]), {"draftkings"})
        self.assertEqual(legs["ev1:spreads:ATL:7"]["t"], "ATL")
        self.assertEqual(legs["ev1:totals:over:47.5"]["nb"], 2)
        self.assertEqual(legs["ev1:totals:under:47.5"]["s"], "under")
        self.assertEqual(legs["ev1:totals:under:47.5"]["k"], "total")

    def test_props(self):
        legs = {l["id"]: l for l in C.prop_legs(PROPS, ROSTER, "BAL", "ATL")}
        over = legs["ev1:player_reception_yds:zay_flowers:68.5:over"]
        self.assertEqual((over["t"], over["pos"], over["k"], over["nb"]), ("BAL", "WR", "reception_yds", 2))
        self.assertAlmostEqual(over["fair"] + legs["ev1:player_reception_yds:zay_flowers:68.5:under"]["fair"], 1.0, places=3)
        td = legs["ev1:player_anytime_td:derrick_henry:yes"]
        self.assertEqual(td["px"], {"draftkings": -150, "fanduel": -140})
        self.assertLess(td["fair"], C.implied(-150))                  # one-sided price corrected, two-sided de-vigged
        self.assertIn("ev1:player_anytime_td:derrick_henry:no", legs)
        self.assertNotIn("ev1:player_pass_yds:lamar_jackson:245.5:over", legs)   # over-only line has no fair price

    def test_team_defense_and_no_scorer_are_skipped(self):
        ev = {"id": "ev1", "bookmakers": [{"key": "draftkings", "markets": [{"key": "player_anytime_td", "outcomes": [
            {"name": "Yes", "description": "Baltimore Ravens D/ST", "price": 400},
            {"name": "Yes", "description": "No Scorer", "price": 2500},
            {"name": "Yes", "description": "Derrick Henry", "price": -150}]}]}]}
        legs = C.prop_legs(ev, ROSTER, "BAL", "ATL")
        self.assertEqual([l["p"] for l in legs], ["Derrick Henry"])

    def test_unknown_player_keeps_no_team(self):
        legs = C.prop_legs(PROPS, {}, "BAL", "ATL")
        self.assertTrue(all(l["t"] is None and l["pos"] is None for l in legs))

    def test_norm_name(self):
        self.assertEqual(C.norm_name("Michael Penix Jr."), "michael penix")
        self.assertEqual(C.norm_name("Amon-Ra St. Brown"), "amonra st brown")
        self.assertEqual(C.norm_name("A.J. Brown"), "aj brown")


class TestDemoAndKalshi(unittest.TestCase):
    def test_demo_payload_is_consistent(self):
        d = C.demo_payload()
        ids = {l["id"] for g in d["games"] for l in g["legs"]}
        for g in d["games"]:
            for l in g["legs"]:
                if not l["pair"].endswith(":no"):               # "No TD" is rarely offered
                    self.assertIn(l["pair"], ids)
                self.assertTrue(0 < l["fair"] < 1)
                self.assertTrue(l["px"])
        self.assertTrue(any("kx" in l for g in d["games"] for l in g["legs"]))

    def test_kalshi_parsing(self):
        m = {"ticker": "KXNFLGAME-26OCT18ATLBAL-BAL", "yes_bid_dollars": "0.7400", "yes_ask_dollars": "0.7600"}
        self.assertEqual(C.market_team(m), "BAL")
        self.assertEqual(str(C.market_date(m)), "2026-10-18")
        self.assertAlmostEqual(C.dollars(m, "yes_ask"), 0.76)
        self.assertAlmostEqual(C.dollars({"yes_ask": 76}, "yes_ask"), 0.76)       # cents form
        self.assertEqual(C.market_team({"ticker": "KXNFLGAME-26OCT18ATLBAL-LA"}), "LAR")


if __name__ == "__main__":
    unittest.main()
