from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gacrawl.history", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="read what has not been read yet")
    r.add_argument("--only", help="comma list of sources")
    r.add_argument("--max-minutes", type=float)
    b = sub.add_parser("build", help="the bundle Electo reads")
    b.add_argument("--out", default="dist/history")
    sub.add_parser("status", help="what is on file")
    v = sub.add_parser("verify", help="is the bundle in Electo, file for file")
    v.add_argument("--bundle", required=True)
    v.add_argument("--electo", required=True, help="Electo's ga/pipeline_archive/hist")
    a = ap.parse_args(argv)
    cfg = config.load()
    if a.cmd == "run":
        from .run import run
        s = run(cfg, only=[x.strip() for x in a.only.split(",")] if a.only else None, max_minutes=a.max_minutes)
        print(json.dumps(s))
        return 0
    if a.cmd == "build":
        from .build import build
        build(cfg, Path(a.out))
        return 0
    if a.cmd == "status":
        from .run import status
        print(json.dumps(status(cfg), indent=1))
        return 0
    if a.cmd == "verify":
        from .build import verify
        res = verify(Path(a.bundle), Path(a.electo))
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
