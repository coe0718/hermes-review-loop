"""Paths visible to a turn; this describes a layout, not a containment backend."""
from dataclasses import dataclass
from pathlib import Path
import re
import shlex


@dataclass(frozen=True)
class TurnLayout:
    code: Path
    venv: Path
    home: Path
    work: Path
    export: Path
    client: Path
    scratch: Path
    query: Path

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            path = Path(getattr(self, name))
            if not path.is_absolute() or any(ord(c) < 32 or c in '`:' for c in str(path)):
                raise ValueError('turn paths must be absolute and safe for command examples')
            object.__setattr__(self, name, path)

    @classmethod
    def linux(cls):
        return cls(code=Path('/opt/code'), venv=Path('/opt/venv'), home=Path('/home/agent'),
                   work=Path('/work'), export=Path('/opt/export'), client=Path('/opt/client'),
                   scratch=Path('/tmp'), query=Path('/opt/query'))

    def tool_paths(self, text: str) -> str:
        """Render fixed host instruction examples, never rewrite PR/model text."""
        roots = {'/work': self.work, '/tmp': self.scratch}
        def replace(match):
            root, suffix = match.group(1), match.group(2)
            return shlex.quote(str(roots[root]) + suffix)
        return re.sub(r'(/work|/tmp)((?:/[A-Za-z0-9_.-]+)*)', replace, text)

    def environment(self) -> dict[str, str]:
        return {'HOME': str(self.home), 'HERMES_HOME': str(self.home),
                'PYTHONPATH': f'{self.client}:{self.code}', 'TMPDIR': str(self.scratch),
                'CARGO_HOME': str(self.scratch / 'cargo'),
                'RUSTUP_HOME': str(self.scratch / 'rustup'),
                'CARGO_TARGET_DIR': str(self.work / 'target'),
                'DIAKTOROS_WORK': str(self.work), 'DIAKTOROS_EXPORT': str(self.export),
                'DIAKTOROS_TURN_FILE': str(self.client / 'review-loop-turn.json')}

    def hermes_entry(self, *, provider: str, model: str, max_steps: int, timeout: int) -> list[str]:
        return [str(self.venv / 'bin/python'), str(self.venv / 'bin/hermes'), 'chat',
                '--query-file', str(self.query), '--oneshot', '-Q', '--provider', provider,
                '-m', model, '-t', 'terminal,file', '--ignore-rules',
                '--max-turns', str(max_steps), '--run-budget', str(timeout)]
