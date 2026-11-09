from gacrawl.entities import norm_person


def test_names_of_initials_only_do_not_crash():
    # a Georgia Ethics line with a donor name of initials only stopped the money step (IndexError)
    for n in ("J.", "J. R.", "A B", ".  .", ""):
        assert isinstance(norm_person(n), str)
    assert norm_person("Mary J Jones") == "mary jones"
