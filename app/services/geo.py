"""Lead timezones: from a timezone column when given, otherwise from the country (and state or city).

Countries with one timezone map straight to it. For big countries with several (USA, Canada, Australia,
Brazil, Mexico, Russia, Indonesia) a State/Province/Region or City column picks the zone; without one the
country's main business zone is used (USA → New York, Canada → Toronto, Australia → Sydney…).
Leads whose country we can't place get no timezone, and campaigns use their own.
"""

from functools import lru_cache
from zoneinfo import available_timezones

# Extra fields (as stored in Lead.custom) that hold a state or a city.
REGION_KEYS = ("state", "province", "region", "state_province", "state_or_province", "state_region")
CITY_KEYS = ("city", "town")


def _norm(value: str | None) -> str:
    return " ".join((value or "").strip().lower().replace(".", "").split())


# --- Countries with one timezone (or one that nearly everyone uses) -----------------------------------
# Keys are country names, common short forms and ISO 3166 alpha-2 codes.
_COUNTRIES: list[tuple[tuple[str, ...], str]] = [
    (("united arab emirates", "uae", "ae", "emirates"), "Asia/Dubai"),
    (("pakistan", "pk"), "Asia/Karachi"),
    (
        (
            "united kingdom",
            "uk",
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
    (("china", "cn", "prc"), "Asia/Shanghai"),
    (("hong kong", "hk"), "Asia/Hong_Kong"),
    (("japan", "jp"), "Asia/Tokyo"),
    (("south korea", "korea", "republic of korea", "kr"), "Asia/Seoul"),
    (("philippines", "ph"), "Asia/Manila"),
    (("ireland", "ie"), "Europe/Dublin"),
    (("germany", "de", "deutschland"), "Europe/Berlin"),
    (("france", "fr"), "Europe/Paris"),
    (("netherlands", "the netherlands", "holland", "nl"), "Europe/Amsterdam"),
    (("belgium", "be"), "Europe/Brussels"),
    (("switzerland", "ch"), "Europe/Zurich"),
    (("austria", "at"), "Europe/Vienna"),
    (("italy", "it"), "Europe/Rome"),
    (("spain", "es"), "Europe/Madrid"),
    (("portugal", "pt"), "Europe/Lisbon"),
    (("sweden", "se"), "Europe/Stockholm"),
    (("norway", "no"), "Europe/Oslo"),
    (("denmark", "dk"), "Europe/Copenhagen"),
    (("finland", "fi"), "Europe/Helsinki"),
    (("iceland", "is"), "Atlantic/Reykjavik"),
    (("poland", "pl"), "Europe/Warsaw"),
    (("czech republic", "czechia", "cz"), "Europe/Prague"),
    (("slovakia", "sk"), "Europe/Bratislava"),
    (("hungary", "hu"), "Europe/Budapest"),
    (("romania", "ro"), "Europe/Bucharest"),
    (("bulgaria", "bg"), "Europe/Sofia"),
    (("greece", "gr"), "Europe/Athens"),
    (("cyprus", "cy"), "Asia/Nicosia"),
    (("malta", "mt"), "Europe/Malta"),
    (("croatia", "hr"), "Europe/Zagreb"),
    (("slovenia", "si"), "Europe/Ljubljana"),
    (("serbia", "rs", "kosovo", "xk"), "Europe/Belgrade"),
    (("bosnia and herzegovina", "bosnia", "ba"), "Europe/Sarajevo"),
    (("montenegro", "me"), "Europe/Podgorica"),
    (("north macedonia", "macedonia", "mk"), "Europe/Skopje"),
    (("albania", "al"), "Europe/Tirane"),
    (("moldova", "md"), "Europe/Chisinau"),
    (("ukraine", "ua"), "Europe/Kyiv"),
    (("belarus", "by"), "Europe/Minsk"),
    (("estonia", "ee"), "Europe/Tallinn"),
    (("latvia", "lv"), "Europe/Riga"),
    (("lithuania", "lt"), "Europe/Vilnius"),
    (("luxembourg", "lu"), "Europe/Luxembourg"),
    (("israel", "il"), "Asia/Jerusalem"),
    (("iran", "ir"), "Asia/Tehran"),
    (("iraq", "iq"), "Asia/Baghdad"),
    (("syria", "sy"), "Asia/Damascus"),
    (("yemen", "ye"), "Asia/Aden"),
    (("afghanistan", "af"), "Asia/Kabul"),
    (("nepal", "np"), "Asia/Kathmandu"),
    (("bhutan", "bt"), "Asia/Thimphu"),
    (("maldives", "mv"), "Indian/Maldives"),
    (("myanmar", "burma", "mm"), "Asia/Yangon"),
    (("thailand", "th"), "Asia/Bangkok"),
    (("vietnam", "viet nam", "vn"), "Asia/Ho_Chi_Minh"),
    (("cambodia", "kh"), "Asia/Phnom_Penh"),
    (("laos", "la"), "Asia/Vientiane"),
    (("taiwan", "tw"), "Asia/Taipei"),
    (("macau", "macao", "mo"), "Asia/Macau"),
    (("brunei", "bn"), "Asia/Brunei"),
    (("kazakhstan", "kz"), "Asia/Almaty"),
    (("uzbekistan", "uz"), "Asia/Tashkent"),
    (("kyrgyzstan", "kg"), "Asia/Bishkek"),
    (("tajikistan", "tj"), "Asia/Dushanbe"),
    (("turkmenistan", "tm"), "Asia/Ashgabat"),
    (("azerbaijan", "az"), "Asia/Baku"),
    (("armenia", "am"), "Asia/Yerevan"),
    (("georgia", "ge"), "Asia/Tbilisi"),
    (("morocco", "ma"), "Africa/Casablanca"),
    (("algeria", "dz"), "Africa/Algiers"),
    (("tunisia", "tn"), "Africa/Tunis"),
    (("libya", "ly"), "Africa/Tripoli"),
    (("sudan", "sd"), "Africa/Khartoum"),
    (("ethiopia", "et"), "Africa/Addis_Ababa"),
    (("south africa", "za"), "Africa/Johannesburg"),
    (("nigeria", "ng"), "Africa/Lagos"),
    (("ghana", "gh"), "Africa/Accra"),
    (("kenya", "ke"), "Africa/Nairobi"),
    (("tanzania", "tz"), "Africa/Dar_es_Salaam"),
    (("uganda", "ug"), "Africa/Kampala"),
    (("rwanda", "rw"), "Africa/Kigali"),
    (("zambia", "zm"), "Africa/Lusaka"),
    (("zimbabwe", "zw"), "Africa/Harare"),
    (("senegal", "sn"), "Africa/Dakar"),
    (("ivory coast", "cote d'ivoire", "côte d'ivoire", "ci"), "Africa/Abidjan"),
    (("cameroon", "cm"), "Africa/Douala"),
    (("mauritius", "mu"), "Indian/Mauritius"),
    (("new zealand", "nz"), "Pacific/Auckland"),
    (("argentina", "ar"), "America/Argentina/Buenos_Aires"),
    (("chile", "cl"), "America/Santiago"),
    (("colombia", "co"), "America/Bogota"),
    (("peru", "pe"), "America/Lima"),
    (("ecuador", "ec"), "America/Guayaquil"),
    (("venezuela", "ve"), "America/Caracas"),
    (("bolivia", "bo"), "America/La_Paz"),
    (("paraguay", "py"), "America/Asuncion"),
    (("uruguay", "uy"), "America/Montevideo"),
    (("panama", "pa"), "America/Panama"),
    (("costa rica", "cr"), "America/Costa_Rica"),
    (("guatemala", "gt"), "America/Guatemala"),
    (("honduras", "hn"), "America/Tegucigalpa"),
    (("el salvador", "sv"), "America/El_Salvador"),
    (("nicaragua", "ni"), "America/Managua"),
    (("dominican republic", "do"), "America/Santo_Domingo"),
    (("puerto rico", "pr"), "America/Puerto_Rico"),
    (("jamaica", "jm"), "America/Jamaica"),
    (("trinidad and tobago", "trinidad", "tt"), "America/Port_of_Spain"),
    (("cuba", "cu"), "America/Havana"),
]
SINGLE_ZONE: dict[str, str] = {_norm(name): zone for names, zone in _COUNTRIES for name in names}


# --- Countries with several timezones: a main zone, and the zone of each state/province and big city --
def _table(rows: list[tuple[tuple[str, ...], str]]) -> dict[str, str]:
    return {_norm(name): zone for names, zone in rows for name in names}


_US_STATES = _table(
    [
        (
            (
                "connecticut",
                "ct",
                "delaware",
                "de",
                "district of columbia",
                "dc",
                "washington dc",
                "florida",
                "fl",
                "georgia",
                "ga",
                "indiana",
                "in",
                "kentucky",
                "ky",
                "maine",
                "me",
                "maryland",
                "md",
                "massachusetts",
                "ma",
                "michigan",
                "mi",
                "new hampshire",
                "nh",
                "new jersey",
                "nj",
                "new york",
                "ny",
                "north carolina",
                "nc",
                "ohio",
                "oh",
                "pennsylvania",
                "pa",
                "rhode island",
                "ri",
                "south carolina",
                "sc",
                "vermont",
                "vt",
                "virginia",
                "va",
                "west virginia",
                "wv",
            ),
            "America/New_York",
        ),
        (
            (
                "alabama",
                "al",
                "arkansas",
                "ar",
                "illinois",
                "il",
                "iowa",
                "ia",
                "kansas",
                "ks",
                "louisiana",
                "la",
                "minnesota",
                "mn",
                "mississippi",
                "ms",
                "missouri",
                "mo",
                "nebraska",
                "ne",
                "north dakota",
                "nd",
                "oklahoma",
                "ok",
                "south dakota",
                "sd",
                "tennessee",
                "tn",
                "texas",
                "tx",
                "wisconsin",
                "wi",
            ),
            "America/Chicago",
        ),
        (
            (
                "colorado",
                "co",
                "montana",
                "mt",
                "new mexico",
                "nm",
                "utah",
                "ut",
                "wyoming",
                "wy",
                "idaho",
                "id",
            ),
            "America/Denver",
        ),
        (("arizona", "az"), "America/Phoenix"),
        (("california", "ca", "nevada", "nv", "oregon", "or", "washington", "wa"), "America/Los_Angeles"),
        (("alaska", "ak"), "America/Anchorage"),
        (("hawaii", "hi"), "Pacific/Honolulu"),
        (("puerto rico", "pr"), "America/Puerto_Rico"),
    ]
)
_US_CITIES = _table(
    [
        (
            (
                "new york city",
                "nyc",
                "manhattan",
                "brooklyn",
                "boston",
                "philadelphia",
                "miami",
                "atlanta",
                "washington dc",
                "charlotte",
                "orlando",
                "tampa",
                "pittsburgh",
                "baltimore",
                "raleigh",
                "detroit",
                "columbus",
                "cleveland",
                "jacksonville",
                "indianapolis",
                "newark",
                "jersey city",
            ),
            "America/New_York",
        ),
        (
            (
                "chicago",
                "houston",
                "dallas",
                "austin",
                "san antonio",
                "fort worth",
                "nashville",
                "minneapolis",
                "kansas city",
                "st louis",
                "saint louis",
                "new orleans",
                "milwaukee",
                "oklahoma city",
                "memphis",
                "omaha",
            ),
            "America/Chicago",
        ),
        (("denver", "salt lake city", "boise", "albuquerque", "boulder"), "America/Denver"),
        (("phoenix", "scottsdale", "tucson", "tempe", "mesa"), "America/Phoenix"),
        (
            (
                "los angeles",
                "la",
                "san francisco",
                "sf",
                "san jose",
                "san diego",
                "seattle",
                "portland",
                "las vegas",
                "sacramento",
                "oakland",
                "palo alto",
                "mountain view",
                "sunnyvale",
                "santa clara",
                "irvine",
                "bellevue",
                "redmond",
                "menlo park",
                "cupertino",
            ),
            "America/Los_Angeles",
        ),
        (("anchorage",), "America/Anchorage"),
        (("honolulu",), "Pacific/Honolulu"),
    ]
)
_CA_PROVINCES = _table(
    [
        (("british columbia", "bc"), "America/Vancouver"),
        (("alberta", "ab"), "America/Edmonton"),
        (("saskatchewan", "sk"), "America/Regina"),
        (("manitoba", "mb"), "America/Winnipeg"),
        (("ontario", "on", "quebec", "québec", "qc"), "America/Toronto"),
        (("new brunswick", "nb"), "America/Moncton"),
        (("nova scotia", "ns", "prince edward island", "pe", "pei"), "America/Halifax"),
        (("newfoundland and labrador", "newfoundland", "nl"), "America/St_Johns"),
        (("yukon", "yt"), "America/Whitehorse"),
        (("northwest territories", "nt"), "America/Edmonton"),
        (("nunavut", "nu"), "America/Iqaluit"),
    ]
)
_CA_CITIES = _table(
    [
        (("vancouver", "victoria", "surrey", "burnaby"), "America/Vancouver"),
        (("calgary", "edmonton"), "America/Edmonton"),
        (("regina", "saskatoon"), "America/Regina"),
        (("winnipeg",), "America/Winnipeg"),
        (
            (
                "toronto",
                "ottawa",
                "montreal",
                "montréal",
                "mississauga",
                "quebec city",
                "waterloo",
                "hamilton",
            ),
            "America/Toronto",
        ),
        (("halifax",), "America/Halifax"),
        (("st john's", "st johns"), "America/St_Johns"),
    ]
)
_AU_STATES = _table(
    [
        (("new south wales", "nsw", "australian capital territory", "act"), "Australia/Sydney"),
        (("victoria", "vic"), "Australia/Melbourne"),
        (("queensland", "qld"), "Australia/Brisbane"),
        (("south australia", "sa"), "Australia/Adelaide"),
        (("western australia", "wa"), "Australia/Perth"),
        (("tasmania", "tas"), "Australia/Hobart"),
        (("northern territory", "nt"), "Australia/Darwin"),
    ]
)
_AU_CITIES = _table(
    [
        (("sydney", "canberra", "newcastle"), "Australia/Sydney"),
        (("melbourne", "geelong"), "Australia/Melbourne"),
        (("brisbane", "gold coast", "sunshine coast"), "Australia/Brisbane"),
        (("adelaide",), "Australia/Adelaide"),
        (("perth",), "Australia/Perth"),
        (("hobart",), "Australia/Hobart"),
        (("darwin",), "Australia/Darwin"),
    ]
)
_BR_STATES = _table(
    [
        (("amazonas", "am"), "America/Manaus"),
        (("acre", "ac"), "America/Rio_Branco"),
        (("mato grosso", "mt"), "America/Cuiaba"),
        (("mato grosso do sul", "ms"), "America/Campo_Grande"),
        (("rondonia", "rondônia", "ro"), "America/Porto_Velho"),
        (("roraima", "rr"), "America/Boa_Vista"),
    ]
)
_BR_CITIES = _table([(("manaus",), "America/Manaus")])
_MX_STATES = _table(
    [
        (("baja california", "bc"), "America/Tijuana"),
        (("sonora", "son"), "America/Hermosillo"),
        (("chihuahua", "chih"), "America/Chihuahua"),
        (("sinaloa", "sin", "nayarit", "nay", "baja california sur", "bcs"), "America/Mazatlan"),
        (("quintana roo", "q roo", "qroo"), "America/Cancun"),
    ]
)
_MX_CITIES = _table(
    [
        (("tijuana", "mexicali"), "America/Tijuana"),
        (("hermosillo",), "America/Hermosillo"),
        (("cancun", "cancún", "playa del carmen", "tulum"), "America/Cancun"),
        (("mazatlan", "mazatlán", "culiacan", "culiacán", "la paz", "los cabos"), "America/Mazatlan"),
    ]
)
_RU_CITIES = _table(
    [
        (("kaliningrad",), "Europe/Kaliningrad"),
        (("yekaterinburg", "ekaterinburg", "chelyabinsk", "perm", "tyumen"), "Asia/Yekaterinburg"),
        (("omsk",), "Asia/Omsk"),
        (("novosibirsk", "tomsk"), "Asia/Novosibirsk"),
        (("krasnoyarsk",), "Asia/Krasnoyarsk"),
        (("irkutsk",), "Asia/Irkutsk"),
        (("vladivostok", "khabarovsk"), "Asia/Vladivostok"),
    ]
)
_ID_REGIONS = _table(
    [
        (("bali", "south sulawesi", "north sulawesi", "east kalimantan", "nusa tenggara"), "Asia/Makassar"),
        (("papua", "west papua", "maluku"), "Asia/Jayapura"),
    ]
)
_ID_CITIES = _table(
    [
        (("denpasar", "makassar", "balikpapan", "manado"), "Asia/Makassar"),
        (("jayapura", "ambon"), "Asia/Jayapura"),
    ]
)

# Country names/codes → (main zone, zones by state or province, zones by city).
_MULTI: list[tuple[tuple[str, ...], str, dict[str, str], dict[str, str]]] = [
    (
        ("united states", "united states of america", "usa", "us", "u s", "u s a", "america"),
        "America/New_York",
        _US_STATES,
        _US_CITIES,
    ),
    (("canada", "ca"), "America/Toronto", _CA_PROVINCES, _CA_CITIES),
    (("australia", "au"), "Australia/Sydney", _AU_STATES, _AU_CITIES),
    (("brazil", "brasil", "br"), "America/Sao_Paulo", _BR_STATES, _BR_CITIES),
    (("mexico", "méxico", "mx"), "America/Mexico_City", _MX_STATES, _MX_CITIES),
    (("russia", "russian federation", "ru"), "Europe/Moscow", {}, _RU_CITIES),
    (("indonesia", "id"), "Asia/Jakarta", _ID_REGIONS, _ID_CITIES),
]
MULTI_ZONE: dict[str, tuple[str, dict[str, str], dict[str, str]]] = {
    _norm(name): (main, regions, cities) for names, main, regions, cities in _MULTI for name in names
}


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


def region_and_city(custom: dict | None) -> tuple[str | None, str | None]:
    """The state/province and city from a lead's extra fields, if the import had such columns."""
    custom = custom or {}
    region = next((str(custom[k]) for k in REGION_KEYS if custom.get(k)), None)
    city = next((str(custom[k]) for k in CITY_KEYS if custom.get(k)), None)
    return region, city


def timezone_for(
    timezone: str | None, country: str | None, region: str | None = None, city: str | None = None
) -> str | None:
    """The lead's timezone: the given one if valid, else from the country (with state or city for big
    countries), else None."""
    found = valid_timezone(timezone)
    if found or not country:
        return found
    key = _norm(country)
    if key in SINGLE_ZONE:
        return SINGLE_ZONE[key]
    if key in MULTI_ZONE:
        main, regions, cities = MULTI_ZONE[key]
        return regions.get(_norm(region)) or cities.get(_norm(city)) or main
    return None


# "UK" is a common short form, but GB is the ISO 3166 code.
_NOT_ISO = {"uk"}


@lru_cache
def _country_codes() -> dict[str, str]:
    """Every known name of a country → its ISO 3166-1 alpha-2 code (taken from the two-letter name in its
    row)."""
    codes: dict[str, str] = {}
    groups = [names for names, _ in _COUNTRIES] + [names for names, *_ in _MULTI]
    for names in groups:
        two = [n for n in names if len(n) == 2 and n.isalpha() and n not in _NOT_ISO]
        if len(two) == 1:
            for name in names:
                codes[_norm(name)] = two[0].upper()
    return codes


def country_code(country: str | None) -> str:
    """ISO 3166-1 alpha-2 ("GB") for a country written any known way ("United Kingdom", "UK", "gb"), or ""."""
    return _country_codes().get(_norm(country), "") if country else ""
