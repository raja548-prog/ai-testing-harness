from ath_core.masking import Masker, luhn_valid


def test_luhn():
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")


def test_pan_masked_only_when_luhn_valid():
    m = Masker()
    assert m.mask("card 4111 1111 1111 1111 ok") == "card [PAN****1111] ok"
    assert m.mask("order 1234567890123") == "order 1234567890123"  # not Luhn-valid
    assert m.hits["pan"] == 1


def test_track2_cvv_pinblock():
    m = Masker()
    out = m.mask("t2=;4111111111111111=29121011234? cvv=123 pinBlock: 0412AC89FE3B7D21")
    assert "[TRACK2]" in out and "4111" not in out
    assert "cvv=***" in out
    assert "[PINBLOCK]" in out


def test_aadhaar_and_mobile():
    m = Masker()
    out = m.mask("aadhaar 2345 6789 0124 mobile +91 9876543210")  # Verhoeff-valid test number
    assert "[AADHAAR]" in out and "[MOBILE]" in out


def test_rrn_is_not_mistaken_for_aadhaar():
    # RRNs are 12 digits too and are the key logs are fetched by; only Verhoeff-valid numbers are Aadhaar.
    m = Masker()
    assert m.mask("settlement failed for rrn=629014773311") == "settlement failed for rrn=629014773311"
    assert m.mask("id 2345 6789 0123") == "id 2345 6789 0123"  # checksum fails: not an Aadhaar number


def test_categories_follow_profile():
    m = Masker(categories=["pan"])
    assert "9876543210" in m.mask("mobile 9876543210")


def test_declared_correlation_key_values_are_not_masked_as_aadhaar():
    valid = "234567890124"  # Verhoeff-valid, so it would otherwise be masked as Aadhaar
    m = Masker(correlation_keys=["rrn", "stan"])
    for text in (f"rrn={valid}", f"RRN: {valid}", f'{{"rrn": "{valid}"}}', f"stan='{valid}'"):
        assert valid in m.mask(text), text
    # not a declared key, or not directly its value: still masked
    assert "[AADHAAR]" in m.mask(f"aadhaar={valid}")
    assert "[AADHAAR]" in m.mask(f"rrn=1 customer {valid}")
    assert "[AADHAAR]" in Masker().mask(f"rrn={valid}")  # no keys declared: nothing exempt


def test_correlation_keys_never_exempt_card_numbers():
    m = Masker(correlation_keys=["rrn"])
    assert "4111111111111111" not in m.mask("rrn=4111111111111111")
