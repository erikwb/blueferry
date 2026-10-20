"""Country-aware lookup identities, separate from message destinations."""
from __future__ import annotations

import re
from collections.abc import Mapping

import phonenumbers

from blueferry.events import canonical_address, is_phone_shaped, normalize_phone


def default_phone_region(environment: Mapping[str, str]) -> str | None:
    """Use an explicit numbering region, then the telephone locale's territory."""
    if "BLUEFERRY_PHONE_REGION" in environment:
        region = environment["BLUEFERRY_PHONE_REGION"].strip().upper()
        return region if region in phonenumbers.SUPPORTED_REGIONS else None
    locale = next((environment[key] for key in ("LC_ALL", "LC_TELEPHONE", "LANG")
                   if environment.get(key)), "")
    language = re.split(r"[.@]", locale, maxsplit=1)[0]
    parts = re.split(r"[_-]", language)
    region = parts[-1].upper() if len(parts) > 1 else ""
    return region if region in phonenumbers.SUPPORTED_REGIONS else None


def _phone_spelling(raw: str | None) -> str:
    # A slash is an area-code separator in some national formats (including
    # older libphonenumber metadata). Keep the general event grammar unchanged.
    value = (raw or "").replace("/", " ")
    return value if is_phone_shaped(value) else ""


def _parse_phone(raw: str | None, region: str | None) -> phonenumbers.PhoneNumber | None:
    value = _phone_spelling(raw)
    if not value:
        return None
    try:
        return phonenumbers.parse(value, region)
    except phonenumbers.NumberParseException:
        return None


def _full_national_number(number: phonenumbers.PhoneNumber, region: str | None) -> bool:
    """Require a full number in this region, excluding known service short codes."""
    return bool(
        region
        and phonenumbers.is_valid_number_for_region(number, region)
        and not phonenumbers.is_valid_short_number_for_region(number, region)
    )


def contact_phone(raw: str, region: str | None) -> str | None:
    """Preserve a contact's delivery spelling, including valid short national numbers."""
    digits = normalize_phone(_phone_spelling(raw))
    if digits is None:
        number = _parse_phone(raw, region)
        if number is None or not _full_national_number(number, region):
            return None
        digits = re.sub(r"\D", "", raw)
    return ("+" if raw.strip().startswith("+") else "") + digits


def address_identities(raw: str | None, region: str | None) -> tuple[str, ...]:
    """Keep the existing identity and add possible national/international forms.

    National aliases belong only to the configured numbering plan. A foreign
    number must never acquire a local alias just because its suffix matches.
    These keys are for lookup only: never send a message to a generated alias.
    """
    identity = canonical_address(raw) or canonical_address(_phone_spelling(raw))
    identities = [identity] if identity else []
    number = _parse_phone(raw, region)
    if number is None or not phonenumbers.is_possible_number(number):
        return tuple(identities)
    if not normalize_phone(_phone_spelling(raw)):
        # A short national number may contain spaces or punctuation that the
        # region-independent short-code identity intentionally does not accept.
        if not _full_national_number(number, region):
            return tuple(identities)
        identity = canonical_address(re.sub(r"\D", "", raw or ""))
        if identity and identity not in identities:
            identities.append(identity)
    if phonenumbers.is_valid_short_number_for_region(number, region or ""):
        return tuple(identities)
    formats = [phonenumbers.PhoneNumberFormat.E164]
    if region and number.country_code == phonenumbers.country_code_for_region(region):
        formats.append(phonenumbers.PhoneNumberFormat.NATIONAL)
    for style in formats:
        formatted = phonenumbers.format_number(number, style)
        digits = re.sub(r"\D", "", formatted)
        if len(digits) < 7 and not _full_national_number(number, region):
            continue
        alias = canonical_address(digits)
        if alias and alias not in identities:
            identities.append(alias)
    return tuple(identities)
