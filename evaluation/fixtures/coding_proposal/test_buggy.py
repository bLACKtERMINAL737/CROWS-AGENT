from buggy import normalize_name


def test_normalize_name_collapses_internal_whitespace():
    assert normalize_name("  Crows   Agent  ") == "Crows Agent"
