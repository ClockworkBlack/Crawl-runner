"""Join Census 2020 incorporated places + Wikipedia municipalities/counties into config/jurisdictions.yaml."""
import json
import re
import sys
import unicodedata

import yaml

S = "/tmp/claude-0/-home-claude/ad47f7c2-0735-5215-9613-8f189ac20495/scratchpad/"
OUT = sys.argv[1]

counties = json.load(open(S + "counties.json"))
munis = json.load(open(S + "municipalities.json"))

# ---- census places
census = {}
for line in open(S + "census_places.txt").read().splitlines()[1:]:
    st, stfp, cofp, coname, placefp, placens, pname, ptype, classfp, func = line.split("|")
    if ptype != "INCORPORATED PLACE":
        continue
    rec = census.setdefault(placefp, {"fips": stfp + placefp, "census_name": pname, "classfp": classfp,
                                      "funcstat": func, "counties": []})
    rec["counties"].append(re.sub(r" County$", "", coname))

print("census incorporated places:", len(census))


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("saint ", "st. ").replace("st ", "st. ")
    s = re.sub(r"\b(city|town|consolidated government|unified government)\b", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def slug(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("'", "").replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# index census by normalized base name
by_name = {}
for fp, rec in census.items():
    base = re.sub(r" (city|town)$", "", rec["census_name"])
    base = re.sub(r" consolidated government.*$", "", base)
    base = re.sub(r" unified government.*$", "", base)
    base = re.sub(r"\(balance\)", "", base).strip()
    by_name.setdefault(norm(base), []).append(rec)

# Municode library slugs confirmed in search-engine results (library.municode.com/ga/<slug>).
MUNICODE_SEEN = set("""madison cornelia washington_county fairburn thomson metter nelson bremen perry grantville
forest_park east_point pooler richmond_hill liberty_county chamblee nashville seminole_county woodbine heard_county
berkeley_lake savannah thunderbolt midway lawrenceville fayette_county gordon_county cobb_county harlem ware_county
demorest sky_valley holly_springs jasper_county gainesville bryan_county ringgold stockbridge clayton_county
haralson_county south_fulton burke_county griffin leslie woodstock gwinnett_county carroll_county kingsland
cartersville auburn putnam_county dacula vidalia atlanta butler lavonia troup_county alpharetta dawson norcross
buena_vista bowdon oglethorpe_county ocilla west_point rutledge jasper garden_city johnson_county college_park
hart_county grovetown rockmart bartow_county mitchell_county tucker dalton roswell lagrange henry_county smyrna
columbia_county sandy_springs franklin buford albany""".split())

CONSOLIDATED = {  # municipality name -> county it is consolidated with
    "Athens": "Clarke", "Augusta": "Richmond", "Columbus": "Muscogee", "Macon": "Bibb",
    "Cusseta": "Chattahoochee", "Georgetown": "Quitman",
}
UNIFIED_COUNTIES = {"Echols", "Webster"}

out_counties = []
for c in sorted(counties, key=lambda x: x["name"]):
    cid = "co-" + slug(c["name"])
    entry = {
        "id": cid, "name": c["name"] + " County", "kind": "county", "fips": c["fips"], "seat": c["seat"],
        "population": c.get("population"), "wikipedia": c["wikipedia"],
        "municode": f"https://library.municode.com/ga/{c['name'].lower().replace(' ', '_')}_county",
    }
    if c["name"].lower().replace(" ", "_") + "_county" not in MUNICODE_SEEN:
        entry.pop("municode")  # only links actually seen; never pattern-built
    merged = [m for m, co in CONSOLIDATED.items() if co == c["name"]]
    if merged:
        entry["consolidated_with"] = merged[0]
    if c["name"] in UNIFIED_COUNTIES:
        entry["unified_government"] = True
    out_counties.append(entry)

out_munis, unmatched, ambiguous = [], [], []
seen_fips = set()
for m in sorted(munis, key=lambda x: x["name"]):
    if m["type"] == "Unified government":
        continue  # county-level; handled above
    key = norm(m["name"].replace("–", "-"))
    cands = by_name.get(key, [])
    MANUAL = {"Athens": "03440", "Augusta": "04204", "Cusseta": "21017", "Georgetown": "32528", "Macon": "49008",
              "McRae–Helena": "49100", "Mableton": "48288"}
    if m["name"] in MANUAL:
        cands = [census[MANUAL[m["name"]]]] if MANUAL[m["name"]] in census else [
            {"fips": "13" + MANUAL[m["name"]], "census_name": m["name"], "counties": m["counties"], "funcstat": "A"}]
    # disambiguate by county
    if len(cands) > 1:
        cands2 = [r for r in cands if set(r["counties"]) & set(m["counties"])]
        if len(cands2) == 1:
            cands = cands2
        else:
            ambiguous.append((m["name"], [r["census_name"] for r in cands]))
    rec = cands[0] if len(cands) == 1 else None
    if rec is None:
        unmatched.append(m["name"])
    fips = rec["fips"] if rec else None
    if fips:
        seen_fips.add(fips)
    name = m["name"].replace("–", "-")
    base = re.sub(r"[^a-z0-9]+", "_", name.lower().replace("'", "").replace(".", "")).strip("_")
    entry = {
        "id": slug(name) if m["name"] not in CONSOLIDATED else slug(name) + "-" + slug(CONSOLIDATED[m["name"]]),
        "name": name, "kind": "city" if m["type"] != "Town" else "town",
        "fips": fips, "counties": rec["counties"] if rec else m["counties"],
        "county_seat": m["county_seat"], "population": m["population"], "wikipedia": m["wikipedia"],
        "municode": f"https://library.municode.com/ga/{base}",
    }
    if base not in MUNICODE_SEEN:
        entry.pop("municode")  # only links actually seen; never pattern-built
    if m["name"] in CONSOLIDATED:
        entry["kind"] = "consolidated"
        entry["consolidated_with"] = CONSOLIDATED[m["name"]] + " County"
    if m["type"] == "Consolidated city-county" and m["name"] not in CONSOLIDATED:
        entry["kind"] = "consolidated"
    out_munis.append(entry)

missing_from_wiki = [r["census_name"] for fp, r in census.items() if r["fips"] not in seen_fips and r["funcstat"] == "A"]
print("munis:", len(out_munis), "unmatched:", unmatched, "ambiguous:", ambiguous)
print("census places not in wikipedia list:", missing_from_wiki)
ids = [e["id"] for e in out_counties + out_munis]
dups = {i for i in ids if ids.count(i) > 1}
print("duplicate ids:", dups)

doc = {
    "_about": "Every Georgia county and municipality. Generated from Census 2020 place codes + Wikipedia; "
              "hand edits go in overrides.yaml, machine discoveries in state/discovered.json.",
    "counties": out_counties,
    "municipalities": out_munis,
}
yaml.safe_dump(doc, open(OUT, "w"), sort_keys=False, allow_unicode=True, width=200)
print("wrote", OUT)
