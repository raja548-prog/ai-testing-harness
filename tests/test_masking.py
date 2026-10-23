import pytest

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


def test_pan_next_to_other_digits_is_masked():
    m = Masker()
    assert m.mask("card=4111 1111 1111 1111 12/25") == "card=[PAN****1111] 12/25"  # expiry kept
    assert m.mask("PAN 4111111111111111 100.00 INR") == "PAN [PAN****1111] 100.00 INR"
    assert m.mask("DE002=164111111111111111") == "DE002=16[PAN****1111]"  # ISO 8583 LLVAR length prefix
    assert m.mask("4111111111111111 5500000000000004") == "[PAN****1111] [PAN****0004]"
    assert m.mask("at 2026-10-23 10:15:30 amount 100.00") == "at 2026-10-23 10:15:30 amount 100.00"


def test_cvv_and_pinblock_key_spellings():
    m = Masker()
    for text in ('{"cvv": "123"}', '{"cvv":"123"}', "card_cvv=123", "cvvCode=123", "securityCode: 1234", "CVV 123"):
        assert "123" not in m.mask(text), text
    assert m.mask("acid: 123") == "acid: 123"  # `cid` inside a longer word is not a key
    for text in ('{"pinBlock":"0123456789abcdef"}', '"pin_block": "0412AC89FE3B7D21"'):
        assert "[PINBLOCK]" in m.mask(text), text


def test_track2_with_d_separator_or_expiry_only():
    m = Masker()
    assert m.mask(";4111111111111111D25121010000000000000?") == "[TRACK2]"
    assert m.mask("4111111111111111=2512") == "[TRACK2]"


def test_aadhaar_with_dashes():
    assert Masker().mask("aadhaar 2345-6789-0124") == "aadhaar [AADHAAR]"


def test_non_ascii_digits_do_not_crash():
    Masker().mask("id 2३४५६७८९०१२३ ref ४११११११११११११११११")


def test_categories_are_normalised_and_checked():
    assert Masker(categories=[" PAN "]).mask("4111111111111111") == "[PAN****1111]"
    with pytest.raises(ValueError, match="unknown masking categories"):
        Masker(categories=["pna"])
