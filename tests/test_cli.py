"""The command line builds and every subcommand parses: a duplicated option once broke every `gacrawl` command."""
import argparse

import pytest

from gacrawl import cli


def test_every_subcommand_parses_help():
    ap = cli.build_parser()
    subs = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
    assert {"run", "seed", "derive", "ocr", "posts", "candidates", "codeacts", "backfill", "probe", "summary"} <= set(subs.choices)
    for name, sp in subs.choices.items():
        with pytest.raises(SystemExit) as e:
            sp.parse_args(["--help"])
        assert e.value.code == 0, name


def test_known_invocations_parse():
    ap = cli.build_parser()
    for argv in (["run", "--shard", "0/3", "--max-minutes", "40", "--kind", "video"],
                 ["posts", "verify", "--all"], ["ocr", "pending", "--count"], ["ocr", "pending", "--max-minutes", "12"],
                 ["candidates", "research", "--max-minutes", "10", "--workers", "6"],
                 ["summary", "--since", "2026-11-06T00:00:00+00:00", "--title", "t", "--out", "/dev/null"],
                 ["backfill", "next"], ["codeacts", "--max-minutes", "45"], ["derive", "--max-minutes", "5"]):
        ns = ap.parse_args(argv)
        assert ns.cmd == argv[0]


def test_every_command_line_in_the_workflows_parses():
    """Every `gacrawl ...` line the workflows run parses with the current parser (workflow and CLI drift apart
    silently otherwise: a renamed flag would only fail on the runner, at night)."""
    import re
    import shlex
    from pathlib import Path

    ap = cli.build_parser()
    seen = 0
    for wf in (Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"):
        joined, buf = [], ""
        for raw in wf.read_text(encoding="utf-8").splitlines():   # shell lines continued with a trailing backslash
            if raw.rstrip().endswith("\\"):
                buf += raw.rstrip()[:-1] + " "
                continue
            joined.append(buf + raw.strip() if buf else raw)
            buf = ""
        for line in joined:
            m = re.search(r"(?<![`\w])gacrawl (?!\.)(.*)", line)
            if not m or line.lstrip().startswith("#"):
                continue
            rest = re.sub(r"\$\{\{[^}]*\}\}", "5", m.group(1))        # workflow expressions first: they hold '||'
            rest = re.split(r"\s(?:\|\||&&|\||>|2>|;|#)\s|\)", rest)[0]
            rest = (rest.replace("$ARGS", "--shard 0/1 --max-minutes 5").replace('"${DELTAS[@]}"', "a.db")
                    .replace('"${STATES[@]}"', "s").replace("$FLAG", ""))
            rest = re.sub(r'"\$\w+(?:\.\w+)?"|\$\w+', "5", rest)
            argv = shlex.split(rest)
            ns = ap.parse_args(argv)
            assert ns.cmd == argv[0], line
            seen += 1
    assert seen >= 25


def test_workflows_have_no_duplicate_keys():
    """GitHub refuses a workflow with a key given twice (PyYAML would quietly keep the last)."""
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
        dup = {k for k in keys if keys.count(k) > 1}
        assert not dup, f"duplicate keys {dup} at line {node.start_mark.line + 1}"
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    from pathlib import Path

    for p in (Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"):
        yaml.load(p.read_text(), Loader=Strict)
