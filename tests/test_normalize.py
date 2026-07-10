from leadops.importers import normalize_phone


def test_plain_10_digit():
    assert normalize_phone("7195550101") == ("7195550101", True)


def test_formatted():
    assert normalize_phone("(719) 555-0101") == ("7195550101", True)


def test_leading_country_code():
    assert normalize_phone("+1 (720) 555-0188") == ("7205550188", True)


def test_too_short_invalid():
    digits, ok = normalize_phone("55512")
    assert ok is False


def test_non_numeric_invalid():
    assert normalize_phone("not-a-phone") == ("", False)


def test_empty_invalid():
    assert normalize_phone("") == ("", False)


def test_eleven_digits_not_starting_one_invalid():
    digits, ok = normalize_phone("23456789012")
    assert ok is False
