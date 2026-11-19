#!/usr/bin/env python3
"""A stand-in for the `gh` program in tests/test_ci_db.py: one release kept in a folder (FAKE_GH_DIR).

  api repos/R/releases/tags/TAG                      the release, or "HTTP 404" when there is none
  api repos/R/releases/ID/assets?per_page=..&page=N  its files
  api -H 'Accept: application/octet-stream' repos/R/releases/assets/ID     a file's bytes
  api -X DELETE repos/R/releases/assets/ID
  release upload TAG FILE [--clobber]                like GitHub: refused when the name exists (unless --clobber, which
                                                     deletes first), and refused above `max_bytes`
  release create TAG ...
Failures on request (files in FAKE_GH_DIR): fail_download / fail_upload / fail_list hold a number of calls to fail;
fail_upload_match holds a text a file name must contain for fail_upload to apply."""
import json
import os
import sys
import time
from pathlib import Path

D = Path(os.environ["FAKE_GH_DIR"])
META = D / "assets.json"


def meta():
    return json.loads(META.read_text()) if META.exists() else {}


def save(m):
    META.write_text(json.dumps(m))


def failing(kind):
    p = D / f"fail_{kind}"
    if p.exists() and int(p.read_text() or 0) > 0:
        p.write_text(str(int(p.read_text()) - 1))
        return True
    return False


def die(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def tag_dir(tag):
    return D / "releases" / tag


def main(a):
    if a[:1] == ["api"]:
        method, path, octet, i = "GET", None, False, 1
        while i < len(a):
            if a[i] == "-X":
                method = a[i + 1]; i += 2
            elif a[i] == "-H":
                octet = "octet-stream" in a[i + 1]; i += 2
            else:
                path = a[i]; i += 1
        parts = path.split("?")[0].split("/")
        if "tags" in parts:
            if failing("list"):
                die("gh: HTTP 502: Bad Gateway")
            tag = parts[-1]
            if not tag_dir(tag).exists():
                die("gh: Not Found (HTTP 404)")
            print(json.dumps({"id": abs(hash(tag)) % 10**6, "tag_name": tag, "assets": []}))
            return
        if parts[-1] == "assets":       # the release's files: one release per test
            page = int(path.split("page=")[-1]) if "page=" in path.split("?")[-1].split("per_page=")[-1] else 1
            rows = [{"id": v["id"], "name": k, "size": v["size"], "updated_at": v["updated_at"]} for k, v in meta().items()]
            print(json.dumps(rows[(page - 1) * 100: page * 100]))
            return
        aid = int(parts[-1])
        m = meta()
        name = next((k for k, v in m.items() if v["id"] == aid), None)
        if name is None:
            die("gh: Not Found (HTTP 404)")
        if method == "DELETE":
            (D / "files" / str(aid)).unlink()
            del m[name]
            save(m)
            return
        if octet:
            if failing("download"):
                sys.stdout.buffer.write((D / "files" / str(aid)).read_bytes()[:10])     # a transfer cut short
                die("gh: unexpected EOF")
            sys.stdout.buffer.write((D / "files" / str(aid)).read_bytes())
            return
        die("fake gh: unsupported api call " + " ".join(a))
    if a[:2] == ["release", "create"]:
        tag_dir(a[2]).mkdir(parents=True, exist_ok=True)
        return
    if a[:2] == ["release", "upload"]:
        tag, f, clobber = a[2], Path(a[3]), "--clobber" in a
        if not tag_dir(tag).exists():
            die("release not found")
        m = meta()
        match = (D / "fail_upload_match").read_text() if (D / "fail_upload_match").exists() else ""
        if match in f.name and failing("upload"):
            if clobber and f.name in m:      # --clobber deletes first: a failed upload leaves nothing under that name
                (D / "files" / str(m[f.name]["id"])).unlink()
                del m[f.name]
                save(m)
            die("gh: HTTP 500")
        if f.stat().st_size == 0:          # GitHub: "HTTP 400: Bad Content-Length" for an empty file
            die("gh: HTTP 400: Bad Content-Length")
        limit = int((D / "max_bytes").read_text()) if (D / "max_bytes").exists() else 0
        if f.name in m:
            if not clobber:
                die(f"asset under the same name already exists: [{f.name}]")
            (D / "files" / str(m[f.name]["id"])).unlink()
            del m[f.name]
            save(m)
        if limit and f.stat().st_size >= limit:
            die("HTTP 422: Validation Failed (size must be less than 2147483648)")
        seq = D / "seq"
        n = int(seq.read_text()) + 1 if seq.exists() else 1
        seq.write_text(str(n))
        (D / "files").mkdir(exist_ok=True)
        (D / "files" / str(n)).write_bytes(f.read_bytes())
        m[f.name] = {"id": n, "size": f.stat().st_size, "updated_at": f"2026-11-19T00:00:{n:02d}Z" if n < 60 else f"2026-11-19T00:{n // 60:02d}:{n % 60:02d}Z"}
        save(m)
        time.sleep(0)
        return
    die("fake gh: unsupported " + " ".join(a))


main(sys.argv[1:])
