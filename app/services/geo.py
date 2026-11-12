"""Lead timezones: from a timezone column when given, otherwise from the country.

Only countries with a single timezone are mapped. For the USA, Canada, Australia and the like the
country alone can't tell us, so the lead gets no timezone and campaigns use their own.
"""

from functools import lru_cache
from zoneinfo import available_timezones

# Keys are lowercased country names, common short forms and ISO 3166 alpha-2 codes.
SINGLE_ZONE: dict[str, str] = {}
_COUNTRIES = [
    (("united arab emirates", "uae", "u.a.e.", "ae", "emirates"), "Asia/Dubai"),
    (("pakistan", "pk"), "Asia/Karachi"),
    (
        (
            "united kingdom",
            "uk",
            "u.k.",
            "gb",
            "great britain",
            "britain",
            "england",
            "scotland",
            "wales",
            "northern ireland",
        ),
        "Europe/London",
    ),
    (("saudi arabia", "ksa", "sa", "kingdom of saudi arabia"), "Asia/Riyadh"),
    (("qatar", "qa"), "Asia/Qatar"),
    (("bahrain", "bh"), "Asia/Bahrain"),
    (("kuwait", "kw"), "Asia/Kuwait"),
    (("oman", "om"), "Asia/Muscat"),
    (("jordan", "jo"), "Asia/Amman"),
    (("lebanon", "lb"), "Asia/Beirut"),
    (("egypt", "eg"), "Africa/Cairo"),
    (("turkey", "türkiye", "turkiye", "tr"), "Europe/Istanbul"),
    (("india", "in"), "Asia/Kolkata"),
    (("bangladesh", "bd"), "Asia/Dhaka"),
    (("sri lanka", "lk"), "Asia/Colombo"),
    (("singapore", "sg"), "Asia/Singapore"),
    (("malaysia", "my"), "Asia/Kuala_Lumpur"),
    (("china", "cn"), "Asia/Shanghai"),
    (("hong kong", "hk"), "Asia/Hong_Kong"),
    (("japan", "jp"), "Asia/Tokyo"),
    (("south korea", "korea", "kr"), "Asia/Seoul"),
    (("philippines", "ph"), "Asia/Manila"),
    (("ireland", "ie"), "Europe/Dublin"),
    (("germany", "de"), "Europe/Berlin"),
    (("france", "fr"), "Europe/Paris"),
    (("netherlands", "the netherlands", "holland", "nl"), "Europe/Amsterdam"),
    (("belgium", "be"), "Europe/Brussels"),
    (("switzerland", "ch"), "Europe/Zurich"),
    (("austria", "at"), "Europe/Vienna"),
    (("italy", "it"), "Europe/Rome"),
    (("spain", "es"), "Europe/Madrid"),
    (("sweden", "se"), "Europe/Stockholm"),
    (("norway", "no"), "Europe/Oslo"),
    (("denmark", "dk"), "Europe/Copenhagen"),
    (("finland", "fi"), "Europe/Helsinki"),
    (("poland", "pl"), "Europe/Warsaw"),
    (("greece", "gr"), "Europe/Athens"),
    (("south africa", "za"), "Africa/Johannesburg"),
    (("nigeria", "ng"), "Africa/Lagos"),
    (("kenya", "ke"), "Africa/Nairobi"),
    (("new zealand", "nz"), "Pacific/Auckland"),
]
for names, zone in _COUNTRIES:
    for name in names:
        SINGLE_ZONE[name] = zone


@lru_cache
def _zones() -> frozenset[str]:
    return frozenset(available_timezones())


def valid_timezone(value: str | None) -> str | None:
    """The IANA name if `value` is one (case-insensitive), else None."""
    if not value:
        return None
    value = value.strip()
    if value in _zones():
        return value
    lowered = {z.lower(): z for z in _zones()}
    return lowered.get(value.lower())


def timezone_for(timezone: str | None, country: str | None) -> str | None:
    return valid_timezone(timezone) or (SINGLE_ZONE.get(country.strip().lower()) if country else None)
