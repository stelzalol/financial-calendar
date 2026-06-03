"""
Financial Macro Calendar -> iCal feed

MVP goal:
- Pull official US release calendars where possible.
- Pull Australian ABS/RBA release pages.
- Filter to market-moving macro keywords.
- Output a single .ics file you can subscribe to in Apple Calendar / Google Calendar / Outlook.

Install:
    pip install requests beautifulsoup4 python-dateutil flask

Generate once:
    python financial_calendar_ical_feed.py --build

Serve locally as a live feed:
    python financial_calendar_ical_feed.py --serve
    Then subscribe to: http://127.0.0.1:8000/macro-calendar.ics

Suggested production hosting:
- GitHub Actions scheduled daily -> writes macro-calendar.ics -> GitHub Pages URL.
- Or a tiny VPS / Render / Railway Flask app.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta

try:
    from flask import Flask, Response
except Exception:  # Flask only needed for --serve
    Flask = None
    Response = None


# ----------------------------
# CONFIG
# ----------------------------

OUTPUT_FILE = Path("macro-calendar.ics")
CACHE_SECONDS = 60 * 60 * 6  # 6 hours when serving locally

US_EASTERN = ZoneInfo("America/New_York")
AU_SYDNEY = ZoneInfo("Australia/Sydney")
UTC = timezone.utc

OFFICIAL_ICS_SOURCES = {
    "US BEA": "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics",
}

ABS_BASE = "https://www.abs.gov.au/release-calendar/future-releases"
RBA_COMING_UP = "https://www.rba.gov.au/coming-up/"

# Dedicated ABS series pages for important Australian financial releases.
# These help retain/recover dates after a release drops out of the general
# future-release calendar.
ABS_CORE_SERIES = {
    "Australian National Accounts: National Income, Expenditure and Product": "https://www.abs.gov.au/statistics/economy/national-accounts/australian-national-accounts-national-income-expenditure-and-product",
    "Consumer Price Index, Australia": "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia",
    "Labour Force, Australia": "https://www.abs.gov.au/statistics/labour/employment-and-unemployment/labour-force-australia",
    "Wage Price Index, Australia": "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/wage-price-index-australia",
    "Producer Price Indexes, Australia": "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/producer-price-indexes-australia",
}

# Kept for source reference / future use.
FED_FOMC = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# Broad keyword list used to identify possible macro events from official source pages.
# The final feed is tightened later by is_core_high_impact_release().
MARKET_KEYWORDS = [
    # Inflation
    "cpi",
    "consumer price",
    "ppi",
    "producer price",
    "pce",
    "personal consumption expenditures",
    "import price",
    "export price",
    "wage price",

    # Growth / spending / activity
    "gdp",
    "gross domestic product",
    "personal income and outlays",
    "retail sales",
    "retail trade",
    "household spending",
    "construction work done",
    "building approvals",
    "capital expenditure",
    "new capital expenditure",
    "business indicators",
    "trade in goods",
    "international trade",
    "balance of payments",
    "national accounts",

    # Jobs
    "employment situation",
    "nonfarm",
    "payroll",
    "unemployment",
    "job openings",
    "jolts",
    "labour force",
    "labor force",
    "average weekly earnings",

    # Central banks
    "monetary policy",
    "interest rate",
    "cash rate",
    "fomc",
    "federal open market",
    "rba",
    "statement on monetary policy",
    "minutes",
]

# Broad high-impact hint list. This is used for icon/alert impact.
# The final feed is tightened later by is_core_high_impact_release().
HIGH_IMPACT_TERMS = [
    "cpi",
    "consumer price",
    "ppi",
    "producer price",
    "gdp",
    "gross domestic product",
    "national accounts",
    "australian national accounts",
    "national income, expenditure and product",
    "employment situation",
    "nonfarm",
    "payroll",
    "labour force",
    "labor force",
    "pce",
    "personal income and outlays",
    "fomc",
    "monetary policy decision",
    "cash rate",
    "interest rate",
]


# ----------------------------
# RELEASE EXPLAINERS
# ----------------------------
# These are our own Forex Factory-style explainers.
# They are written for trader context and are not copied from Forex Factory.

RELEASE_EXPLAINERS = {
    "core_pce": {
        "name": "Core PCE Price Index",
        "source": "US Bureau of Economic Analysis",
        "measures": "Change in prices paid by consumers for goods and services, excluding food and energy.",
        "usual_effect": "Higher-than-forecast inflation is usually bullish for USD and bearish for bonds, because it can increase expectations of tighter Federal Reserve policy.",
        "frequency": "Monthly, usually released about four weeks after the month ends.",
        "why_traders_care": "Core PCE is one of the Federal Reserve's preferred inflation measures, so it can strongly influence interest-rate expectations.",
        "notes": "Core PCE differs from Core CPI because it uses a different basket and weighting method. CPI often gets more immediate market attention, but Core PCE is very important for Fed policy.",
        "acronyms": "PCE = Personal Consumption Expenditures; CPI = Consumer Price Index.",
    },
    "cpi": {
        "name": "Consumer Price Index",
        "source": "BLS / ABS depending on country",
        "measures": "Change in the prices paid by consumers for a basket of goods and services.",
        "usual_effect": "Higher-than-forecast inflation is usually bullish for the local currency and bearish for bonds, because markets may price in higher interest rates.",
        "frequency": "US CPI is monthly. Australian CPI has both monthly indicator releases and quarterly CPI releases.",
        "why_traders_care": "CPI is one of the most watched inflation indicators because it can quickly shift central-bank rate expectations.",
        "notes": "Core CPI removes volatile items such as food and energy to give a cleaner read on underlying inflation pressure.",
        "acronyms": "CPI = Consumer Price Index.",
    },
    "ppi": {
        "name": "Producer Price Index",
        "source": "US Bureau of Labor Statistics / ABS depending on country",
        "measures": "Change in prices received by producers for goods and services.",
        "usual_effect": "Higher-than-forecast PPI can be bullish for the local currency if traders expect producer inflation to flow through to consumer inflation.",
        "frequency": "Monthly in the US. Quarterly in Australia.",
        "why_traders_care": "PPI can provide an early signal of inflation pressure before it reaches consumers.",
        "notes": "PPI is usually less market-moving than CPI, but it can still matter when inflation is the main macro theme.",
        "acronyms": "PPI = Producer Price Index.",
    },
    "employment": {
        "name": "Employment / Payrolls / Labour Force",
        "source": "BLS / ABS depending on country",
        "measures": "Change in employment, unemployment, participation and labour-market strength.",
        "usual_effect": "Stronger-than-forecast jobs data is usually bullish for the local currency and bearish for bonds if it increases rate-hike expectations.",
        "frequency": "Monthly.",
        "why_traders_care": "Jobs data is a major driver of interest-rate expectations, consumer spending expectations and recession-risk pricing.",
        "notes": "For the US, Non-Farm Payrolls is one of the most market-moving releases. For Australia, Labour Force data is important for RBA expectations.",
        "acronyms": "NFP = Non-Farm Payrolls; ABS = Australian Bureau of Statistics; BLS = Bureau of Labor Statistics.",
    },
    "gdp": {
        "name": "Gross Domestic Product",
        "source": "BEA / ABS depending on country",
        "measures": "Broad change in the value of goods and services produced by the economy.",
        "usual_effect": "Stronger-than-forecast GDP can be bullish for the local currency and equities, although the reaction depends on inflation and rate expectations.",
        "frequency": "Quarterly, with revisions.",
        "why_traders_care": "GDP is the broadest measure of economic growth and helps traders judge whether the economy is accelerating or slowing.",
        "notes": "GDP can be backward-looking, so markets often react more strongly when the result changes the outlook for central-bank policy or recession risk.",
        "acronyms": "GDP = Gross Domestic Product.",
    },
    "retail_sales": {
        "name": "Retail Sales / Retail Trade",
        "source": "Census / ABS depending on country",
        "measures": "Change in retail spending by consumers.",
        "usual_effect": "Stronger-than-forecast retail sales can be bullish for the local currency and consumer-related equities if it suggests resilient demand.",
        "frequency": "Monthly.",
        "why_traders_care": "Consumer spending is a major part of economic activity, so retail data can influence growth and rate expectations.",
        "notes": "Retail sales can be volatile month to month. Traders often compare the result with inflation, jobs and wages data.",
        "acronyms": "ABS = Australian Bureau of Statistics.",
    },
    "central_bank": {
        "name": "Central Bank Rate Decision",
        "source": "Federal Reserve / Reserve Bank of Australia",
        "measures": "Interest-rate decision, policy statement and guidance from the central bank.",
        "usual_effect": "More hawkish-than-expected guidance is usually bullish for the local currency and bearish for bonds. More dovish-than-expected guidance is usually bearish for the currency and bullish for bonds.",
        "frequency": "Scheduled several times per year.",
        "why_traders_care": "Central-bank decisions directly affect interest rates, currency valuation, bond yields and equity risk appetite.",
        "notes": "The market often reacts not only to the rate decision, but also to the statement, forecasts, press conference and tone.",
        "acronyms": "FOMC = Federal Open Market Committee; RBA = Reserve Bank of Australia.",
    },
    "wages": {
        "name": "Wage Price Index / Average Earnings",
        "source": "ABS / BLS depending on country",
        "measures": "Change in wages and earnings.",
        "usual_effect": "Higher-than-forecast wage growth can be bullish for the local currency if traders expect stronger inflation pressure and tighter central-bank policy.",
        "frequency": "Monthly or quarterly depending on release.",
        "why_traders_care": "Wage growth can feed into inflation and is closely watched by central banks.",
        "notes": "Strong wages can be positive for consumers but can also increase inflation pressure.",
        "acronyms": "WPI = Wage Price Index.",
    },
}


HEADERS = {
    "User-Agent": "Mozilla/5.0 macro-calendar-builder/1.0 (+personal-use)",
    "Accept": "text/html,application/xhtml+xml,application/xml,text/calendar,text/plain,*/*",
}


@dataclass(frozen=True)
class MacroEvent:
    source: str
    title: str
    start: datetime
    end: datetime | None = None
    url: str | None = None
    description: str | None = None
    impact: str = "medium"

    @property
    def uid(self) -> str:
        raw = f"{self.source}|{self.title}|{self.start.isoformat()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest() + "@macro-calendar.local"


# ----------------------------
# Helpers
# ----------------------------

def fetch_text(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def is_market_event(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in MARKET_KEYWORDS)


def impact_for(title: str) -> str:
    t = title.lower()
    return "high" if any(k in t for k in HIGH_IMPACT_TERMS) else "medium"


def release_family(title: str) -> str:
    """
    Normalised family key used for strict filtering and deduping.
    """
    t = title.lower()

    if "fomc press conference" in t:
        return "fomc_press_conference"

    if "fomc" in t or "federal open market" in t:
        return "fomc_rate_decision"

    if "cash rate" in t or "monetary policy decision" in t or "statement on monetary policy" in t:
        return "rba_policy"

    if "personal income and outlays" in t or "pce price index" in t or "core pce" in t:
        return "core_pce"

    if "consumer price index" in t or re.search(r"\bcpi\b", t):
        return "cpi"

    if "producer price index" in t or "producer price indexes" in t or re.search(r"\bppi\b", t):
        return "ppi"

    if "labour force" in t or "labor force" in t or "employment situation" in t or "nonfarm" in t or "payroll" in t:
        return "employment"

    if "wage price index" in t or "average weekly earnings" in t:
        return "wages"

    if (
        "australian national accounts" in t
        and "national income, expenditure and product" in t
    ):
        return "au_gdp"

    if "gdp" in t or "gross domestic product" in t:
        return "us_gdp"

    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")


def is_core_high_impact_release(title: str) -> bool:
    """
    Strict whitelist for the final public calendar feed.

    This keeps the main market-moving releases and removes broad false positives
    like regional GDP, finance/wealth national accounts, system-of-accounts
    publications, labour-force participation feature releases, etc.
    """
    t = title.lower()

    excluded_phrases = [
        "gdp by county",
        "gdp by state",
        "personal income by county",
        "gross domestic product by county",
        "gross domestic product by state",
        "australian national accounts: finance and wealth",
        "australian system of national accounts",
        "barriers and incentives",
        "international trade",
        "trade in goods",
        "balance of payments",
        "building approvals",
        "business indicators",
        "construction work done",
        "capital expenditure",
        "new capital expenditure",
        "household spending",
        "jolts",
        "job openings",
        "minutes",
    ]

    if any(x in t for x in excluded_phrases):
        return False

    return release_family(title) in {
        "fomc_rate_decision",
        "fomc_press_conference",
        "rba_policy",
        "au_gdp",
        "us_gdp",
        "core_pce",
        "cpi",
        "ppi",
        "employment",
        "wages",
    }


def event_quality_score(ev: MacroEvent) -> int:
    """
    When duplicate events exist, keep the most useful version.
    Usually this means the title that includes the reference period.
    """
    score = 0
    t = ev.title.lower()

    if ev.url:
        score += 5

    if ev.description:
        score += 3

    if re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}\b",
        t,
    ):
        score += 15

    if re.search(r"\b(1st|2nd|3rd|4th)\s+quarter\s+\d{4}\b", t):
        score += 10

    if ev.source == "AU ABS" and any(
        phrase in t
        for phrase in [
            "consumer price index, australia,",
            "labour force, australia,",
            "australian national accounts: national income, expenditure and product,",
            "wage price index, australia,",
            "producer price indexes, australia,",
        ]
    ):
        score += 20

    score += min(len(ev.title), 120)
    return score


def release_explainer_key_for(title: str) -> str | None:
    family = release_family(title)

    if family == "core_pce":
        return "core_pce"

    if family == "cpi":
        return "cpi"

    if family == "ppi":
        return "ppi"

    if family == "employment":
        return "employment"

    if family in {"au_gdp", "us_gdp"}:
        return "gdp"

    if family in {"fomc_rate_decision", "fomc_press_conference", "rba_policy"}:
        return "central_bank"

    if family == "wages":
        return "wages"

    return None


def release_explainer_for(title: str) -> str | None:
    key = release_explainer_key_for(title)

    if not key:
        return None

    explainer = RELEASE_EXPLAINERS.get(key)

    if not explainer:
        return None

    parts = [
        f"• What it is: {explainer['name']}",
        f"• Measures: {explainer['measures']}",
        f"• Usual market effect: {explainer['usual_effect']}",
        f"• Why traders care: {explainer['why_traders_care']}",
        f"• Frequency: {explainer['frequency']}",
    ]

    if explainer.get("notes"):
        parts.append(f"• Notes: {explainer['notes']}")

    if explainer.get("acronyms"):
        parts.append(f"• Acronyms: {explainer['acronyms']}")

    return "\n\n".join(parts)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def fold_ics_line(line: str) -> str:
    """Fold iCalendar lines to a conservative length."""
    max_len = 73
    out = []

    while len(line.encode("utf-8")) > max_len:
        cut = max_len

        while True:
            try:
                head = line.encode("utf-8")[:cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1

        out.append(head)
        line = line[len(head):]
        line = " " + line

    out.append(line)
    return "\r\n".join(out)


def ics_escape(text: str) -> str:
    clean_text = html.unescape(text or "").replace("\r\n", "\n").replace("\r", "\n")
    clean_lines = [normalise(line) for line in clean_text.split("\n")]
    clean_text = "\n".join(clean_lines)

    return (
        clean_text
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def ics_unescape(text: str) -> str:
    """Unescape the limited iCalendar escaping used by this script."""
    return (
        (text or "")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def summary_to_source_title(summary: str) -> tuple[str, str]:
    clean = normalise(summary)
    clean = re.sub(r"^[🔥📊\s]+", "", clean).strip()

    if ":" in clean:
        source, title = clean.split(":", 1)
        return normalise(source), normalise(title)

    return "Unknown", clean


def parse_existing_calendar_events(path: Path = OUTPUT_FILE) -> list[MacroEvent]:
    """
    Read the already-generated .ics file and keep prior events.

    This prevents official releases from disappearing after source websites move
    them from a future-release page to a latest-release/archive page.
    """
    if not path.exists():
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Warning: failed to read existing calendar {path}: {e}")
        return []

    lines = unfold_ics(text)
    events: list[MacroEvent] = []
    in_event = False
    in_alarm = False
    current: dict[str, str] = {}

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            in_alarm = False
            current = {}
            continue

        if line == "END:VEVENT" and in_event:
            summary = ics_unescape(current.get("SUMMARY", ""))
            source, title = summary_to_source_title(summary)
            start = parse_ics_datetime(current.get("DTSTART", ""), UTC)
            end = parse_ics_datetime(current.get("DTEND", ""), UTC)
            description = ics_unescape(current.get("DESCRIPTION", ""))

            impact = impact_for(title)
            m = re.search(r"Impact:\s*(high|medium|low)", description, flags=re.IGNORECASE)
            if m:
                impact = m.group(1).lower()

            url = None
            m = re.search(r"Official source:\s*(https?://\S+)", description)
            if m:
                url = m.group(1)

            if start and title:
                events.append(
                    MacroEvent(
                        source=source,
                        title=title,
                        start=start,
                        end=end,
                        url=url,
                        description=description,
                        impact=impact,
                    )
                )

            in_event = False
            in_alarm = False
            current = {}
            continue

        if in_event and line == "BEGIN:VALARM":
            in_alarm = True
            continue

        if in_event and line == "END:VALARM":
            in_alarm = False
            continue

        if in_event and not in_alarm and ":" in line:
            key, value = line.split(":", 1)
            base_key = key.split(";", 1)[0]
            if base_key in {"SUMMARY", "DESCRIPTION", "DTSTART", "DTEND"}:
                current[base_key] = line if base_key in {"DTSTART", "DTEND"} else value

    return events


def current_year_start_utc() -> datetime:
    local_start = datetime(datetime.now(AU_SYDNEY).year, 1, 1, tzinfo=AU_SYDNEY)
    return local_start.astimezone(UTC)


def dedupe_events(events: Iterable[MacroEvent]) -> list[MacroEvent]:
    """
    Remove duplicates caused by:
    - ABS general future calendar + ABS series pages
    - retained historical calendar + newly fetched event
    - slightly different titles for same core event/time
    """
    best: dict[tuple[str, str, str], MacroEvent] = {}

    for ev in events:
        if not is_core_high_impact_release(ev.title):
            continue

        key = (
            ev.source.lower().strip(),
            release_family(ev.title),
            ensure_utc(ev.start).strftime("%Y-%m-%dT%H:%M"),
        )

        current = best.get(key)

        if current is None or event_quality_score(ev) > event_quality_score(current):
            best[key] = ev

    return sorted(best.values(), key=lambda e: e.start)


def format_ics_datetime(dt: datetime) -> str:
    return ensure_utc(dt).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events: Iterable[MacroEvent]) -> str:
    now = datetime.now(UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Personal Macro Calendar//AU-US Economic Releases//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:AU + US Macro Releases",
        "X-WR-TIMEZONE:UTC",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    deduped = dedupe_events(events)

    for ev in deduped:
        start = ensure_utc(ev.start)
        end = ensure_utc(ev.end) if ev.end else start + timedelta(minutes=30)
        summary_prefix = "🔥 " if ev.impact == "high" else "📊 "
        summary = f"{summary_prefix}{ev.source}: {ev.title}"

        desc_parts = [
            f"Source: {ev.source}",
            f"Impact: {ev.impact}",
        ]

        if ev.description:
            desc_parts.append(ev.description)

        explainer = release_explainer_for(ev.title)
        if explainer:
            desc_parts.append("")
            desc_parts.append("Explainer:")
            desc_parts.append(explainer)

        if ev.url:
            desc_parts.append("")
            desc_parts.append(f"Official source: {ev.url}")

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{ev.uid}",
                f"DTSTAMP:{format_ics_datetime(now)}",
                f"DTSTART:{format_ics_datetime(start)}",
                f"DTEND:{format_ics_datetime(end)}",
                f"SUMMARY:{ics_escape(summary)}",
                "DESCRIPTION:" + ics_escape("\n".join(desc_parts)),
                "STATUS:CONFIRMED",
                "TRANSP:TRANSPARENT",
            ]
        )

        if ev.impact == "high":
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "TRIGGER:-PT24H",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{ics_escape('Tomorrow: ' + summary)}",
                    "END:VALARM",
                    "BEGIN:VALARM",
                    "TRIGGER:-PT30M",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{ics_escape('30 minutes: ' + summary)}",
                    "END:VALARM",
                ]
            )
        else:
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "TRIGGER:-PT30M",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{ics_escape('30 minutes: ' + summary)}",
                    "END:VALARM",
                ]
            )

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


# ----------------------------
# ICS source parsing: BEA
# ----------------------------

def unfold_ics(text: str) -> list[str]:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []

    for line in raw_lines:
        if not line:
            continue

        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    return lines


def parse_ics_datetime(line: str, fallback_tz: ZoneInfo = UTC) -> datetime | None:
    if ":" not in line:
        return None

    key, value = line.split(":", 1)
    value = value.strip()

    tz = fallback_tz
    m = re.search(r"TZID=([^;:]+)", key)

    if m:
        try:
            tz = ZoneInfo(m.group(1))
        except Exception:
            tz = fallback_tz

    try:
        if "VALUE=DATE" in key or re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=tz)

        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)

        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz)

    except Exception:
        return None


def parse_ics_duration(value: str) -> timedelta:
    days = hours = minutes = 0

    m = re.search(r"(\d+)D", value)
    if m:
        days = int(m.group(1))

    m = re.search(r"(\d+)H", value)
    if m:
        hours = int(m.group(1))

    m = re.search(r"(\d+)M", value)
    if m:
        minutes = int(m.group(1))

    return timedelta(days=days, hours=hours, minutes=minutes)


def parse_source_ics(ics_text: str, source: str, source_url: str) -> list[MacroEvent]:
    lines = unfold_ics(ics_text)
    events: list[MacroEvent] = []
    in_event = False
    current: dict[str, str] = {}

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            current = {}
            continue

        if line == "END:VEVENT" and in_event:
            title = current.get("SUMMARY", "").replace("\\,", ",")
            start_line = current.get("DTSTART")
            end_line = current.get("DTEND")
            duration = current.get("DURATION")
            desc = current.get("DESCRIPTION", "").replace("\\n", " ")

            start = parse_ics_datetime(start_line or "", US_EASTERN)
            end = parse_ics_datetime(end_line or "", US_EASTERN) if end_line else None

            if start and not end and duration:
                end = start + parse_ics_duration(duration)

            if start and title and is_market_event(title):
                events.append(
                    MacroEvent(
                        source=source,
                        title=normalise(title),
                        start=start,
                        end=end,
                        url=source_url,
                        description=normalise(desc),
                        impact=impact_for(title),
                    )
                )

            in_event = False
            current = {}
            continue

        if in_event and ":" in line:
            key, value = line.split(":", 1)
            base_key = key.split(";", 1)[0]
            current[base_key] = line if base_key in {"DTSTART", "DTEND"} else value

    return events


def fetch_official_ics_events() -> list[MacroEvent]:
    events: list[MacroEvent] = []

    for source, url in OFFICIAL_ICS_SOURCES.items():
        try:
            text = fetch_text(url)
            events.extend(parse_source_ics(text, source, url))
        except Exception as e:
            print(f"Warning: failed to fetch {source}: {e}")

    return events


# ----------------------------
# ABS parser
# ----------------------------

def month_urls_from_abs_index() -> list[str]:
    """Find ABS future-release pages."""
    try:
        text = fetch_text(ABS_BASE)
        soup = BeautifulSoup(text, "html.parser")
        urls = {ABS_BASE}

        for a in soup.find_all("a", href=True):
            href = a["href"]

            if "/release-calendar/future-releases/" in href:
                if href.startswith("http"):
                    urls.add(href)
                else:
                    urls.add("https://www.abs.gov.au" + href)

        return sorted(urls)

    except Exception as e:
        print(f"Warning: failed to discover ABS month URLs: {e}")

        today = datetime.now(AU_SYDNEY).date().replace(day=1)
        urls = [ABS_BASE]

        for i in range(1, 6):
            ym = (today + relativedelta(months=i)).strftime("%Y%m")
            urls.append(f"{ABS_BASE}/{ym}")

        return urls


def parse_abs_datetime(line: str) -> datetime | None:
    clean = normalise(line).replace("| Updated information", "")

    pattern = (
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) "
        r"(\d{1,2} [A-Za-z]+ \d{4}) "
        r"(\d{1,2}:\d{2})(am|pm) "
        r"([A-Z]{3,4})"
    )

    m = re.match(pattern, clean)

    if not m:
        return None

    date_part = m.group(2)
    time_part = m.group(3) + m.group(4).upper()

    try:
        naive = datetime.strptime(f"{date_part} {time_part}", "%d %B %Y %I:%M%p")
        return naive.replace(tzinfo=AU_SYDNEY)

    except Exception:
        return None


def parse_abs_release_datetime(value: str) -> datetime | None:
    """
    Parse ABS date/time strings like:
    - 03/06/2026 11:30am AEST
    - 2/09/2026 11:30am AEST
    - 02/12/2026 11:30am AEDT
    """
    value = normalise(value)

    m = re.search(
        r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2})(am|pm)\s+([A-Z]{3,4})",
        value,
        flags=re.IGNORECASE,
    )

    if not m:
        return None

    date_part = m.group(1)
    time_part = f"{m.group(2)}{m.group(3).upper()}"

    try:
        naive = datetime.strptime(f"{date_part} {time_part}", "%d/%m/%Y %I:%M%p")
        # ABS release times are Canberra/Sydney time.
        # AU_SYDNEY handles AEST/AEDT automatically for the given date.
        return naive.replace(tzinfo=AU_SYDNEY)
    except Exception:
        return None


def parse_abs_core_future_releases(title_base: str, text: str, url: str) -> list[MacroEvent]:
    """
    Parse important ABS series pages that list future release dates.
    """
    soup = BeautifulSoup(text, "html.parser")
    lines = [normalise(x) for x in soup.get_text("\n").split("\n")]
    lines = [x for x in lines if x]
    events: list[MacroEvent] = []

    for line in lines:
        if title_base.lower() not in line.lower():
            continue

        if "release date" not in line.lower():
            continue

        m = re.search(
            r"(?P<title>.+?)\s+Release date\s+"
            r"(?P<dt>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(?:am|pm)\s+[A-Z]{3,4})",
            line,
            flags=re.IGNORECASE,
        )

        if not m:
            continue

        title = normalise(m.group("title"))
        start = parse_abs_release_datetime(m.group("dt"))

        if not start:
            continue

        events.append(
            MacroEvent(
                source="AU ABS",
                title=title,
                start=start,
                end=start + timedelta(minutes=30),
                url=url,
                description="Official ABS series page future release schedule",
                impact=impact_for(title),
            )
        )

    return events


def find_abs_latest_release_url(text: str, base_url: str, title_base: str) -> str | None:
    """
    Find the first latest-release link from an ABS series page.
    The latest-release link is normally the first release-specific link after the title.
    """
    soup = BeautifulSoup(text, "html.parser")
    candidates: list[str] = []

    for a in soup.find_all("a", href=True):
        label = normalise(a.get_text(" "))

        if not label.lower().startswith(title_base.lower()):
            continue

        href = a["href"]

        if href.startswith("#"):
            continue

        full_url = href if href.startswith("http") else "https://www.abs.gov.au" + href

        if full_url.rstrip("/") == base_url.rstrip("/"):
            continue

        candidates.append(full_url)

    return candidates[0] if candidates else None


def parse_abs_latest_release_page(url: str, title_base: str) -> MacroEvent | None:
    """
    Parse an ABS latest-release page.

    This fixes same-day releases disappearing after they move from future release
    to latest release, e.g. GDP/National Accounts on release day.
    """
    try:
        text = fetch_text(url)
    except Exception as e:
        print(f"Warning: failed to fetch ABS latest release {url}: {e}")
        return None

    soup = BeautifulSoup(text, "html.parser")
    plain = normalise(soup.get_text(" "))

    ref_period = ""
    m_ref = re.search(r"Reference period\s+(.+?)\s+Released", plain, flags=re.IGNORECASE)
    if m_ref:
        ref_period = normalise(m_ref.group(1))

    m_dt = re.search(
        r"Release date and time\s+"
        r"(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(?:am|pm)\s+[A-Z]{3,4})",
        plain,
        flags=re.IGNORECASE,
    )

    if not m_dt:
        return None

    start = parse_abs_release_datetime(m_dt.group(1))

    if not start:
        return None

    title = title_base
    if ref_period:
        title = f"{title_base}, {ref_period}"

    return MacroEvent(
        source="AU ABS",
        title=title,
        start=start,
        end=start + timedelta(minutes=30),
        url=url,
        description="Official ABS latest release page",
        impact=impact_for(title),
    )


def parse_abs_core_series_page(title_base: str, text: str, url: str) -> list[MacroEvent]:
    events = parse_abs_core_future_releases(title_base, text, url)

    latest_url = find_abs_latest_release_url(text, url, title_base)
    if latest_url:
        latest_event = parse_abs_latest_release_page(latest_url, title_base)
        if latest_event:
            events.append(latest_event)

    return events


def fetch_abs_core_series_events() -> list[MacroEvent]:
    events: list[MacroEvent] = []

    for title_base, url in ABS_CORE_SERIES.items():
        try:
            text = fetch_text(url)
            events.extend(parse_abs_core_series_page(title_base, text, url))
        except Exception as e:
            print(f"Warning: failed to fetch ABS core series {title_base}: {e}")

    return events


def parse_abs_page(text: str, url: str) -> list[MacroEvent]:
    soup = BeautifulSoup(text, "html.parser")
    raw_lines = [normalise(x) for x in soup.get_text("\n").split("\n")]
    lines = [x for x in raw_lines if x]
    events: list[MacroEvent] = []

    for i, line in enumerate(lines):
        start = parse_abs_datetime(line)

        if not start:
            continue

        title = None

        for j in range(i + 1, min(i + 8, len(lines))):
            candidate = lines[j].lstrip("# ").strip()

            if not candidate:
                continue

            if candidate.lower().startswith(("reference period", "view current release")):
                continue

            if candidate.lower() in {"choose month", "choose theme", "add month to your calendar"}:
                continue

            title = candidate
            break

        if title and is_market_event(title):
            ref_period = ""

            for j in range(i + 1, min(i + 12, len(lines))):
                if lines[j].lower().startswith("reference period"):
                    ref_period = lines[j]
                    break

            events.append(
                MacroEvent(
                    source="AU ABS",
                    title=title,
                    start=start,
                    end=start + timedelta(minutes=30),
                    url=url,
                    description=ref_period,
                    impact=impact_for(title),
                )
            )

    return events


def fetch_abs_events() -> list[MacroEvent]:
    events: list[MacroEvent] = []

    for url in month_urls_from_abs_index():
        try:
            text = fetch_text(url)
            events.extend(parse_abs_page(text, url))
        except Exception as e:
            print(f"Warning: failed to fetch ABS page {url}: {e}")

    return events


# ----------------------------
# RBA parser
# ----------------------------

def parse_rba_datetime(line: str) -> datetime | None:
    clean = normalise(line).replace("\u00a0", " ")

    m = re.search(
        r"(\d{1,2} [A-Za-z]+ \d{4})\s+"
        r"(\d{1,2})[.:](\d{2})\s*(am|pm)\s*([A-Z]{3,4})",
        clean,
        flags=re.IGNORECASE,
    )

    if not m:
        return None

    date_part = m.group(1)
    time_part = f"{m.group(2)}:{m.group(3)}{m.group(4).upper()}"

    try:
        naive = datetime.strptime(f"{date_part} {time_part}", "%d %B %Y %I:%M%p")
        return naive.replace(tzinfo=AU_SYDNEY)

    except Exception:
        return None


def fetch_rba_events() -> list[MacroEvent]:
    events: list[MacroEvent] = []

    try:
        text = fetch_text(RBA_COMING_UP)
        soup = BeautifulSoup(text, "html.parser")
        lines = [normalise(x) for x in soup.get_text("\n").split("\n")]
        lines = [x for x in lines if x]

        for i, line in enumerate(lines):
            title = line

            if not is_market_event(title):
                continue

            start = None

            for j in range(i + 1, min(i + 6, len(lines))):
                start = parse_rba_datetime(lines[j])

                if start:
                    break

            if start:
                events.append(
                    MacroEvent(
                        source="AU RBA",
                        title=title,
                        start=start,
                        end=start + timedelta(minutes=30),
                        url=RBA_COMING_UP,
                        description="RBA scheduled publication / announcement",
                        impact=impact_for(title),
                    )
                )

    except Exception as e:
        print(f"Warning: failed to fetch RBA events: {e}")

    return events


# ----------------------------
# FOMC parser
# ----------------------------

def fetch_fomc_events() -> list[MacroEvent]:
    """
    Pull FOMC dates from the Federal Reserve's official monthly calendar pages.

    This avoids the old loose parser that scanned the FOMC calendar page text
    and accidentally created false duplicate FOMC dates.
    """
    events: list[MacroEvent] = []

    month_start = datetime.now(US_EASTERN).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    for i in range(0, 18):
        month_dt = month_start + relativedelta(months=i)
        year = month_dt.year
        month_slug = month_dt.strftime("%B").lower()
        url = f"https://www.federalreserve.gov/newsevents/{year}-{month_slug}.htm"

        try:
            text = fetch_text(url)
            soup = BeautifulSoup(text, "html.parser")
            lines = [normalise(x) for x in soup.get_text("\n").split("\n")]
            lines = [x for x in lines if x]

            start_idx = None
            for idx, line in enumerate(lines):
                if line.lower() == "fomc meetings":
                    start_idx = idx
                    break

            if start_idx is None:
                continue

            section_lines = []
            for line in lines[start_idx + 1:]:
                if line in {
                    "Beige Book",
                    "Statistical Releases",
                    "Speeches",
                    "Testimony",
                    "Other",
                    "Announcements",
                    "Conferences",
                }:
                    break
                section_lines.append(line)

            section = " ".join(section_lines)

            meeting_match = re.search(
                r"(\d{1,2}):(\d{2})\s*(a\.m\.|p\.m\.)\s+"
                r"FOMC Meeting\s+Two-day meeting,\s+"
                r"([A-Za-z]+)\s+(\d{1,2})\s*[-–]\s*(\d{1,2})",
                section,
                flags=re.IGNORECASE,
            )

            if meeting_match:
                hour = int(meeting_match.group(1))
                minute = int(meeting_match.group(2))
                ampm = meeting_match.group(3).lower()
                meeting_month = meeting_match.group(4)
                decision_day = int(meeting_match.group(6))

                if ampm == "p.m." and hour != 12:
                    hour += 12
                if ampm == "a.m." and hour == 12:
                    hour = 0

                start = datetime.strptime(
                    f"{decision_day} {meeting_month} {year}",
                    "%d %B %Y",
                ).replace(
                    hour=hour,
                    minute=minute,
                    tzinfo=US_EASTERN,
                )

                events.append(
                    MacroEvent(
                        source="US Fed",
                        title="FOMC Interest Rate Decision",
                        start=start,
                        end=start + timedelta(minutes=60),
                        url=url,
                        description="Official Federal Reserve monthly calendar FOMC meeting entry.",
                        impact="high",
                    )
                )

            press_match = re.search(
                r"(\d{1,2}):(\d{2})\s*(a\.m\.|p\.m\.)\s+"
                r"FOMC Press Conference\s+(\d{1,2})",
                section,
                flags=re.IGNORECASE,
            )

            if press_match:
                hour = int(press_match.group(1))
                minute = int(press_match.group(2))
                ampm = press_match.group(3).lower()
                press_day = int(press_match.group(4))

                if ampm == "p.m." and hour != 12:
                    hour += 12
                if ampm == "a.m." and hour == 12:
                    hour = 0

                start = datetime(
                    year,
                    month_dt.month,
                    press_day,
                    hour,
                    minute,
                    tzinfo=US_EASTERN,
                )

                events.append(
                    MacroEvent(
                        source="US Fed",
                        title="FOMC Press Conference",
                        start=start,
                        end=start + timedelta(minutes=60),
                        url=url,
                        description="Official Federal Reserve monthly calendar FOMC press conference entry.",
                        impact="high",
                    )
                )

        except Exception as e:
            print(f"Warning: failed to fetch Fed monthly calendar {url}: {e}")

    return events


# ----------------------------
# Build / serve
# ----------------------------

def collect_events() -> list[MacroEvent]:
    newly_fetched: list[MacroEvent] = []

    newly_fetched.extend(fetch_official_ics_events())
    newly_fetched.extend(fetch_abs_core_series_events())
    newly_fetched.extend(fetch_rba_events())
    newly_fetched.extend(fetch_fomc_events())

    # Retain existing current-year events so releases do not disappear after
    # source websites move them from future-release pages to latest/archive pages.
    retained = parse_existing_calendar_events(OUTPUT_FILE)

    cutoff = current_year_start_utc()
    events = [e for e in retained + newly_fetched if ensure_utc(e.start) >= cutoff]

    # Final strict filter. This also prevents old false positives from being retained.
    events = [e for e in events if is_core_high_impact_release(e.title)]

    # Ensure all retained/fetched events are treated as high impact once they pass
    # the strict public-feed whitelist.
    events = [
        MacroEvent(
            source=e.source,
            title=e.title,
            start=e.start,
            end=e.end,
            url=e.url,
            description=e.description,
            impact="high",
        )
        for e in events
    ]

    return dedupe_events(events)


def build_file() -> Path:
    events = collect_events()
    ics = build_ics(events)
    OUTPUT_FILE.write_text(ics, encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE.resolve()} with {len(events)} events")
    return OUTPUT_FILE


def serve() -> None:
    if Flask is None:
        raise RuntimeError("Flask is not installed. Run: pip install flask")

    app = Flask(__name__)
    last_build: dict[str, object] = {"ts": None, "ics": None}

    @app.route("/")
    def index():
        return "Subscribe to /macro-calendar.ics"

    @app.route("/macro-calendar.ics")
    def macro_calendar():
        now = datetime.now(UTC)
        last_ts = last_build.get("ts")

        if not last_ts or (now - last_ts).total_seconds() > CACHE_SECONDS:
            events = collect_events()
            last_build["ics"] = build_ics(events)
            last_build["ts"] = now

        return Response(
            last_build["ics"],
            mimetype="text/calendar",
            headers={
                "Content-Disposition": "inline; filename=macro-calendar.ics",
                "Cache-Control": "public, max-age=3600",
            },
        )

    app.run(host="0.0.0.0", port=8000, debug=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="Generate macro-calendar.ics once")
    parser.add_argument("--serve", action="store_true", help="Serve iCal feed at /macro-calendar.ics")
    args = parser.parse_args()

    if args.serve:
        serve()
    else:
        build_file()


if __name__ == "__main__":
    main()
