"""Pure detection of one-time verification codes in incoming message text.

The detector is deliberately conservative. A number only counts as a code
when an OTP keyword (in English, German, French, Italian, Spanish, or Dutch)
sits close to it, and numbers that look like amounts, dates, times, phone
numbers, URLs, or labelled order/tracking/customer numbers are rejected.
Alphanumeric codes such as ``K7X2PQ`` additionally need an OTP-specific
keyword, because generic ``code`` messages are often promotions or booking
references.

Nothing here logs, stores, or performs I/O; callers decide what to do with
the returned code and must never log it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# OTP messages are short. Long chat messages that merely mention a code and
# contain a number are far more likely to be false positives.
MAX_OTP_MESSAGE_CHARS = 1000

# Characters a candidate may be at most this far from a keyword.
_MAX_DISTANCE_BEFORE = 48  # keyword ... code
_MAX_DISTANCE_AFTER = 32   # code ... keyword ("123456 is your code")
# Codes that could be a year, and alphanumeric codes, must follow a keyword
# almost immediately ("code: 2024", "code is K7X2PQ").
_TIGHT_DISTANCE = 12
# How far back a negative label ("order", "Nr.") may precede a candidate.
_LABEL_WINDOW = 24

_DASHES = dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), "-")

# ``\w*code`` covers compounds (Bestätigungscode, Sicherheitscode,
# verificatiecode, passcode) as well as the plain word.
_CODE_WORD = re.compile(
    r"(?<!\w)(\w*?)(codes?|kode|codice|c[oó]digos?)(?!\w)", re.IGNORECASE
)

# Keywords specific enough that alphanumeric candidates are acceptable.
_STRONG_KEYWORD = re.compile(
    r"(?<!\w)(?:"
    r"verif\w*|v[ée]rif\w*"
    r"|authenti\w*|authentifi\w*|autenti\w*"
    r"|otp|2fa|mfa|two[- ]?factor|two[- ]?step|zwei[- ]?faktor\w*|double authentification"
    r"|one[- ]time|einmal\w*|usage unique|monouso|un solo uso"
    r"|passcode"
    r"|do not share|don'?t share|never share|nicht weiter\w*|niemandem|"
    r"ne (?:le |la )?partagez|non condividere|no (?:lo )?compartas"
    r")(?!\w)",
    re.IGNORECASE,
)

# Bank transaction numbers. Case-sensitive on purpose: "tan" is an ordinary
# word in English ("a tan") and Spanish ("tan barato").
_TAN_KEYWORD = re.compile(r"(?<!\w)(?:m|sms|push|photo|chip)?TAN(?!\w)")

# Weak keywords are accepted for plain numeric codes only.
_WEAK_KEYWORD = re.compile(
    r"(?<!\w)(?:pin|password|passwort|kennwort|mot de passe|wachtwoord)(?!\w)",
    re.IGNORECASE,
)

# ``<prefix>code`` compounds and ``<word> code`` pairs that are OTP specific.
_STRONG_CODE_PREFIXES = frozenset({
    "security", "sicherheits", "bestätigungs", "bestaetigungs", "verification",
    "verifizierungs", "verifikations", "einmal", "anmelde", "login", "log-in",
    "signin", "sign-in", "zugangs", "access", "auth", "authentication",
    "authentifizierungs", "freigabe", "prüf", "pruef", "otp", "2fa", "sms",
    "verificatie", "beveiligings", "toegangs", "pass", "one-time", "onetime",
})

# ``<prefix>code`` compounds and ``<word> code`` pairs that are never OTPs.
_NEGATIVE_CODE_PREFIXES = frozenset({
    "promo", "promotion", "promotional", "rabatt", "gutschein", "discount",
    "coupon", "voucher", "gift", "geschenk", "bar", "qr", "post", "zip",
    "area", "country", "länder", "laender", "tracking", "sendungs", "referral",
    "invite", "invitation", "einladungs", "empfehlungs", "source", "dress",
    "error", "fehler", "status", "color", "colour", "farb", "cheat", "aktions",
    "bonus", "sale", "booking", "buchungs", "reservation", "reservierungs",
    "flight", "flug", "tarif", "rabais", "réduction", "sconto", "promozionale",
    "descuento", "kortings", "cadeau", "regalo", "iban", "bic", "swift",
    "sort", "bank", "bankleit", "zugriffs-promo", "uni", "geo", "html",
})

# Labels that turn a following number into an order/tracking/etc. number.
_NEGATIVE_LABEL = re.compile(
    r"(?<!\w)(?:"
    r"order|bestell\w*|auftrag\w*|tracking|sendung\w*|paket\w*|parcel|shipment"
    r"|invoice|rechnung\w*|kunden\w*|customer|account|konto\w*|iban"
    r"|ref|reference|referenz|commande|ordine|pedido|facture|fattura|factura"
    r"|nr|no|n°|nº|tel|phone|telefon\w*|téléphone|telefono|mobile?|handy|call"
    r"|anruf\w*|rufen|appel\w*|chiam\w*|llam\w*|flight|flug\w*|gate|seat|sitz\w*"
    r"|room|zimmer|ticket\w*|booking|buchung\w*|reservation|reservierung\w*"
    r"|plz|postleitzahl|zip|postcode|case|fall|vorgang\w*|dossier|ausweis\w*"
    r"|page|pages|seite\w*|pagina|line|zeile|chapter|kapitel|version|build|model|modell"
    r")(?!\w)[\s.:#-]*$",
    re.IGNORECASE,
)

_CURRENCY_BEFORE = re.compile(
    r"(?:[€$£¥₹#+]|(?<!\w)(?:eur|usd|chf|gbp|sfr|fr|rs|inr))\.?\s?$",
    re.IGNORECASE,
)
_UNIT_AFTER = re.compile(
    r"^\s?(?:[€$£¥₹%°]|(?:eur|euro|euros|usd|chf|gbp|fr|franken|francs?|dollars?"
    r"|km|kg|mb|gb|kb|min|mins|minutes?|minuten|minuti|sec|secs|seconds?"
    r"|sek|sekunden|h|hrs|hours?|stunden|std|tage|days?|jours?|giorni|días"
    r"|punkte|points|pts|x)(?!\w))",
    re.IGNORECASE,
)

# Linear on hostile input: the e-mail branch cannot backtrack across "@"
# or "." boundaries (a naive \S+@\S+\.\w+ is cubic).
_URL = re.compile(
    r"(?:https?://|www\.)\S+|[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", re.IGNORECASE
)

# One token: optional letter prefix (Google's G-123456), then 3-3 grouped
# digits, 4-8 plain digits, or 4-8 upper-case alphanumerics with at least one
# letter and one digit. Only ASCII digits count: NFKC already maps full-width
# digits, while other scripts' digits (Arabic-Indic, Devanagari, ...) are
# deliberately not treated as codes because verification fields expect ASCII.
_CANDIDATE = re.compile(
    r"(?<![\w])"
    r"(?:(?P<prefix>[A-Z]{1,3})-)?"
    r"(?P<body>"
    r"(?P<grouped>[0-9]{3}[- ][0-9]{3})"
    r"|(?P<digits>[0-9]{4,8})"
    r"|(?P<alnum>(?=[A-Z0-9]{0,7}[0-9])(?=[A-Z0-9]{0,7}[A-Z])[A-Z0-9]{4,8})"
    r")"
    r"(?![\w])"
)

_NUMERIC_JOINERS = ".,:/'-"


@dataclass(frozen=True, slots=True)
class _Keyword:
    start: int
    end: int
    strong: bool


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: int
    end: int
    value: str
    numeric: bool


def _normalize(text: str) -> str:
    # NFKC maps full-width digits and no-break spaces to ASCII forms.
    return unicodedata.normalize("NFKC", text).translate(_DASHES)


def _previous_word(text: str, end: int) -> str:
    match = re.search(r"(\w[\w-]*)[\s-]+$", text[max(0, end - 32):end])
    return match.group(1).casefold() if match else ""


def _keywords(text: str) -> list[_Keyword]:
    found: list[_Keyword] = []
    for match in _CODE_WORD.finditer(text):
        prefix = match.group(1).casefold().rstrip("-")
        previous = _previous_word(text, match.start())
        if prefix in _NEGATIVE_CODE_PREFIXES or (
            not prefix and previous in _NEGATIVE_CODE_PREFIXES
        ):
            continue
        strong = prefix in _STRONG_CODE_PREFIXES or (
            not prefix and previous in _STRONG_CODE_PREFIXES
        )
        found.append(_Keyword(match.start(), match.end(), strong))
    for match in _STRONG_KEYWORD.finditer(text):
        found.append(_Keyword(match.start(), match.end(), True))
    for match in _TAN_KEYWORD.finditer(text):
        found.append(_Keyword(match.start(), match.end(), True))
    for match in _WEAK_KEYWORD.finditer(text):
        found.append(_Keyword(match.start(), match.end(), False))
    return found


def _url_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _URL.finditer(text)]


def _glued_to_number(text: str, start: int, end: int) -> bool:
    """True when the token is one part of a longer number, date, or time."""
    before = text[max(0, start - 2):start]
    after = text[end:end + 2]
    if len(before) == 2 and before[1] in _NUMERIC_JOINERS + " " and before[0].isdigit():
        return True
    if len(after) == 2 and after[0] in _NUMERIC_JOINERS + " " and after[1].isdigit():
        return True
    return False


def _candidates(text: str) -> list[_Candidate]:
    urls = _url_spans(text)
    found: list[_Candidate] = []
    for match in _CANDIDATE.finditer(text):
        start, end = match.span()
        body_start = match.start("body")
        if any(url_start <= start < url_end for url_start, url_end in urls):
            continue
        if start and text[start - 1] in "/=?&@_\\":
            continue
        if _glued_to_number(text, body_start if not match.group("prefix") else start, end):
            continue
        if _CURRENCY_BEFORE.search(text[max(0, start - 5):start]):
            continue
        if _UNIT_AFTER.match(text[end:end + 12]):
            continue
        if match.group("alnum"):
            found.append(_Candidate(start, end, match.group("alnum"), False))
        else:
            digits = re.sub(r"[^0-9]", "", match.group("body"))
            found.append(_Candidate(start, end, digits, True))
    return found


def _looks_like_year(value: str) -> bool:
    return len(value) == 4 and 1900 <= int(value) <= 2099


def _has_negative_label(text: str, candidate: _Candidate, keywords: list[_Keyword]) -> bool:
    window_start = max(0, candidate.start - _LABEL_WINDOW)
    for keyword in keywords:
        if window_start <= keyword.end <= candidate.start:
            # A keyword between the label and the number wins:
            # "order code: 123456" is still a code.
            window_start = max(window_start, keyword.end)
    return bool(_NEGATIVE_LABEL.search(text[window_start:candidate.start]))


def _distance(candidate: _Candidate, keyword: _Keyword) -> tuple[int, bool] | None:
    """Return (distance, keyword_before) when the keyword is close enough."""
    if keyword.end <= candidate.start:
        gap = candidate.start - keyword.end
        return (gap, True) if gap <= _MAX_DISTANCE_BEFORE else None
    if candidate.end <= keyword.start:
        gap = keyword.start - candidate.end
        return (gap, False) if gap <= _MAX_DISTANCE_AFTER else None
    return None


def extract_otp(body: str | None) -> str | None:
    """Return the one-time code in ``body``, or ``None``.

    Numeric codes are returned as bare digits (``G-123456`` and ``123-456``
    both become the digits a verification field expects). Alphanumeric
    codes are returned unchanged.
    """
    if not body or len(body) > MAX_OTP_MESSAGE_CHARS:
        return None
    text = _normalize(body)
    keywords = _keywords(text)
    if not keywords:
        return None
    has_strong = any(keyword.strong for keyword in keywords)
    best: tuple[int, int, str] | None = None
    for candidate in _candidates(text):
        if not candidate.numeric and not has_strong:
            continue
        if _has_negative_label(text, candidate, keywords):
            continue
        needs_tight = not candidate.numeric or _looks_like_year(candidate.value)
        closest: int | None = None
        for keyword in keywords:
            measured = _distance(candidate, keyword)
            if measured is None:
                continue
            gap, before = measured
            if needs_tight and (not before or gap > _TIGHT_DISTANCE):
                continue
            if closest is None or gap < closest:
                closest = gap
        if closest is None:
            continue
        # Prefer numeric codes, then the closest keyword, then the earliest.
        rank = (0 if candidate.numeric else 1, closest)
        if best is None or rank < best[:2]:
            best = (rank[0], rank[1], candidate.value)
    return best[2] if best is not None else None
