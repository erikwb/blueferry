"""Country-aware lookup identities, separate from message destinations."""
from __future__ import annotations

import re
from collections.abc import Mapping

import phonenumbers

from blueferry.events import canonical_address


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


def address_identities(raw: str | None, region: str | None) -> tuple[str, ...]:
    """Keep the existing identity and add possible national/international forms.

    National aliases belong only to the configured numbering plan. A foreign
    number must never acquire a local alias just because its suffix matches.
    These keys are for lookup only: never send a message to a generated alias.
    """
    identity = canonical_address(raw)
    if identity is None:
        return ()
    identities = [identity]
    if not identity.startswith("phone:"):
        return tuple(identities)
    try:
        number = phonenumbers.parse(raw or "", region)
    except phonenumbers.NumberParseException:
        return tuple(identities)
    if not phonenumbers.is_possible_number(number):
        return tuple(identities)
    formats = [phonenumbers.PhoneNumberFormat.E164]
    if region and number.country_code == phonenumbers.country_code_for_region(region):
        formats.append(phonenumbers.PhoneNumberFormat.NATIONAL)
    for style in formats:
        alias = canonical_address(phonenumbers.format_number(number, style))
        if alias and alias not in identities:
            identities.append(alias)
    return tuple(identities)
