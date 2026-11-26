"""The reply score (Deals integration step 3): 0 to 100 from plain word rules that admins edit.

Company rule: no AI, LLM or machine learning may read, classify or score replies, or touch anything that
becomes a lead (the company's Meta ad account was restricted once over AI on leads). This module is pure: no
database, no network, the same reply always gets the same score.

1. Keep only the new words (`extract_new_text`): quoted lines, everything from a reply header ("On … wrote:",
   "-----Original Message-----", "From: … Sent:", and the Spanish, French and German forms) and the signature
   are removed, so words from our own email ("call", "meeting") don't count. Someone who answers *below* the
   quoted email (bottom-posting) leaves nothing above it: then the words after the quote count, with our own
   email taken off (lines starting with ">", or the copy of what we sent, found word by word).
2. Lower-case and collapse whitespace.
3. Each rule that matches counts once: whole words or phrases, an admin's regex (with a time limit), or a
   built-in signal (question_mark, phone_or_time, company_domain, colleague_cc).
4. Add the points and keep the total between 0 and 100. A matching hard-stop rule makes it 0.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import regex

# A rule's regex gets this long per reply; longer counts as no match (and such a pattern is refused on save).
REGEX_TIMEOUT = 0.05
SIGNALS = ("question_mark", "phone_or_time", "company_domain", "colleague_cc", "machine")
HARD_STOPS = ("no", "machine")


@dataclass(frozen=True)
class Rule:
    name: str
    kind: str  # words | regex | signal
    points: int
    words: tuple[str, ...] = ()
    pattern: str | None = None
    signal: str | None = None
    hard_stop: str | None = None  # "no" | "machine"


@dataclass
class ScoreResult:
    value: int
    matched: list[str] = field(default_factory=list)
    hard_stop: str | None = None


# --- Keeping only the new words -----------------------------------------------------------------------------

# A line that starts the quoted earlier email. Gmail wraps its "On <date>, <name> <address>" line, so the
# closing word may be on the next line.
_REPLY_HEADER = regex.compile(
    r"^\s*(?:"
    r"on\s.{0,250}?(?:\n.{0,160}?)?\bwrote\s*:"  # Gmail, Apple Mail (EN)
    r"|el\s.{0,250}?(?:\n.{0,160}?)?\bescribió\s*:"  # ES
    r"|le\s.{0,250}?(?:\n.{0,160}?)?\ba\s+écrit\s*:"  # FR
    r"|am\s.{0,250}?(?:\n.{0,160}?)?\bschrieb\b.{0,160}?:"  # DE
    r"|-{2,}\s*(?:original message|mensaje original|message d'origine|ursprüngliche nachricht)\s*-{2,}"
    r"|_{10,}"  # Outlook's rule above the quoted header
    r")",
    regex.IGNORECASE | regex.MULTILINE,
)
# Outlook: "From: …" followed within two lines by "Sent:" / "Date:" (and the same in other languages).
_OUTLOOK_HEADER = regex.compile(
    r"^\s*(?:from|de|von|da)\s*:.*\n(?:.*\n)?\s*(?:sent|date|enviado|fecha|envoyé|gesendet|datum|inviato)\s*:",
    regex.IGNORECASE | regex.MULTILINE,
)
_SIGN_OFF = regex.compile(
    r"^\s*(?:best(?: regards| wishes)?|kind regards|warm regards|regards|many thanks|thanks|thank you|cheers"
    r"|sincerely|yours(?: sincerely| truly)?|br|saludos|un saludo|cordialement|bien à vous"
    r"|mit freundlichen grüßen|viele grüße|liebe grüße|grüße)\s*[,.!]?\s*$",
    regex.IGNORECASE,
)


_WORD = regex.compile(r"\w+", regex.UNICODE)
# Share of an email we sent that must be found, in order, for a stretch of the reply to count as its copy.
QUOTE_SHARE = 0.6


def _quote_header(text: str) -> regex.Match | None:
    """Where the quoted earlier email starts (the earliest reply header), or None."""
    found = [m for m in (p.search(text) for p in (_REPLY_HEADER, _OUTLOOK_HEADER)) if m]
    return min(found, key=lambda m: m.start()) if found else None


def _without_quoted_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if not line.lstrip().startswith(">")]


def _without_signature(lines: list[str]) -> str:
    # A signature after the standard "-- " line.
    for i, line in enumerate(lines):
        if line.rstrip() == "--":
            lines = lines[:i]
            break
    # A sign-off ("Best regards,", "Thanks,") followed only by a few short lines (name, title, phone).
    for i, line in enumerate(lines):
        rest = [x for x in lines[i + 1 :] if x.strip()]
        if _SIGN_OFF.match(line) and len(rest) <= 6 and all(len(x.strip()) <= 60 for x in rest):
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def after_quoted_copy(text: str, sent: Sequence[str]) -> str:
    """`text` without a leading copy of one of the emails we `sent` (the quote in a reply whose quoted lines
    aren't marked with ">", e.g. an HTML reply turned into text). Matching is word by word and in order, so
    re-wrapped lines and a missing word or two don't matter. Unchanged when no copy is found."""
    words = list(_WORD.finditer(text))
    lower = [w.group().lower() for w in words]
    cut = 0
    for body in sent:
        ours = [w.lower() for w in _WORD.findall(body or "")]
        if len(ours) < 5:
            continue
        blocks = [
            b for b in SequenceMatcher(None, ours, lower, autojunk=False).get_matching_blocks() if b.size >= 3
        ]
        if not blocks or sum(b.size for b in blocks) < QUOTE_SHARE * len(ours):
            continue
        last = blocks[-1]
        cut = max(cut, words[last.b + last.size - 1].end())
    return text[cut:]


def extract_new_text(text: str, sent: Sequence[str] = ()) -> str:
    """What the person wrote in this reply, without quoted history or their signature. `sent`: the emails we
    sent them, to recognise an unmarked quote when they answered below it."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for _ in range(3):  # a quote inside a quote, each answered below
        found = _quote_header(text)
        new = _without_signature(_without_quoted_lines(text[: found.start()] if found else text))
        if new or found is None:
            return new
        # Nothing above the quoted email: they answered below it.
        text = after_quoted_copy("\n".join(_without_quoted_lines(text[found.end() :])), sent)
    return ""


def normalize(text: str) -> str:
    return " ".join(text.replace("’", "'").replace("‘", "'").lower().split())


# --- Matching -----------------------------------------------------------------------------------------------

_WEEKDAYS = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_TIME = r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s?(?:am|pm)\b|\b(?:[01]?\d|2[0-3]):[0-5]\d\b"
_PHONE = regex.compile(r"\+?\(?\d[\d\s().\-]{5,}\d")
_WEEKDAY_OR_TIME = regex.compile(rf"\b(?:{_WEEKDAYS})\b|{_TIME}", regex.IGNORECASE)


def _has_phone_or_time(text: str) -> bool:
    for found in _PHONE.finditer(text):
        if sum(c.isdigit() for c in found.group()) >= 7:
            return True
    return bool(_WEEKDAY_OR_TIME.search(text))


def _domain(address: str) -> str:
    return address.strip().lower().rsplit("@", 1)[-1] if "@" in address else ""


def _phrase(words: Iterable[str]) -> regex.Pattern | None:
    alternatives = [regex.escape(normalize(w)) for w in words if normalize(w)]
    if not alternatives:
        return None
    # Unicode-aware whole words: no letter or digit right before or after.
    either = "|".join(alternatives)
    return regex.compile(rf"(?<!\w)(?:{either})(?!\w)", regex.UNICODE)


def _signal(name: str | None, text: str, sender: str, cc: list[str], free_mail: set[str]) -> bool:
    sender_domain = _domain(sender)
    if name == "question_mark":
        return "?" in text
    if name == "phone_or_time":
        return _has_phone_or_time(text)
    if name == "company_domain":
        return bool(sender_domain) and sender_domain not in free_mail
    if name == "colleague_cc":
        if not sender_domain or sender_domain in free_mail:
            return False
        return any(_domain(a) == sender_domain and a.strip().lower() != sender.strip().lower() for a in cc)
    return False  # "machine" is decided by the auto-reply / bounce / warm-up checks, never by words


def matches(rule: Rule, text: str, sender: str, cc: list[str], free_mail: set[str]) -> bool:
    if rule.kind == "words":
        pattern = _phrase(rule.words)
        return bool(pattern and pattern.search(text))
    if rule.kind == "regex" and rule.pattern:
        try:
            return bool(regex.search(rule.pattern, text, regex.IGNORECASE, timeout=REGEX_TIMEOUT))
        except (regex.error, TimeoutError):
            return False
    if rule.kind == "signal":
        return _signal(rule.signal, text, sender, cc, free_mail)
    return False


def score_reply(
    text: str,
    sender: str,
    cc: list[str],
    rules: list[Rule],
    free_mail_domains: set[str],
    sent: Sequence[str] = (),
) -> ScoreResult:
    """The score of one reply. `text` is the whole plain-text reply; quoted history is removed here. `sent`:
    the emails we sent this person (see `extract_new_text`)."""
    cleaned = normalize(extract_new_text(text, sent))
    result = ScoreResult(value=0)
    total = 0
    for rule in rules:
        if not matches(rule, cleaned, sender, cc, free_mail_domains):
            continue
        result.matched.append(rule.name)
        if rule.hard_stop in HARD_STOPS and result.hard_stop is None:
            result.hard_stop = rule.hard_stop
        total += rule.points
    result.value = 0 if result.hard_stop else max(0, min(100, total))
    return result


def check_pattern(pattern: str) -> str | None:
    """Why an admin's regex can't be used (a problem in words), or None. Refuses patterns that are invalid or
    that take too long on an awkward text (catastrophic backtracking)."""
    try:
        compiled = regex.compile(pattern, regex.IGNORECASE)
    except regex.error as e:
        return f"Not a valid pattern: {e}."
    # Short runs that end in a character the pattern can't match: where backtracking blows up.
    bases = ("a" * 30, "aaaa " * 200, "x" * 40, "1" * 40, "ab" * 20, "a" * 3000)
    for probe in (base + tail for base in bases for tail in ("!", "\u2603")):
        try:
            compiled.search(probe, timeout=REGEX_TIMEOUT)
        except TimeoutError:
            return "This pattern can take too long on some replies. Make it simpler (avoid nested repeats)."
    return None


# Free-mail domains: a reply from one of these isn't from a company address. Admins can edit the list.
DEFAULT_FREE_MAIL = (
    "gmail.com googlemail.com outlook.com hotmail.com hotmail.co.uk hotmail.fr hotmail.de hotmail.it "
    "hotmail.es "
    "live.com live.co.uk live.fr msn.com yahoo.com yahoo.co.uk yahoo.fr yahoo.de yahoo.it yahoo.es yahoo.ca "
    "yahoo.com.au yahoo.co.in yahoo.co.jp ymail.com rocketmail.com aol.com aim.com icloud.com me.com mac.com "
    "proton.me protonmail.com pm.me zoho.com zohomail.com zoho.in yandex.com yandex.ru yandex.ua gmx.com "
    "gmx.de "
    "gmx.net gmx.at gmx.ch web.de mail.com email.com mail.ru inbox.ru list.ru bk.ru qq.com 163.com 126.com "
    "sina.com sohu.com rediffmail.com fastmail.com fastmail.fm hey.com tutanota.com tuta.io tutanota.de "
    "t-online.de orange.fr wanadoo.fr free.fr laposte.net sfr.fr libero.it virgilio.it tiscali.it alice.it "
    "btinternet.com sky.com virginmedia.com ntlworld.com talktalk.net comcast.net verizon.net att.net "
    "sbcglobal.net cox.net charter.net shaw.ca rogers.com sympatico.ca bigpond.com optusnet.com.au "
    "naver.com daum.net hanmail.net seznam.cz wp.pl o2.pl onet.pl interia.pl hushmail.com lycos.com "
    "inbox.com mailinator.com"
).split()
