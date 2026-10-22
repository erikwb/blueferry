from __future__ import annotations

import pytest

from blueferry.phone_numbers import address_identities, default_phone_region


@pytest.mark.parametrize("environment, expected", [
    ({"BLUEFERRY_PHONE_REGION": "au", "LC_ALL": "en_US.UTF-8"}, "AU"),
    ({"BLUEFERRY_PHONE_REGION": "invalid", "LANG": "en_AU.UTF-8"}, None),
    ({"BLUEFERRY_PHONE_REGION": "", "LANG": "en_AU.UTF-8"}, None),
    ({"LC_ALL": "en_GB.UTF-8", "LC_TELEPHONE": "en_AU.UTF-8"}, "GB"),
    ({"LC_ALL": "", "LC_TELEPHONE": "en_AU.UTF-8", "LANG": "en_US.UTF-8"}, "AU"),
    ({"LANG": "de_DE.UTF-8@euro"}, "DE"),
    ({"LANG": "en-NZ"}, "NZ"),
    ({"LC_ALL": "C", "LANG": "en_AU.UTF-8"}, None),
    ({"LANG": "C.UTF-8"}, None),
    ({"LANG": "POSIX"}, None),
    ({"LANG": "en", "LANGUAGE": "en_AU"}, None),
    ({}, None),
])
def test_numbering_region_uses_explicit_override_then_telephone_locale(environment, expected):
    assert default_phone_region(environment) == expected


@pytest.mark.parametrize("region, national, international", [
    ("AU", "0412345678", "+61412345678"),
    ("NZ", "0211234567", "+64211234567"),
    ("GB", "02079460018", "+442079460018"),
    ("DE", "030123456", "+4930123456"),
    ("IT", "0236618300", "+390236618300"),
    ("US", "2025550123", "+12025550123"),
])
def test_national_and_international_forms_share_identities(region, national, international):
    expected = {f"phone:{national}", f"phone:{international[1:]}"}
    assert set(address_identities(national, region)) == expected
    assert set(address_identities(international, region)) == expected


@pytest.mark.parametrize("raw, region, expected", [
    ("0412345678", None, ("phone:0412345678",)),
    ("5551234567", None, ("phone:5551234567",)),
    ("+61412345678", "US", ("phone:61412345678",)),
    ("+12025550123", "AU", ("phone:12025550123",)),
    ("+4415551234567", "US", ("phone:4415551234567",)),
    ("+999123456789", "AU", ("phone:999123456789",)),
    ("12345678901234567890", "AU", ("phone:12345678901234567890",)),
    ("Alice@example.com", "AU", ("email:alice@example.com",)),
    ("12345", "AU", ("short:12345",)),
    ("Alice", "AU", ()),
    (None, "AU", ()),
])
def test_foreign_unknown_and_non_phone_addresses_are_not_guessed(raw, region, expected):
    assert address_identities(raw, region) == expected
