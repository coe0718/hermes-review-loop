"""Every command the docs print, parsed by the CLI that prints it.

A documented invocation the parser rejects is worse than no documentation at all: the operator
copies it, gets an argparse usage error, and concludes the loop is broken. Two such lines reached
``main`` — ``pause --loop name`` and ``cleanup --loop name --sweep --dry-run``, both written when
the verbs were different — and nothing could have caught them, because no test read the prose.

Hermes hands the plugin's ``register_cli`` a ``setup(parser)`` closure for its subcommands, so a
recorder object can capture that closure and this group can build the real parser itself, with no
gateway and no network. Every ``hermes review-loop …`` line inside a fenced code block in
``README.md``, ``docs/*.md``, ``skill/SKILL.md`` and ``plugin.yaml`` is then tokenised
(placeholders normalised, ``#`` comments dropped) and parsed. ``python -m review_loop.<module>``
and ``scripts/<name>.py`` references must exist, with the generated watchdog shim as the one
exception — and it is named by the CLI rather than hard-coded here.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import re
import shlex

from .fixture import *  # noqa: F403 - the shared harness namespace

DOCS = ("README.md", "skill/SKILL.md", "plugin.yaml")


def _real_parser() -> argparse.ArgumentParser:
    """The parser ``hermes review-loop`` actually builds, captured from the CLI itself."""
    from review_loop import cli

    captured: dict = {}

    class _Recorder:
        """Catches the ``setup`` callable whatever method the CLI context is asked for."""

        def __getattr__(self, name):  # noqa: ANN001
            def call(*args, **kwargs):
                for value in list(args) + list(kwargs.values()):
                    if callable(value) and getattr(value, "__name__", "") == "setup":
                        captured["setup"] = value
                return None
            return call

    cli.register_cli(_Recorder(), {})
    parser = argparse.ArgumentParser(prog="hermes review-loop")
    if "setup" in captured:
        captured["setup"](parser)
    return parser


def _doc_files() -> list[pathlib.Path]:
    files = [ROOT / name for name in DOCS]
    files += sorted((ROOT / "docs").glob("*.md"))
    return [path for path in files if path.is_file()]


def _invocations(path: pathlib.Path):
    """``(line number, command)`` for each fenced-block invocation, continuations joined."""
    lines = path.read_text().splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence or "hermes review-loop" not in line:
            continue
        start = line.index("hermes review-loop")
        command = line[start:]
        # Inside sample output a command is often quoted inline: `hermes review-loop …` — prose
        # follows the closing backtick, and it is not part of the command.
        if line[:start].endswith("`"):
            command = command.split("`", 1)[0]
        following = index
        while command.rstrip().endswith("\\") and following + 1 < len(lines):
            following += 1
            command = command.rstrip()[:-1] + " " + lines[following].strip()
        yield index + 1, command.strip()


def _argv(command: str) -> list[str] | None:
    """The argv a documented command asks for, or ``None`` when the line is prose, not a command.

    ``<placeholders>`` become a token so the parser sees a value, and ``shlex`` with comments on
    drops a trailing ``# …`` note the way a shell would. An elision (``…``) or a bare ``--help``
    is not a command and is skipped.
    """
    tail = re.sub(r"^hermes\s+review-loop\s*", "", command)
    if not tail:
        return None
    try:
        tokens = shlex.split(re.sub(r"<[^>]*>", "VALUE", tail), comments=True)
    except ValueError:                      # an unbalanced quote in prose
        return None
    if not tokens or tokens[0].startswith("-") or "..." in tokens:
        return None
    return tokens


def _rejects(parser: argparse.ArgumentParser, tokens: list[str], depth: int = 0) -> str | None:
    """``None`` when the parser accepts this argv, else argparse's own error line.

    A documented ``--pr N`` is a placeholder for a number, so an "invalid int value" is retried
    once with a real integer: the check is about the command's shape, not its sample data.
    """
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            parser.parse_args(tokens)
        return None
    except SystemExit:
        message = err.getvalue().strip().splitlines()[-1]
    placeholder = re.search(r"invalid int value: '([^']*)'", message)
    if placeholder and depth < 5:
        return _rejects(parser, [str(1) if t == placeholder.group(1) else t for t in tokens],
                        depth + 1)
    return message


def group_docs() -> None:
    """The commands we print are commands the CLI accepts, and the references we make exist."""
    section("docs — the invocations we print, parsed by the CLI that prints them")

    parser = _real_parser()
    # First the checker itself. A sweep that cannot fail proves nothing, so this pins both
    # directions: it must reject the exact shape that reached main, and accept the real fix.
    check("the CLI's own subcommands were captured (its `list` verb parses)",
          _rejects(parser, ["list"]), None)
    check("the checker rejects a command the parser has no verb for",
          _rejects(parser, ["pause", "--loop", "widgets"]) is not None, True)
    check("  and accepts the one that replaced it",
          _rejects(parser, ["arm", "--loop", "widgets", "--pause"]), None)

    # A command quoted inline in sample output ends at its closing backtick: the prose after it is
    # not arguments, and a bad command quoted that way must still be caught.
    sample = TMP / "inline-sample.md"
    sample.write_text("```\n  next: run `hermes review-loop arm --loop widgets --pause` — then wait\n"
                      "  next: run `hermes review-loop pause --loop widgets` — then wait\n```\n")
    inline = [_argv(command) for _, command in _invocations(sample)]
    check("an inline-quoted command stops at its closing backtick",
          inline[0], ["arm", "--loop", "widgets", "--pause"])
    check("  and a bad one quoted that way is still rejected",
          _rejects(parser, inline[1]) is not None, True)

    found: list[tuple[pathlib.Path, int, list[str]]] = []
    for path in _doc_files():
        for line, command in _invocations(path):
            tokens = _argv(command)
            if tokens is not None:
                found.append((path.relative_to(ROOT), line, tokens))
    # An extractor that silently matches nothing would make every check below vacuous.
    check("the docs still contain invocations to check", len(found) > 50, True)

    for rel, line, tokens in found:
        check(f"{rel}:{line} parses — {' '.join(tokens)[:52]}", _rejects(parser, tokens), None)

    from review_loop import cli as cli_module

    for path in _doc_files():
        text = path.read_text()
        for module in sorted(set(re.findall(r"python\s+-m\s+review_loop\.(\w+)", text))):
            check(f"{path.relative_to(ROOT)}: review_loop/{module}.py exists",
                  (ROOT / "review_loop" / f"{module}.py").is_file(), True)
        for script in sorted(set(re.findall(r"(?:scripts/|\$HERMES_HOME/scripts/)([\w-]+\.py)",
                                           text))):
            check(f"{path.relative_to(ROOT)}: scripts/{script} exists",
                  (ROOT / "scripts" / script).is_file() or script == cli_module.SHIM_NAME, True)


GROUPS = {
    "docs": group_docs,
}
