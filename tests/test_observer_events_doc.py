"""Every name in observer.EVENTS has a row in both event tables (docs/observer.md and
docs/configuration.md), so a new event cannot be left undocumented (#510)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from diaktoros import observer  # noqa: E402


def rows(name):
    text = (ROOT / "docs" / name).read_text()
    # Only the events table: configuration.md has setting rows (e.g. `human_paths`) too.
    start = re.search(r"^\| Event \|", text, re.M)
    text = text[start.start():] if start else ""
    return [m.group(1) for m in re.finditer(r"^\| `([a-z_]+)` \|", text, re.M)]


class ObserverEventsDoc(unittest.TestCase):
    def test_every_event_is_in_both_tables(self):
        for doc in ("observer.md", "configuration.md"):
            listed = rows(doc)
            self.assertEqual([e for e in observer.EVENTS if e not in listed], [],
                             f"docs/{doc} is missing an observer event row")

    def test_configuration_lists_events_in_order(self):
        listed = [r for r in rows("configuration.md") if r in observer.EVENTS]
        self.assertEqual(listed, list(observer.EVENTS))


if __name__ == "__main__":
    unittest.main()
