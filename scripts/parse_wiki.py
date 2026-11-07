"""Parse the two scraped Wikipedia pages into counties.json and municipalities.json."""
import json
import re
import sys

TR = "/root/.claude/projects/-home-claude/ad47f7c2-0735-5215-9613-8f189ac20495/tool-results/"
muni_md = open(TR + "mcp-Firecrawl-firecrawl_scrape-1793923049772.txt.md", encoding="utf-8").read()
cty_md = open(TR + "mcp-Firecrawl-firecrawl_scrape-1793923052291.txt.md", encoding="utf-8").read()

LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def strip_links(s):
    s = LINK.sub(lambda m: m.group(1), s)
    s = re.sub(r"\[\\\[\d+\\\]\]\([^)]*\)", "", s)  # footnotes
    return s.strip()


def num(s):
    s = strip_links(s).replace(",", "").replace("−", "-")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


# ---------------------------------------------------------------- counties
counties = {}
for line in cty_md.splitlines():
    if not line.startswith("| [") or "County](" not in line.split("|")[1]:
        continue
    c = cells(line)
    if len(c) < 4:
        continue
    m = LINK.search(c[0])
    if not m:
        continue
    name = re.sub(r"County$", "", m.group(1)).strip()  # "ApplingCounty" -> "Appling"
    fips = strip_links(c[1])
    if not re.fullmatch(r"\d{3}", fips):
        continue
    seat = strip_links(c[2])
    counties[name] = {"name": name, "fips": "13" + fips, "seat": seat, "wikipedia": m.group(2)}

# population table (second table): "| [Appling](...#Demographics) | map | Total Population | ..."
for line in cty_md.splitlines():
    if "#Demographics" not in line or not line.startswith("| ["):
        continue
    c = cells(line)
    m = LINK.search(c[0])
    name = m.group(1).strip()
    if name in counties and len(c) > 2:
        counties[name]["population"] = int(num(c[2]) or 0)

# ---------------------------------------------------------------- municipalities
munis = []
for line in muni_md.splitlines():
    if not line.startswith("| ["):
        continue
    c = cells(line)
    if len(c) < 4:
        continue
    m = LINK.search(c[0])
    if not m or "wikipedia.org/wiki/" not in m.group(2):
        continue
    raw = m.group(1)
    seat = "†" in raw
    name = raw.replace("†", "").replace("‡", "").strip()
    name = re.sub(r"\s+", " ", name)
    mtype = strip_links(c[1])
    if mtype not in ("City", "Town", "Consolidated city-county", "Consolidated city–county", "Village"):
        # some rows: "City" may carry a footnote
        mtype = re.sub(r"\[.*", "", mtype).strip()
    cty_links = LINK.findall(c[2])
    cty_names = [re.sub(r"\s*County.*$", "", t).strip() for t, _ in cty_links] or [strip_links(c[2])]
    cty_names = [re.sub(r"\s+", " ", x) for x in cty_names]
    cty_names = ["DeKalb" if x.lower() == "dekalb" else x for x in cty_names]
    pop2020 = num(c[3])
    munis.append({
        "name": name,
        "type": mtype,
        "counties": cty_names,
        "county_seat": seat,
        "population": int(pop2020) if pop2020 is not None else None,
        "wikipedia": m.group(2),
    })

json.dump(list(counties.values()), open(sys.argv[1] + "/counties.json", "w"), indent=1)
json.dump(munis, open(sys.argv[1] + "/municipalities.json", "w"), indent=1)
print(len(counties), "counties;", len(munis), "municipalities")
print("types:", {t: sum(1 for m in munis if m["type"] == t) for t in set(m["type"] for m in munis)})
print("missing county pop:", [c["name"] for c in counties.values() if "population" not in c])
print("unknown counties in munis:", sorted({x for m in munis for x in m["counties"] if x not in counties}))
print("no pop:", [m["name"] for m in munis if m["population"] is None])
