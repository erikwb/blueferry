"""Pure detection of one-time verification codes in incoming message text.

The detector is deliberately conservative. A number only counts as a code
when it is bound to a code noun ("code", "Bestätigungscode", "TAN", "OTP",
"PIN", ...) in a way OTP messages use:

* an OTP-specific noun ("verification code", "Sicherheitscode", "mTAN")
  close to the number;
* a plain noun followed by a connector ("code: 123456", "code is 123456",
  "Code lautet 123456") or preceded by "123456 is your ... code";
* a plain noun near the number when the message also carries OTP context
  ("verify", "one-time", "do not share", "Anmeldung", ...);
* an entry instruction ("enter 123456", "geben Sie 123456 ein") in a
  message with OTP context.

Context words alone ("Verify your email to get 5000 points", "one-time
offer", "Einmalzahlung") never bind a number. Numbers that look like
amounts, dates, times, phone numbers, URLs, or labelled order/tracking/
customer numbers are rejected, and promotional messages need an
OTP-specific noun. Alphanumeric codes such as ``K7X2PQ`` need an
OTP-specific noun or OTP context, because generic ``code`` messages are
often promotions or booking references.

Nothing here logs, stores, or performs I/O; callers decide what to do with
the returned code and must never log it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

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

# PINs and passwords are code nouns only for numeric codes, and only
# OTP-specific with a one-time prefix ("Einmal-PIN", "one-time password").
_PIN_WORD = re.compile(
    r"(?<!\w)(\w*?)(pin|passwor[dt]|kennwort|mot de passe|wachtwoord)(?!\w)",
    re.IGNORECASE,
)

# "Nummer"/"number" is a code noun only with an OTP-specific prefix
# (Bestätigungsnummer, Transaktionsnummer); order and customer numbers are
# labels instead.
_NUMBER_WORD = re.compile(r"(?<!\w)(\w*?)(nummer|number)(?!\w)", re.IGNORECASE)
_STRONG_NUMBER_PREFIXES = frozenset({
    "bestätigungs", "bestaetigungs", "verifizierungs", "verifikations",
    "transaktions", "sicherheits", "einmal", "verification",
})

# Nouns that are OTP-specific on their own.
_STRONG_NOUN = re.compile(r"(?<!\w)(?:otp|passcode)(?!\w)", re.IGNORECASE)

# Bank transaction numbers. Case-sensitive on purpose: "tan" is an ordinary
# word in English ("a tan") and Spanish ("tan barato").
_TAN_KEYWORD = re.compile(r"(?<!\w)(?:m|sms|push|photo|chip)?TAN(?!\w)")

# OTP context. These words never bind a number on their own ("Verify your
# email to get 5000 points", "one-time offer"); they only let a plain code
# noun or an entry instruction count.
_CONTEXT = re.compile(
    r"(?<!\w)(?:"
    r"verif\w*|v[ée]rif\w*"
    r"|authenti\w*|authentifi\w*|autenti\w*"
    r"|otp|2fa|mfa|two[- ]?factor|two[- ]?step|zwei[- ]?faktor\w*|double authentification"
    r"|one[- ]time|einmal\w*|usage unique|monouso|un solo uso"
    r"|log[- ]?in\w*|sign[- ]?in|anmeld\w*|bestätig\w*|bestaetig\w*|confirm\w*"
    r"|expire\w*|gültig\w*|valid"
    r"|do not share|don'?t share|never share|do not give|nicht weiter\w*|niemandem"
    r"|ne (?:le |la )?partagez|non condividere|no (?:lo )?compartas"
    r")(?!\w)",
    re.IGNORECASE,
)

# Marketing and booking messages. A plain code noun near a number does not
# count there, even with context words ("Verify your account and use code
# 4821 for 10%", "Your booking confirmation code: 482915").
_PROMO = re.compile(
    r"[0-9]\s?%|(?<!\w)(?:"
    r"checkout|discount|promo\w*|coupon|voucher|sale|offer|bonus|redeem"
    r"|order|bestell\w*|booking|buchung\w*|reservation|reservierung\w*|flight|flug\w*"
    r"|hotel|ticket\w*|commande|réservation|prenotazione|reserva"
    r"|rabatt\w*|gutschein\w*|angebot\w*|aktion\w*|einlösen|kasse"
    r"|réduction|soldes|sconto|offerta|descuento|oferta|korting"
    r")(?!\w)",
    re.IGNORECASE,
)

# What may sit between a plain noun and its number ("code: 1234",
# "code is 1234", "Code lautet: 1234").
_CONNECTOR = re.compile(
    r"\s*(?:[:=]|(?:is|ist|lautet|lauten|est|è|es|are|sind)(?!\w))(?:\s*[:=])?\s*$",
    re.IGNORECASE,
)
# "123456 is your Instagram code", "123456 ist Ihr Code".
_IS_YOUR = re.compile(
    r"^\s*(?:is|ist|est|è|es)\s+(?:your|dein\w*|ihr\w*|votre|ton|il tuo|tu|uw|je)(?!\w)",
    re.IGNORECASE,
)
# "Enter 123456", "geben Sie bitte 123456 ein", "use 123456 to verify".
_INSTRUCTION = re.compile(
    r"(?<!\w)(?:use|enter|type|input|eingeben|geben(?:\s+sie)?|gib|saisissez|entrez"
    r"|inserisci|introduce|ingresa|voer)"
    r"(?:\s+(?:bitte|please|den|die|the|this|folgenden?|s'il vous plaît))*\s+$",
    re.IGNORECASE,
)
# A sentence boundary: punctuation followed by white space or the end.
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)|\n")
# Long-range binding for OTP-specific nouns ("Ihre mTAN für die Überweisung
# über 1.250,00 EUR auf Konto DE89... lautet: 482915") stays in one sentence.
_LONG_RANGE = 160

# ``<prefix>code`` compounds and ``<word> code`` pairs that are OTP specific.
_STRONG_CODE_PREFIXES = frozenset({
    "security", "sicherheits", "bestätigungs", "bestaetigungs", "verification",
    "verifizierungs", "verifikations", "einmal", "anmelde", "login", "log-in",
    "signin", "sign-in", "zugangs", "access", "auth", "authentication",
    "authentifizierungs", "freigabe", "prüf", "pruef", "otp", "2fa", "sms",
    "verificatie", "beveiligings", "toegangs", "pass", "one-time", "onetime",
    "guard", "bestätigung", "2-step", "two-factor",
})
_STRONG_PIN_PREFIXES = frozenset({
    "einmal", "one-time", "onetime", "otp", "sms", "transaktions",
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
    r"|nr|no|n°|nº|number|nummer|numéro|numero|tel|phone|telefon\w*|téléphone|telefono|mobile?|handy|call"
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

KeywordKind = Literal["strong", "plain", "pin"]


@dataclass(frozen=True, slots=True)
class _Keyword:
    start: int
    end: int
    kind: KeywordKind


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: int
    end: int
    value: str
    numeric: bool


def _normalize(text: str) -> str:
    # NFKC maps full-width digits and no-break spaces to ASCII forms.
    # Invisible format characters (zero-width space, word joiner, soft
    # hyphen, bidi controls) are dropped so "12<ZWSP>3456" reads as one
    # number instead of yielding the fragment "3456".
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def _previous_word(text: str, end: int) -> str:
    match = re.search(r"(\w[\w-]*)[\s-]+$", text[max(0, end - 32):end])
    return match.group(1).casefold() if match else ""


def _prefixed(
    text: str,
    match: re.Match[str],
    strong_prefixes: frozenset[str],
) -> tuple[bool, bool]:
    """Return (negative, strong) for a ``<prefix>noun`` or ``<word> noun``."""
    prefix = match.group(1).casefold().rstrip("-")
    previous = _previous_word(text, match.start())
    negative = prefix in _NEGATIVE_CODE_PREFIXES or (
        not prefix and previous in _NEGATIVE_CODE_PREFIXES
    )
    strong = prefix in strong_prefixes or (not prefix and previous in strong_prefixes)
    return negative, strong


def _keywords(text: str) -> list[_Keyword]:
    found: list[_Keyword] = []
    for match in _CODE_WORD.finditer(text):
        negative, strong = _prefixed(text, match, _STRONG_CODE_PREFIXES)
        if not negative:
            found.append(_Keyword(match.start(), match.end(), "strong" if strong else "plain"))
    for match in _PIN_WORD.finditer(text):
        negative, strong = _prefixed(text, match, _STRONG_PIN_PREFIXES)
        if not negative:
            found.append(_Keyword(match.start(), match.end(), "strong" if strong else "pin"))
    for match in _NUMBER_WORD.finditer(text):
        _negative, strong = _prefixed(text, match, _STRONG_NUMBER_PREFIXES)
        if strong:
            found.append(_Keyword(match.start(), match.end(), "strong"))
    for pattern in (_STRONG_NOUN, _TAN_KEYWORD):
        for match in pattern.finditer(text):
            found.append(_Keyword(match.start(), match.end(), "strong"))
    return found


def _url_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _URL.finditer(text)]


def _joins(char: str) -> bool:
    return char in _NUMERIC_JOINERS or char.isspace() or not char.isalnum()


def _glued_to_number(text: str, start: int, end: int) -> bool:
    """True when the token is one part of a longer number, date, or time.

    Any single separator that is not a letter (".", ",", ":", "/", a space,
    a middle dot, ...) between two digit runs glues them together.
    """
    before = text[max(0, start - 2):start]
    after = text[end:end + 2]
    if len(before) == 2 and before[0].isdigit() and _joins(before[1]):
        return True
    if len(after) == 2 and after[1].isdigit() and _joins(after[0]):
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


def _looks_like_date(value: str) -> bool:
    """Eight digits that read as YYYYMMDD or DDMMYYYY."""
    if len(value) != 8:
        return False

    def valid(year: int, month: int, day: int) -> bool:
        return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31

    return valid(int(value[:4]), int(value[4:6]), int(value[6:])) or valid(
        int(value[4:]), int(value[2:4]), int(value[:2])
    )


def _has_negative_label(text: str, candidate: _Candidate, keywords: list[_Keyword]) -> bool:
    window_start = max(0, candidate.start - _LABEL_WINDOW)
    for keyword in keywords:
        if window_start <= keyword.end <= candidate.start:
            # A keyword between the label and the number wins:
            # "order code: 123456" is still a code.
            window_start = max(window_start, keyword.end)
    window = text[window_start:candidate.start]
    # "order number is 482915" and "Kundennummer: 482915" are still labels.
    connector = _CONNECTOR.search(window)
    if connector is not None:
        window = window[:connector.start()]
    return bool(_NEGATIVE_LABEL.search(window))


def _distance(candidate: _Candidate, keyword: _Keyword) -> tuple[int, bool] | None:
    """Return (distance, keyword_before) when the keyword is close enough."""
    if keyword.end <= candidate.start:
        gap = candidate.start - keyword.end
        return (gap, True) if gap <= _MAX_DISTANCE_BEFORE else None
    if candidate.end <= keyword.start:
        gap = keyword.start - candidate.end
        return (gap, False) if gap <= _MAX_DISTANCE_AFTER else None
    return None


@dataclass(frozen=True, slots=True)
class _Message:
    text: str
    keywords: list[_Keyword]
    context: bool
    promo: bool


def _keyword_gap(message: _Message, candidate: _Candidate, keyword: _Keyword) -> int | None:
    """Return the distance at which ``keyword`` binds ``candidate``, or None."""
    text = message.text
    if keyword.kind == "pin" and not candidate.numeric:
        return None
    needs_tight = (
        not candidate.numeric
        or _looks_like_year(candidate.value)
        or _looks_like_date(candidate.value)
    )
    measured = _distance(candidate, keyword)
    if measured is not None:
        gap, before = measured
        between = (
            text[keyword.end:candidate.start] if before else text[candidate.end:keyword.start]
        )
        tight = before and gap <= _TIGHT_DISTANCE
        if needs_tight and not tight:
            measured = None
        elif keyword.kind == "strong":
            return gap
        elif message.promo:
            # Promotions need an OTP-specific noun.
            return None
        elif not candidate.numeric and not message.context:
            return None
        elif before and _CONNECTOR.fullmatch(between):
            return gap
        elif not before and _IS_YOUR.match(between):
            return gap
        elif message.context:
            return gap
        else:
            return None
    # An OTP-specific noun earlier in the same sentence binds a number that
    # a connector introduces, however long the sentence is.
    if (
        keyword.kind == "strong"
        and keyword.end <= candidate.start
        and candidate.start - keyword.end <= _LONG_RANGE
    ):
        between = text[keyword.end:candidate.start]
        if _CONNECTOR.search(between) and not _SENTENCE_END.search(between):
            return len(between)
    return None


def _instructed(message: _Message, candidate: _Candidate) -> bool:
    """True for "enter 123456" or "geben Sie 123456 ein" in an OTP message."""
    if not message.context or message.promo:
        return False
    return bool(_INSTRUCTION.search(message.text[max(0, candidate.start - 40):candidate.start]))


def extract_otp(body: str | None) -> str | None:
    """Return the one-time code in ``body``, or ``None``.

    Numeric codes are returned as bare digits (``G-123456`` and ``123-456``
    both become the digits a verification field expects). Alphanumeric
    codes are returned unchanged.
    """
    if not body or len(body) > MAX_OTP_MESSAGE_CHARS:
        return None
    text = _normalize(body)
    message = _Message(
        text=text,
        keywords=_keywords(text),
        context=bool(_CONTEXT.search(text)),
        promo=bool(_PROMO.search(text)),
    )
    if not message.keywords and not message.context:
        return None
    best: tuple[int, int, str] | None = None
    for candidate in _candidates(text):
        if _has_negative_label(text, candidate, message.keywords):
            continue
        gaps = [
            gap
            for keyword in message.keywords
            if (gap := _keyword_gap(message, candidate, keyword)) is not None
        ]
        if _instructed(message, candidate):
            gaps.append(0)
        if not gaps:
            continue
        # Prefer numeric codes, then the closest keyword, then the earliest.
        rank = (0 if candidate.numeric else 1, min(gaps))
        if best is None or rank < best[:2]:
            best = (rank[0], rank[1], candidate.value)
    return best[2] if best is not None else None
