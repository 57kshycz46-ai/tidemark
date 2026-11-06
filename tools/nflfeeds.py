#!/usr/bin/env python3
"""NFL team calendars for Tidemark, from Wikipedia.

Reads each team's season article ("2026 New York Jets season") through the Wikipedia API and writes one iCalendar
file per team to sports/nfl/<slug>.ics: every regular-season and postseason game, with its kickoff (Eastern Time on
Wikipedia), opponent, venue and TV network, and the result once it's played. A game without a kickoff time yet is an
all-day event on its day. Wikipedia's text is CC BY-SA 4.0; every calendar credits it and links its article.

Run daily by .github/workflows/nfl-feeds.yml. Only files whose games changed are rewritten.
Usage: python3 tools/nflfeeds.py [output dir] [season]
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

UA = "TidemarkFeeds/1.0 (https://57kshycz46-ai.github.io/tidemark/; schedule calendars)"
ET = ZoneInfo("America/New_York")
TEAMS = {
    "cardinals": "Arizona Cardinals", "falcons": "Atlanta Falcons", "ravens": "Baltimore Ravens", "bills": "Buffalo Bills",
    "panthers": "Carolina Panthers", "bears": "Chicago Bears", "bengals": "Cincinnati Bengals", "browns": "Cleveland Browns",
    "cowboys": "Dallas Cowboys", "broncos": "Denver Broncos", "lions": "Detroit Lions", "packers": "Green Bay Packers",
    "texans": "Houston Texans", "colts": "Indianapolis Colts", "jaguars": "Jacksonville Jaguars", "chiefs": "Kansas City Chiefs",
    "raiders": "Las Vegas Raiders", "chargers": "Los Angeles Chargers", "rams": "Los Angeles Rams", "dolphins": "Miami Dolphins",
    "vikings": "Minnesota Vikings", "patriots": "New England Patriots", "saints": "New Orleans Saints", "giants": "New York Giants",
    "jets": "New York Jets", "eagles": "Philadelphia Eagles", "steelers": "Pittsburgh Steelers", "49ers": "San Francisco 49ers",
    "seahawks": "Seattle Seahawks", "buccaneers": "Tampa Bay Buccaneers", "titans": "Tennessee Titans", "commanders": "Washington Commanders",
}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                                       "october", "november", "december"], 1)}


def wikitext(title):
    q = urllib.parse.urlencode({"action": "parse", "page": title, "prop": "wikitext", "format": "json", "formatversion": 2, "redirects": 1})
    req = urllib.request.Request("https://en.wikipedia.org/w/api.php?" + q, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return None if "error" in data else data["parse"]["wikitext"]


def plain(cell):
    """Wiki markup down to its text: [[Page|Text]] → Text, templates and refs dropped."""
    c = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", cell, flags=re.S)
    c = re.sub(r"\{\{nbsp\}\}|&nbsp;", " ", c, flags=re.I)
    # Dates inside templates: {{dow tooltip|September 17, 2026}}, {{dts|2026|9|17}}.
    c = re.sub(r"\{\{dts\|(\d{4})\|(\d{1,2})\|(\d{1,2})[^{}]*\}\}",
               lambda m: f"{list(MONTHS)[int(m.group(2)) - 1].title()} {int(m.group(3))}", c, flags=re.I)
    c = re.sub(r"\{\{(?:dow tooltip|nowrap|small|nobr|center)\|([^{}|]*)(?:\|[^{}]*)?\}\}", r"\1", c, flags=re.I)
    c = re.sub(r"\{\{(?:small|nowrap)\|([^{}]*)\}\}", r"\1", c, flags=re.I)
    for _ in range(3):
        c = re.sub(r"\{\{[^{}]*\}\}", "", c)
    c = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", c)
    c = re.sub(r"\[https?://\S+\s*([^\]]*)\]", r"\1", c)
    c = re.sub(r"'''?", "", c)
    c = re.sub(r"<[^>]+>", "", c)
    return re.sub(r"\s+", " ", c).strip()


def cells(row):
    """The cells of one table row (each starting with | or !, on their own lines or joined by ||)."""
    out = []
    for line in row.split("\n"):
        line = line.strip()
        if not line or line.startswith("|-") or line.startswith("|}") or line.startswith("{|"):
            continue
        if line[0] in "|!":
            for part in re.split(r"\|\||!!", line[1:]):
                # Drop a cell's attributes ("style=… | text"), but not a link's pipe.
                m = re.match(r'^\s*[a-z-]+\s*=\s*"[^"]*"\s*\|(?!\|)(.*)$', part, flags=re.S)
                out.append(m.group(1) if m else part)
    return out


def schedule_tables(text):
    """Regular-season and postseason tables: those with Date and Opponent columns."""
    found = []
    for m in re.finditer(r"\{\|.*?\n\|\}", text, flags=re.S):
        table = m.group(0)
        rows = re.split(r"\n\|-[^\n]*", table)
        header = cells(rows[0]) if rows else []
        names = [plain(h).lower() for h in header]
        if not any(n.startswith("date") for n in names) or not any("opponent" in n for n in names):
            continue
        # The heading just above says which part of the season it is.
        before = text[:m.start()]
        heads = re.findall(r"^=+\s*([^=\n]+?)\s*=+\s*$", before, flags=re.M)
        part = "Postseason" if heads and re.search(r"post|playoff", heads[-1], flags=re.I) else "Regular season"
        if heads and re.search(r"pre-?season", heads[-1], flags=re.I):
            continue
        # Kickoff times are in the zone the Time column names (Eastern unless it says otherwise).
        raw = rows[0] if rows else ""
        zone = next((z for k, z in [("Central", "America/Chicago"), ("Mountain", "America/Denver"), ("Pacific", "America/Los_Angeles"),
                                     ("Eastern", "America/New_York")] if re.search(r"time[^\n]*" + k, raw, flags=re.I)), "America/New_York")
        found.append((part, names, rows[1:], zone))
    return found


def games(team, season, text):
    out = []
    for part, names, rows, zone in schedule_tables(text):
        # Arizona keeps Mountain Standard Time all year (no daylight saving).
        tz = ZoneInfo("America/Phoenix" if team.startswith("Arizona") and zone == "America/Denver" else zone)
        col = lambda key: next((i for i, n in enumerate(names) if n.startswith(key)), None)
        iw, idate, itime, iopp, ires, iven, inet = col("week") if col("week") is not None else col("round"), col("date"), col("time"), col("opponent"), col("result"), col("venue"), col("network")
        for row in rows:
            c = cells(row)
            if idate is None or iopp is None or len(c) <= max(idate, iopp):
                continue
            opponent = plain(c[iopp])
            date_text = plain(c[idate])
            if not opponent or re.search(r"\bbye\b", opponent + " " + date_text, flags=re.I):
                continue
            m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:\s*/\s*(\d{1,2}))?", date_text)
            if not m or m.group(1).lower() not in MONTHS:
                continue
            month, day = MONTHS[m.group(1).lower()], int(m.group(2))
            # "January 9/10": the league hasn't picked the day yet.
            undecided = f"{m.group(1)[:3]} {day} or {m.group(3)}" if m.group(3) else None
            year = season + 1 if month <= 3 else season
            kickoff = None
            if itime is not None and itime < len(c):
                t = plain(c[itime]).lower().replace(".", "")
                tm = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t)
                if tm:
                    h = int(tm.group(1)) % 12 + (12 if tm.group(3) == "pm" else 0)
                    kickoff = dt.datetime(year, month, day, h, int(tm.group(2) or 0), tzinfo=tz)
            away = opponent.lower().startswith("at ") or opponent.startswith("@")
            other = re.sub(r"^(at|vs\.?|@)\s+", "", opponent, flags=re.I).strip()
            short = team.split()[-1]
            other_short = other.split()[-1] if other else other
            title = f"{short} at {other_short}" if away else f"{other_short} at {short}"
            if undecided:
                title += f" ({undecided})"
                kickoff = None
            week = plain(c[iw]) if iw is not None and iw < len(c) else ""
            result = plain(c[ires]) if ires is not None and ires < len(c) else ""
            venue = plain(c[iven]) if iven is not None and iven < len(c) else ""
            network = plain(c[inet]) if inet is not None and inet < len(c) else ""
            out.append(dict(part=part, week=week, date=dt.date(year, month, day), kickoff=kickoff, title=title,
                            opponent=other, away=away, result=result, venue=venue, network=network))
    return out


def ics_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, b = [], line.encode()
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def calendar(slug, team, season, page, found):
    url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(page.replace(" ", "_"))
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tidemark//NFL schedules from Wikipedia//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{ics_escape(team)}", f"X-WR-CALDESC:{ics_escape(f'{team} {season} schedule, from Wikipedia ({url}), CC BY-SA 4.0.')}",
             "X-PUBLISHED-TTL:PT12H", "REFRESH-INTERVAL;VALUE=DURATION:PT12H"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for g in found:
        uid = f"nfl-{season}-{slug}-{g['part'][:3].lower()}-{re.sub(r'[^0-9a-z]+', '', g['week'].lower()) or g['date'].isoformat()}@tidemark"
        notes = [x for x in [f"Week {g['week']}" if g['week'].isdigit() else g['week'], g['network'] and f"TV: {g['network']}",
                             g['result'] and f"Result: {g['result']}"] if x]
        notes.append(f"Schedule from Wikipedia ({url}), CC BY-SA 4.0. Times can change.")
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}"]
        if g["kickoff"]:
            start = g["kickoff"].astimezone(dt.timezone.utc)
            lines += [f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}", f"DTEND:{(start + dt.timedelta(hours=3, minutes=15)).strftime('%Y%m%dT%H%M%SZ')}"]
        else:
            lines += [f"DTSTART;VALUE=DATE:{g['date'].strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(g['date'] + dt.timedelta(days=1)).strftime('%Y%m%d')}"]
        lines += [f"SUMMARY:{ics_escape(g['title'])}", f"DESCRIPTION:{ics_escape(chr(10).join(notes))}"]
        if g["venue"]:
            lines.append(f"LOCATION:{ics_escape(g['venue'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "sports/nfl"
    today = dt.date.today()
    seasons = [int(sys.argv[2])] if len(sys.argv) > 2 else ([today.year, today.year - 1] if today.month >= 5 else [today.year - 1])
    os.makedirs(out_dir, exist_ok=True)
    summary, failed = {}, []
    for slug, team in TEAMS.items():
        made = False
        for season in seasons:
            page = f"{season} {team} season"
            try:
                text = wikitext(page)
            except Exception as e:  # network trouble: keep yesterday's file
                print(f"{slug}: {e}", file=sys.stderr)
                text = None
            time.sleep(0.5)
            if not text:
                continue
            found = games(team, season, text)
            if len(found) < 10:  # no schedule yet (or a page that changed shape): try last season, else keep the old file
                continue
            body = calendar(slug, team, season, page, found)
            path = os.path.join(out_dir, f"{slug}.ics")
            # DTSTAMP changes every run; only real changes are written.
            key = lambda s: re.sub(r"DTSTAMP:\S+", "", s)
            old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if key(old) != key(body):
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(body)
            summary[slug] = {"team": team, "season": season, "games": len(found), "timed": sum(1 for g in found if g["kickoff"])}
            made = True
            break
        if not made:
            failed.append(slug)
    print(json.dumps({"teams": len(summary), "failed": failed}, indent=1))
    for slug, s in summary.items():
        print(f"  {slug:12} {s['season']} {s['games']:3} games, {s['timed']:3} with times")
    return 1 if len(summary) < 30 else 0


if __name__ == "__main__":
    sys.exit(main())
