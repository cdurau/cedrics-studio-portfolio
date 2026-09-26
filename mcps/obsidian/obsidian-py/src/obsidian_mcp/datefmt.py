"""Moment.js-compatible date formatting (as used by Obsidian) and template rendering."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_MONTHS = {
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
           "September", "Oktober", "November", "Dezember"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
}
_MONTHS_SHORT = {
    "de": ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}
# index 0 = Monday (Python weekday())
_DAYS = {
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
_DAYS_SHORT = {
    "de": ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
_DAYS_MIN = {
    "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
    "en": ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"],
}

_TOKEN = re.compile(
    r"\[[^\]]*\]|YYYY|YY|Q|MMMM|MMM|MM|M|DDDD|DDD|Do|DD|D|dddd|ddd|dd|d|E|e|"
    r"GGGG|gggg|WW|W|ww|w|HH|H|hh|h|kk|k|mm|m|ss|s|A|a|X|x|."
)


def _ordinal(n: int, locale: str) -> str:
    if locale == "de":
        return f"{n}."
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_moment(fmt: str, dt: datetime | date, locale: str = "de") -> str:
    """Format *dt* using a moment.js format string (subset used in practice)."""
    loc = locale if locale in _MONTHS else "en"
    if not isinstance(dt, datetime):
        dt = datetime(dt.year, dt.month, dt.day)
    iso_year, iso_week, iso_wd = dt.isocalendar()
    out: list[str] = []
    for tok in _TOKEN.findall(fmt):
        if tok.startswith("[") and tok.endswith("]"):
            out.append(tok[1:-1])
            continue
        match tok:
            case "YYYY": out.append(f"{dt.year:04d}")
            case "YY": out.append(f"{dt.year % 100:02d}")
            case "Q": out.append(str((dt.month - 1) // 3 + 1))
            case "MMMM": out.append(_MONTHS[loc][dt.month - 1])
            case "MMM": out.append(_MONTHS_SHORT[loc][dt.month - 1])
            case "MM": out.append(f"{dt.month:02d}")
            case "M": out.append(str(dt.month))
            case "DDDD": out.append(f"{dt.timetuple().tm_yday:03d}")
            case "DDD": out.append(str(dt.timetuple().tm_yday))
            case "Do": out.append(_ordinal(dt.day, loc))
            case "DD": out.append(f"{dt.day:02d}")
            case "D": out.append(str(dt.day))
            case "dddd": out.append(_DAYS[loc][dt.weekday()])
            case "ddd": out.append(_DAYS_SHORT[loc][dt.weekday()])
            case "dd": out.append(_DAYS_MIN[loc][dt.weekday()])
            case "d": out.append(str((dt.weekday() + 1) % 7))  # Sunday = 0
            case "E": out.append(str(iso_wd))
            case "e": out.append(str(dt.weekday() if loc == "de" else (dt.weekday() + 1) % 7))
            case "GGGG" | "gggg": out.append(f"{iso_year:04d}")
            case "WW" | "ww": out.append(f"{iso_week:02d}")
            case "W" | "w": out.append(str(iso_week))
            case "HH": out.append(f"{dt.hour:02d}")
            case "H": out.append(str(dt.hour))
            case "hh": out.append(f"{(dt.hour % 12) or 12:02d}")
            case "h": out.append(str((dt.hour % 12) or 12))
            case "kk": out.append(f"{dt.hour or 24:02d}")
            case "k": out.append(str(dt.hour or 24))
            case "mm": out.append(f"{dt.minute:02d}")
            case "m": out.append(str(dt.minute))
            case "ss": out.append(f"{dt.second:02d}")
            case "s": out.append(str(dt.second))
            case "A": out.append("AM" if dt.hour < 12 else "PM")
            case "a": out.append("am" if dt.hour < 12 else "pm")
            case "X": out.append(str(int(dt.timestamp())))
            case "x": out.append(str(int(dt.timestamp() * 1000)))
            case _: out.append(tok)
    return "".join(out)


def parse_date_arg(value: str | None, today: date | None = None) -> date:
    """Accept 'today', 'yesterday', 'tomorrow', 'heute', 'gestern', 'morgen',
    '+3' / '-2' (day offsets) or an ISO date (YYYY-MM-DD)."""
    today = today or date.today()
    if value is None or not str(value).strip():
        return today
    v = str(value).strip().lower()
    aliases = {"today": 0, "heute": 0, "yesterday": -1, "gestern": -1, "tomorrow": 1, "morgen": 1,
               "vorgestern": -2, "übermorgen": 2}
    if v in aliases:
        return today + timedelta(days=aliases[v])
    if re.fullmatch(r"[+-]\d+", v):
        return today + timedelta(days=int(v))
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise ValueError(
            f"Unbekanntes Datum '{value}'. Erlaubt: YYYY-MM-DD, today/heute, yesterday/gestern, "
            "tomorrow/morgen oder Offsets wie -1/+2."
        ) from exc


_TEMPLATE_VAR = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+)\s*(?::\s*([^}]*?))?\s*\}\}")


def render_template(
    template: str,
    *,
    title: str,
    now: datetime | None = None,
    date_format: str = "YYYY-MM-DD",
    time_format: str = "HH:mm",
    locale: str = "de",
    variables: dict[str, str] | None = None,
) -> str:
    """Render Obsidian core-template syntax: {{title}}, {{date}}, {{time}},
    {{date:FORMAT}}, {{time:FORMAT}} plus custom {{variables}}."""
    now = now or datetime.now()
    variables = {k.lower(): str(v) for k, v in (variables or {}).items()}

    def repl(m: re.Match[str]) -> str:
        name, fmt = m.group(1), m.group(2)
        key = name.lower()
        if key in variables:
            return variables[key]
        if key == "title":
            return title
        if key == "date":
            return format_moment(fmt or date_format, now, locale)
        if key == "time":
            return format_moment(fmt or time_format, now, locale)
        return m.group(0)  # leave unknown placeholders untouched

    return _TEMPLATE_VAR.sub(repl, template)
