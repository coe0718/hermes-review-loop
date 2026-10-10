"""Portable examples must name the same paths the broker and entry point use."""
import _home_guard  # noqa: F401
from dataclasses import replace
from pathlib import Path
import re
import shlex
import unittest

from diaktoros import trusted_turn, turn_layout


class TurnLayoutTests(unittest.TestCase):
    def native(self):
        root = Path("/private/tmp/turn with spaces and 'quotes'")
        return turn_layout.TurnLayout(**{name: root / name for name in
            ('code', 'venv', 'home', 'work', 'export', 'client', 'scratch', 'query')})

    def test_linux_instructions_remain_identical_for_every_role(self):
        layout = turn_layout.TurnLayout.linux()
        for role in trusted_turn.TOOLS:
            self.assertEqual(trusted_turn.tool_instructions(role, layout=layout),
                             trusted_turn.TOOLS[role] + trusted_turn._COMMON)

    def test_native_command_examples_match_broker_paths_for_every_role(self):
        layout = self.native()
        found = 0
        for role in trusted_turn.TOOLS:
            text = trusted_turn.tool_instructions(role, layout=layout)
            self.assertNotIn('`/work`', text)
            self.assertNotIn('`/tmp`', text)
            for example in re.findall(r'`(python -m diaktoros\.broker_client [^`]+)`', text):
                argv = shlex.split(example)
                for flag in ('--body-file', '--message-file', '--answers-file', '--comment-file'):
                    if flag in argv:
                        file = Path(argv[argv.index(flag) + 1])
                        self.assertIn(file.parent, (layout.work, layout.scratch))
                        found += 1
        self.assertGreaterEqual(found, 7)
        environment = layout.environment()
        self.assertEqual(environment['DIAKTOROS_WORK'], str(layout.work))
        self.assertEqual(environment['DIAKTOROS_EXPORT'], str(layout.export))
        self.assertEqual(environment['DIAKTOROS_TURN_FILE'],
                         str(layout.client / 'review-loop-turn.json'))

    def test_entry_keeps_paths_and_model_as_single_arguments(self):
        layout = self.native()
        entry = layout.hermes_entry(provider='native', model='model with spaces', max_steps=10, timeout=180)
        self.assertEqual(entry[:2], [str(layout.venv / 'bin/python'), str(layout.venv / 'bin/hermes')])
        self.assertEqual(entry[entry.index('--query-file') + 1], str(layout.query))
        self.assertEqual(entry[entry.index('-m') + 1], 'model with spaces')

    def test_invalid_prompt_or_environment_paths_fail_closed(self):
        for path in ('relative', '/tmp/control\nline', '/tmp/markdown`code', '/tmp/path:injection'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                replace(self.native(), work=Path(path))


if __name__ == '__main__':
    unittest.main()
