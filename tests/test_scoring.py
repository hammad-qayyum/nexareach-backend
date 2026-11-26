"""The reply score (Deals integration step 3): pure word rules, table-driven."""

import pytest

from app.services import scoring
from app.services.scoring import Rule, extract_new_text, score_reply

FREE = set(scoring.DEFAULT_FREE_MAIL)
# The starting rules (as seeded by the migration).
RULES = [
    Rule(
        "Clear interest",
        "words",
        40,
        words=(
            "interested",
            "let's talk",
            "lets talk",
            "sounds good",
            "keen",
            "tell me more",
            "happy to chat",
        ),
    ),
    Rule(
        "Wants a meeting",
        "words",
        30,
        words=(
            "call",
            "meeting",
            "schedule",
            "book a",
            "calendar",
            "available on",
            "zoom",
            "teams",
            "google meet",
        ),
    ),
    Rule(
        "Asks about price",
        "words",
        30,
        words=("price", "pricing", "cost", "quote", "proposal", "budget", "rates"),
    ),
    Rule(
        "Asks for details",
        "words",
        20,
        words=("send details", "more information", "more info", "brochure", "case study", "portfolio"),
    ),
    Rule("Gives phone or time", "signal", 15, signal="phone_or_time"),
    Rule("Asks a question", "signal", 10, signal="question_mark"),
    Rule("Company email", "signal", 10, signal="company_domain"),
    Rule("Brings a colleague", "signal", 10, signal="colleague_cc"),
    Rule("Not now", "words", -30, words=("not now", "maybe later", "next quarter", "next year", "no budget")),
    Rule("Wrong person", "words", -40, words=("wrong person", "not my area", "no longer work")),
    Rule(
        "Hard stop: no",
        "words",
        0,
        words=(
            "not interested",
            "no thanks",
            "unsubscribe",
            "remove me",
            "stop emailing",
            "don't contact",
            "do not contact",
        ),
        hard_stop="no",
    ),
    Rule("Hard stop: machine", "signal", 0, signal="machine", hard_stop="machine"),
]

GMAIL = "jane@gmail.com"
ACME = "jane@acme.com"


@pytest.mark.parametrize(
    ("text", "sender", "cc", "score", "matched"),
    [
        # Each rule on its own (from a free-mail address, so "Company email" doesn't add up).
        ("I'm interested", GMAIL, [], 40, ["Clear interest"]),
        ("Lets talk", GMAIL, [], 40, ["Clear interest"]),
        ("Let’s talk", GMAIL, [], 40, ["Clear interest"]),
        ("Happy to chat", GMAIL, [], 40, ["Clear interest"]),
        ("Can we do a zoom", GMAIL, [], 30, ["Wants a meeting"]),
        ("Send me your calendar link", GMAIL, [], 30, ["Wants a meeting"]),
        ("What's the pricing", GMAIL, [], 30, ["Asks about price"]),
        ("Please send a quote", GMAIL, [], 30, ["Asks about price"]),
        ("Send a case study please", GMAIL, [], 20, ["Asks for details"]),
        ("Call me on +44 20 7946 0000", GMAIL, [], 45, ["Wants a meeting", "Gives phone or time"]),
        ("Tuesday works", GMAIL, [], 15, ["Gives phone or time"]),
        ("3pm works", GMAIL, [], 15, ["Gives phone or time"]),
        ("15:30 works", GMAIL, [], 15, ["Gives phone or time"]),
        ("3:30 pm works", GMAIL, [], 15, ["Gives phone or time"]),
        ("Who are you?", GMAIL, [], 10, ["Asks a question"]),
        ("ok", ACME, [], 10, ["Company email"]),
        ("ok", ACME, ["boss@acme.com"], 20, ["Company email", "Brings a colleague"]),
        # Counted once, however often the words appear.
        ("Interested, very interested, interested!", GMAIL, [], 40, ["Clear interest"]),
        # Several rules together.
        (
            "Interested. Can we have a call on Thursday?",
            ACME,
            [],
            100,
            ["Clear interest", "Wants a meeting", "Gives phone or time", "Asks a question", "Company email"],
        ),
        (
            "Sounds good, what's the price?",
            ACME,
            [],
            90,
            ["Clear interest", "Asks about price", "Asks a question", "Company email"],
        ),
        # Clamped to 0..100.
        (
            "Interested, let's book a call, send the pricing and a case study. Monday 3pm?",
            ACME,
            ["cto@acme.com"],
            100,
            [
                "Clear interest",
                "Wants a meeting",
                "Asks about price",
                "Asks for details",
                "Gives phone or time",
                "Asks a question",
                "Company email",
                "Brings a colleague",
            ],
        ),
        ("Wrong person, not now", GMAIL, [], 0, ["Not now", "Wrong person"]),
        ("Maybe later, sounds good", GMAIL, [], 10, ["Clear interest", "Not now"]),
        # Hard stops win over everything.
        ("Not interested", ACME, [], 0, ["Clear interest", "Company email", "Hard stop: no"]),
        (
            "No thanks, but call me?",
            ACME,
            [],
            0,
            ["Wants a meeting", "Asks a question", "Company email", "Hard stop: no"],
        ),
        ("Please remove me", GMAIL, [], 0, ["Hard stop: no"]),
        # Whole words only.
        ("Recall the details", GMAIL, [], 0, []),
        ("Costly mistake", GMAIL, [], 0, []),
        ("We are uninterested", GMAIL, [], 0, []),
        # Colleague: same company domain, not the sender, not free mail.
        ("ok", ACME, ["jane@acme.com"], 10, ["Company email"]),
        ("ok", ACME, ["someone@other.com"], 10, ["Company email"]),
        ("ok", GMAIL, ["friend@gmail.com"], 0, []),
        # Six digits isn't a phone number.
        ("Order 123456", GMAIL, [], 0, []),
        ("Ref 12-34-567", GMAIL, [], 15, ["Gives phone or time"]),
    ],
)
def test_rules(text, sender, cc, score, matched):
    result = score_reply(text, sender, cc, RULES, FREE)
    assert result.value == score
    assert result.matched == matched


@pytest.mark.parametrize(
    ("text", "new"),
    [
        (
            "Sounds good\n\nOn Tue, 24 Nov 2026 at 10:00, Sara <sara@x.com> wrote:\n> can we call?",
            "Sounds good",
        ),
        ("Yes\nOn Tue, 24 Nov 2026 at 10:00 Sara Khan\n<sara@x.com> wrote:\n> call", "Yes"),  # Gmail wraps it
        ("Yes\n\nOn 24 Nov 2026, at 10:00, Sara wrote:\n\n> call", "Yes"),  # Apple Mail
        ("Yes\n-----Original Message-----\nFrom: Sara\nSent: Tuesday\ncall", "Yes"),
        ("Yes\n\nFrom: Sara Khan <sara@x.com>\nSent: Tuesday, 24 November 2026\nSubject: call", "Yes"),
        ("Sí\n\nEl mar, 24 nov 2026 a las 10:00, Sara <s@x.com> escribió:\n> llamada", "Sí"),
        ("Oui\n\nLe mar. 24 nov. 2026 à 10:00, Sara <s@x.com> a écrit :\n> appel", "Oui"),
        ("Ja\n\nAm Di., 24. Nov. 2026 um 10:00 Uhr schrieb Sara <s@x.com>:\n> Anruf", "Ja"),
        ("Ja\n\nVon: Sara\nGesendet: Dienstag\nAnruf", "Ja"),
        ("Oui\n\nDe : Sara\nEnvoyé : mardi\nappel", "Oui"),
        ("Yes please\n> earlier line\nsee you then", "Yes please\nsee you then"),
        ("Interested\n-- \nJane Doe\nHead of IT\n+44 20 7946 0000", "Interested"),
        ("Interested\n\nBest regards,\nJane Doe\nHead of IT, Acme", "Interested"),
        ("Interested\n\nKind regards\nJane", "Interested"),
        ("Interested\n\nMit freundlichen Grüßen\nJane", "Interested"),
        ("Thanks, sounds good", "Thanks, sounds good"),  # a sign-off word inside a sentence stays
    ],
)
def test_only_the_new_words_count(text, new):
    assert extract_new_text(text) == new


def test_quoted_words_from_our_email_dont_raise_the_score():
    text = "Not for us.\n\nOn Tue, Ali wrote:\n> Can we book a call to discuss pricing?"
    assert score_reply(text, GMAIL, [], RULES, FREE).value == 0


def test_the_machine_hard_stop_is_never_matched_by_words():
    assert "Hard stop: machine" not in score_reply("auto-reply machine", GMAIL, [], RULES, FREE).matched


def test_an_admin_regex_rule_and_its_time_limit():
    rules = [
        Rule("Big budget", "regex", 30, pattern=r"budget of \$?\d{2,}k"),
        Rule("Slow", "regex", 50, pattern=r"(a+)+!"),
    ]
    assert score_reply("We have a budget of $50k", GMAIL, [], rules, FREE).matched == ["Big budget"]
    # The slow pattern gives up (no match) instead of hanging the reply check.
    assert score_reply("a" * 30 + "☃", GMAIL, [], rules, FREE).matched == []
    assert scoring.check_pattern(r"(a+)+!") is not None
    assert scoring.check_pattern(r"(oops") is not None
    assert scoring.check_pattern(r"budget of \d+") is None


def test_about_a_hundred_free_mail_domains():
    assert 90 <= len(scoring.DEFAULT_FREE_MAIL) <= 120
    assert {"gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me"} <= FREE
