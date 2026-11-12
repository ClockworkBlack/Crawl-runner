"""A city whose directory entry was read with an older parser is due at once (its officials had first names only)."""
from gacrawl import seed


def test_old_directory_reading_is_due():
    j = {"id": "dunwoody", "kind": "city", "name": "Dunwoody", "fips": "1324768"}
    fresh = {"seed_version": seed.SEED_VERSION, "checked_at": seed._now(), "website": "https://www.dunwoodyga.gov/"}
    assert seed.due({**fresh, "gma_parser": seed.GMA_PARSER}, j, 30) is False
    assert seed.due({**fresh, "gma_parser": seed.GMA_PARSER - 1}, j, 30) is True
    assert seed.due(fresh, j, 30) is True                    # read before the parser was versioned
    assert seed.due(fresh, {**j, "kind": "county", "id": "co-dekalb"}, 30) is False
