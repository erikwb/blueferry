"""One-time code detection over inert message text."""
from __future__ import annotations

import re
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blueferry.otp import MAX_OTP_MESSAGE_CHARS, extract_otp

PROPERTY_SETTINGS = settings(max_examples=300, derandomize=True, deadline=None)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # English
        ("Your Google verification code is G-123456", "123456"),
        ("G-482913 is your Google verification code.", "482913"),
        ("123456 is your Instagram code. Don't share it.", "123456"),
        ("Use 918273 to verify your account", "918273"),
        ("<#> 443322 is your verification code. FA+9qCX9VSu", "443322"),
        ("Your one-time passcode is 90210 and expires in 10 minutes.", "90210"),
        ("Apple ID code: 123456. Don't share it with anyone.", "123456"),
        ("Microsoft account security code: 1234567", "1234567"),
        ("Your WhatsApp code: 123-456", "123456"),
        ("Your code: 004512", "004512"),
        ("Your verification code is K7X2PQ", "K7X2PQ"),
        ("Code 2024 is your login code", "2024"),
        ("Your Amazon OTP is 551234", "551234"),
        ("Steam Guard code: K7X2P", "K7X2P"),
        ("Your Telegram code: 12345. Do not give this code to anyone", "12345"),
        ("Enter 482915 to sign in", "482915"),
        # Invisible format characters do not split a code.
        ("code: 12\u200b3456", "123456"),
        ("Your code is 48\u206029\u00ad15", "482915"),
        ("Telegram code 58291", "58291"),
        ("Amazon: Use 583920 to reset your password. Don't give this code to anyone.", "583920"),
        ("Your Ticketmaster code is 482915. Do not share it.", "482915"),
        ("Your one-time PIN is 8844", "8844"),
        # German
        ("Ihr Bestätigungscode lautet: 482913", "482913"),
        ("Dein Sicherheitscode: 7391. Gib ihn niemandem weiter.", "7391"),
        ("Dein Einmalcode lautet 5521.", "5521"),
        ("Ihr Code für die Anmeldung: 123 456", "123456"),
        ("mTAN: 12345678 für Überweisung von CHF 250.00", "12345678"),
        ("Ihre SMS-TAN lautet 443355", "443355"),
        ("Your codes: 998877 (valid 5 minutes)", "998877"),
        (
            "Ihre mTAN für die Überweisung über 1.250,00 EUR auf Konto "
            "DE89370400440532013000 lautet: 482915",
            "482915",
        ),
        ("Ihre Bestätigungsnummer lautet 482915", "482915"),
        ("Zur Bestätigung geben Sie bitte 482915 ein.", "482915"),
        ("Ihr Einmal-PIN: 4711", "4711"),
        ("Ihr Bestätigungscode für Bestellung 12345678 lautet 654321", "654321"),
        # French
        ("Votre code de vérification est 834211.", "834211"),
        ("Code de sécurité : 7788. Ne le partagez pas.", "7788"),
        # Italian
        ("Il tuo codice di verifica è 552190", "552190"),
        ("Codice OTP: 44556677", "44556677"),
        # Spanish, Dutch
        ("Tu código de verificación es 339812", "339812"),
        ("Uw verificatiecode is 481516", "481516"),
        # Normalization: full-width digits and a no-break space.
        ("\uff11\uff12\uff13\uff14\uff15\uff16 ist Ihr Code", "123456"),
        ("Code:\u00a0987654", "987654"),
        # En dash separator.
        ("Your code is 123\u2013456", "123456"),
    ],
)
def test_detects_codes(body: str, expected: str) -> None:
    assert extract_otp(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "PIN: 8844",
        "Your wifi password is 48291573",
        "Your PIN is 4821",
        "The door code is 4821",
        "Please confirm you got my message. I paid 4500 for the code review course",
        "Log in to see your 2500 new messages. Use the code from the email",
        "Enter 5000 steps to verify your fitness goal",
        # No keyword at all.
        "123456",
        "Meet me at gate 12, it's 4711 steps away",
        "Happy birthday! See you in 2027",
        # Keyword without a code.
        "code",
        "Send me the verification code please",
        # Amounts.
        "Total: CHF 1234.50, code sent",
        "I paid 1500 EUR for the code course",
        "Your code saves you $2500 today",
        "Code gilt für 1200 Punkte",
        # Dates and times.
        "The code review is at 2026-09-28",
        "Meeting at 14:30 on 28.09.2026, code room 1234",
        "Code freeze 28/09/2026",
        # Phone numbers.
        "Call me at 079 123 45 67 about the code",
        "Call +41791234567 for your code",
        "Code questions? Ring 0800 555 1234",
        # Tracking, order, invoice, customer numbers.
        "Your order 12345678 has shipped. Tracking code will follow.",
        "Your package 1Z999AA10123456784 code",
        "Your Bestellnummer 98765432 — Bestätigungscode folgt",
        "Rechnung Nr. 20261234: Code folgt",
        "Kundennummer 5566778 — Ihr Code folgt separat",
        "Order #55443322, code pending",
        # Promotions and other non-OTP codes.
        "Use promo code SAVE20 for 20% off",
        "Gutscheincode 12345678 einlösen",
        "Hey, the zip code is 8004",
        "We fixed error code 5001 today",
        "Your booking reference is ABC123, flight code LX1234",
        "Use code SUMMER2026 at checkout",
        "Our code is on page 1234 of the book",
        # Context words alone never bind a number (review of #196).
        "Verify your email to get 5000 bonus points",
        "one-time offer: iPhone 15 for 1299",
        "Einmalzahlung von 1500 erhalten",
        # A plain "code" without a connector, OTP context, or "is your".
        "Use code 482915 at checkout for 10% off your next order",
        "I'll send the code at 1830",
        "Kannst du mir 2500 leihen? Code schick ich dir spaeter",
        "password changed on 20261007",
        "Your order number is 482915. Use code SAVE20 for 20% off",
        # Promotions and bookings need an OTP-specific noun.
        "Verify your account and use code 4821 for 10% off",
        "Your Amazon order code is 551234",
        "Your booking confirmation code: 482915",
        "Your confirmation code is ABC123 for flight LX1234",
        "Ihre Bestätigungsnummer für die Reservierung lautet 482915",
        # Long-range binding stops at the end of a sentence.
        "Ihre mTAN wurde gesendet. Die Überweisung über 1500 EUR an Konto 4711 lautet: 8899",
        # A separator other than a letter glues digit runs together.
        "Code: 12\u00b73456",
        # "tan" as an ordinary word, not a bank TAN.
        "Es tan barato: 1500 pesos",
        "I got a tan, 2024",
        "Tan 2024 was a great year",
        # URLs.
        "see https://example.com/code/123456",
        # Too long to be an OTP message.
        "code 123456 " + "x" * MAX_OTP_MESSAGE_CHARS,
        # Empty input.
        "",
    ],
)
def test_rejects_false_positives(body: str) -> None:
    assert extract_otp(body) is None


def test_dates_need_the_keyword_right_before_them() -> None:
    assert extract_otp("Your code: 20261007") == "20261007"
    assert extract_otp("Your verification code was reset on 20261007") is None


def test_none_body_is_ignored() -> None:
    assert extract_otp(None) is None


def test_alphanumeric_code_needs_an_otp_specific_keyword() -> None:
    assert extract_otp("Your code is K7X2PQ") is None
    assert extract_otp("Your security code is K7X2PQ") == "K7X2PQ"


def test_year_like_code_must_follow_the_keyword_closely() -> None:
    assert extract_otp("Your code: 2024") == "2024"
    assert extract_otp("We met in 2019 and I still remember the code") is None


def test_the_code_closest_to_the_keyword_wins() -> None:
    body = "Hi Anna, flat 4021. Your verification code is 665544."
    assert extract_otp(body) == "665544"


_NON_KEYWORD_WORDS = [
    "hello", "meet", "at", "the", "station", "tomorrow", "hallo", "morgen",
    "bahnhof", "bonjour", "demain", "ciao", "domani", "see", "you", "on",
    "paid", "for", "dinner", "flat", "gate", "number", "is", "und", "et",
]
_NUMBER = st.integers(min_value=0, max_value=10**12).map(str)
_TOKEN = st.one_of(st.sampled_from(_NON_KEYWORD_WORDS), _NUMBER)
_SEPARATOR = st.sampled_from([" ", ", ", ". ", ": ", " - ", "\n", "/", "-"])


@PROPERTY_SETTINGS
@given(st.lists(st.tuples(_TOKEN, _SEPARATOR), max_size=30))
def test_numbers_without_keywords_never_match(parts) -> None:
    body = "".join(token + separator for token, separator in parts)
    assert extract_otp(body) is None


@PROPERTY_SETTINGS
@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=400))
def test_arbitrary_text_returns_a_plausible_code_or_nothing(body: str) -> None:
    result = extract_otp(body)
    if result is None:
        return
    assert re.fullmatch(r"[0-9]{4,8}|[A-Z0-9]{4,8}", result)


@PROPERTY_SETTINGS
@given(
    st.sampled_from(["code", "Code", "Bestätigungscode", "codice", "OTP"]),
    st.integers(min_value=0, max_value=99_999_999),
)
def test_a_keyword_next_to_a_number_is_well_formed(keyword: str, number: int) -> None:
    value = str(number)
    result = extract_otp(f"Your {keyword}: {value}")
    if len(value) < 4:
        assert result is None
    elif not (len(value) == 4 and 1900 <= number <= 2099):
        assert result == value


def test_hostile_email_like_input_stays_linear() -> None:
    body = "code " + "@.@" * 331
    assert len(body) <= MAX_OTP_MESSAGE_CHARS
    started = time.perf_counter()
    extract_otp(body)
    # The old pattern took about 1.6 s here; linear matching takes well under
    # a millisecond, so 0.5 s leaves ample room on a loaded machine.
    assert time.perf_counter() - started < 0.5


def test_non_ascii_script_digits_are_not_codes() -> None:
    # Arabic-Indic digits: verification fields expect ASCII digits.
    assert extract_otp("Your code: \u0664\u0668\u0662\u0669\u0661\u0663") is None


@pytest.mark.parametrize("unit", ["mTAN 4821 ", "code 4821 ", "Einmal-PIN 4821 "])
def test_many_keyword_and_number_pairs_stay_fast(unit: str) -> None:
    body = (unit * (MAX_OTP_MESSAGE_CHARS // len(unit)))[:MAX_OTP_MESSAGE_CHARS]
    started = time.perf_counter()
    extract_otp(body)
    # About 20 ms here; the budget leaves room for a loaded machine.
    assert time.perf_counter() - started < 0.5
