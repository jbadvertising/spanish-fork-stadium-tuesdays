#!/usr/bin/env python3
"""
Cinemark Spanish Fork Tuesday stadium-seating watcher (production).

Theater: Cinemark Spanish Fork and XD (theaterId=1153)
Target: every Tuesday Cinemark has scheduled, showtimes strictly after 4:00 PM,
        in the stadium-seating auditoriums (10/11).

Reliable signal (validated 2026-09-27 against browser-verified ground truth,
8/8 Tuesdays match): the JSON-LD ScreeningEvent "name" contains
"Cinemark XD Screening" for auditorium 10/11 showtimes. Auditoriums 10/11 are
the XD screens. RealD 3D screenings almost always play in the XD auditoriums
too, but the user hates 3D -> all 3D screenings are excluded regardless.

Rule: include iff "Cinemark XD" in name AND "3D" not in name
       AND Tuesday AND start time strictly after 16:00.

first_seen: each showtime carries the ISO timestamp it was first observed
(persisted across runs via latest.json), so the board can sort by newest
additions. Re-added entries after a gap get a fresh first_seen.

posters: tmdb_posters.json maps board titles to TMDB poster URLs. Any
title missing from it is looked up on TMDB automatically each run and
persisted, so new movies get their posters without manual work.

sale_status: each showtime carries on_sale / presale / unknown from the
page's offer availability (PreOrder = tickets not yet on sale). Flips
from presale to on_sale are reported as "now_on_sale" in diff.json so
the user hears when tickets open up.

notifications: the script pushes alerts itself to ntfy (the user reads
them in the ntfy phone app instead of chat messages). Topic comes from
the CINEMARK_NTFY_TOPIC env var, or the first line of ntfy_topic.txt.
The last_notified.json baseline advances only after a successful ntfy
send; a failed or unconfigured send leaves the baseline untouched so
nothing is silently swallowed.

Pure HTTP, no browser: no tracker requests, no approval popups.
State files:
  latest.json          - this run's results (only overwritten on success)
  last_notified.json   - baseline the user was last told about
  diff.json            - new/changed/removed vs last_notified.json
  all_tuesdays.json/csv- full data dumps
  ntfy_topic.txt       - ntfy topic (unguessable random string)
Fail-closed: a failed date keeps its previous entry (marked stale); a fully
failed run never overwrites latest.json.
"""
import json, os, re, sys, time, random
import urllib.request, urllib.error, urllib.parse, gzip
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# CINEMARK_WORKSPACE lets the script run from a repo checkout (e.g. a
# GitHub Actions runner) instead of this machine's home directory.
WORKSPACE = os.environ.get("CINEMARK_WORKSPACE",
                           os.path.expanduser("~/workspace/cinemark-watcher"))
LATEST_JSON = os.path.join(WORKSPACE, "latest.json")
SHOWTIMES_JSON = os.path.join(WORKSPACE, "showtimes.json")  # board feed file
NOTIFIED_JSON = os.path.join(WORKSPACE, "last_notified.json")
DIFF_JSON = os.path.join(WORKSPACE, "diff.json")
ALL_JSON = os.path.join(WORKSPACE, "all_tuesdays.json")
CSV_PATH = os.path.join(WORKSPACE, "all_tuesdays.csv")
POSTERS_JSON = os.path.join(WORKSPACE, "tmdb_posters.json")
LOG_MD = os.path.join(WORKSPACE, "run_log.md")
GIST_ID_FILE = os.path.join(WORKSPACE, "gist_id.txt")
NTFY_TOPIC_FILE = os.path.join(WORKSPACE, "ntfy_topic.txt")
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"

THEATER_URL = "https://www.cinemark.com/theatres/ut-spanish-fork/cinemark-spanish-fork-and-xd"
DENVER = ZoneInfo("America/Denver")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
}
# 26 Tuesdays ~ 6 months: covers anything Cinemark could have scheduled.
PROBE_WEEKS = 26
NAME_PAT = re.compile(r'\\"name\\":\\"([^\\"]*?Screening)\\",\\"offers\\":\{\\"@type\\":\\"Offer\\",\\"availability\\":\\"([^\\"]+)\\".*?\\"startDate\\":\\"([^\\"]+)\\"')

def sale_status_for(availability):
    """Map schema.org availability to a short sale status.

    InStock -> on_sale, PreOrder -> presale (tickets not yet on sale),
    anything else/missing -> unknown.
    """
    v = (availability or "").rsplit("/", 1)[-1]
    if v == "InStock":
        return "on_sale"
    if v == "PreOrder":
        return "presale"
    return "unknown"

def log_run(detail):
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(LOG_MD, "a") as f:
        f.write(f"{datetime.now(DENVER).isoformat()} | {detail}\n")

def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def fetch(url, timeout=30):
    last_err = None
    for attempt in range(3):
        try:
            time.sleep(5 + random.uniform(0, 2))
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if raw[:2] == b'\x1f\x8b':
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (429, 403, 500, 502, 503):
                time.sleep(120 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"fetch_failed: {last_err}")

def tuesdays_ahead(n):
    today = datetime.now(DENVER).date()
    days_ahead = (1 - today.weekday()) % 7  # Monday=0; Tuesday=1
    d = today + timedelta(days=days_ahead)
    return [(d + timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(n)]

def clean_title(name):
    t = re.sub(r'\s+\d{1,2}:\d{2}\s*(AM|PM)', '', name)
    t = re.sub(r'\s*(Cinemark XD|RealD 3D)?\s*Screening\s*$', '', t).strip()
    return t

def parse_day(html, date):
    """Return list of {movie, time, iso, sale_status} for XD non-3D screenings after 4pm."""
    out, seen = [], set()
    for m in NAME_PAT.finditer(html):
        name, availability, start = m.group(1), m.group(2), m.group(3)
        if date not in start:
            continue
        if "Cinemark XD" not in name:
            continue
        if "3D" in name:  # user excludes all 3D
            continue
        try:
            dt = datetime.fromisoformat(start)
        except Exception:
            continue
        if dt.hour < 16 or (dt.hour == 16 and dt.minute == 0):
            continue  # strictly after 4:00 PM
        key = (clean_title(name), dt.strftime("%H:%M"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"movie": key[0], "time": dt.strftime("%-I:%M %p"),
                    "iso": dt.isoformat(),
                    "sale_status": sale_status_for(availability)})
    out.sort(key=lambda r: r["iso"])
    return out

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def clean_search_title(movie):
    """Board titles sometimes carry screening suffixes TMDB won't match."""
    return re.sub(r'\s+Insider Screenings\s*$', '', movie, flags=re.I).strip()

def norm_title(t):
    return re.sub(r'[^a-z0-9]', '', (t or '').lower())

def pick_poster(movie, candidates):
    """Best poster URL for a board title from TMDB candidates.

    Prefers an exact title match (most recently released wins, so new
    movies beat older same-name titles); falls back to TMDB's top
    relevance result. Returns None when nothing has a poster.
    """
    if isinstance(candidates, dict):  # error dict from the search helper
        return None
    want = norm_title(movie)
    exact = [c for c in candidates
             if c.get("poster_path")
             and want in (norm_title(c.get("title")),
                          norm_title(c.get("original_title")))]
    if exact:
        exact.sort(key=lambda c: c.get("release_date") or "", reverse=True)
        return TMDB_IMG_BASE + exact[0]["poster_path"]
    for c in candidates:
        if c.get("poster_path"):
            return TMDB_IMG_BASE + c["poster_path"]
    return None

def tmdb_search_candidates(query):
    """TMDB candidates for a title, from either credential route.

    On GitHub Actions the skill helper's vault isn't available, so a
    TMDB_API_KEY env var (repo secret) is used with a direct API call.
    Locally, the tmdb skill helper supplies the key via its own
    credential. Returns a list (possibly empty) or an error dict.
    """
    api_key = os.environ.get("TMDB_API_KEY", "").strip()
    if api_key:
        try:
            params = urllib.parse.urlencode({
                "query": query, "include_adult": "false",
                "language": "en-US", "page": "1", "api_key": api_key})
            req = urllib.request.Request(
                "https://api.themoviedb.org/3/search/movie?" + params,
                headers={"User-Agent": "cinemark-watcher/1.0",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            return data.get("results", [])
        except Exception as e:
            return {"error": f"tmdb api failed: {type(e).__name__}"}
    sys.path.insert(0, os.path.expanduser("~/workspace/skills/tmdb/bin"))
    from tmdb_search import search_candidates
    return search_candidates(query)

def refresh_missing_posters(movies):
    """Fill tmdb_posters.json for board titles that lack a poster.

    Self-learning: new hits are persisted, so each title is looked up
    once. Best-effort: a lookup failure just leaves that movie
    poster-less for this run; it never breaks the watcher.
    Returns the full title -> poster-URL mapping.
    """
    mapping = load_json(POSTERS_JSON) or {}
    missing = sorted(m for m in movies if m not in mapping)
    if not missing:
        return mapping
    for m in missing:
        try:
            poster = pick_poster(m, tmdb_search_candidates(clean_search_title(m)))
        except Exception as e:
            log_run(f"posters | {m}: lookup failed ({type(e).__name__})")
            continue
        if poster:
            mapping[m] = poster
            log_run(f"posters | backfilled {m}")
        else:
            log_run(f"posters | {m}: no confident TMDB match")
    atomic_write(POSTERS_JSON, mapping)
    return mapping

def sync_gist_board():
    """Push latest.json to the GitHub gist so the public board stays fresh.

    Skipped on GitHub Actions runners: there the board reads showtimes.json
    straight from the repo, so no gist push is needed (and this machine's
    credential helper doesn't exist there anyway). Best-effort everywhere
    else: a gist failure is logged but never breaks the run.
    """
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return "skipped on GitHub Actions (board reads repo file)"
    try:
        with open(GIST_ID_FILE) as f:
            gist_id = f.read().strip()
        if not gist_id:
            return "no gist_id configured, skipped"
        sys.path.insert(0, os.path.expanduser("~/workspace/skills/github/bin"))
        from gist_sync import sync_gist
        res = sync_gist(LATEST_JSON, gist_id)
        return f"gist {res['gist_id']} updated={res['updated']}"
    except Exception as e:
        return f"gist sync failed: {type(e).__name__}: {e}"

def ntfy_topic():
    """Return the configured ntfy topic, or None.

    CINEMARK_NTFY_TOPIC env var wins; otherwise the first line of
    ntfy_topic.txt in the workspace. Topics are public-by-default on
    ntfy.sh, so this must be an unguessable random string.
    """
    topic = os.environ.get("CINEMARK_NTFY_TOPIC", "").strip()
    if topic:
        return topic
    try:
        with open(NTFY_TOPIC_FILE) as f:
            topic = f.read().strip().split()[0]
            return topic or None
    except Exception:
        return None

def send_ntfy(title, message):
    """POST a push notification to the configured ntfy topic.

    Returns (ok, status). ok is True only when ntfy.sh accepted the
    message. Best-effort: failures are reported, never raised.
    """
    topic = ntfy_topic()
    if not topic:
        return False, "not configured (set CINEMARK_NTFY_TOPIC or ntfy_topic.txt)"
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/" + topic,
            data=message.encode("utf-8"),
            method="POST",
            headers={"Title": title, "Priority": "default",
                     "Tags": "film_projector"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return True, f"sent (HTTP {resp.status})"
    except Exception as e:
        return False, f"send failed: {type(e).__name__}: {e}"

def main():
    os.makedirs(WORKSPACE, exist_ok=True)
    checked_at = datetime.now(DENVER).isoformat()
    prev_latest = load_json(LATEST_JSON) or {}
    prev_by_date = {d["tuesday_date"]: d for d in prev_latest.get("tuesdays", [])}
    # first-seen timestamps keyed by (date, movie, time); carried forward so
    # the board can sort by when each showtime first appeared.
    prev_first_seen = {}
    for d in prev_latest.get("tuesdays", []):
        for s in d["showtimes"]:
            if s.get("first_seen"):
                prev_first_seen[(d["tuesday_date"], s["movie"],
                                 s["time"])] = s["first_seen"]

    dates = tuesdays_ahead(PROBE_WEEKS)
    print(f"Probing {len(dates)} Tuesdays...")
    tuesdays_out, errors = [], []
    for td in dates:
        try:
            html = fetch(f"{THEATER_URL}?showDate={td}")
        except Exception as e:
            errors.append(f"{td}: {e}")
            # fail-closed: keep previous entry, mark stale
            if td in prev_by_date:
                stale = dict(prev_by_date[td])
                stale["stale"] = True
                tuesdays_out.append(stale)
            print(f"  {td}: FETCH FAILED, kept previous ({e})")
            continue
        shows = parse_day(html, td)
        for s in shows:
            s["auditorium"] = "10/11"
            key = (td, s["movie"], s["time"])
            s["first_seen"] = prev_first_seen.get(key, checked_at)
        if shows:
            tuesdays_out.append({"tuesday_date": td, "showtimes": shows,
                                 "count": len(shows)})
        print(f"  {td}: {len(shows)} stadium showtimes")

    if not tuesdays_out and prev_latest.get("tuesdays"):
        # total failure: keep previous good data
        log_run("run_failed_all_dates | kept previous latest.json")
        print("ALL FETCHES FAILED - kept previous data", file=sys.stderr)
        sys.exit(2)

    # Historical retention: past Tuesdays (strictly before today) are carried
    # forward from the previous run so they are never deleted. The board
    # hides them by default and reveals them behind its "show historical
    # showtimes" setting. Only past dates are retained here — a future
    # Tuesday that vanishes from the site must still surface as "removed"
    # in the diff, so future dates are never carried forward.
    today_str = datetime.now(DENVER).date().isoformat()
    seen_dates = {t["tuesday_date"] for t in tuesdays_out}
    for d in prev_latest.get("tuesdays", []):
        td = d["tuesday_date"]
        if td not in seen_dates and td < today_str:
            tuesdays_out.append(d)
            seen_dates.add(td)
    tuesdays_out.sort(key=lambda d: d["tuesday_date"])

    # posters: backfill any new titles from TMDB, then attach to all entries
    posters = refresh_missing_posters(
        {s["movie"] for d in tuesdays_out for s in d["showtimes"]})
    for d in tuesdays_out:
        for s in d["showtimes"]:
            s["poster"] = posters.get(s["movie"])

    output = {
        "theater": "Cinemark Spanish Fork and XD",
        "theater_id": "1153",
        "checked_at": checked_at,
        "filter": "Tuesdays, strictly after 4:00 PM, Cinemark XD (auditoriums 10/11), no 3D",
        "tuesdays": tuesdays_out,
        "total_tuesdays": len(tuesdays_out),
        "total_showtimes": sum(d["count"] for d in tuesdays_out),
        "errors": errors,
    }
    atomic_write(LATEST_JSON, output)
    atomic_write(SHOWTIMES_JSON, output)  # board feed (used on Actions)
    atomic_write(ALL_JSON, output)
    with open(CSV_PATH + ".tmp", "w") as f:
        f.write("tuesday_date,movie,time,auditorium\n")
        for d in tuesdays_out:
            for s in d["showtimes"]:
                mv = '"' + s["movie"].replace('"', '""') + '"'
                f.write(f'{d["tuesday_date"]},{mv},{s["time"]},{s["auditorium"]}\n')
    os.replace(CSV_PATH + ".tmp", CSV_PATH)

    # diff vs last notified baseline
    baseline = load_json(NOTIFIED_JSON) or {}
    base_set = {(d["tuesday_date"], s["movie"], s["time"])
                for d in baseline.get("tuesdays", []) for s in d["showtimes"]}
    cur_set = {(d["tuesday_date"], s["movie"], s["time"])
               for d in tuesdays_out for s in d["showtimes"]}
    new = sorted(cur_set - base_set)
    removed = sorted(base_set - cur_set)
    # sale-status flips: only when the baseline already recorded a status
    # (entries predating the sale_status field can't flip yet).
    base_status = {(d["tuesday_date"], s["movie"], s["time"]): s.get("sale_status")
                   for d in baseline.get("tuesdays", []) for s in d["showtimes"]}
    cur_status = {(d["tuesday_date"], s["movie"], s["time"]): s.get("sale_status")
                  for d in tuesdays_out for s in d["showtimes"]}
    now_on_sale = sorted(k for k in cur_status
                         if k in base_status
                         and base_status[k] == "presale"
                         and cur_status[k] == "on_sale")
    no_longer_on_sale = sorted(k for k in cur_status
                               if k in base_status
                               and base_status[k] == "on_sale"
                               and cur_status[k] == "presale")
    def entry(t, m, ti):
        return {"tuesday_date": t, "movie": m, "time": ti}
    diff = {"checked_at": checked_at,
            "new_or_changed": [entry(t, m, ti) for (t, m, ti) in new],
            "removed": [entry(t, m, ti) for (t, m, ti) in removed],
            "now_on_sale": [entry(t, m, ti) for (t, m, ti) in now_on_sale],
            "no_longer_on_sale": [entry(t, m, ti) for (t, m, ti) in no_longer_on_sale]}
    atomic_write(DIFF_JSON, diff)

    # notifications: the script pushes to ntfy itself (the user reads
    # alerts in the ntfy phone app instead of chat messages). The
    # last_notified baseline advances only after the user has actually
    # been told; a failed or unconfigured send leaves the baseline
    # untouched so nothing is silently swallowed.
    notify_lines = []
    for (t, m, ti) in new:
        notify_lines.append(f"NEW  {t}  {m} at {ti}")
    for (t, m, ti) in now_on_sale:
        notify_lines.append(f"ON SALE  {t}  {m} at {ti}")
    if notify_lines:
        ok, ntfy_status = send_ntfy("Cinemark Tuesday showtimes",
                                     "\n".join(notify_lines))
        log_run(f"ntfy | {ntfy_status}")
        print(f"ntfy: {ntfy_status}")
        if ok:
            atomic_write(NOTIFIED_JSON, output)  # user has been told
    elif removed or no_longer_on_sale:
        # nothing worth a push, but the baseline must still move on
        atomic_write(NOTIFIED_JSON, output)

    # TEMPORARY heartbeat for ntfy delivery testing — REMOVE after the
    # user confirms the phone alert arrives. Sends a plain "run
    # completed" ping (no fake showtimes) so delivery can be verified.
    hb_ok, hb_status = send_ntfy(
        "Cinemark watcher run completed",
        f"Daily check finished: {len(tuesdays_out)} Tuesdays, "
        f"{output['total_showtimes']} showtimes checked. "
        f"{len(new)} new, {len(now_on_sale)} newly on sale.")
    log_run(f"ntfy-heartbeat | {hb_status}")
    print(f"ntfy heartbeat: {hb_status}")

    log_run(f"ok | {len(tuesdays_out)} Tuesdays | "
            f"{output['total_showtimes']} showtimes | "
            f"+{len(new)} -{len(removed)} vs notified | errors={len(errors)}")
    gist_status = sync_gist_board()
    log_run(f"gist | {gist_status}")
    print(f"Gist: {gist_status}")
    print(f"\nDone: {len(tuesdays_out)} Tuesdays, {output['total_showtimes']} showtimes")
    print(f"New/changed vs last notified: {len(new)}, removed: {len(removed)}, "
          f"now on sale: {len(now_on_sale)}, no longer on sale: {len(no_longer_on_sale)}")
    if errors:
        print(f"Fetch errors: {len(errors)} (previous data kept, marked stale)")

if __name__ == "__main__":
    main()
