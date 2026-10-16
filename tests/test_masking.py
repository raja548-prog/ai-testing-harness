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
    out = m.mask("aadhaar 2345 6789 0123 mobile +91 9876543210")
    assert "[AADHAAR]" in out and "[MOBILE]" in out


def test_categories_follow_profile():
    m = Masker(categories=["pan"])
    assert "9876543210" in m.mask("mobile 9876543210")
