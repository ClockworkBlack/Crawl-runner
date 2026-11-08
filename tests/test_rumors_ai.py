from gacrawl.rumors_ai import _json, verdict

TEXT = "Atlanta Mayor Andre Dickens wrote a letter to the DNC. Dickens said the party was abandoning Black voters. Others disagreed."


def test_kept_when_quote_is_exact():
    keep, why = verdict(_json('{"named": true, "quote": "Dickens said the party was abandoning Black voters.", '
                              '"attribution": "named source", "opinion": false}'), "Andre Dickens", TEXT)
    assert keep and not why and keep["attribution"] == "named source"


def test_dropped_when_quote_is_made_up_or_name_is_there():
    keep, why = verdict({"named": True, "quote": "Dickens resigned.", "attribution": "anonymous"}, "Andre Dickens", TEXT)
    assert keep is None and "not copied" in why[0]
    keep, why = verdict({"named": False, "quote": "", "attribution": "none"}, "Andre Dickens", TEXT)
    assert keep is None and "has the name" in why[0]
    keep, _ = verdict({"named": False, "quote": "", "attribution": "none", "opinion": True}, "Austin Scott", TEXT)
    assert keep == {"named": False, "quote": "", "attribution": "none", "opinion": True}
