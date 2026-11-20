"""The format registry's checker: a data file (or one record) against its schema in formats/.

The schemas are JSON Schema (draft 2020-12), the subset below, so any JSON Schema tool reads them too; this checker
needs nothing but the standard library (the runner installs no extra package for it). The same file is in Crawl
(formats/check.py) and Electo (formats/check.py); a test in each keeps the two the same when both are checked out.

    python3 -I formats/check.py SCHEMA.json DATA.json [--max 20]     # a file: exit 1 and the first faults if it does not fit
    from formats.check import errors, load_schema                     # in code and tests

Keywords understood: type (one or a list: object, array, string, integer, number, boolean, null), properties,
required, additionalProperties (true, false or a schema), patternProperties, propertyNames, items, prefixItems,
minItems, maxItems, uniqueItems, enum, const, pattern, minLength, maxLength, minimum, maximum, format (date,
date-time, uri, email: checked; others noted), anyOf, oneOf, allOf, not, $ref (to "#/$defs/..." or to another schema
file "name.schema.json#/$defs/..."), $defs. Annotations (title, description, examples, x-*) are read by people and
by formats/README.md, never by the checker. A keyword outside this list is a fault of the schema itself
(SchemaError), so a schema cannot quietly mean more than is checked."""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KNOWN = {"$schema", "$id", "$defs", "$ref", "$comment", "title", "description", "examples", "default", "deprecated",
         "type", "properties", "required", "additionalProperties", "patternProperties", "propertyNames", "items",
         "prefixItems", "minItems", "maxItems", "uniqueItems", "enum", "const", "pattern", "minLength", "maxLength",
         "minimum", "maximum", "format", "anyOf", "oneOf", "allOf", "not"}
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
DATE = re.compile(r"\d{4}-\d\d-\d\d")
DATETIME = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?")
URI = re.compile(r"[a-z][a-z0-9+.-]*:\S+", re.I)
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def at(path: str, key: str) -> str:
    """A key's place in a fault's path: $.a.b, or $.a["U.S. Senate"] for a key that is no plain name."""
    return f"{path}.{key}" if re.fullmatch(r"[A-Za-z_][\w-]*", key) else f"{path}[{json.dumps(key, ensure_ascii=False)}]"


class SchemaError(ValueError):
    """The schema itself does not fit the subset this checker reads."""


_cache: dict[str, dict] = {}


def load_schema(path) -> dict:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = HERE / p
    k = str(p.resolve())
    if k not in _cache:
        s = json.loads(p.read_text(encoding="utf-8"))
        _audit(s, str(p))
        s["__file__"] = str(p.resolve())
        _cache[k] = s
    return _cache[k]


def _audit(s, where: str, path: str = "#") -> None:
    if isinstance(s, bool):
        return
    if not isinstance(s, dict):
        raise SchemaError(f"{where} {path}: a schema is an object or true/false")
    for k in s:
        if k not in KNOWN and not k.startswith("x-") and k != "__file__":
            raise SchemaError(f"{where} {path}: keyword {k!r} is not one this checker reads (formats/check.py KNOWN)")
    for k in ("properties", "patternProperties", "$defs"):
        for n, sub in (s.get(k) or {}).items():
            _audit(sub, where, f"{path}/{k}/{n}")
    for k in ("items", "additionalProperties", "propertyNames", "not"):
        if isinstance(s.get(k), (dict, bool)):
            _audit(s[k], where, f"{path}/{k}")
    for k in ("prefixItems", "anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(s.get(k) or []):
            _audit(sub, where, f"{path}/{k}/{i}")
    for pat in list((s.get("patternProperties") or {})) + ([s["pattern"]] if "pattern" in s else []):
        re.compile(pat)


def _resolve(ref: str, root: dict) -> tuple[dict, dict]:
    file, _, frag = ref.partition("#")
    if file:
        root = load_schema(Path(root.get("__file__", HERE / "x")).parent / file)
    node = root
    for part in [p for p in frag.split("/") if p]:
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node, root


def _is(v, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, TYPES[t])


def _fmt(v, f: str) -> bool:
    if not isinstance(v, str):
        return True
    try:
        if f == "date":
            return bool(DATE.fullmatch(v)) and bool(_dt.date.fromisoformat(v))
        if f == "date-time":
            return bool(DATETIME.fullmatch(v)) and bool(_dt.datetime.fromisoformat(v.replace("Z", "+00:00").replace(" ", "T")))
    except ValueError:
        return False
    if f == "uri":
        return bool(URI.fullmatch(v))
    if f == "email":
        return bool(EMAIL.fullmatch(v))
    return True


def errors(value, schema, root: dict | None = None, path: str = "", limit: int = 50) -> list[str]:
    """Every way `value` does not fit `schema` (a dict, or a path to a schema file), as "path: what" lines; [] when it
    fits. At most `limit` lines."""
    if not isinstance(schema, (dict, bool)):
        schema = load_schema(schema)
    elif root is None:
        _audit(schema, "the schema given")                    # a keyword the checker does not read is never ignored
    root = root if root is not None else (schema if isinstance(schema, dict) else {})
    out: list[str] = []
    _check(value, schema, root, path or "$", out, limit)
    return out[:limit]


def _check(v, s, root, path, out, limit):
    if len(out) >= limit:
        return
    if s is True:
        return
    if s is False:
        out.append(f"{path}: not allowed here")
        return
    if "$ref" in s:
        node, r = _resolve(s["$ref"], root)
        _check(v, node, r, path, out, limit)
        if len(s) == 1 or set(s) <= {"$ref", "description", "title", "$comment"} | {k for k in s if k.startswith("x-")}:
            return
    t = s.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(v, x) for x in ts):
            out.append(f"{path}: {type(v).__name__} where {' or '.join(ts)} belongs")
            return
    if "const" in s and v != s["const"]:
        out.append(f"{path}: {v!r} where {s['const']!r} belongs")
    if "enum" in s and v not in s["enum"]:
        out.append(f"{path}: {str(v)[:80]!r} is not one of {s['enum'][:12]}{'…' if len(s['enum']) > 12 else ''}")
    if isinstance(v, str):
        if "pattern" in s and not re.search(s["pattern"], v):
            out.append(f"{path}: {v[:80]!r} does not match {s['pattern']}")
        if "minLength" in s and len(v) < s["minLength"]:
            out.append(f"{path}: shorter than {s['minLength']}")
        if "maxLength" in s and len(v) > s["maxLength"]:
            out.append(f"{path}: longer than {s['maxLength']} ({len(v)})")
        if "format" in s and not _fmt(v, s["format"]):
            out.append(f"{path}: {v[:60]!r} is no {s['format']}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            out.append(f"{path}: {v} is below {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            out.append(f"{path}: {v} is above {s['maximum']}")
    if isinstance(v, dict):
        for k in s.get("required") or []:
            if k not in v:
                out.append(f"{path}: {k!r} is missing")
        props, pats = s.get("properties") or {}, s.get("patternProperties") or {}
        for k, x in v.items():
            if "propertyNames" in s:
                _check(k, s["propertyNames"], root, f"{path}[key {k!r}]", out, limit)
            hit = False
            if k in props:
                hit = True
                _check(x, props[k], root, at(path, k), out, limit)
            for pat, sub in pats.items():
                if re.search(pat, k):
                    hit = True
                    _check(x, sub, root, at(path, k), out, limit)
            if not hit and "additionalProperties" in s:
                ap = s["additionalProperties"]
                if ap is False:
                    out.append(f"{path}: key {k!r} is not in the format")
                else:
                    _check(x, ap, root, at(path, k), out, limit)
            if len(out) >= limit:
                return
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            out.append(f"{path}: fewer than {s['minItems']} items")
        if "maxItems" in s and len(v) > s["maxItems"]:
            out.append(f"{path}: more than {s['maxItems']} items")
        if s.get("uniqueItems"):
            seen = set()
            for i, x in enumerate(v):
                k = json.dumps(x, sort_keys=True, default=str)
                if k in seen:
                    out.append(f"{path}[{i}]: listed twice")
                seen.add(k)
        pre = s.get("prefixItems") or []
        for i, x in enumerate(v):
            if i < len(pre):
                _check(x, pre[i], root, f"{path}[{i}]", out, limit)
            elif "items" in s:
                _check(x, s["items"], root, f"{path}[{i}]", out, limit)
            if len(out) >= limit:
                return
    for k in ("allOf",):
        for sub in s.get(k) or []:
            _check(v, sub, root, path, out, limit)
    if "anyOf" in s:
        if not any(not errors(v, sub, root, path, 1) for sub in s["anyOf"]):
            out.append(f"{path}: fits none of its forms ({len(s['anyOf'])})")
    if "oneOf" in s:
        n = sum(1 for sub in s["oneOf"] if not errors(v, sub, root, path, 1))
        if n != 1:
            out.append(f"{path}: fits {n} of its forms where exactly one belongs")
    if "not" in s and not errors(v, s["not"], root, path, 1):
        out.append(f"{path}: fits what is not allowed here")


def read_data(data_path):
    """A data file as its schema describes it: JSON as it is; JSON Lines (.jsonl) as the list of its lines, a line
    that starts with // being a comment (the training files open with one saying what wrote them); YAML as JSON
    (dates as strings, keys as strings: `on` is read by YAML as true, so as the key "True")."""
    p = Path(data_path)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        import yaml      # PyYAML: the crawler's own dependency; YAML's dates and keys become strings, as JSON has them

        def plain(x):
            if isinstance(x, (_dt.date, _dt.datetime)):
                return x.isoformat()
            if isinstance(x, dict):
                return {str(k): plain(v) for k, v in x.items()}
            if isinstance(x, list):
                return [plain(v) for v in x]
            return x
        return plain(yaml.safe_load(text))
    if p.suffix == ".jsonl":
        return [json.loads(ln) for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("//")]
    return json.loads(text)


def check_file(schema_path, data_path, limit: int = 20) -> list[str]:
    try:
        doc = read_data(data_path)
    except ValueError as exc:
        return [f"$: does not read as {'JSON Lines' if str(data_path).endswith('.jsonl') else 'JSON'} ({str(exc)[:80]})"]
    return errors(doc, schema_path, limit=limit)


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    limit = 20
    if "--max" in a:
        i = a.index("--max")
        limit = int(a[i + 1])
        del a[i:i + 2]
    if len(a) != 2:
        print(__doc__.split("\n\n")[1])
        return 2
    errs = check_file(a[0], a[1], limit)
    for e in errs:
        print(e)
    print(f"{a[1]}: {'fits' if not errs else 'does not fit'} {a[0]}" + (f" ({len(errs)}{'+' if len(errs) >= limit else ''} faults)" if errs else ""))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
