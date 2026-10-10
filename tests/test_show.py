"""`hermes dk show` (#555): every setting, its value and where it came from."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import unittest

from diaktoros import cli, config, observer, show
from test_token_file_settings import TokenFileSettingsTests as _Base, SENTINEL


class ShowTests(_Base):
    # Reuse the fixture (temp home, token files, init argv); only our own tests run here.
    def shown(self, *extra, settings=None):
        self.install()
        return self.run_cli(["show", "--loop", "widgets", *extra], settings)

    def rows(self, settings=None):
        rc, out = self.shown("--json", settings=settings)
        self.assertEqual(rc, 0, out)
        return {r["name"]: r for r in json.loads(out)["settings"]}

    def test_every_schema_key_is_shown(self):
        rows = self.rows()
        missing = set(config.SETTINGS_SCHEMA) - set(rows)
        self.assertFalse(missing, f"settings schema keys `show` does not list: {sorted(missing)}")

    def test_every_loop_key_is_shown(self):
        rows = self.rows()
        covered = {n.split(".")[0] for n in rows}
        # Every documented loop key is a row or the root of nested rows; the nested blocks list
        # every key their normalizer accepts.
        unshown = set(config.DEFAULTS) - set(rows) - covered
        self.assertFalse(unshown, f"DEFAULTS keys `show` does not list: {sorted(unshown)}")
        for key in config.TRIAGE_KEYS:
            self.assertTrue(f"triage.{key}" in rows or key in rows, f"triage.{key}")
        for key in show.OBSERVER_KEYS:
            self.assertIn(f"observer.{key}", rows)
        self.assertIn("adjudicator.route", rows)

    def test_observer_keys_match_the_normalizer(self):
        norm = config.normalize_observer({"route": "r", "events": ["stall"], "digest_min": 5,
                                          "urgent_route": "u", "urgent_profile": "p",
                                          "urgent_deliver": "d"})
        self.assertLessEqual(set(norm), set(show.OBSERVER_KEYS))

    def test_json_round_trips(self):
        self.install()
        rc, out = self.run_cli(["show", "--loop", "widgets", "--json"])
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(json.loads(json.dumps(data)), data)
        self.assertEqual(data["loop"], "widgets")
        for row in data["settings"]:
            self.assertEqual(set(row), {"name", "value", "source", "meaning", "state", "note"})
            self.assertIn(row["source"], {"loop file", "default", "derived"})

    def test_sources_and_off_notes(self):
        rows = self.rows()
        self.assertEqual(rows["review_only_cap"]["source"], "derived")
        self.assertEqual(rows["review_only_cap"]["value"], rows["cap"]["value"])
        self.assertEqual(rows["fix_ci"]["state"], "off")
        self.assertIn("red CI on fixer PRs waits for a review", rows["fix_ci"]["note"])
        self.assertEqual(rows["ci_fix_cap"]["source"], "default")
        self.assertEqual(rows["reviewer_login"]["source"], "loop file")

    def test_a_loop_file_value_wins_and_text_groups_on_first(self):
        self.install()
        path = self.loop_file()
        data = json.loads(path.read_text())
        data.update({"fix_ci": True, "unattended_fixer_push": True, "cap": 5,
                     "review_only_cap": 2,
                     "observer": {"route": "feed", "events": ["opened", "verdict"]}})
        path.write_text(json.dumps(data))
        rc, out = self.run_cli(["show", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertLess(out.index("\non\n"), out.index("off — would matter"))
        rows = {r["name"]: r for r in json.loads(
            self.run_cli(["show", "--loop", "widgets", "--json"])[1])["settings"]}
        self.assertEqual((rows["fix_ci"]["value"], rows["fix_ci"]["state"]), (True, "on"))
        self.assertEqual((rows["review_only_cap"]["value"], rows["review_only_cap"]["source"]),
                         (2, "loop file"))
        self.assertIn("stall", rows["observer.events"]["note"])
        self.assertEqual(set(rows["observer.events"]["note"].split("hides: ")[1].split(", ")),
                         set(observer.EVENTS) - {"opened", "verdict"})

    def test_form_value_is_a_note_not_the_effective_value(self):
        rows = self.rows({"fix_ci": True})
        self.assertIs(rows["fix_ci"]["value"], False)
        self.assertIn("settings form holds", rows["fix_ci"]["note"])

    def test_token_values_are_never_printed_and_nothing_is_written(self):
        self.install()
        before = self.loop_file().read_bytes()
        for argv in (["show", "--loop", "widgets"], ["show", "--loop", "widgets", "--json"]):
            rc, out = self.run_cli(argv)
            self.assertEqual(rc, 0, out)
            self.assertNotIn(SENTINEL, out)
            self.assertIn(str(self.pats["rev"]), out)         # the path, not the token
        self.assertEqual(self.loop_file().read_bytes(), before)

    def test_unknown_loop_is_refused(self):
        rc, out = self.run_cli(["show", "--loop", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot show loop", out)


# The inherited tests would run again under this class; keep only ours.
for _name in [n for n in dir(_Base) if n.startswith("test_")]:
    if _name not in ShowTests.__dict__:
        setattr(ShowTests, _name, None)

if __name__ == "__main__":
    unittest.main()
