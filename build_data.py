#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Saque Inicial (ES) — recolector de datos.

Fork español de "Coup d'envoi" / "Kick-off". Reúne los partidos en un único
matches.json servido por la PWA (mismo origen -> sin CORS).

Fuentes:
  - Fútbol : openfootball (JSON de dominio público, sin clave)
             -> La Liga (es.1), Segunda División (es.2), Mundial 2026, Euro 2028
  - Copas  : Wikipedia (EN, plantillas {{Football box}})
             -> Champions League, Europa League, Nations League, Copa del Rey

Nota: los horarios de liga de openfootball son horas locales sin desfase UTC
-> se interpretan en la zona de origen (Europe/Madrid) y se guardan como UTC.
Mundial / Euro llevan un desfase "UTC±H" explícito.
"""

import json, re, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TIMEOUT = 25
UA = {"User-Agent": "saque-inicial/1.0 (+github action)"}

def get_json(url):
    # Wikipedia rate-limits (HTTP 429) when many pages are fetched in a row.
    # We now query ~24 Wikipedia pages per build (foot + rugby), so a plain retry
    # isn't enough: we also throttle *before* every Wikipedia call to stay polite.
    if "wikipedia.org" in url:
        time.sleep(2.5)
    delay = 5.0
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise

# ---------------------------------------------------------------- time helpers
def get_text(url):
    if "wikipedia.org" in url:
        time.sleep(2.5)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8")

def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_openfootball_time(date, t, tz=None):
    """ '13:00 UTC-6' -> explicit offset. '15:00' + tz -> local in tz. None -> (None, True). """
    if not t:
        return None, True
    y, mo, d = (int(x) for x in date.split("-"))
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*UTC([+-]\d{1,2})", t)
    if m:
        hh, mm, off = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return iso_z(datetime(y, mo, d, hh, mm, tzinfo=timezone(timedelta(hours=off)))), False
    m2 = re.match(r"\s*(\d{1,2}):(\d{2})", t)
    if m2:
        hh, mm = int(m2.group(1)), int(m2.group(2))
        tzinfo = ZoneInfo(tz) if tz else timezone.utc
        return iso_z(datetime(y, mo, d, hh, mm, tzinfo=tzinfo)), False
    return None, True

def parse_tsdb_time(ev):
    ts = ev.get("strTimestamp")
    if ts:
        ts = ts.replace(" ", "T").replace("Z", "").split("+")[0]
        try:
            return iso_z(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)), False
        except ValueError:
            pass
    d, t = ev.get("dateEvent"), ev.get("strTime")
    if d and t and t != "00:00:00":
        try:
            return iso_z(datetime.fromisoformat(f"{d}T{t}").replace(tzinfo=timezone.utc)), False
        except ValueError:
            pass
    return None, True

def slug(*parts):
    s = "-".join(str(p) for p in parts if p)
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()[:80]

# ---------------------------------------------------------------- openfootball
def parse_openfootball(data, name, tz=None):
    out = []
    for m in data.get("matches", []):
        date = m.get("date")
        if not date:
            continue
        start, tbd = parse_openfootball_time(date, m.get("time"), tz)
        sc = m.get("score")
        ft = (sc.get("et") or sc.get("ft")) if isinstance(sc, dict) else (sc if isinstance(sc, list) else None)
        score = f"{ft[0]}\u2013{ft[1]}" if isinstance(ft, list) and len(ft) == 2 else None
        h, a = m.get("team1"), m.get("team2")
        out.append({
            "id": slug(name, date, h, a),
            "sport": "Football",
            "competition": name,
            "date": date, "start": start, "tbd": tbd,
            "home": h, "away": a, "score": score,
            "status": "finished" if score else "scheduled",
            "group": m.get("group") or m.get("round"),
            "venue": m.get("ground"),
        })
    return out

# Tournois à fichier fixe (offset UTC explicite dans les heures)
OPENFOOTBALL_FIXED = [
    ("Mundial 2026",   "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"),
    ("Eurocopa 2028",  "https://raw.githubusercontent.com/openfootball/euro.json/master/2028/euro.json"),
]

# Ligues club (heures locales -> timezone) : on prend la saison la plus récente disponible
def league_season_candidates():
    now = datetime.now(timezone.utc)
    y, mth = now.year, now.month
    start = y if mth >= 7 else y - 1          # année de début de saison en cours
    yrs = [start + 1, start, start - 1]        # plus récente d'abord
    return [f"{a}-{str(a+1)[2:]}" for a in yrs]

def collect_openfootball_league(name, code, tz):
    # Pour chaque saison, la plus récente d'abord : on tente le JSON, puis la
    # source texte. Sinon le JSON d'une saison passée masquerait le texte de la
    # saison en cours (le texte est publié des semaines avant sa conversion).
    repo_file = OPENFOOTBALL_TXT.get(code)
    for s in league_season_candidates():
        url = f"https://raw.githubusercontent.com/openfootball/football.json/master/{s}/{code}.json"
        try:
            data = get_json(url)
            return parse_openfootball(data, name, tz), s
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        if repo_file:
            rows = _openfootball_txt_season(name, code, tz, s)
            if rows:
                return rows, s
    return [], None


# ---------------------------------------------------------------- openfootball (texte)
# football.json est GÉNÉRÉ à partir des dépôts texte (openfootball/espana...).
# Le texte est publié plusieurs semaines avant sa conversion JSON : on l'utilise
# en repli pour ne pas attendre la moulinette en début de saison.
OPENFOOTBALL_TXT = {
    "es.1": ("espana", "1-liga.txt"),
    "es.2": ("espana", "2-liga2.txt"),
}

MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}

# "Sun Aug 16 2026" ou "Sun Aug 23" (année omise -> héritée)
RE_DATE = re.compile(r"^\s*[A-Z][a-z]{2}\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:\s+(\d{4}))?\s*$")
RE_MATCHDAY = re.compile(r"^\s*[^\w\s]*\s*Matchday\s+(\d+)", re.I)
# ligne de match : heure optionnelle en tête, puis "A v B" (à venir) ou "A 1-3 (0-3) B" (joué)
RE_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s+(.*)$")
RE_PLAYED = re.compile(r"^(.+?)\s{2,}(\d+)\s*-\s*(\d+)(?:\s*\([^)]*\))?\s{2,}(.+?)\s*$")
RE_UPCOMING = re.compile(r"^(.+?)\s+v\s+(.+?)\s*$")


def parse_openfootball_txt(text, competition, tz_name="Europe/Madrid"):
    """Parse le format texte openfootball (ex: espana/2026-27/1-liga.txt).

    Ce format est la source primaire du projet : il est publié plusieurs
    semaines avant sa conversion en football.json. Le parser gère les deux
    états d'une saison :
      - à venir : "17:00  Equipo A v Equipo B"
      - jouée   : "19:00   Equipo A  1-3 (0-3)  Equipo B" + lignes de buteurs
    La date et l'heure sont héritées de la dernière rencontrée (le fichier ne
    les répète pas pour chaque match d'un même créneau).
    """
    out = []
    cur_date = None          # (year, month, day)
    cur_time = None          # "HH:MM"
    matchday = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = RE_MATCHDAY.search(line)
        if m:
            matchday = int(m.group(1))
            cur_time = None
            continue

        m = RE_DATE.match(line)
        if m:
            mon = MONTHS.get(m.group(1))
            day = int(m.group(2))
            if m.group(3):
                # année explicite dans le fichier : elle fait autorité
                cur_year = int(m.group(3))
            elif cur_date:
                # année omise : on la déduit, en basculant au passage déc -> jan
                cur_year = cur_date[0] + 1 if (mon < cur_date[1] and cur_date[1] == 12) else cur_date[0]
            else:
                cur_year = None
            if mon and cur_year:
                cur_date = (cur_year, mon, day)
            cur_time = None
            continue

        if cur_date is None:
            continue

        body = line
        m = RE_TIME.match(line)
        if m:
            cur_time = f"{int(m.group(1)):02d}:{m.group(2)}"
            body = m.group(3)

        body = body.strip()
        if not body or body.startswith("(") or body.startswith("#") or body.startswith("="):
            continue        # ligne de buteurs / entête

        home = away = score = None
        m = RE_PLAYED.match(body)
        if m:
            home, away = m.group(1).strip(), m.group(4).strip()
            score = f"{m.group(2)}\u2013{m.group(3)}"
        else:
            m = RE_UPCOMING.match(body)
            if m:
                home, away = m.group(1).strip(), m.group(2).strip()
        if not home or not away:
            continue

        date_iso = f"{cur_date[0]:04d}-{cur_date[1]:02d}-{cur_date[2]:02d}"
        out.append({
            "date": date_iso,
            "time_local": cur_time,
            "home": home, "away": away,
            "score": score,
            "matchday": matchday,
            "competition": competition,
        })
    return out


def _openfootball_txt_season(name, code, tz, season):
    """Lit la source texte pour UNE saison donnée (repli quand le JSON manque)."""
    repo, fname = OPENFOOTBALL_TXT[code]
    url = f"https://raw.githubusercontent.com/openfootball/{repo}/master/{season}/{fname}"
    try:
        txt = get_text(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    rows = []
    for m in parse_openfootball_txt(txt, name, tz):
        start, tbd = parse_openfootball_time(m["date"], m["time_local"], tz)
        rows.append({
            "id": slug(code, m["date"], m["home"], m["away"]),
            "sport": "Football", "competition": name,
            "date": m["date"], "start": start,
            "tbd": tbd and m["score"] is None,
            "home": m["home"], "away": m["away"], "score": m["score"],
            "status": "finished" if m["score"] else "scheduled",
            "group": f"J{m['matchday']}" if m.get("matchday") else None,
            "venue": None,
        })
    return rows

OPENFOOTBALL_LEAGUES = [
    ("La Liga",           "es.1", "Europe/Madrid"),
    ("Segunda División",  "es.2", "Europe/Madrid"),
]

# ---------------------------------------------------------------- TheSportsDB
TSDB = "https://www.thesportsdb.com/api/v1/json/123"

def season_guesses():
    now = datetime.now(timezone.utc)
    y, mth = now.year, now.month
    s = y if mth >= 7 else y - 1
    return [f"{s}-{s+1}", f"{y}", f"{y}-{y+1}"]

def tsdb_events(idl):
    # eventsnextleague.php is throttled on the free key (returns a single event),
    # so we read the whole season instead: eventsseason.php is a free method and
    # returns every fixture + result, like openfootball does for the leagues.
    for s in season_guesses():
        try:
            evs = get_json(f"{TSDB}/eventsseason.php?id={idl}&s={s}").get("events") or []
        except Exception:
            evs = []
        if evs:
            return evs
        time.sleep(0.4)
    return []

def collect_tsdb(name, idl, label):
    out = []
    for e in tsdb_events(idl):
        start, tbd = parse_tsdb_time(e)
        h = e.get("strHomeTeam") or e.get("strEvent")
        a = e.get("strAwayTeam")
        hs, as_ = e.get("intHomeScore"), e.get("intAwayScore")
        score = f"{hs}\u2013{as_}" if hs not in (None, "") and as_ not in (None, "") else None
        out.append({
            "id": slug(name, e.get("idEvent")),
            "sport": label,
            "competition": name,
            "date": e.get("dateEvent"), "start": start, "tbd": tbd,
            "home": h, "away": a, "score": score,
            "status": "finished" if score else "scheduled",
            "group": e.get("strRound") or None,
            "venue": e.get("strVenue") or None,
        })
    return out

# (display name, TheSportsDB league id, output sport label)
# Only the WSL is left here: the free key caps eventsseason.php at the first 15
# events of a season. That's tolerable for a season not started yet (you get its
# opening fixtures) but useless once a competition is under way — hence the
# European cups moved to Wikipedia below.
# To add a competition: find it on thesportsdb.com, the id is in the page URL
# (e.g. /league/4849-English-Womens-Super-League -> 4849).
TSDB_SOURCES = [
]

# ---------------------------------------------------------------- Wikipedia (EN)
# The free TheSportsDB key caps eventsseason.php at the first 15 events of a
# season, which is useless for a competition already under way. English Wikipedia
# carries every match in {{Football box}} templates, with kick-off times.
# Machinery ported from the (proven) rugby scraper in Coup d'envoi.
WIKI_EN = "https://en.wikipedia.org/w/api.php"

EN_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}

def wiki_season():
    now = datetime.now(timezone.utc)
    y = now.year if now.month >= 6 else now.year - 1
    return f"{y}\u2013{str(y+1)[2:]}"        # en dash, e.g. "2026–27"

def wiki_wikitext(page):
    """Raw wikitext of a page, or None when the page doesn't exist yet."""
    url = (f"{WIKI_EN}?action=parse&page={urllib.parse.quote(page.replace(' ', '_'))}"
           f"&format=json&prop=wikitext&utf8=1&redirects=1")
    try:
        d = get_json(url)
    except Exception:
        return None
    if "parse" not in d:                      # missing page -> {"error": ...}
        return None
    return d["parse"]["wikitext"]["*"]

def _fb_extract(wikitext):
    """Every {{Football box ...}} / {{Footballbox ...}}, brace-matched."""
    out, low, i = [], wikitext.lower(), 0
    while True:
        hits = [h for h in (low.find("{{football box", i), low.find("{{footballbox", i)) if h != -1]
        if not hits:
            break
        idx = min(hits)
        depth, j = 0, idx
        while j < len(wikitext):
            if wikitext[j:j+2] == "{{":
                depth += 1; j += 2
            elif wikitext[j:j+2] == "}}":
                depth -= 1; j += 2
                if depth == 0:
                    out.append(wikitext[idx:j]); break
            else:
                j += 1
        i = j if j > idx else idx + 2
    return out

def _fb_fields(body):
    """Split template params on top-level pipes (nested {{ }} and [[ ]] safe)."""
    if body.startswith("{{"): body = body[2:]
    if body.endswith("}}"): body = body[:-2]
    parts, current, db, dk, i = [], [], 0, 0, 0
    while i < len(body):
        c, nxt = body[i], body[i+1] if i+1 < len(body) else ""
        if c == "{" and nxt == "{": db += 1; current += [c, nxt]; i += 2; continue
        if c == "}" and nxt == "}": db -= 1; current += [c, nxt]; i += 2; continue
        if c == "[" and nxt == "[": dk += 1; current += [c, nxt]; i += 2; continue
        if c == "]" and nxt == "]": dk -= 1; current += [c, nxt]; i += 2; continue
        if c == "|" and db == 0 and dk == 0:
            parts.append("".join(current)); current = []; i += 1; continue
        current.append(c); i += 1
    if current:
        parts.append("".join(current))
    fields = {}
    for p in parts[1:]:
        if "=" in p:
            k, _, v = p.partition("=")
            fields[k.strip().lower()] = v.strip()
    return fields

def _fb_date(text):
    if not text:
        return None
    # {{dts|2026|07|07}} / {{start date|2026|7|7}}
    m = re.search(r"\{\{\s*(?:dts|start date)[^}]*?\|\s*(\d{4})\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})", text, re.I)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # "7 July 2026"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if m and m.group(2).lower() in EN_MONTHS:
        return f"{int(m.group(3)):04d}-{EN_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    # "July 7, 2026"
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text)
    if m and m.group(1).lower() in EN_MONTHS:
        return f"{int(m.group(3)):04d}-{EN_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    return None

def _fb_time(text):
    if not text:
        return None
    m = re.search(r"(\d{1,2})[:.](\d{2})", text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None

def _fb_team(text):
    if not text:
        return None
    text = text.replace("'''", "")
    text = re.sub(r"\{\{\s*(?:flagicon|fb|fbicon|fbaicon)[^}]*\}\}", "", text, flags=re.I)
    m = re.search(r"\[\[[^\]|]+\|([^\]]+)\]\]", text)   # [[Arsenal F.C.|Arsenal]]
    if m:
        return m.group(1).strip()
    m = re.search(r"\[\[([^\]|]+)\]\]", text)            # [[Arsenal]]
    if m:
        return m.group(1).strip()
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip() or None

def _fb_score(text):
    if not text:
        return None
    text = text.replace("'''", "").strip()
    m = re.match(r"^\s*(\d+)\s*[-\u2013]\s*(\d+)", text)
    return f"{m.group(1)}\u2013{m.group(2)}" if m else None

def collect_wiki_football(name, page_base, label):
    """Merge every Football box across the pages of one competition."""
    s = wiki_season()
    if page_base == "UEFA Nations League":
        # The main article only carries standings grids (dates, no kick-off times).
        # The actual Football boxes live on the per-division articles.
        pages = [f"{s} UEFA Nations League {d}" for d in ("A", "B", "C", "D")]
        pages.append(f"{s} UEFA Nations League Finals")
    elif "Women's" in page_base:
        # Women's competitions use "qualifying rounds" (not just "qualifying").
        pages = [f"{s} {page_base} qualifying rounds",
                 f"{s} {page_base} league phase",
                 f"{s} {page_base} knockout phase",
                 f"{s} {page_base}"]
    elif page_base == "Copa del Rey":
        # Coupe à élimination directe : une seule page par saison porte tous les tours.
        pages = [f"{s} {page_base}"]
    else:
        pages = [f"{s} {page_base} qualifying",
                 f"{s} {page_base} league phase",
                 f"{s} {page_base} knockout phase",
                 f"{s} {page_base}"]
    out, seen, pages_ok = [], set(), []
    for page in pages:
        wt = wiki_wikitext(page)
        if not wt:
            continue
        pages_ok.append(page)
        for body in _fb_extract(wt):
            f = _fb_fields(body)
            date = _fb_date(f.get("date", ""))
            home, away = _fb_team(f.get("team1", "")), _fb_team(f.get("team2", ""))
            if not date or not home or not away:
                continue
            key = (date, home, away)
            if key in seen:
                continue
            seen.add(key)
            start = combine_date_time_cet(date, _fb_time(f.get("time", "")))
            score = _fb_score(f.get("score", ""))
            out.append({
                "id": slug(name, date, home, away),
                "sport": label, "competition": name,
                "date": date, "start": start,
                "tbd": start is None and score is None,
                "home": home, "away": away, "score": score,
                "status": "finished" if score else "scheduled",
                "group": f.get("round") or None,
                "venue": _fb_team(f.get("stadium", "")) or None,
            })
        time.sleep(0.3)
    return out, pages_ok

def combine_date_time_cet(date_iso, time_str):
    """UEFA lists kick-off times in CET/CEST -> store as UTC."""
    if not date_iso or not time_str:
        return None
    try:
        y, mo, d = (int(x) for x in date_iso.split("-"))
        hh, mm = (int(x) for x in time_str.split(":"))
        is_dst = 3 < mo < 10 or (mo == 3 and d >= 28) or (mo == 10 and d < 28)
        local = datetime(y, mo, d, hh, mm, tzinfo=timezone(timedelta(hours=2 if is_dst else 1)))
        return iso_z(local)
    except Exception:
        return None


# (display name, Wikipedia page base, output sport label)
WIKI_SOURCES = [
    ("UEFA Champions League", "UEFA Champions League", "Football"),
    ("UEFA Europa League",    "UEFA Europa League",    "Football"),
    ("UEFA Nations League",   "UEFA Nations League",   "Football"),
    ("UEFA Conference League","UEFA Conference League","Football"),
    ("Copa del Rey",          "Copa del Rey",          "Football"),
]

# ---------------------------------------------------------------- main
def main():
    matches, sources = [], []

    for name, url in OPENFOOTBALL_FIXED:
        try:
            rows = parse_openfootball(get_json(url), name)
            matches += rows
            sources.append({"name": name, "sport": "Football", "ok": True, "count": len(rows)})
            print(f"[ok] {name}: {len(rows)}")
        except Exception as e:
            sources.append({"name": name, "sport": "Football", "ok": False, "error": str(e)})
            print(f"[!!] {name}: {e}", file=sys.stderr)

    for name, code, tz in OPENFOOTBALL_LEAGUES:
        try:
            rows, season = collect_openfootball_league(name, code, tz)
            matches += rows
            sources.append({"name": name, "sport": "Football", "ok": True, "count": len(rows), "season": season})
            print(f"[ok] {name} ({season}): {len(rows)}")
        except Exception as e:
            sources.append({"name": name, "sport": "Football", "ok": False, "error": str(e)})
            print(f"[!!] {name}: {e}", file=sys.stderr)

    for name, page_base, label in WIKI_SOURCES:
        try:
            rows, pages_ok = collect_wiki_football(name, page_base, label)
            matches += rows
            sources.append({"name": name, "sport": label, "ok": True,
                            "count": len(rows), "season": wiki_season()})
            print(f"[ok] {name}: {len(rows)} (pages: {', '.join(pages_ok) or 'none'})")
        except Exception as e:
            sources.append({"name": name, "sport": label, "ok": False, "error": str(e)})
            print(f"[!!] {name}: {e}", file=sys.stderr)

    for name, idl, label in TSDB_SOURCES:
        try:
            rows = collect_tsdb(name, idl, label)
            matches += rows
            sources.append({"name": name, "sport": label, "ok": True, "count": len(rows)})
            print(f"[ok] {name}: {len(rows)}")
        except Exception as e:
            sources.append({"name": name, "sport": label, "ok": False, "error": str(e)})
            print(f"[!!] {name}: {e}", file=sys.stderr)

    seen, uniq = set(), []
    for m in matches:
        if m["id"] in seen:
            continue
        seen.add(m["id"]); uniq.append(m)
    uniq.sort(key=lambda m: (m.get("start") or ((m.get("date") or "9999") + "T99")))

    out = {"generated": iso_z(datetime.now(timezone.utc)), "sources": sources,
           "count": len(uniq), "matches": uniq}
    with open("matches.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\nTotal: {len(uniq)} matchs -> matches.json")

if __name__ == "__main__":
    main()
