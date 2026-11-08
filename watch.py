#!/usr/bin/env python3
"""Notify (macOS) when a watched manga page lists a chapter it didn't list last run."""
import email.utils, html, json, os, re, shutil, subprocess, sys, threading, time, unicodedata, urllib.error, urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

HERE = Path(__file__).parent
STATE = HERE / os.environ.get("MANGA_STATE", "state.json")  # GitHub Actions keeps its own state file
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def chapters(url, html):
    """{chapter id: chapter link} on a series page: sub-links of the page (Madara theme) + data-num items (MangaReader theme)."""
    base = unquote(url).rstrip("/") + "/"
    out, placeholders = {}, set()
    for h, text in re.findall(r'href="([^"#]+)"[^>]*>([^<]*)', html):
        l = unquote(h).rstrip("/")
        if l.startswith(base) and "/feed" not in l:
            out[l[len(base):]] = h
            if "อัพเดท" in text:  # manga-lc lists the next chapter as an "update" placeholder before it is readable
                placeholders.add(l[len(base):])
    for c in placeholders:
        del out[c]
    # ponytail: takes the first link within 300 chars of the data-num tag; use an HTML parser if a theme nests differently
    for num, h in re.findall(r'data-num="([^"{]+)"[^>]*>.{0,300}?href="([^"#]+)"', html, re.S):
        out[num] = h
    return out


WEBTOON_EP = re.compile(r"(?:(?:\bS{1,2}|Season\s*)(\d+)\]?\s*)?Ep\.?\s*(\d+(?:\.\d+)?)", re.I)


def webtoon_ep(title):
    """'S2 Ep.12' -> (2, 12.0), 'SS2 Ep.10' -> (2, 10.0), '[Season 2] Ep.163' -> (2, 163.0), 'Ep.215' -> (1, 215.0); None for a
    notice or a review."""
    found = WEBTOON_EP.search(title)
    return (int(found[1] or 1), float(found[2])) if found else None


def webtoon_rows(list_url, page):
    """[(WEBTOON episode number, title)] on one page of a series' episode list (newest first, 10 a page)."""
    return [(int(n), html.unescape(t).strip()) for n, t in re.findall(r'data-episode-no="(\d+)".*?class="subj"><span>([^<]*)', get(f"{list_url}&page={page}"), re.S)]


def season_start(list_url, season, newest_no, ep, seasons):
    """How many chapters came before this season (Juvenile Prison's S2 Ep.1 is chapter 101: 100), from the list: the
    chapter just before the season's first episode. Kept per series in seasons ({season: chapters before})."""
    if season <= 1:
        return 0
    if str(season) not in seasons:
        page, rows = max(1, int(ep) // 10), []
        for page in range(page, page + 6):  # the season's first episode is about ep episodes back, 10 a page
            rows += webtoon_rows(list_url, page)
            older = [(n, webtoon_ep(t)) for n, t in rows if webtoon_ep(t) and webtoon_ep(t)[0] < season]
            if older:
                n, (s_prev, last) = max(older)
                seasons[str(season)] = season_start(list_url, s_prev, n, last, seasons) + last
                break
        else:
            raise RuntimeError(f"season {season}'s start not found")
    return seasons[str(season)]


def webtoon_chapters(url, state):
    """LINE Webtoon: {"EP.112": 112.0}, each free episode's chapter number counted on through the seasons as the manga sites
    count it (Juvenile Prison's "S2 Ep.12" is chapter 112); WEBTOON's own episode number counts notices and extras too.
    Episodes locked behind coins or ads exist only in the WEBTOON app, so they aren't counted."""
    list_url = url.replace("://m.webtoons.com", "://www.webtoons.com").replace("/rss?", "/list?")
    rss = get(webtoon_rss(url))
    seasons = state.setdefault("webtoon_seasons", {}).setdefault(re.search(r"title_no=(\d+)", url)[1], {})
    out = {}
    for title, n in re.findall(r"<item>\s*<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>.*?episode_no=(\d+)", rss, re.S):
        found = webtoon_ep(html.unescape(title))
        if found:
            out[f"EP.{n}"] = season_start(list_url, found[0], int(n), found[1], seasons) + found[1]
    return out


def chapter_runs(nums):
    """[340, 341, ..., 345] -> "Chapter 340-345": a run of chapters in a row as one range, gaps and parts (9.1) apart."""
    runs = []
    for n in sorted(nums):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return "Chapter " + ", ".join(f"{a:g}" if a == b else f"{a:g}-{b:g}" for a, b in runs)


def chapter_number(c):
    """'ซีซั่น-1/ตอนที่-226' -> 226.0, '9_1' -> 9.1: a manga site's chapter id as a number."""
    found = re.search(r"\d+(?:_\d+)?", re.sub(r"^ซี่?ซั่น-\d+/", "", c))
    return float(found[0].replace("_", ".")) if found else 0.0


def webtoon_rss(url):
    """A WEBTOON series' RSS feed (its latest free episodes), from its list or feed address."""
    return url.replace("://m.webtoons.com", "://www.webtoons.com").replace("/list?", "/rss?")


def webtoon(url):
    """LINE Webtoon: {"EP.216": link} from the series' RSS feed, which lists its latest free episodes."""
    rss = get(webtoon_rss(url))
    return {f"EP.{n}": link.replace("&amp;", "&") for link, n in re.findall(r"<link>([^<]*episode_no=(\d+))</link>", rss)}


SLOW = set()  # sites that timed out this run: skipped for the rest of it, so one dead site can't hold the run up for 15 minutes
EARLY = {}  # url -> the page being read in the background since the run began (prefetch); get() waits for it
POOL = ThreadPoolExecutor(8)
SITES = {}  # host -> at most 3 requests to one site at a time


def prefetch(urls):
    """Start reading these pages now, eight at a time, while the run does other things: each is read once, and get() takes it."""
    for url in urls:
        EARLY.setdefault(url, POOL.submit(fetch, url))


def get(url):
    early = EARLY.get(url)
    return early.result() if early else fetch(url)


def fetch(url):
    host = urlparse(url).netloc
    if host in SLOW:
        raise TimeoutError(f"{host} skipped: it timed out earlier in this run")
    req = urllib.request.Request(quote(url, safe=":/%?&="), headers={"User-Agent": UA})
    try:
        with SITES.setdefault(host, threading.Semaphore(3)):
            return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    except (TimeoutError, urllib.error.URLError) as e:
        if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            SLOW.add(host)
        raise


CARD_KIND = {"New episode": ("NEW EPISODE", "Watch"), "Watch now": ("WATCH NOW", "Watch"), "New special": ("NEW SPECIAL", "Watch"),
             "New chapter": ("NEW CHAPTER", "Read")}


def card(title, msg, image, uri=None):
    """A LINE card in the anime website's style: one white rounded card, the cover tall with rounded corners, a small
    red label (NEW EPISODE, NEW CHAPTER), the title, the details in grey, and a Watch or Read pill. Tapping anywhere
    opens uri, if given."""
    kind, _, name = title.partition(": ")
    label, verb = CARD_KIND.get(kind, (kind.upper(), "Open")) if name else ("", "Open")
    t = lambda x, **k: {"type": "text", "text": x, "wrap": True, **k}
    info = ([t(label, size="xxs", weight="bold", color="#DF1B2B")] if label else []) + [
        t(name or title, size="sm", weight="bold", color="#1C1C1E"), t(msg, size="xs", color="#636366")]
    if uri:
        info.append({"type": "box", "layout": "horizontal", "margin": "md", "contents": [
            {"type": "box", "layout": "horizontal", "flex": 0, "cornerRadius": "999px", "backgroundColor": "#FDE8EA", "paddingTop": "6px",
             "paddingBottom": "6px", "paddingStart": "14px", "paddingEnd": "14px", "action": {"type": "uri", "label": verb, "uri": uri},
             "contents": [t(f"▶ {verb}", size="xs", weight="bold", color="#DF1B2B", flex=0, wrap=False)]}]})
    return {"type": "bubble", "size": "kilo", **({"action": {"type": "uri", "label": "open", "uri": uri}} if uri else {}),
            "body": {"type": "box", "layout": "horizontal", "backgroundColor": "#FFFFFF", "paddingAll": "14px", "spacing": "md", "alignItems": "flex-start", "contents": [
                {"type": "box", "layout": "vertical", "width": "64px", "flex": 0, "cornerRadius": "12px", "backgroundColor": "#E9E6EE", "contents": [
                    {"type": "image", "url": image, "aspectMode": "cover", "aspectRatio": "2:3", "size": "full"}]},
                {"type": "box", "layout": "vertical", "spacing": "xs", "contents": info}]}}


def notify(title, msg, links, image=None, uri=None):
    if shutil.which("osascript"):  # absent on the GitHub runner
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title {json.dumps(title)}"])
    token = os.environ.get("LINE_TOKEN")  # phone push: LINE official account broadcast; only set on GitHub
    if not token:
        return
    plain = {"type": "text", "text": f"{title}\n{links}"[:4500]}
    tries = ([{"type": "flex", "altText": f"{title} {msg}"[:400], "contents": card(title, msg, image, uri)}]
             if image and re.search(r"\.(jpe?g|png)$", urlparse(image).path, re.I) else [])  # LINE cards show JPEG/PNG only + [plain]
    for message in tries:  # the card first when there is a cover; plain text if LINE refuses it, so the alert is never lost
        if line_send(token, message):
            return
    if "ping" in sys.argv:
        sys.exit(1)


def line_send(token, message):
    """Broadcast one message to the Alert Watch LINE account; False if LINE refused it."""
    # compact: LINE limits each card to 30 KB, and the F1 standings with 22 photos come close
    req = urllib.request.Request("https://api.line.me/v2/bot/message/broadcast", data=json.dumps({"messages": [message]}, separators=(",", ":")).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        print(f"FAIL line ({message['type']}): {e} {getattr(e, 'read', lambda: b'')()[:300]}", file=sys.stderr)
        return False


OPENF1 = "https://api.openf1.org/v1"  # free F1 timing data: every session's results, driver photos and team colours
# the official F1 logo for card headers (a copy of formula1.com's red logo on the GitHub Pages site)
F1_LOGO_IMAGE = {"type": "image", "url": "https://isaraapae.github.io/alert-watch/f1-logo.png", "size": "64px", "aspectRatio": "4:1", "aspectMode": "fit", "flex": 0}


def f1_time(sec):
    """92.274 -> "1:32.274", 6120.785 -> "1:42:00.785"."""
    h, rem = divmod(sec, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{sec:06.3f}" if h else f"{int(m)}:{sec:06.3f}"


def f1_total(sec):
    """A race's total time, hours to thousandths all on colons: 8428.772 -> "2:20:28:772", 6120.785 -> "1:42:00:785"."""
    ms = round(sec * 1000)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    return f"{h}:{m:02d}:{rem // 1000:02d}:{rem % 1000:03d}" if h else f"{m}:{rem // 1000:02d}:{rem % 1000:03d}"


def f1_rows(results, drivers):
    """[(position, driver, time)] in finishing order: a race's time, gaps and laps down; a qualifying's lap in the last
    part the driver reached (Q3 for the top 10); a practice's best lap and gaps; DNF, DNS or DSQ."""
    rank = lambda x: x["position"] if isinstance(x.get("position"), int) else 99
    rows = []
    for x in sorted(results, key=rank):
        out = next((k.upper() for k in ("dsq", "dns", "dnf") if x.get(k)), None)
        pos = str(x["position"]) if isinstance(x.get("position"), int) else "NC"  # "DNF" doesn't fit the column; the reason is in the time
        dur, gap = x.get("duration"), x.get("gap_to_leader")
        if isinstance(dur, list):  # qualifying: Q1, Q2, Q3
            lap = next((v for v in reversed(dur) if v), None)
            val = f1_time(lap) if lap else out or "No time"
        elif out:
            val = out
        elif not gap and dur:
            val = f1_total(dur) if dur >= 600 else f1_time(dur)  # a race's total time, or a practice's best lap
        elif isinstance(gap, (int, float)):
            val = f"+{gap:.3f}s"
        else:
            val = str(gap or "").replace("LAPS", "Laps").replace("LAP", "Lap") or "No time"  # LINE refuses a card with an empty text
        rows.append((pos, drivers.get(x["driver_number"], {}), val))
    return rows


# drivers formula1.com has no photo of in a season (checked for 2024-2026): the year and team of the photo to use instead,
# as on the F1 Live site (2024: Doohan, Alpine's stand-in for the last race only; his 2025 Alpine photo)
F1_PHOTO_FROM = {(2024, "jacdoo01"): (2025, "alpine")}


def f1_photo(d, px):
    """A driver's face as PNG, from formula1.com's portraits for the current season in their current team's suit
    (OpenF1's own photo links are last season's, in old team suits, and rookies have none)."""
    code = re.search(r"/([A-Z]{6}\d{2})_", d.get("headshot_url") or "")
    if code:
        code = code[1].lower()
    else:  # formula1.com's code: 3 letters of the first name, 3 of the last, 01 (Isack HADJAR: isahad01)
        words = unicodedata.normalize("NFKD", d.get("full_name") or "").encode("ascii", "ignore").decode().split()
        code = f"{words[0][:3]}{words[-1][:3]}01".lower() if len(words) > 1 else ""
    team = re.sub(r"[^a-z0-9]", "", (d.get("team_name") or "").lower())
    if not code or not team:
        return None
    year = d.get("year") or datetime.now(THAI).year
    year, team = F1_PHOTO_FROM.get((year, code), (year, team))
    # c_thumb,g_face: cropped to the face; d_: formula1.com's blank driver if the portrait is missing, never a broken image
    return (f"https://media.formula1.com/image/upload/c_thumb,g_face,z_0.6,w_{px},h_{px}/d_common:f1:{year}:fallback:driver:"
            f"{year}fallbackdriverright.webp/common/f1/{year}/{team}/{code}/{year}{team}{code}right.png")


def f1_logo(team, year):
    """A team's logo for the season as PNG, in its own colours for the cards' light background (as the F1 Live site uses in
    light mode), from formula1.com (Red Bull Racing: .../2026/redbullracing/2026redbullracinglogo)."""
    slug = re.sub(r"[^a-z0-9]", "", (team or "").lower())
    return f"https://media.formula1.com/image/upload/c_fit,w_96,h_96/common/f1/{year}/{slug}/{year}{slug}logo.png" if slug else None


def f1_name(d):
    """"Andrea Kimi ANTONELLI" -> "A. Antonelli"."""
    words = (d.get("full_name") or d.get("name_acronym") or "?").split()
    last = [w for w in words if w.isupper() and len(w) > 1] or words[-1:]
    first = [w for w in words if w not in last]
    return (f"{first[0][0]}. " if first else "") + " ".join(last).title()


def f1_drivers(s, wanted=()):
    """{car number: driver} for a session. OpenF1 can leave a driver out of a session (Lawson, Mexico 2026 Practice 3) or blank
    his name and team (Hirakawa, Ocon's stand-in, in that race), so those come from the weekend's other sessions."""
    drivers = {d["driver_number"]: {**d, "year": s.get("year")} for d in json.loads(get(f"{OPENF1}/drivers?session_key={s['session_key']}"))}
    blank = {n for n, d in drivers.items() if not d.get("team_name")} | (set(wanted) - set(drivers))
    if blank:
        for d in json.loads(get(f"{OPENF1}/drivers?meeting_key={s['meeting_key']}")):
            if d["driver_number"] in blank and d.get("team_name"):
                drivers[d["driver_number"]] = {**d, "year": s.get("year")}
    return drivers


# the F1 Live site's light look: white panels on light grey, team-colour driver tags (car number on the team colour, the
# name tinted with it)
F1C = {"bg": "#EEF0F4", "panel": "#FFFFFF", "row": "#F5F6F9", "line": "#E4E6EC", "text": "#111118", "dim": "#6B6F7C",
       "red": "#E10600", "green": "#16A34A", "purple": "#9333EA"}


def f1_mix(a, b, share):
    """a mixed into b: share of a (0-1), like the page's color-mix."""
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(f"{round(int(a[i:i + 2], 16) * share + int(b[i:i + 2], 16) * (1 - share)):02X}" for i in (0, 2, 4))


def f1_ink(hexc):
    """Black or white text on a team colour, as the page picks it."""
    h = hexc.lstrip("#"); r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#000000" if (r * 299 + g * 587 + b * 114) / 1000 > 165 else "#FFFFFF"


def f1_tag(d, size="xs"):
    """The page's driver tag: [ 81 | PIA ], the number on the team colour, the name tinted with it."""
    c = "#" + (d.get("team_colour") or "777777")
    num = str(d.get("driver_number") or "")
    t = lambda text, **k: {"type": "text", "text": text or "?", "size": size, "weight": "bold", "align": "center", "gravity": "center", **k}
    return {"type": "box", "layout": "horizontal", "flex": 0, "cornerRadius": "4px", "height": "20px", "contents": [
        {"type": "box", "layout": "vertical", "backgroundColor": c, "width": "26px", "contents": [t(num, color=f1_ink(c))]},
        {"type": "box", "layout": "vertical", "backgroundColor": f1_mix(c, F1C["panel"], .14), "width": "38px",
         "contents": [t(d.get("name_acronym"), color=f1_mix(c, "#000000", .78))]}]}


def f1_head(meeting, title, sub):
    """The header: F1 logo, the Grand Prix and its flag, a big title, a grey line under it."""
    t = lambda text, **k: {"type": "text", "text": text, **k}
    return {"type": "box", "layout": "vertical", "backgroundColor": F1C["bg"], "spacing": "xs", "paddingAll": "16px", "contents": [
        {"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center", "contents": [
            F1_LOGO_IMAGE,
            t(meeting.get("meeting_name") or "Formula 1", size="xs", color=F1C["dim"], flex=1, wrap=True),
            *([{"type": "image", "url": meeting["country_flag"], "size": "xxs", "aspectRatio": "16:9", "flex": 0}]
              if (meeting.get("country_flag") or "").endswith(".png") else [])]},
        t(title, size="xl", weight="bold", color=F1C["text"], wrap=True), t(sub, size="xxs", color=F1C["dim"], wrap=True)]}


def f1_button(label, uri):
    return {"type": "box", "layout": "vertical", "backgroundColor": F1C["panel"], "paddingAll": "12px", "contents": [
        {"type": "button", "style": "primary", "color": F1C["red"], "height": "sm", "action": {"type": "uri", "label": label, "uri": uri}}]}


def f1_buttons(*buttons):
    """Buttons side by side, the first red and the others plain: [(label, uri)]."""
    return {"type": "box", "layout": "horizontal", "spacing": "sm", "backgroundColor": F1C["panel"], "paddingAll": "12px", "contents": [
        {"type": "button", "style": "primary" if not i else "secondary", "height": "sm", **({"color": F1C["red"]} if not i else {}),
         "action": {"type": "uri", "label": label, "uri": uri}} for i, (label, uri) in enumerate(buttons)]}


def f1_full(d):
    """"Andrea Kimi ANTONELLI" -> "Andrea Kimi Antonelli"."""
    return " ".join(w.title() if w.isupper() and len(w) > 1 else w for w in (d.get("full_name") or d.get("name_acronym") or "?").split())


def f1_pos(pos):
    """A position two digits wide ("1" padded with a figure space, as wide as a digit), so what follows it lines up."""
    return str(pos).rjust(2, "\u2007")


def f1_badge(n):
    """▲3 on green (places gained), ▼5 on red (places lost)."""
    c = F1C["green"] if n > 0 else F1C["red"]
    return {"type": "box", "layout": "vertical", "flex": 0, "cornerRadius": "4px", "paddingStart": "4px", "paddingEnd": "4px",
            "backgroundColor": f1_mix(c, F1C["panel"], .14), "contents": [
        {"type": "text", "text": f"{'▲' if n > 0 else '▼'}{abs(n)}", "size": "xxs", "weight": "bold", "color": c}]}


def f1_row(moved, contents, **box):
    """A row in the cards' style: rounded, on the row grey, green outlined if it moved up, red if down. Every row has the
    1px border, unmoved ones in their own colour, so an outline never shifts what's inside."""
    edge = F1C["green"] if moved > 0 else F1C["red"]
    fill = f1_mix(edge, F1C["panel"], .08) if moved else F1C["row"]
    return {"type": "box", "layout": "horizontal", "cornerRadius": "6px", "backgroundColor": fill, "borderWidth": "1px",
            "borderColor": f1_mix(edge, F1C["panel"], .5) if moved else fill, **box, "contents": contents}


def f1_stripe(colour, height=None):
    return {"type": "box", "layout": "vertical", "width": "4px", "flex": 0, **({"height": height} if height else {}), "backgroundColor": colour, "contents": []}


def f1_changes(title, changes):
    """Who moved, under a red heading, in the classification's rows: each on their team's stripe with code, car number and
    full name, then P13 struck through -> P10 and the points it changed, outlined green if they gained and red if they lost.
    changes: [(driver, old position, new position, points gained)]."""
    t = lambda text, **k: {"type": "text", "text": text, **k}
    place = lambda p: f"P{p}" if isinstance(p, int) else "NC"

    def line(d, old, new, pts):
        c = "#" + (d.get("team_colour") or "777777")
        gained = old - new if isinstance(old, int) and isinstance(new, int) else -1 if isinstance(old, int) else 1
        return f1_row(gained, [f1_stripe(c), {"type": "box", "layout": "baseline", "spacing": "sm", "paddingAll": "6px", "contents": [
            t(d.get("name_acronym") or str(d.get("driver_number") or "?"), size="sm", weight="bold", color=F1C["text"], flex=0),
            t(str(d.get("driver_number") or ""), size="xxs", color=f1_mix(c, "#000000", .8), flex=0),
            t(f1_full(d), size="xxs", color=F1C["dim"], flex=1),
            t(place(old), size="xxs", color=F1C["dim"], decoration="line-through", flex=0),
            t("→", size="xxs", color=F1C["dim"], flex=0),
            t(place(new), size="xs", weight="bold", color=F1C["text"], flex=0),
            *([t(f"{'+' if pts > 0 else ''}{pts:g} pt{'s' if abs(pts) != 1 else ''}", size="xxs", weight="bold",
                 color=F1C["green"] if pts > 0 else F1C["red"], flex=0)] if pts else [])]}], margin="xs")
    return {"type": "box", "layout": "vertical", "contents": [t(title, size="xxs", weight="bold", color=F1C["red"])] + [line(*c) for c in changes[:8]]}


def f1_card(meeting, session, when, rows, replay_key=None, note=None, changes=None):
    """A session's result card: the top 3's faces (transparent, a team-colour bar under each), then everyone on their team's
    stripe: position, code, car number, full name, time. After a change after the flag (changes), a "Result changed" panel
    titled note on top and the drivers who moved outlined, green up and red down; buttons open the session's replay and its
    full result on the site (which follows the official result, penalties after the flag included)."""
    t = lambda text, **k: {"type": "text", "text": text, **k}
    moved = {d.get("driver_number"): (old - new if isinstance(old, int) and isinstance(new, int) else 0) for d, old, new, _ in changes or []}

    def podium(pos, d, val):
        photo, c = f1_photo(d, 300), "#" + (d.get("team_colour") or "777777")
        return {"type": "box", "layout": "vertical", "flex": 1, "spacing": "xs", "contents": [
            {"type": "image", "url": photo, "aspectMode": "fit", "aspectRatio": "1:1", "size": "full"} if photo else
            {"type": "box", "layout": "vertical", "height": "80px", "contents": []},
            {"type": "box", "layout": "vertical", "height": "3px", "cornerRadius": "2px", "backgroundColor": c, "contents": []},
            {"type": "box", "layout": "baseline", "spacing": "xs", "margin": "sm", "contents": [
                t(pos, size="sm", weight="bold", color=F1C["red"] if pos == "1" else F1C["dim"], flex=0),
                t(d.get("name_acronym") or "?", size="sm", weight="bold", color=F1C["text"], flex=0),
                t(str(d.get("driver_number") or ""), size="xxs", color=f1_mix(c, "#000000", .8), flex=0)]},
            t(f1_full(d), size="xxs", color=F1C["text"], wrap=True), t(val, size="xxs", color=F1C["dim"], wrap=True)]}

    def row(pos, d, val):
        c = "#" + (d.get("team_colour") or "777777")
        return f1_row(moved.get(d.get("driver_number"), 0), [f1_stripe(c), {"type": "box", "layout": "baseline", "spacing": "sm", "paddingAll": "6px", "contents": [
            t(f1_pos(pos), size="xs", weight="bold", color=F1C["dim"], flex=0),
            t(d.get("name_acronym") or "?", size="sm", weight="bold", color=F1C["text"], flex=0),
            t(str(d.get("driver_number") or ""), size="xxs", color=f1_mix(c, "#000000", .8), flex=0),
            t(f1_full(d), size="xxs", color=F1C["dim"], flex=1),
            t(val, size="xs", weight="bold", color=F1C["text"], flex=0)]}], margin="xs")

    top = [rows[i] for i in (1, 0, 2) if i < len(rows)] if len(rows) >= 3 else rows[:3]   # a podium: P2, P1 in the middle, P3
    head = [f1_changes(note or "RESULT CHANGED", changes)] if changes else [t(note, size="xs", weight="bold", color=F1C["red"], wrap=True)] if note else []
    body = head + [
        {"type": "box", "layout": "horizontal", "spacing": "md", "margin": "lg" if head else "none", "contents": [podium(*r) for r in top]},
        t("CLASSIFICATION", size="xxs", weight="bold", color=F1C["dim"], margin="lg"),
        {"type": "box", "layout": "vertical", "margin": "xs", "contents": [row(*r) for r in rows[3:]]}]
    return {"type": "bubble", "size": "mega", "header": f1_head(meeting, session, when),
            "body": {"type": "box", "layout": "vertical", "backgroundColor": F1C["panel"], "paddingAll": "12px", "contents": body},
            **({"footer": f1_buttons(("Watch replay", f"{F1_SITE}?replay={replay_key}"), ("Full result", f"{F1_SITE}?result={replay_key}"))}
               if replay_key else {})}


def f1_standings_card(title, sub_title, rows, colour_of):
    """A championship table: each one on their team's stripe, position, face (transparent) or team logo, full name and team,
    ▲/▼ places since before the race (outlined green or red), points and points gained."""
    t = lambda text, **k: {"type": "text", "text": text, **k}

    def row(pos, move, colour, photo, name, team, points, gained):
        return f1_row(move, [
            f1_stripe(colour, "38px"),
            t(f1_pos(pos), size="xs", weight="bold", color=F1C["dim"], flex=0),
            *([{"type": "image", "url": photo, "size": "26px", "flex": 0, **({} if "logo" in photo else {"aspectMode": "cover"})}] if photo else []),
            {"type": "box", "layout": "vertical", "contents": [t(name, size="xs", weight="bold", color=F1C["text"]), *([t(team, size="xxs", color=F1C["dim"])] if team else [])]},
            *([f1_badge(move)] if move else []),
            {"type": "box", "layout": "vertical", "flex": 0, "contents": [
                t(f"{points:g}", size="xs", weight="bold", color=F1C["text"], align="end"),
                *([t(f"+{gained:g}", size="xxs", color=F1C["green"], align="end")] if gained else [])]}],
            spacing="sm", alignItems="center", margin="xs", paddingEnd="6px")

    return {"type": "bubble", "size": "mega", "header": f1_head({"meeting_name": sub_title}, title, "Points after the race · ▲▼ places since before it"),
            "body": {"type": "box", "layout": "vertical", "backgroundColor": F1C["panel"], "paddingAll": "12px", "contents": [row(*r) for r in rows]}}


def f1_standings(state, token, s, meeting, again=False):
    """After a race: the drivers' and teams' championships as two swipeable cards, sent once (when OpenF1 has them), and
    again (again=True) when the race's result changes after the flag. The points are each one's points before the race plus
    their points in its result: OpenF1's own championship can stay on the result from before post-race penalties (Mexico
    2026: Hirakawa 0 points, Piastri 150, while the result had Hirakawa 10th with 1 and Piastri out of the points)."""
    sent = state.setdefault("f1_standings", [])
    key = str(s["session_key"])
    if key in sent and not again:
        return
    try:
        champ_d = json.loads(get(f"{OPENF1}/championship_drivers?session_key={key}"))
        champ_t = json.loads(get(f"{OPENF1}/championship_teams?session_key={key}"))
        if not champ_d or not champ_t:
            return  # not published yet: tried again on the next check
        results = json.loads(get(f"{OPENF1}/session_result?session_key={key}"))
        drivers = f1_drivers(s, {x["driver_number"] for x in results})
    except Exception as e:
        print(f"FAIL f1 standings: {e}", file=sys.stderr)
        return
    for n in {c["driver_number"] for c in champ_d} - set(drivers):  # scored earlier in the season, not in this race
        try:
            entries = json.loads(get(f"{OPENF1}/drivers?driver_number={n}")) or [{}]  # latest team; an older entry has the photo code
            drivers[n] = {**entries[-1], "year": s.get("year"),
                          "headshot_url": next((x["headshot_url"] for x in reversed(entries) if x.get("headshot_url")), None)}
        except Exception as e:
            print(f"FAIL f1 driver {n}: {e}", file=sys.stderr)
            drivers[n] = {"full_name": f"#{n}"}
    colour = {d.get("team_name"): "#" + (d.get("team_colour") or "999999") for d in drivers.values()}
    scored = {x["driver_number"]: x.get("points") or 0 for x in results}
    if not any("points" in x for x in results):  # a result without points: OpenF1's championship as it is
        scored = {c["driver_number"]: (c.get("points_current") or 0) - (c.get("points_start") or 0) for c in champ_d}
    team_scored = {}
    for n, p in scored.items():
        team = (drivers.get(n) or {}).get("team_name")
        team_scored[team] = team_scored.get(team, 0) + p

    def table(champ, gain):
        """[(entry, position, points)] by points after the race; level on points: OpenF1's order (it knows the countback)."""
        now = [(c, (c.get("points_start") or 0) + gain(c)) for c in champ]
        now.sort(key=lambda e: (-e[1], e[0].get("position_current") or 99))
        return [(c, i + 1, p) for i, (c, p) in enumerate(now)]
    move = lambda c, pos: (c.get("position_start") or pos) - pos
    drows = [(pos, move(c, pos), "#" + (drivers[c["driver_number"]].get("team_colour") or "999999"),
              f1_photo(drivers[c["driver_number"]], 96), f1_full(drivers[c["driver_number"]]),
              drivers[c["driver_number"]].get("team_name") or "", p, p - (c.get("points_start") or 0))
             for c, pos, p in table(champ_d, lambda c: scored.get(c["driver_number"], 0))]
    trows = [(pos, move(c, pos), colour.get(c["team_name"], "#999999"), f1_logo(c["team_name"], s.get("year") or datetime.now(THAI).year), c["team_name"], "",
              p, p - (c.get("points_start") or 0)) for c, pos, p in table(champ_t, lambda c: team_scored.get(c["team_name"], 0))]
    name = meeting.get("meeting_name") or "Formula 1"
    after = f"After the {name}" + (" · updated" if again else "")
    top = lambda rows: ", ".join(f"{r[0]} {r[4]} {r[6]:g}" for r in rows[:3])
    if not token:  # on the Mac: show what would be sent
        print(f"F1 standings {after[:1].lower()}{after[1:]} | drivers: {top(drows)} | teams: {top(trows)}")
        if key not in sent:
            sent.append(key)
        return
    dcard = f1_standings_card("Drivers' Championship", after, drows, colour)
    if len(json.dumps(dcard, separators=(",", ":"))) > 29000:  # LINE takes at most 30 KB a card: without faces it always fits
        dcard = f1_standings_card("Drivers' Championship", after, [r[:3] + (None,) + r[4:] for r in drows], colour)
    card = {"type": "flex", "altText": f"F1 standings {after[:1].lower()}{after[1:]}: {top(drows)}"[:400], "contents": {"type": "carousel", "contents": [
        dcard, f1_standings_card("Teams' Championship", after, trows, colour)]}}
    plain = {"type": "text", "text": f"F1 standings {after[:1].lower()}{after[1:]}\n\nDrivers\n" + "\n".join(
        f"{r[0]} ({'+' if r[1] > 0 else ''}{r[1] or '–'}) {r[4]} {r[6]:g}" for r in drows) + "\n\nTeams\n" + "\n".join(
        f"{r[0]} ({'+' if r[1] > 0 else ''}{r[1] or '–'}) {r[4]} {r[6]:g}" for r in trows)}
    if (line_send(token, card) or line_send(token, plain)) and key not in sent:
        sent.append(key)
    del sent[:-50]


def f1_sig(results):
    """A race's or sprint's result in short, to notice when it changes: [[car number, position, points]]."""
    return sorted([x["driver_number"], x.get("position"), x.get("points")] for x in results)


def f1_recheck(state, token, s, meeting):
    """A session whose card is out: if its result changes after the flag (penalties, deleted laps), a "Result updated" card with
    who moved, and for a race the championship cards again. A session sent before this was followed (Mexico 2026) is
    only noted, so the first change from then on is what gets sent. A change is sent only once two checks in a row see it:
    on 3 Nov 2026 OpenF1 gave Mexico's result from before the penalties for one check, which sent a card and then a
    second one changing it back."""
    final = state.setdefault("f1_final", {})  # {session: f1_sig of the result as last sent}
    seen = state.setdefault("f1_seen", {})  # {session: f1_sig of a changed result seen once, waiting for the next check}
    key = str(s["session_key"])
    results = json.loads(get(f"{OPENF1}/session_result?session_key={key}"))
    if not results:
        return
    sig = f1_sig(results)
    if key not in final or final[key] == sig:
        final[key] = sig
        seen.pop(key, None)  # a one-check blip: nothing sent
        return
    if seen.get(key) != sig:
        seen[key] = sig  # sent if the next check (5 minutes on) gives the same result
        return
    was = {n: (p, pts) for n, p, pts in final[key]}
    drivers = f1_drivers(s, {x["driver_number"] for x in results})
    pos = lambda p: f"P{p}" if isinstance(p, int) else "NC"
    moved, changes = [], []
    for x in sorted(results, key=lambda x: x["position"] if isinstance(x.get("position"), int) else 99):
        old_pos, old_pts = was.get(x["driver_number"], (None, None))
        if (old_pos, old_pts) != (x.get("position"), x.get("points")):
            gain = (x.get("points") or 0) - (old_pts or 0)
            changes.append((drivers.get(x["driver_number"]) or {"driver_number": x["driver_number"]}, old_pos, x.get("position"), gain))
            moved.append(f"{(drivers.get(x['driver_number']) or {}).get('name_acronym') or x['driver_number']} {pos(old_pos)} → {pos(x.get('position'))}"
                         + (f" ({'+' if gain > 0 else ''}{gain:g} pt{'s' if abs(gain) != 1 else ''})" if gain else ""))
    rows = f1_rows(results, drivers)
    name, session = meeting.get("meeting_name") or s.get("country_name") or "Formula 1", s["session_name"]
    when = datetime.fromisoformat(s["date_start"]).astimezone(THAI).strftime("%a %d %b %Y · %H:%M")
    after = "the race" if session == "Race" else session  # "after Practice 2", "after Sprint Qualifying"
    note = f"Result changed after {after}: " + ", ".join(moved[:8]) + (f" and {len(moved) - 8} more" if len(moved) > 8 else "")
    if not token:  # on the Mac: show what would be sent
        print(f"F1 {session} · {name} · result updated | {note}")
    else:
        card = {"type": "flex", "altText": f"F1 {session} · {name}: result updated. {note}"[:400],
                "contents": f1_card(meeting, f"{session} · Updated", when, rows, key, f"RESULT CHANGED AFTER {after.upper()}", changes)}
        plain = {"type": "text", "text": f"F1 {session} · {name}: result updated\n{note}\n\n" + "\n".join(
            f"{p} {f1_name(d)} ({d.get('team_name') or ''}) {v}" for p, d, v in rows)}
        if not (line_send(token, card) or line_send(token, plain)):
            return  # tried again on the next check
    final[key] = sig
    del seen[key]
    if session == "Race":
        f1_standings(state, token, s, meeting, again=True)


def f1_grid_card(meeting, race, grid, drivers, qual, key):
    """The Starting grid card: a red Grid penalties panel (who dropped back, P2 -> P7 and by how many places), then the grid
    as it stands on the track, two staggered columns, each slot on its team's colour stripe and a dropped one outlined red."""
    t = lambda text, **k: {"type": "text", "text": text, **k}
    drop = lambda x: max(x["position"] - (qual.get(x["driver_number"]) or x["position"]), 0)

    def slot(x):
        n, d = x["driver_number"], drivers.get(x["driver_number"], {})
        c = "#" + (d.get("team_colour") or "777777")
        return f1_row(-drop(x), [f1_stripe(c), {"type": "box", "layout": "baseline", "spacing": "sm", "paddingAll": "7px", "contents": [
            t(f1_pos(x["position"]), size="sm", weight="bold", color=F1C["dim"], flex=0),
            t(d.get("name_acronym") or str(n), size="sm", weight="bold", color=F1C["text"], flex=0),
            t(str(n), size="xxs", color=f1_mix(c, "#000000", .8), flex=1),
            *([t(f"▼{drop(x)}", size="xxs", weight="bold", color=F1C["red"], flex=0)] if drop(x) else [])]}], margin="sm")

    back = sorted((x for x in grid if drop(x)), key=lambda x: x["position"])
    start = datetime.fromisoformat(race["date_start"]).astimezone(THAI).strftime("%a %d %b · %H:%M")
    body = ([f1_changes("GRID PENALTIES", [(drivers.get(x["driver_number"], {}), qual[x["driver_number"]], x["position"], 0) for x in back])] if back else []) + [
        t("STARTING GRID", size="xxs", weight="bold", color=F1C["dim"], margin="lg" if back else "none"),
        {"type": "box", "layout": "horizontal", "spacing": "md", "contents": [
            {"type": "box", "layout": "vertical", "contents": [slot(x) for x in grid if x["position"] % 2]},
            {"type": "box", "layout": "vertical", "paddingTop": "18px", "contents": [slot(x) for x in grid if not x["position"] % 2]}]}]
    return {"type": "bubble", "size": "mega", "header": f1_head(meeting, "Starting grid", f"{race['session_name']} · {start} · changed after qualifying"),
            "body": {"type": "box", "layout": "vertical", "backgroundColor": F1C["panel"], "paddingAll": "12px", "contents": body},
            "footer": f1_buttons(("See the grid", f"{F1_SITE}?result={key}"), ("Watch qualifying", f"{F1_SITE}?replay={key}"))}


def f1_grid(state, token, s, meeting, race):
    """A qualifying whose card is out: when the starting grid differs from its order (grid penalties: São Paulo 2026,
    Russell P2 -> P7, Leclerc P4 -> P14), a "Starting grid" card in grid order with who moved; again if the grid changes
    after that. Only until the race (or sprint) starts, and only once two checks in a row see it, as in f1_recheck."""
    key = str(s["session_key"])
    order = state.get("f1_final", {}).get(key)  # the qualifying result as sent: [[car, position, points]]
    if not order or not race or time.time() >= datetime.fromisoformat(race["date_start"]).timestamp():
        return
    try:
        grid = json.loads(get(f"{OPENF1}/starting_grid?session_key={key}"))
    except urllib.error.HTTPError as e:
        if e.code == 404:  # OpenF1's "No results found": no grid yet
            return
        raise
    if not isinstance(grid, list) or not grid:
        return
    sent = state.setdefault("f1_grid", {})  # {qualifying: [[car, grid place]] as last sent}
    seen = state.setdefault("f1_grid_seen", {})  # {qualifying: a changed grid seen once, waiting for the next check}
    sig = sorted([x["driver_number"], x["position"]] for x in grid)
    was = sent.get(key) or sorted([n, p] for n, p, _ in order)
    if sig == was:
        seen.pop(key, None)
        return
    if seen.get(key) != sig:
        seen[key] = sig  # sent if the next check (5 minutes on) gives the same grid
        return
    grid = sorted(grid, key=lambda x: x["position"])
    drivers = f1_drivers(s, {x["driver_number"] for x in grid})
    qual = {n: p for n, p, _ in order}  # each car's qualifying place: the panel and the arrows are against it
    back = [x for x in grid if (qual.get(x["driver_number"]) or x["position"]) < x["position"]]  # who dropped back: the rest moved up into the gaps
    acr = lambda n: (drivers.get(n) or {}).get("name_acronym") or str(n)
    moved = [f"{acr(x['driver_number'])} P{qual[x['driver_number']]} → P{x['position']}" for x in back]
    rows = [(str(x["position"]), drivers.get(x["driver_number"], {}), f1_time(x["lap_duration"]) if x.get("lap_duration") else "No time") for x in grid]
    name, session = meeting.get("meeting_name") or s.get("country_name") or "Formula 1", s["session_name"]
    note = f"{race['session_name']} starting grid, changed after qualifying: " + ", ".join(moved[:8]) + (f" and {len(moved) - 8} more" if len(moved) > 8 else "")
    if not token:  # on the Mac: show what would be sent
        print(f"F1 {session} · {name} · starting grid | {note}")
    else:
        card = {"type": "flex", "altText": f"F1 {name}: starting grid. {note}"[:400],
                "contents": f1_grid_card(meeting, race, grid, drivers, qual, key)}
        plain = {"type": "text", "text": f"F1 {name}: {race['session_name'].lower()} starting grid\n{note}\n\n" + "\n".join(
            f"{p} {f1_name(d)} ({d.get('team_name') or ''}) {v}" for p, d, v in rows)}
        if not (line_send(token, card) or line_send(token, plain)):
            return  # tried again on the next check
    sent[key] = sig
    del seen[key]


# the F1 Live site (its own repo, IsaraApae/f1-live): "?live" opens its Live tab, "?replay=KEY" a session's replay
F1_SITE = "https://isaraapae.github.io/f1-live/"
F1_PAGE = F1_SITE + "?live"


def f1_live_card(meeting, session, when, live):
    """A LINE card when a session starts, in the F1 Live page's style: tap to open the page."""
    t = lambda text, **k: {"type": "text", "text": text, **k}
    badge = {"type": "box", "layout": "horizontal", "flex": 0, "cornerRadius": "999px", "backgroundColor": F1C["red"] if live else F1C["row"],
             "paddingStart": "10px", "paddingEnd": "10px", "paddingTop": "3px", "paddingBottom": "3px", "contents": [
        t("● LIVE NOW" if live else "STARTING SOON", size="xxs", weight="bold", color="#FFFFFF")]}
    return {"type": "bubble", "size": "kilo", "action": {"type": "uri", "label": "Open F1 Live", "uri": F1_PAGE},
            "header": f1_head(meeting, session, when),
            "body": {"type": "box", "layout": "horizontal", "backgroundColor": F1C["panel"], "paddingAll": "12px", "contents": [badge, {"type": "filler"}]},
            "footer": f1_button("Open F1 Live", F1_PAGE)}


JOB_START = float(os.environ.get("JOB_START") or time.time())  # when this GitHub job began (watch.yml): it has 9 minutes in all
CHECK_EVERY = 5 * 60  # how often cron-job.org starts a check (watch.yml)


def f1_spare():
    """Seconds this check can still wait for an F1 session to start, leaving 2.5 minutes of the job's 9 for the rest
    (the schedule, manga, saving). None on the Mac."""
    return max(0, JOB_START + 8 * 60 - 150 - time.time()) if os.environ.get("LINE_TOKEN") else 0


def f1_schedule(state, now):
    """The weekend's sessions and their meetings, from OpenF1; during a session OpenF1 refuses free requests (HTTP 401), so
    the last list it gave is kept in the state and used then."""
    since = datetime.fromtimestamp(now - 3 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    keep = ("session_key", "session_name", "date_start", "date_end", "meeting_key", "year", "country_name")
    ts = lambda x: datetime.fromisoformat(x).timestamp()
    try:
        sessions = [s for s in json.loads(get(f"{OPENF1}/sessions?date_end>={since}")) if ts(s["date_start"]) < now + 10 * 86400]
        state["f1_schedule"] = [{k: s.get(k) for k in keep} for s in sessions]
    except Exception as e:
        sessions = [s for s in state.get("f1_schedule", []) if ts(s["date_end"]) >= now - 3 * 86400]
        print(f"f1: OpenF1 didn't answer ({e}); using the saved schedule ({len(sessions)} sessions)")
    meetings = state.setdefault("f1_meetings", {})
    for mk in {str(s["meeting_key"]) for s in sessions} - set(meetings):
        try:
            meetings[mk] = (json.loads(get(f"{OPENF1}/meetings?meeting_key={mk}")) or [{}])[0]
        except Exception:
            pass
    for mk in set(meetings) - {str(s["meeting_key"]) for s in sessions}:
        del meetings[mk]
    return sessions, meetings


def f1_started(state, token, sessions, meetings):
    """A LINE card when a session starts, once: at its start time if this check can wait for it (it waits up to f1_spare()),
    else on the next check, up to 30 minutes in."""
    sent = state.setdefault("f1_live", [])
    for s in sorted(sessions, key=lambda s: s["date_start"]):
        start, key = datetime.fromisoformat(s["date_start"]).timestamp(), str(s["session_key"])
        if key in sent or time.time() > start + 30 * 60:
            continue
        if start > time.time():
            if start - time.time() > f1_spare():
                continue  # the next check sends it
            time.sleep(start - time.time())
        meeting = meetings.get(str(s["meeting_key"])) or {}
        name = meeting.get("meeting_name") or s.get("country_name") or "Formula 1"
        when = datetime.fromtimestamp(start, THAI).strftime("%a %d %b · %H:%M")
        if not token:  # on the Mac: show what would be sent
            print(f"F1 live: {s['session_name']} · {name} · {when}")
        else:
            card = {"type": "flex", "altText": f"F1 {s['session_name']} · {name} is live: open F1 Live", "contents": f1_live_card(meeting, s["session_name"], when, True)}
            plain = {"type": "text", "text": f"F1 {s['session_name']} · {name} is live now\n{F1_PAGE}"}
            if not (line_send(token, card) or line_send(token, plain)):
                continue
        sent.append(key)
    del sent[:-100]


def f1(state):
    """Send every Formula 1 session's order once it is published: Practice 1-3, Sprint Qualifying, Sprint, Qualifying, Race."""
    token, now = os.environ.get("LINE_TOKEN"), time.time()
    state.pop("f1", None)  # the earlier race-only records
    sessions, meetings = f1_schedule(state, now)
    f1_started(state, token, sessions, meetings)
    now = time.time()
    end = lambda s: datetime.fromisoformat(s["date_end"]).timestamp()
    ended = sorted((s for s in sessions if end(s) <= now), key=end)
    if "f1_sessions" not in state:  # the first run sends only the newest session, as a sample, not the whole weekend
        state["f1_sessions"] = [str(s["session_key"]) for s in ended[:-1]]
    sent = state["f1_sessions"]
    meetings = {}
    for s in ended:
        if s["session_name"] == "Race" and str(s["session_key"]) in sent:  # race card out: then the championship tables
            try:
                m = meetings.setdefault(s["meeting_key"], (json.loads(get(f"{OPENF1}/meetings?meeting_key={s['meeting_key']}")) or [{}])[0])
                f1_standings(state, token, s, m)
            except Exception as e:  # never stops the other sessions' cards
                print(f"FAIL f1 standings: {e}", file=sys.stderr)
                continue
        key = str(s["session_key"])
        if key in sent:
            # its card is out: follow its result for changes after the flag (every session: practice too, for deleted laps)
            try:
                f1_recheck(state, token, s, meetings.get(s["meeting_key"]) or
                           meetings.setdefault(s["meeting_key"], (json.loads(get(f"{OPENF1}/meetings?meeting_key={s['meeting_key']}")) or [{}])[0]))
            except Exception as e:
                print(f"FAIL f1 recheck {s['session_name']}: {e}", file=sys.stderr)
            if s["session_name"] in ("Qualifying", "Sprint Qualifying"):  # and the grid it sets, for penalties
                race = next((r for r in sessions if r["meeting_key"] == s["meeting_key"]
                             and r["session_name"] == ("Race" if s["session_name"] == "Qualifying" else "Sprint")), None)
                try:
                    f1_grid(state, token, s, meetings.get(s["meeting_key"]) or
                            meetings.setdefault(s["meeting_key"], (json.loads(get(f"{OPENF1}/meetings?meeting_key={s['meeting_key']}")) or [{}])[0]), race)
                except Exception as e:
                    print(f"FAIL f1 grid {s['session_name']}: {e}", file=sys.stderr)
            continue
        try:
            results = json.loads(get(f"{OPENF1}/session_result?session_key={key}"))
            if not results:
                continue  # not published yet: tried again on the next check
            drivers = f1_drivers(s, {x["driver_number"] for x in results})
            meeting = meetings.get(str(s["meeting_key"])) or (json.loads(get(f"{OPENF1}/meetings?meeting_key={s['meeting_key']}")) or [{}])[0]
            rows = f1_rows(results, drivers)
            name, session = meeting.get("meeting_name") or s.get("country_name") or "Formula 1", s["session_name"]
            when = datetime.fromisoformat(s["date_start"]).astimezone(THAI).strftime("%a %d %b %Y · %H:%M")
            top = ", ".join(f"{p} {d.get('name_acronym', '?')}" for p, d, _ in rows[:3])
            if not token:  # on the Mac: show what would be sent
                print(f"F1 {session} · {name} · {when} | {top}")
                sent.append(key)
                state.setdefault("f1_final", {})[key] = f1_sig(results)
                continue
            plain = {"type": "text", "text": f"F1 {session} · {name}\n{when}\n" + "\n".join(f"{p} {f1_name(d)} ({d.get('team_name') or ''}) {v}" for p, d, v in rows)}
            card = {"type": "flex", "altText": f"F1 {session} · {name}: {top}"[:400], "contents": f1_card(meeting, session, when, rows, key)}
            if line_send(token, card) or line_send(token, plain):
                sent.append(key)
                state.setdefault("f1_final", {})[key] = f1_sig(results)  # the result as sent: a later change gets its own card
                if session == "Race":
                    f1_standings(state, token, s, meeting)
        except Exception as e:  # one session's trouble never stops the next one's card (it's tried again on the next check)
            print(f"FAIL f1 {s['session_name']}: {e}", file=sys.stderr)
    del sent[:-100]
    following = {str(s["session_key"]) for s in sessions}
    for k in [k for k in state.get("f1_final", {}) if k not in following]:  # out of the 3-day window: no longer followed
        del state["f1_final"][k]
    for kind in ("f1_seen", "f1_grid", "f1_grid_seen"):
        for k in [k for k in state.get(kind, {}) if k not in following]:
            del state[kind][k]


COVER_OVERRIDES = {  # series whose own cover LINE can't show (AVIF): a JPEG copy on the GitHub Pages site
    "https://speed-manga.net/manga/investors-who-see-the-future/": "https://isaraapae.github.io/alert-watch/covers/investors-who-see-the-future.jpg",
}


def cover(url):
    """(title, cover image) from a series page's preview tags, or (None, None)."""
    if url in COVER_OVERRIDES:
        return None, COVER_OVERRIDES[url]
    try:
        page = get(url)
        tags = dict(re.findall(r'<meta property="og:(title|image)" content="([^"]+)"', page))
        if not tags.get("image"):  # some themes (slow-manga) show the cover only in the page layout
            m = re.search(r'<div class="(?:summary_image|thumb)"[^>]*>.*?<img[^>]+?(?:data-src|src)="([^"]+)"', page, re.S)
            tags["image"] = m and m.group(1)
        return tags.get("title"), tags.get("image")
    except Exception as e:
        print(f"FAIL cover {url}: {e}", file=sys.stderr)
        return None, None


WEBTOON_APP = "https://isaraapae.github.io/alert-watch/w.html?t="  # redirect page in site/ (the website); opens linewebtoon://
WEBTOON_SERIES_PAGE = {"6227", "9274", "6957"}  # Enrolling in the Transcendent Academy, Zodiac Girls, My Virtual God: cards open the series page, not the episode
# manga-site series that are also on WEBTOON Thai (found by their Thai titles there). They're alerted when the manga site
# posts a chapter, the fastest source the watcher can read. The card and the LINE list open the series in the WEBTOON app when
# WEBTOON's app has that chapter (webtoon_newest in the state), and the manga site when the site is ahead. WEBTOON's newest
# episodes, locked behind coins or ads, exist only in the app, so the app's count is the free ones plus WEBTOON_LOCKED.
# how many more episodes the WEBTOON app has than WEBTOON's website shows free (the ones locked behind coins or ads), counted
# in the app in Oct 2026, by WEBTOON series number: added to the free count before comparing with the manga site, and when
# WEBTOON frees one more, a card gives the app's newest episode ("Ep.218": 216 free + 2).
# ponytail: fixed numbers; if WEBTOON changes how many it locks, update them from the app.
WEBTOON_LOCKED = {"6842": 2, "4395": 2, "7294": 3, "5823": 1, "5899": 1, "6718": 1, "7562": 2, "8314": 3, "6392": 3, "7637": 2,
                  "2433": 3,
                  "6227": 2, "6957": 2}  # Enrolling in the Transcendent Academy, My Virtual God (WEBTOON only): their cards and the LINE
                  # list give the app's newest episode ("Ep.140": 138 free + 2), so you know which episode to read
# series whose app count can't be worked out from the free ones (WEBTOON's website shows Reborn Rich only up to Ep.10, Bad Guy
# up to Ep.55): the app
# gets one more every week at a set time, counted from a known episode: {WEBTOON series number: (episode, when it came out)}.
# Reborn Rich Ep.208 and Bad Guy Ep.310 in the app on Wed 14 Oct 2026; one more every Wednesday at 22:00 Thai time, each with a card.
WED_14_OCT = datetime(2026, 10, 14, 22, 0, tzinfo=timezone(timedelta(hours=7))).timestamp()
WEBTOON_WEEKLY = {"4861": (208, WED_14_OCT), "2342": (310, WED_14_OCT)}
WEBTOON_TWINS = {"https://manga-lc.net/manga/the-regressed-son-of-a-duke-is-an-assassin-ลูกชายคนเล็กของดย/": "https://www.webtoons.com/th/fantasy/the-reborn-young-lord-is-an-assassin/list?title_no=6392",
                 "https://manga-lc.net/manga/life-changer-voice-phishing-คอลเซ็นเตอร์เปลี่ยน/": "https://www.webtoons.com/th/drama/life-changing-voice-phishing/list?title_no=7265",
                 "https://manga-lc.net/manga/reborn-rich/": "https://www.webtoons.com/th/drama/reborn-rich/list?title_no=4861",
                 "https://manga-lc.net/manga/the-ultimate-shut-in-โทษที-พื้นที่นี้ห้าม/": "https://www.webtoons.com/th/fantasy/ultimate-shut-in/list?title_no=7562",
                 "https://manga-lc.net/manga/99-wooden-stick-99-ท่อนไม้พร้อมบวก/": "https://www.webtoons.com/th/action/99-reinforced-wooden-stick/list?title_no=4395",
                 "https://manga-lc.net/manga/the-delusional-hunter-in-a-another-world/": "https://www.webtoons.com/th/fantasy/the-misunderstood-hunter/list?title_no=7294",
                 "https://manga-lc.net/manga/secretly-more-powerful-than-the-hero/": "https://www.webtoons.com/th/fantasy/overpowered-than-the-hero/list?title_no=5823",
                 "https://manga-lc.net/manga/juvenile-prison/": "https://www.webtoons.com/th/action/juvenile-detention-center/list?title_no=6842",
                 "https://manga-lc.net/manga/a-sense-of-amusement-สนามเด็กล่า/": "https://www.webtoons.com/th/action/the-toy/list?title_no=6718",
                 "https://manga-lc.net/manga/genius-grandson-of-the-loan-shark-king-หลานอัจฉริยะของรา/": "https://www.webtoons.com/th/action/loan-sharks-genius-grandson/list?title_no=7637",
                 "https://manga-lc.net/manga/what-a-bountiful-harvest-demon-lord-วิถีชาวนาของราชาปี/": "https://www.webtoons.com/th/fantasy/what-a-bountiful-harvest-demon-lord/list?title_no=8314",
                 "https://manga-lc.net/manga/killer-peter-นักฆ่าย้อนวัย/": "https://www.webtoons.com/th/action/killer-peter/list?title_no=5899",
                 "https://manga-lc.net/manga/the-boy-of-death/": "https://www.webtoons.com/th/fantasy/ticket-hero/list?title_no=2433",
                 "https://manga-lc.net/manga/bad-guy/": "https://www.webtoons.com/th/comedy/badguy/list?title_no=2342",
                 "https://manga-lc.net/manga/reality-quest/": "https://www.webtoons.com/th/fantasy/reality-quest/list?title_no=4271"}
BRAVE = "https://isaraapae.github.io/alert-watch/b.html?v="  # redirect page in site/ (the website); opens a YouTube video in Brave


def in_brave(youtube_link):
    """Card link that opens this YouTube video in the Brave browser (LINE cards only allow https:// links)."""
    return BRAVE + re.search(r"[?&]v=([A-Za-z0-9_-]{11})", youtube_link)[1]


THAI = timezone(timedelta(hours=7))


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def thai_times():
    """{AniList id: (where, weekday, "HH:MM", days earlier than Japan)} for shows whose Thai-sub release differs from Japanese TV."""
    auto = HERE / "thai_times_auto.json"  # learned by the watcher; thai_times.txt (yours) overrides it
    out = {int(k): tuple(v) + (0,) for k, v in (json.loads(auto.read_text()) if auto.exists() else {}).items()}
    path = HERE / "thai_times.txt"
    for line in path.read_text().splitlines() if path.exists() else []:
        r = [x.strip() for x in line.split("|")]
        if line.startswith("#") or len(r) not in (4, 5) or not r[0].isdigit():
            continue
        early = int(r[4]) if len(r) == 5 and r[4].isdigit() else 0  # e.g. 7: released a week before Japanese TV
        if r[2] == r[3] == "-":  # platform known, release time not: keep the Japanese time, but name where to watch
            out[int(r[0])] = (r[1], None, None, 0)
        elif r[2][:3].title() in WEEKDAYS and re.fullmatch(r"\d{1,2}[:.]\d{2}", r[3]):
            out[int(r[0])] = (r[1], r[2][:3].title(), r[3].replace(".", ":"), early)
    return out


def learn_release_times(shows, jp, videos, cards):
    """{AniList id: [where, weekday or None, "HH:MM" or None]} from Thai-sub releases seen on the official channels (videos)
    and Bilibili Thailand's schedule (cards: (title, release, episode text)), compared with each episode's Japanese
    broadcast (jp: {AniList id: {episode: broadcast}}). Released up to 2 days later: that weekday and time; at the same time
    (within 10 minutes): the platform name only. Earlier or later releases are early premieres or catch-up uploads and
    say nothing about the regular slot. With several platforms, the one releasing first wins."""
    best = {}
    releases = [(t, p, t, ch) for t, _, p, ch in videos if "ซับไทย" in t.lower() and "พากย์ไทย" not in t.lower()]
    releases += [(t, ts, ep, "Bilibili") for t, ts, ep in cards]
    for title, release, eptext, where in releases:
        n = re.search(r"(?:ตอนที่|บทที่|ep\.?|episode)\s*0*(\d+)", eptext.lower())
        if not n:
            continue
        n = int(n.group(1))
        for aid, s in shows.items():
            if not s["yt"] or not re.search(s["yt"], title.lower()):
                continue
            eps = jp.get(aid, {})
            broadcast = eps.get(n) or eps.get(n - max(s["first"][0], 1) + 1)  # channels may keep counting across seasons
            delay = broadcast and release - broadcast
            if broadcast is None or not -600 <= delay <= 2 * 86400:
                continue
            t = datetime.fromtimestamp(release, THAI)
            slot = [where, None, None] if delay <= 600 else [where, WEEKDAYS[t.weekday()], t.strftime("%H:%M")]
            if aid not in best or delay < best[aid][0]:
                best[aid] = (delay, slot)
    return {aid: slot for aid, (_, slot) in best.items()}


def release_time(broadcast, entry):
    """When an episode comes out with Thai subs, given its Japanese broadcast and the show's thai_times entry."""
    where, day, hhmm, early = entry
    return thai_slot(broadcast - early * 86400, day, hhmm) if day else broadcast


def thai_slot(broadcast, day, hhmm):
    """The first `day` at `hhmm` Thai time at or after the Japanese broadcast: Sat 23:30 -> "Sun 01:00" gives Sun 01:00."""
    h, m = map(int, hhmm.split(":"))
    t = datetime.fromtimestamp(broadcast, THAI).replace(hour=h, minute=m, second=0, microsecond=0)
    t += timedelta(days=(WEEKDAYS.index(day) - t.weekday()) % 7)
    return (t + timedelta(days=7) if t.timestamp() < broadcast else t).timestamp()


def episode_line(ep, total, aired_at, where=None):
    when = datetime.fromtimestamp(aired_at, THAI).strftime("%d %b %Y %H:%M")
    return f"Episode {ep:02d}/{f'{total:02d}' if total else '?'}\n{f'{where} · ' if where else 'Aired '}{when}"


JST = timezone(timedelta(hours=9))


def tv_aired(xml, first_ep, now):
    """{(TV-schedule id, season episode): earliest broadcast} from Syoboi Calendar's ProgLookup XML, for episodes already on air."""
    out = {}
    for item in re.findall(r"<ProgItem\b.*?</ProgItem>", xml, re.S):
        p = dict(re.findall(r"<(\w+)>([^<]*)</\1>", item))
        if p.get("Deleted") == "1" or not p.get("Count", "").isdigit() or int(p["TID"]) not in first_ep:
            continue
        t = datetime.strptime(p["StTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).timestamp()
        # the TV schedule keeps counting across seasons, and some channels count from a different start (Blue Box: 1 and 26)
        start = max((f for f in first_ep[int(p["TID"])] if f <= int(p["Count"])), default=None)
        if start is None:
            continue
        key = (int(p["TID"]), int(p["Count"]) - start + 1)
        if t <= now and (key not in out or t < out[key]):
            out[key] = t
    return out


YT_CHANNELS = {"UCn8hjQOnGYR1AZtYYMYP5jQ": "Muse Thailand", "UCw2bdNSXh4x6e0NCduVoxMQ": "Ani-One Thailand"}  # official uploads
BILIBILI_SCHEDULE = "https://api.bilibili.tv/intl/gateway/web/v2/ogv/timeline?s_locale=th_TH&platform=web"  # Bilibili Thailand's week; it only fills in for connections from Thailand, so the cloud learns nothing from it and keeps what was learned before


VIDEOS = []  # the run's one reading of the channels' uploads (main starts it first): the alerts and the schedule share it


def youtube_videos():
    """The official channels' newest uploads, read once a run (read_youtube) and shared."""
    if not VIDEOS:
        VIDEOS.append(POOL.submit(read_youtube))
    return VIDEOS[0].result()


def read_youtube(channels=None):
    """[(title, link, published timestamp, channel)] for the official channels' newest uploads (only those named in
    `channels`, if given). With a YOUTUBE_KEY, from YouTube's data service, current within seconds; otherwise (or if
    that fails) from the public feeds, which GitHub can get as a copy hours old."""
    out = []
    key = os.environ.get("YOUTUBE_KEY")
    for cid in YT_CHANNELS:
        if channels is not None and YT_CHANNELS[cid] not in channels:
            continue
        if key:
            try:
                url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet,contentDetails&maxResults=50&playlistId=UU{cid[2:]}&key={key}"
                rows = []
                for it in json.loads(get(url)).get("items", []):  # UU... is the channel's uploads list
                    sn, cd = it["snippet"], it.get("contentDetails", {})
                    rows.append((html.unescape(sn["title"]), cd.get("videoId") or sn["resourceId"]["videoId"], cd.get("videoPublishedAt") or sn["publishedAt"]))
                # a premiere is uploaded hours before it plays (Chitose's special: up at 17:36, plays at 23:30): use the start time
                ids = ",".join(v for _, v, _ in rows)
                live = json.loads(get(f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={ids}&key={key}")).get("items", []) if ids else []
                starts = {v["id"]: d.get("actualStartTime") or d.get("scheduledStartTime") for v in live for d in [v.get("liveStreamingDetails") or {}]}
                for title, vid, when in rows:
                    when = (starts.get(vid) or when).replace("Z", "+00:00")
                    out.append((title, "https://www.youtube.com/watch?v=" + vid, datetime.fromisoformat(when).timestamp(), YT_CHANNELS[cid]))
                continue
            except Exception as e:  # the message never includes the key
                print(f"FAIL youtube data service {YT_CHANNELS[cid]}: {e}; using the public feed", file=sys.stderr)
        try:
            x = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
        except Exception as e:
            print(f"FAIL youtube {cid}: {e}", file=sys.stderr)
            continue
        for e in re.findall(r"<entry>.*?</entry>", x, re.S):
            t, l, p = (re.search(r, e) for r in (r"<title>([^<]*)</title>", r'<link rel="alternate" href="([^"]+)"', r"<published>([^<]+)</published>"))
            if t and l and p:
                out.append((html.unescape(t[1]), l[1], datetime.fromisoformat(p[1]).timestamp(), YT_CHANNELS[cid]))
    return out


def youtube_episode(videos, pattern, eps, aired):
    """Link to a [ซับไทย] upload of one of these episode numbers, posted no more than 4 days before its release
    (channels sometimes post early, as Ani-One did for HOTEL INHUMANS episode 14) and so never an older season's video."""
    for title, link, published, *_ in videos:
        t = title.lower()
        if "ซับไทย" in t and "พากย์ไทย" not in t and re.search(pattern, t) and published >= aired - 4 * 86400 \
                and any(re.search(rf"(?:ตอนที่|ep\.?|episode)\s*0*{e}(?!\d)", t) for e in eps):
            return link
    return None


def thai_title(title):
    """The show's Thai name in an official upload's title: "คุรุมิจัง นักสู้ FX ตอนที่ 5  [ซับไทย]【Ani-One Thailand】" -> "คุรุมิจัง
    นักสู้ fx". The part before the episode number, without bracketed tags, the piece (of "Thai / English") with Thai
    letters in it; None if there's none."""
    head = re.split(r"ตอนที่|\bep\.?\s*\d|\bepisode\s*\d", title, maxsplit=1, flags=re.I)[0]
    head = re.sub(r"[\[【(（].*?[\]】)）]", " ", head)
    for part in re.split(r"\s+[/|｜]\s+|\s*/\s*", head):
        if re.search(r"[\u0E00-\u0E7F]", part):
            name = re.sub(r"\s+", " ", part).strip(" -:|/").lower()
            return name if len(name) >= 4 else None
    return None


literal = lambda text: re.sub(r"([.^$*+?{}\[\]\\|()])", r"\\\1", text)   # text as a keyword that matches itself (spaces kept readable)


def learn_thai_titles(shows, videos, due, thai):
    """Muse and Ani-One sometimes title an upload only in Thai (Kurumi-chan's episode 5: "คุรุมิจัง นักสู้ FX ตอนที่ 5"),
    which the English keywords miss. So every show's Thai name is learned from the channels' Thai-sub uploads and added
    to its keywords in anime.txt, where every check uses it: (1) from an upload the keywords already match (its title
    had both names), and (2) from an upload nothing matches, when it's the only tracked show on that channel with that
    episode number due within 45 minutes of the upload (Muse and Ani-One post on the dot). Returns [(show, name)]."""
    learned = []
    match = lambda t: [i for i, s in shows.items() if s["yt"] and re.search(s["yt"], t)]
    for title, link, published, channel in videos:
        t = title.lower()
        name = thai_title(title)
        if "ซับไทย" not in t or "พากย์ไทย" in t or not name:
            continue
        hit = match(t)
        if not hit:   # by timing: that channel's show with this episode number due now
            n = re.search(r"ตอนที่\s*0*(\d+)(?![\d.])", t)
            hit = n and [i for i, s in shows.items() if i in thai and thai[i][0] == channel
                         and any(int(n[1]) in {e, e + s["offset"], e + max(s["first"][0], 1) - 1} and abs(at - published) <= 45 * 60
                                 for (j, e), at in due.items() if j == i)]
        if not hit or len(hit) > 1:
            continue
        i = hit[0]
        if (shows[i]["yt"] and re.search(shows[i]["yt"], name)) or match(name):   # known already, or another show's words
            continue
        shows[i]["yt"] = "|".join(x for x in (shows[i]["yt"], literal(name)) if x)
        learned.append((i, name))
    if learned:
        path = HERE / "anime.txt"
        lines = path.read_text().splitlines()
        for n, line in enumerate(lines):
            r = [x.strip() for x in line.split("|", 5)]
            if len(r) >= 2 and r[1].isdigit() and int(r[1]) in {i for i, _ in learned}:
                i = int(r[1])
                # a short line ("name | id") keeps the Thai name in its last field, which completing it later keeps
                lines[n] = f"{line.rstrip()}|{literal(dict(learned)[i])}" if len(r) == 6 and r[5] else \
                    " | ".join((r + [""] * 6)[:5] + [shows[i]["yt"]])
        path.write_text("\n".join(lines) + "\n")
        for i, name in learned:
            print(f"learned Thai title for {shows[i]['name']}: {name}")
    return learned


def specials(videos, shows, since):
    """[(AniList id, episode number text, link, published, channel)] for Thai-sub uploads of tracked shows numbered 0 or
    with a decimal ("ตอนที่ 13.5"): specials no TV or AniList schedule lists. Only uploads posted at or after `since`."""
    out = []
    for title, link, published, channel in videos:
        t = title.lower()
        n = re.search(r"(?:ตอนที่|ep\.?|episode)\s*0*(\d+(?:\.\d+)?)(?![\d.])", t)
        if published < since or "ซับไทย" not in t or "พากย์ไทย" in t or not n or not (n[1] == "0" or "." in n[1]):
            continue
        aid = next((i for i, s in shows.items() if s["yt"] and re.search(s["yt"], t)), None)
        if aid:
            out.append((aid, n[1], link, published, channel))
    return out


GOJO = "https://animegojos.com"  # animeruka is read from its feed (FEEDS) when it lets this machine in


def gojo_latest(page, shows):
    """[(AniList id, episode, direct episode link)] from animegojo's home page (its newest episodes) for tracked shows.
    Dubbed listings (พากย์ไทย) are skipped; that the series is ซับไทย is checked on its own page by gojo_sub_episodes."""
    out = []
    for path, n, title in re.findall(r'<a href="(/list/\d+/)#ep-(\d+)" title="([^"]+)"', page):
        t = html.unescape(title).lower()
        aid = "พากย์ไทย" not in t and next((i for i, s in shows.items() if s["yt"] and re.search(s["yt"], t)), None)
        if aid:
            out.append((aid, int(n), f"{GOJO}{path}#ep-{n}"))
    return out


def gojo_sub_episodes(page):
    """The episode numbers on an animegojo series page marked (ซับไทย); none for a dubbed (พากย์ไทย) page."""
    title = re.search(r"<title>([^<]*)", page)
    if not title or "(ซับไทย)" not in title[1] or "พากย์ไทย" in title[1]:
        return set()
    return {int(x) for x in re.findall(r'href="#ep-(\d+)"', page)}


# animegojo may carry a new cour on an older page: Yozakura Season 2 Cour 1 was its 1-12, so its 13 would be Cour 2's 1
GOJO_SHIFT = {213657: 12}

# shows also followed on another site: {AniList id: its series page}. It competes with animeruka and animegojo
# for every episode (the fastest gets the card), and is never dropped. suba-anime was the first place to watch Yozakura
# Season 2 in Thailand; its page numbers the season 1-24, so Cour 2's episode 1 is its 13 (GOJO_SHIFT).
# Read with ordinary requests only: if the site ever answers with a bot check, it's reported as a failure, not worked around.
OTHER_PAGES = {213657: "https://suba-anime.com/yozakura-san-chi-no-daisakusen-ss2/"}


def suba_episodes(page):
    """{episode number: its page} on a suba-anime series page; an episode marked (ยังไม่มา), not out yet, has no link there."""
    return {int(n): link for link, n, text in re.findall(r'<a href="([^"#]+/ep-(\d+))"[^>]*>([^<]*)', page) if "ยังไม่มา" not in text}


def site_episodes(url):
    """{episode number: link} for a series page on animegojo (ซับไทย only) or suba-anime."""
    page = get(url)
    if "suba-anime.com" in url:
        if "<title>Just a moment" in page:
            raise RuntimeError("suba-anime answered with a bot check")
        return suba_episodes(page)
    return {n: f"{url.split('#')[0]}#ep-{n}" for n in gojo_sub_episodes(page)}


site_name = lambda url: "suba-anime" if "suba-anime.com" in url else "animegojo"


def series_page(link):
    """The series page an episode's link belongs to, for a whole season released at once: animegojo's /list/3165/#ep-3
    and suba-anime's .../ep-13 are on it already, animeruka's /ep/<show>-ep-3/ is /anime/<show>/. Anything else
    (a YouTube video) stays the episode's own link."""
    if link.startswith(GOJO):
        return link.split("#")[0]
    if "suba-anime.com" in link:
        return re.sub(r"/ep-\d+/?$", "/", link)
    return re.sub(r"^(https://animeruka\.com)/ep/(.+)-ep-\d+/?$", r"\1/anime/\2/", link)


# animeruka: its RSS feed of new episodes, which (unlike animegojo) gives every upload's time. (anime-waku was dropped:
# its Cloudflare bot check refused GitHub's servers.)
# Read with ordinary requests only: when Cloudflare answers with its bot check (as it has for GitHub's servers), the
# run reports it and carries on with the other sites; it is never worked around.
FEEDS = {"animeruka": "https://animeruka.com/feed/?post_type=episodes"}


FEED_PROXY = os.environ.get("FEED_PROXY", "")  # a Cloudflare Worker that fetches the feeds: the sites refuse GitHub's own addresses


FEED_DIR = os.environ.get("FEED_DIR", "")  # feeds saved this run by vpn_feeds.py through a Thai VPN server
FEED_WAIT = 160  # the longest to wait for vpn_feeds.py, which runs alongside this and gives up after 150 seconds
RUN_START = time.time()


def read_feed(url):
    """A site's RSS feed; an error when the site answers with anything else (a bot check, a block page)."""
    site = next((n for n, u in FEEDS.items() if u == url), "")
    saved = HERE / FEED_DIR / f"{site}.xml" if FEED_DIR and site else None
    if saved:  # vpn_feeds.py writes "done" when it has saved the feeds or given up
        waited = time.time()
        while not (HERE / FEED_DIR / "done").exists() and time.time() < RUN_START + FEED_WAIT:
            time.sleep(0.2)
        if time.time() - waited > 1:
            print(f"waited {time.time() - waited:.0f}s for the VPN feeds")
    try:
        if saved and saved.exists():
            page, status = saved.read_text(encoding="utf-8"), "vpn"
        elif FEED_PROXY:
            page, status = get(f"{FEED_PROXY}&site={site}"), "proxy"
        else:
            page, status = get(url), "urllib"
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}{' (Cloudflare bot check)' if e.code in (403, 503) else ''}") from None
    if "<rss" not in page[:1000]:
        raise RuntimeError("answered with a bot check, not its feed" if "Just a moment" in page
                           else f"HTTP {status}, not its feed: {' '.join(page[:200].split())!r}")
    return page


def feed_episodes(xml, shows):
    """[(AniList id, episode, link, upload time)] for tracked shows in a site's episode feed, ซับไทย posts only.
    Titles look like "Tensei shitara Ken deshita II ... (ภาค2) ตอนที่ 3 ซับไทย"; a post of several is "ตอนที่ 1-12"."""
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        tag = lambda name: (re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", item, re.S) or [None, ""])[1].strip()
        title, link, pub = html.unescape(tag("title")).lower(), html.unescape(tag("link")), tag("pubDate")
        num = re.search(r"ตอนที่\s*(\d+)(?:\s*-\s*(\d+))?", title)
        if not (num and link and pub) or "ซับไทย" not in title or "พากย์ไทย" in title:
            continue
        name = title[:num.start()]
        aid = next((i for i, s in shows.items() if s["yt"] and re.search(s["yt"], name)), None)
        if aid:
            ts = int(email.utils.parsedate_to_datetime(pub).timestamp())
            out += [(aid, n, link, ts) for n in range(int(num[1]), int(num[2] or num[1]) + 1)]
    return out


def anime_list():
    """anime.txt: name | AniList id | TV-schedule id (0 = none) | first episode number(s) in the TV schedule, comma-separated when channels count differently
    | episodes before this AniList entry (added to the count) | YouTube title keywords."""
    shows = {}
    for line in (HERE / "anime.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        r = [x.strip() for x in line.split("|", 5)]  # tolerant of missing spaces around |
        r += [""] * (6 - len(r))  # just "name | AniList id" works: times then come from AniList
        try:  # a mistyped line is skipped, so one bad edit can't stop every other alert
            shows[int(r[1])] = {"name": r[0], "tid": int(r[2] or 0), "first": [int(x) for x in (r[3] or "1").split(",")],
                                "offset": int(r[4] or 0), "yt": r[5] if len(r) > 5 else ""}
        except (IndexError, ValueError):
            print(f"SKIP anime.txt line (needs at least: name | AniList id): {line[:80]}", file=sys.stderr)
    return shows


def episode_total(m, state):
    """AniList's episode count; while it has none, MyAnimeList's (Iceblade Season 2: 12 on MAL, nothing on AniList), read from
    the show's MAL page at most every 6 hours until MAL has a number, which is then kept. No count anywhere: None."""
    if m.get("episodes") or not m.get("idMal"):
        return m.get("episodes")
    known = state.setdefault("mal_totals", {})  # {MAL id: [episodes or None, when MAL was last asked]}
    n, asked = known.get(str(m["idMal"]), [None, 0])
    if n is None and time.time() - asked > 6 * 3600:
        try:
            found = re.search(r"Episodes:</span>\s*(\d+)", get(f"https://myanimelist.net/anime/{m['idMal']}"))
            n = found and int(found[1])
        except Exception as e:
            print(f"FAIL myanimelist {m['idMal']}: {e}", file=sys.stderr)
        known[str(m["idMal"])] = [n, int(time.time())]
    return n


def season_label(m):
    """AniList media -> "Fall 2026, TV"."""
    parts = [f"{m['season'].title()} {m['seasonYear']}" if m.get("season") and m.get("seasonYear") else "", (m.get("format") or "").replace("_", " ")]
    return ", ".join(p for p in parts if p)


def anilist(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request("https://graphql.anilist.co", data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]


def saved_media(state, shows):
    """{AniList id: AniList's last answer for the show}, for when AniList is down (on 21 Oct 2026 its API was switched off
    for everyone, "temporarily disabled due to severe stability issues", and every anime alert stopped): what schedule()
    saved in anilist_media, or else the cover, season, count and online airing times schedule.json last had. Japanese TV
    times come from Syoboi, so only online-only shows need AniList's airing times."""
    saved = state.get("anilist_media", {})
    path = HERE / "schedule.json"
    site = {w["name"]: w for w in (json.loads(path.read_text())["shows"] if path.exists() else [])}
    thai = thai_times()
    out = {}
    for i, s in shows.items():
        if str(i) in saved:
            out[i] = saved[str(i)]
        elif s["name"] in site:
            w = site[s["name"]]
            parts = (w.get("label") or "").split(", ")  # season_label's "Fall 2026, TV", back into AniList's fields
            season = re.fullmatch(r"(\w+) (\d{4})", parts[0])
            fmt = parts[-1] if len(parts) == 2 or not season else ""
            # the site's times are AniList's own only for an online show with no Thai slot of its own; specials (0, .5) left out
            nodes = [{"episode": r["ep"] - s["offset"], "airingAt": int(r["at"])} for r in w["episodes"]
                     if not s["tid"] and not (i in thai and thai[i][1]) and r["ep"] == int(r["ep"]) and r["ep"] > s["offset"]]
            out[i] = {"id": i, "idMal": None, "episodes": w["total"] and w["total"] - s["offset"],
                      "season": season and season[1].upper(), "seasonYear": season and int(season[2]),
                      "format": fmt.replace(" ", "_") or None, "coverImage": {"large": w.get("cover")}, "airingSchedule": {"nodes": nodes}}
    return out


def saved_tv(state, shows):
    """{(AniList id, episode): Japanese broadcast} as the last good Syoboi check saved them (tv_times), for when Syoboi is
    down (on 21 Oct 2026 it was, together with AniList); else worked back from schedule.json's Thai release times."""
    if state.get("tv_times"):
        return {(int(k.split(":")[0]), int(k.split(":")[1])): t for k, t in state["tv_times"].items()}
    path = HERE / "schedule.json"
    thai = thai_times()
    by_name = {s["name"]: i for i, s in shows.items() if s["tid"]}
    out = {}
    for w in (json.loads(path.read_text())["shows"] if path.exists() else []):
        i = by_name.get(w["name"])
        for r in w["episodes"] if i else []:
            if r["ep"] == int(r["ep"]) and r["ep"] > shows[i]["offset"]:  # specials (0, .5) left out
                early = thai[i][3] if i in thai and thai[i][1] else 0  # release_time() of this gives the same Thai time back
                out[(i, int(r["ep"]) - shows[i]["offset"])] = r["at"] + early * 86400
    return out


def schedule(state=None):
    """Write schedule.json for the countdown page: every show's episodes from a week ago to two months ahead,
    with the same times and numbering as the alerts. Rewritten only when something changed, so it isn't committed every run."""
    shows, now = anime_list(), int(time.time())
    q = "query($ids:[Int]){Page(perPage:50){media(id_in:$ids){id idMal status episodes season seasonYear format coverImage{large} airingSchedule(perPage:50){nodes{episode airingAt}}}}}"
    state = {} if state is None else state
    try:
        info = {m["id"]: m for m in anilist(q, {"ids": list(shows)})["Page"]["media"]}
        # saved for when AniList is down; TV shows' airing times come from Syoboi, so theirs aren't kept
        state["anilist_media"] = {str(i): {k: v for k, v in m.items() if k != "airingSchedule" or not shows.get(i, {}).get("tid")}
                                  for i, m in info.items()}
    except Exception as e:
        print(f"FAIL anilist: {e} (using its last saved answer)", file=sys.stderr)
        info = saved_media(state, shows)
    try:
        by_tid = {s["tid"]: i for i, s in shows.items() if s["tid"]}
        span = f"{datetime.fromtimestamp(now - 30 * 86400, JST):%Y%m%d_%H%M%S}-{datetime.fromtimestamp(now + 60 * 86400, JST):%Y%m%d_%H%M%S}"  # from a month back: a rerun must not pass for an episode's first broadcast
        xml = get(f"https://cal.syoboi.jp/db.php?Command=ProgLookup&TID={','.join(map(str, by_tid))}&Range={span}")
        tv = {(by_tid[tid], ep): t for (tid, ep), t in tv_aired(xml, {tid: shows[i]["first"] for tid, i in by_tid.items()}, now + 60 * 86400).items()}
        state["tv_times"] = {f"{i}:{ep}": t for (i, ep), t in tv.items()}  # saved for when Syoboi is down
        tv_ok = True
    except Exception as e:
        print(f"FAIL tv schedule: {e} (using the last saved times)", file=sys.stderr)
        tv, tv_ok = saved_tv(state, shows), False
    if not tv:  # keep the previous file rather than publish a half-empty one
        print("FAIL schedule: no TV times", file=sys.stderr)
        return
    videos = youtube_videos()
    thai = thai_times()
    links = state.setdefault("watch_links", {})  # Watch links found earlier: a channel's video drops out of its newest 50 within days
    kept = state.setdefault("special_rows", {})  # specials seen this week: {link: [AniList id, episode, published]}
    for a, n, link, p, ch in specials(videos, shows, now - 8 * 86400):
        kept[link] = [a, n, int(p)]
    pages = state.setdefault("gojo_pages", {})  # {AniList id: [animegojo ซับไทย series page, number shift]}, learned from alerts
    gojo = {}

    def on_gojo(i):  # (shift, {episode number: link} on its page): each page fetched at most once per run
        if i not in gojo:
            url, shift = (GOJO + pages[str(i)][0], pages[str(i)][1]) if str(i) in pages else (OTHER_PAGES[i], GOJO_SHIFT.get(i, 0))
            try:
                gojo[i] = (shift, site_episodes(url))
            except Exception as e:
                print(f"FAIL {site_name(url)} {url}: {e}", file=sys.stderr)
                gojo[i] = (shift, {})
        return gojo[i]
    for link in [k for k, (a, n, p) in kept.items() if p < now - 8 * 86400]:
        del kept[link]
    # the site's Watch button comes with the LINE card, never before it (Magic Repo Man 6, 8 Nov 2026: a premiere's link was
    # on the site at 01:00, its card at 01:55, when it played): episodes whose card is out, not still waiting for a "Watch now"
    carded = set(state.get("anime", [])) - set(state.get("watch_later", {}))
    out, jp = [], {}
    for i, s in shows.items():
        m = info.get(i, {})
        if s["tid"]:
            eps = {ep: t for (j, ep), t in tv.items() if j == i and ep >= 1}
        else:
            eps = {n["episode"]: n["airingAt"] for n in (m.get("airingSchedule") or {}).get("nodes", [])}
        jp[i] = dict(eps)
        rows = []
        for ep, t in sorted(eps.items()):
            t = release_time(t, thai[i]) if i in thai else t
            if now - 8 * 86400 <= t <= now + 60 * 86400:
                yt = t <= now and s["yt"] and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t)
                rec = state.get("first_source", {}).get(f"{i}:{ep}", {})  # the animegojo episode page, if that's where it was found first
                # (episodes found on anime-waku, before it was dropped, keep their link)
                watch = (in_brave(yt) if yt else None) or links.get(f"{i}:{ep}") or (rec["url"] if rec.get("first") in ("animegojo", "suba-anime", "anime-waku", *FEEDS) else None)
                if not watch and t <= now and (str(i) in pages or i in OTHER_PAGES) and not (i in thai and thai[i][0] in YT_CHANNELS.values()):
                    shift, have = on_gojo(i)  # aired, no button yet: the episode on its animegojo (or suba-anime) page, if it's up there
                    watch = have.get(ep + s["offset"] + shift)
                if watch:
                    links[f"{i}:{ep}"] = watch
                rows.append({"ep": ep + s["offset"], "at": t, **({"watch": watch} if watch and f"{i}:{ep}" in carded else {})})  # the platform is the show's own
        for r in rows:  # a whole season out at once (Netflix) is one "EP 01-10" row on the site: its Watch opens the series
            if r.get("watch") and sum(x["at"] == r["at"] for x in rows) > 1:
                r["watch"] = series_page(r["watch"])
        rows += [{"ep": n, "at": p, **({"watch": in_brave(link)} if link in state.get("specials", []) else {})} for link, (a, n, p) in kept.items() if a == i]
        rows.sort(key=lambda r: r["at"])
        if m.get("status") == "FINISHED" and not rows:  # ended, its last episode over a week ago (off Aired too): off the site
            continue
        total = episode_total(m, state)
        out.append({"name": s["name"], "label": season_label(m), "cover": (m.get("coverImage") or {}).get("large"),
                    "total": total and total + s["offset"], "where": thai[i][0] if i in thai else None,
                    "episodes": rows})
    for k in list(links)[:-300]:
        del links[k]
    try:
        week = json.loads(get(BILIBILI_SCHEDULE))["data"]["items"]
        cards = [(c.get("title") or "", int(c["pub_time_ts"]) / 1000, c.get("index_show") or "") for d in week for c in (d.get("cards") or []) if c.get("pub_time_ts")]
    except Exception as e:
        print(f"FAIL bilibili schedule: {e}", file=sys.stderr)
        cards = []
    auto = HERE / "thai_times_auto.json"
    known = json.loads(auto.read_text()) if auto.exists() else {}
    # only from real Syoboi times: ones worked back from schedule.json would "learn" the slots already in it
    learned = {str(k): v for k, v in learn_release_times(shows, jp, videos, cards).items()} if tv_ok else {}
    merged = {k: v for k, v in {**known, **learned}.items() if int(k) in shows}  # a slot stays known after its week scrolls off
    if merged != known:
        auto.write_text(json.dumps(merged, ensure_ascii=False, indent=1))
        print("learned Thai release times:", "; ".join(f"{shows[int(k)]['name']}: {' '.join(x for x in v if x)}" for k, v in merged.items() if known.get(k) != v))
    path = HERE / "schedule.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    if old.get("shows") != out:
        path.write_text(json.dumps({"updated": now, "shows": out}, ensure_ascii=False, indent=1))
    print(f"schedule: {sum(len(x['episodes']) for x in out)} episodes, {'updated' if old.get('shows') != out else 'unchanged'}")


def base_title(t):
    """A show's name without season or cour markers, spacing or punctuation, so listings of the same show compare equal:
    "らんま1/2 (2024) 第3期" and "らんま1/2(2026)", "転生したら剣でした 第2期" and "転生したら剣でしたⅡ"."""
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"第\s*\d+\s*(期|クール|シリーズ)|\d+\s*クール|(season|シーズン)\s*\d+|\d+(st|nd|rd|th)\s*season|part\s*\d+|[(（]\d{4}[)）]|\b(ii|iii|iv)\b|[ⅡⅢⅣⅤ]", "", t)
    return re.sub(r"[\s\W_]+", "", t)


def same_show(a, b):
    """True when two titles name the same show: equal once season markers are set aside, or one is nearly all of the other.
    A franchise name alone ("ジョジョの奇妙な冒険") doesn't match a specific part of it."""
    x, y = sorted((base_title(a), base_title(b)), key=len)
    return bool(x) and x in y and len(x) >= 0.8 * len(y)


def tv_entry(native, start, days_after=21):
    """(TV-schedule id, first episode number) for a show airing between a week before `start` and `days_after` days after it,
    found by its Japanese title; (0, 0) if the TV schedule has no such broadcasts."""
    span = f"{datetime.fromtimestamp(start - 7 * 86400, JST):%Y%m%d_%H%M%S}-{datetime.fromtimestamp(start + days_after * 86400, JST):%Y%m%d_%H%M%S}"
    native = unicodedata.normalize("NFKC", native)  # full-width "２" in AniList vs "2" in the TV schedule
    short = re.split(r"[\s【（(\[]", native)[0]  # the search can miss "アオのハコ Season2" but finds "アオのハコ"
    for key in dict.fromkeys((native, native[:8], short)):  # a long title may need its start only
        found = json.loads(get("https://cal.syoboi.jp/json.php?Req=TitleSearch&Limit=10&Search=" + quote(key))).get("Titles") or {}
        for t in sorted(found.values(), key=lambda t: -int(t["TID"]))[:6]:  # newest entry first: the current season
            if not same_show(t["Title"], native):
                continue
            xml = get(f"https://cal.syoboi.jp/db.php?Command=ProgLookup&TID={t['TID']}&Range={span}")
            counts = [int(c) for c in re.findall(r"<Count>(\d+)</Count>", xml)]
            if counts:  # it really airs around this show's premiere
                return int(t["TID"]), min(counts)
    return 0, 0


def complete_anime():
    """Fill in short anime.txt lines ("name | AniList id", as the LINE command adds them, maybe with a Thai name sent by
    the LINE "thai" command in the last field) with the Japanese TV schedule details and YouTube title keywords,
    so they get TV times and Thai-sub links like the hand-made entries. A Thai name already there is kept."""
    path = HERE / "anime.txt"
    lines = path.read_text().splitlines()
    done = []
    for n, line in enumerate(lines):
        r = [x.strip() for x in line.split("|", 5)]
        pending = len(r) == 2 or (len(r) == 6 and not r[2] and not r[3])  # "name | id" or "name | id |  |  |  | Thai name"
        if line.startswith("#") or not pending or not r[1].isdigit():
            continue
        name, aid = r[0], int(r[1])
        m = anilist("query($i:Int){Media(id:$i){format title{romaji english native} synonyms airingSchedule(perPage:1){nodes{airingAt}}}}", {"i": aid})["Media"]
        start = ((m.get("airingSchedule") or {}).get("nodes") or [{}])[0].get("airingAt")
        tid, first = 0, 0
        if m["format"] in ("TV", "TV_SHORT", "ONA") and m["title"].get("native"):  # online releases can air on TV too
            # no air date on AniList yet: look for broadcasts over the next two months instead
            tid, first = tv_entry(m["title"]["native"], start) if start else tv_entry(m["title"]["native"], int(time.time()), 60)
        if not start and not tid:
            continue  # nothing to go on yet: stays short and is tried again on a later check
        # the English and romaji names (before any subtitle), plus any Thai title AniList knows, for matching YouTube uploads
        words = {t.split(":")[0].strip().lower() for t in (m["title"]["english"], m["title"]["romaji"]) if t}
        words |= {x.lower() for x in m.get("synonyms") or [] if re.search(r"[\u0E00-\u0E7F]", x)}
        keywords = "|".join([re.escape(x) for x in sorted(words) if len(x) >= 4] + ([r[5]] if len(r) == 6 and r[5] else []))
        lines[n] = f"{name} | {aid} | {tid} | {first} | 0 | {keywords}"
        done.append(f"{name}: TV schedule {tid or 'none, AniList times'}, first episode {first or '-'}")
    if done:
        path.write_text("\n".join(lines) + "\n")
        print("completed anime:", "; ".join(done))


def anime(state, meanwhile=lambda: None):
    """Notify when a show in anime.txt airs a new episode: the Japanese TV schedule (Syoboi Calendar, earliest channel)
    for TV shows, AniList's airing schedule for online-only ones. anime.txt: name | AniList id | TV-schedule id (0 = none)
    | first episode number in the TV schedule | episodes before this AniList entry (added to the count).
    The Muse/Ani-One cards go first; then `meanwhile` (the manga checks) runs, and only then the other sites, which wait
    for the Thai VPN feed. Returns (youtube_check, what's still to come from the channels) for wait_for_youtube."""
    shows = anime_list()
    now = int(time.time())
    query = ("query($ids:[Int],$web:[Int],$a:Int,$b:Int){info:Page(perPage:50){media(id_in:$ids){id idMal episodes season seasonYear format coverImage{large}}}"
             " web:Page(perPage:50){airingSchedules(mediaId_in:$web,airingAt_greater:$a,airingAt_lesser:$b){episode airingAt media{id}}}}")
    web_only = [i for i, s in shows.items() if not s["tid"]]
    try:
        data = anilist(query, {"ids": list(shows), "web": web_only or [0], "a": now - 8 * 86400, "b": now + 8 * 86400})
    except Exception as e:  # AniList down: its last answer, so the TV shows (Syoboi) and YouTube links still alert
        print(f"FAIL anilist: {e} (using its last saved answer)", file=sys.stderr)
        saved = saved_media(state, shows)
        data = {"info": {"media": list(saved.values())}, "web": {"airingSchedules": [
            {"episode": n["episode"], "airingAt": n["airingAt"], "media": {"id": i}} for i in web_only if i in saved
            for n in (saved[i].get("airingSchedule") or {}).get("nodes", []) if n["airingAt"] > now - 8 * 86400]}}
    info = {m["id"]: m for m in data["info"]["media"]}
    ahead = now + 8 * 86400  # a Thai release can come before Japanese TV (Chitose: a week), so look past now
    aired = {(a["media"]["id"], a["episode"]): a["airingAt"] for a in data["web"]["airingSchedules"] if a["airingAt"] <= ahead}
    by_tid = {s["tid"]: i for i, s in shows.items() if s["tid"]}
    if by_tid:
        span = f"{datetime.fromtimestamp(now - 30 * 86400, JST):%Y%m%d_%H%M%S}-{datetime.fromtimestamp(ahead, JST):%Y%m%d_%H%M%S}"  # from a month back: a rerun must not pass for an episode's first broadcast
        try:
            xml = get(f"https://cal.syoboi.jp/db.php?Command=ProgLookup&TID={','.join(map(str, by_tid))}&Range={span}")
            first = {tid: shows[i]["first"] for tid, i in by_tid.items()}
            aired.update({(by_tid[tid], ep): t for (tid, ep), t in tv_aired(xml, first, ahead).items()})
        except Exception as e:  # Syoboi down: the times the last good check saved
            print(f"FAIL tv schedule: {e} (using the last saved times)", file=sys.stderr)
            aired.update({k: t for k, t in saved_tv(state, shows).items() if t <= ahead})
    seed = "anime" not in state  # first run: record the past week's episodes silently
    seen = state.setdefault("anime", [])
    thai = thai_times()  # shows released later with Thai subs: alert at that time instead of the Japanese broadcast
    gojo_only = lambda i: not (i in thai and thai[i][0] in YT_CHANNELS.values())  # not on Muse/Ani-One: wait for animegojo, no scheduled card
    aired = {k: (release_time(t, thai[k[0]]) if k[0] in thai else t) for k, t in aired.items()}
    aired = {k: t for k, t in aired.items() if t >= now - 8 * 86400}  # the past week only (older episodes were alerted then)
    due = dict(aired)  # every episode due from a week ago to a week ahead: a number outside it is not trusted
    pending = state.setdefault("watch_later", {})  # episodes alerted before their Thai-sub video showed up: {key: release}
    log = state.setdefault("first_source", {})  # per episode: where it was first seen with Thai subs, link, times, alerted
    # YouTube's list of a channel's newest uploads doesn't always include the newest video (it depends on which server answers),
    # so a video found once, by an earlier check or the schedule, counts: {key: YouTube link or None}
    found_before = lambda key: ("https://www.youtube.com/watch?v=" + l[len(BRAVE):]) if (l := state.get("watch_links", {}).get(key, "")).startswith(BRAVE) else None

    def youtube_check(videos):
        """The cards that come from Muse and Ani-One, each sent once it's due: new episodes, "Watch now" and specials.
        Returns (the episodes due by now that had no card yet, {what: (when, channel)} for what is still to come from
        the channels): an episode due whose video isn't up yet (when it was due), a premiere (when it's scheduled to
        play: its card waits until then) and an episode due later (when it's due). Run by the check, then again by
        wait_for_youtube with newer readings of the channels."""
        now = int(time.time())
        learn_thai_titles(shows, videos, due, thai)  # (new Thai names go into anime.txt and shows, before anything matches)
        later = {}
        aired = {k: t for k, t in due.items() if t <= now}
        new = sorted((t, i, ep) for (i, ep), t in aired.items() if f"{i}:{ep}" not in seen)
        for t, i, ep in new:
            if not seed:
                s, m = shows[i], info.get(i, {})
                if gojo_only(i):
                    continue  # wait for animegojo (handled below): no card at the scheduled time, no video link exists
                total = episode_total(m, state)
                label = f" ({season_label(m)})" if season_label(m) else ""  # e.g. "Fall 2026, TV"
                # if an official channel already has this episode up with Thai subs, the card opens that video; otherwise no link
                yt = s["yt"] and (youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t) or found_before(f"{i}:{ep}"))
                if not yt and i in thai and thai[i][0] in YT_CHANNELS.values() and now - t < 60 * 60:
                    later[f"{i}:{ep}"] = (t, thai[i][0])
                    continue  # its channel may post a little late: wait up to an hour so the alert is one card with the link
                v = next((v for v in videos if v[1] == yt), None)
                if v and v[2] > now:
                    later[f"{i}:{ep}"] = (v[2], v[3])
                    continue  # a premiere that hasn't started (Ani-One, 3 Nov 2026: 22:40, not 22:30): the card comes when it's scheduled to play
                # the time it came out: the video's start when that's later than the slot (a later premiere or upload)
                line = episode_line(ep + s["offset"], total and total + s["offset"], max(t, v[2]) if v else t, thai[i][0] if i in thai else None)
                if not yt and s["yt"]:  # the video may still come later: send a "Watch now" card then
                    pending[f"{i}:{ep}"] = t
                notify(f"New episode: {s['name']}{label}", line, line + (f"\n{yt}" if yt else ""), (m.get("coverImage") or {}).get("large"), yt and in_brave(yt))
                log[f"{i}:{ep}"] = {"title": s["name"], "episode": ep + s["offset"], "version": "ซับไทย", "first": v[3] if v else (thai[i][0] if i in thai else "Japanese TV"),
                                    "url": yt or None, "detected": now, "published": v and int(v[2]), "scheduled": int(t), "notified": True,
                                    **({} if v else {"order": "uncertain"})}  # no video: the platform's time is a schedule, not a verified release
            seen.append(f"{i}:{ep}")
        for (i, ep), t in due.items():  # due later: at its time, or its premiere's if the channel has one set for later
            if t > now and not seed and not gojo_only(i) and f"{i}:{ep}" not in seen:
                s = shows[i]
                yt = s["yt"] and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t)
                later[f"{i}:{ep}"] = (max([t] + [v[2] for v in videos if yt and v[1] == yt]), thai[i][0])
        for key, t in list(pending.items()):
            i, ep = map(int, key.split(":"))
            s = shows.get(i)
            yt = s and (videos and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t) or found_before(key))
            premiere = next(((p, ch) for _, link, p, ch in videos if yt and link == yt and p > now), None)
            if premiere:
                later[key] = premiere
                continue  # a premiere that hasn't started: "Watch now" once it plays
            if yt:
                where = next((ch for title, link, p, ch in videos if link == yt), "YouTube")
                line = f"Episode {ep + s['offset']:02d} with Thai subs is up on {where}"
                m = info.get(i, {})
                notify(f"Watch now: {s['name']}", line, f"{line}\n{yt}", (m.get("coverImage") or {}).get("large"), in_brave(yt))
            if yt or not s or now - t > 2 * 86400:  # sent, show removed, or no video within 2 days
                del pending[key]
        sent = state.setdefault("specials", [])  # links of specials already alerted
        for i, n, link, published, ch in specials(videos, shows, now - 2 * 86400):
            if link in sent:
                continue
            if published > now:  # a premiere: alert once it plays
                later[link] = (published, ch)
                continue
            m = info.get(i, {})
            line = f"Special episode {n}\n{ch} · {datetime.fromtimestamp(published, THAI):%d %b %Y %H:%M}"
            notify(f"New special: {shows[i]['name']}", line, f"{line}\n{link}", (m.get("coverImage") or {}).get("large"), in_brave(link))
            sent.append(link)
        del sent[:-50]
        return new, later

    new, later = youtube_check(youtube_videos() if not seed else [])  # the run's one reading, shared with the schedule
    meanwhile()  # before the other sites: they wait for the Thai VPN feed
    try:
        found = gojo_latest(get(GOJO + "/"), shows) if not seed else []
    except Exception as e:
        print(f"FAIL animegojo: {e}", file=sys.stderr)
        found = []
    # the home page only lists animegojo's newest uploads and moves fast, so also check each show's own ซับไทย page
    # (learned from alerts) from a day before an episode is due
    for k, (path, shift) in (state.get("gojo_pages", {}) if not seed else {}).items():
        i = int(k)
        if i not in shows or not gojo_only(i):
            continue
        waiting = [e for (j, e), t in due.items() if j == i and f"{i}:{e}" not in seen and now >= t - 86400]
        if not waiting:
            continue
        try:
            have = gojo_sub_episodes(get(GOJO + path))
        except Exception as e:
            print(f"FAIL animegojo {path}: {e}", file=sys.stderr)
            continue
        found += [(i, n, f"{GOJO}{path}#ep-{n}") for n in (e + shows[i]["offset"] + shift for e in waiting) if n in have]
    # a show with no known page: search animegojo for it. Pages there at the first look are older seasons (Edgerunners
    # Season 1 already lists 1-10); a page counts once it is new or gains an episode that is due, from a day before then
    search = state.setdefault("gojo_search", {})  # {AniList id: {page: its episodes at the first look}}
    for i, s in shows.items():
        if seed or not gojo_only(i) or not s["yt"] or str(i) in state.get("gojo_pages", {}):
            search.pop(str(i), None)
            continue
        due_eps = [(e, t) for (j, e), t in due.items() if j == i and f"{i}:{e}" not in seen]
        if not due_eps or (str(i) in search and not any(now >= t - 86400 for e, t in due_eps)):
            continue
        words = [k.replace("\\", "") for k in s["yt"].split("|")]
        term = next((k for k in words if k.isascii() and len(k) >= 4), words[0])
        try:
            current = {}
            for path in list(dict.fromkeys(re.findall(r'href="(/list/\d+/)"', get(f"{GOJO}/search?name={quote(term)}"))))[:8]:
                page = get(GOJO + path)
                title = re.search(r"<title>([^<]*)", page)
                if title and re.search(s["yt"], html.unescape(title[1]).lower()):
                    current[path] = sorted(gojo_sub_episodes(page))
        except Exception as e:
            print(f"FAIL animegojo search {term}: {e}", file=sys.stderr)
            continue
        first_look = search.setdefault(str(i), current)
        for path, eps in current.items():
            added = set(eps) - set(first_look.get(path, []))
            shift = next((sh for sh in dict.fromkeys((0, GOJO_SHIFT.get(i, 0))) if any(e + s["offset"] + sh in added for e, t in due_eps)), None)
            if shift is not None:
                state.setdefault("gojo_pages", {})[str(i)] = [path, shift]
                found += [(i, e + s["offset"] + shift, f"{GOJO}{path}#ep-{e + s['offset'] + shift}") for e, t in due_eps if e + s["offset"] + shift in added]
                break
    # every site that has an episode competes, and the fastest one gets the episode's one card: animeruka
    # by its feed's upload times; animegojo and suba-anime show none, so an episode first seen on them in this check is
    # taken as uploaded halfway between the last check and this one
    last = state.get("anime_checked")
    mid = (last + now) // 2 if last and 0 < now - last <= 2 * 3600 else now
    state["anime_checked"] = now
    hits = [(mid, "animegojo", i, n, link) for i, n, link in found]
    for site, url in (FEEDS.items() if not seed else []):
        try:
            hits += [(ts, site, i, n, link) for i, n, link, ts in feed_episodes(read_feed(url), shows)]
        except Exception as e:
            print(f"FAIL {site}: {e}", file=sys.stderr)
    to_ep = lambda i, n: next((e for e in (n - shows[i]["offset"], n - shows[i]["offset"] - GOJO_SHIFT.get(i, 0)) if (i, e) in due), None)
    # suba-anime is for Yozakura only, and races the other sites for every episode
    for key in ("suba_pages", "suba_titles", "suba_search", "suba_dropped"):  # left from earlier ways of using suba-anime
        state.pop(key, None)
    for i, url in (OTHER_PAGES.items() if not seed else []):
        if i not in shows or not gojo_only(i):
            continue
        waiting = [e for (j, e), t in due.items() if j == i and f"{i}:{e}" not in seen and now >= t - 86400]
        if not waiting:
            continue
        try:
            have = site_episodes(url)
        except Exception as e:
            print(f"FAIL {site_name(url)} {url}: {e}", file=sys.stderr)
            continue
        # its numbers carry on through the season (Cour 2's episode 1 is its 13): only the shifted number is this cour's
        hits += [(mid, "suba-anime", i, n, have[n]) for e in waiting for n in [e + shows[i]["offset"] + GOJO_SHIFT.get(i, 0)] if n in have]
    pages_read = {}
    for t, site, i, n, link in sorted(hits, key=lambda h: h[0]):  # fastest first
        s, m = shows[i], info.get(i, {})
        ep = to_ep(i, n)
        if ep is None or f"{i}:{ep}" in seen or not gojo_only(i) or not due[(i, ep)] - 8 * 86400 <= t <= now:
            continue  # shows on Muse/Ani-One are alerted from there only; an old post is an earlier season's
        # every episode of the show on this site, by this cour's numbering
        if site in FEEDS:
            on_site = {to_ep(j, k): (l, ts) for ts, w, j, k, l in sorted(hits, key=lambda h: h[0], reverse=True) if w == site and j == i}
        else:  # animegojo or suba-anime: read the series page (checks animegojo's is ซับไทย)
            page_url = OTHER_PAGES[i] if site == "suba-anime" else link.split("#")[0]
            try:
                if page_url not in pages_read:
                    pages_read[page_url] = site_episodes(page_url)
            except Exception as e:
                print(f"FAIL {site} {link}: {e}", file=sys.stderr)
                continue
            shift = n - s["offset"] - ep
            on_site = {k - s["offset"] - shift: (l, None) for k, l in pages_read[page_url].items()}
            if site == "animegojo":
                state.setdefault("gojo_pages", {})[str(i)] = [page_url[len(GOJO):], shift]  # for the website's Watch buttons
        # every not-yet-alerted episode due at the same time as this one: a whole season released at once gets one card
        batch = [e for (j, e), d in sorted(due.items()) if j == i and d == due[(i, ep)] and f"{i}:{e}" not in seen and e in on_site]
        if not batch:
            continue
        total = episode_total(m, state)
        tot = f"{total + s['offset']:02d}" if total else "?"
        a, b = batch[0] + s["offset"], batch[-1] + s["offset"]
        link = on_site[batch[0]][0] if len(batch) == 1 else series_page(on_site[batch[0]][0])  # several at once: the series
        # when it aired on its platform (Japanese TV if none is set), not when the site posted it
        when = f"{'' if i in thai else 'Aired '}{datetime.fromtimestamp(due[(i, batch[0])], THAI):%d %b %Y %H:%M}"
        line = f"Episode {a:02d}{f'-{b:02d}' if b != a else ''}/{tot} · {site}\n{f'{thai[i][0]} · ' if i in thai else ''}{when}"
        label = f" ({season_label(m)})" if season_label(m) else ""
        notify(f"New episode: {s['name']}{label}", line, f"{line}\n{link}", (m.get("coverImage") or {}).get("large"), link)
        for e in batch:
            seen.append(f"{i}:{e}")
            pending.pop(f"{i}:{e}", None)
            log[f"{i}:{e}"] = {"title": s["name"], "episode": e + s["offset"], "version": "ซับไทย", "first": site, "url": on_site[e][0],
                               "detected": now, "published": on_site[e][1], "notified": True,
                               **({} if on_site[e][1] else {"order": "estimated"})}  # no upload time: the site's place in the race is estimated
    for key in list(log)[:-300]:
        del log[key]
    print(f"anime: {len(shows)} watched, {len(new)} {'seeded' if seed else 'new'}, {len(pending)} waiting for a Thai-sub video")
    return (youtube_check if not seed else None), later


def wait_for_youtube(check, later):
    """Keep the run going for what Muse and Ani-One are about to post, rather than leaving it to the next check up to 5
    minutes later: an episode due whose video isn't up yet (on 29 Oct 2026 Ani-One's 19:30 video only showed in YouTube's
    list at 19:50), a premiere (its card at the time it's scheduled to play) and an episode due before the next check
    starts. The channels concerned are read again, every 5 seconds in the first minute after something was due and every
    15 after that, and check (anime's youtube_check) sends each card as soon as it's due. Something due before the next
    check is looked for until a minute past its time; the rest until 4.5 minutes into the job, when the next check takes
    over. Never on the Mac, nor without YOUTUBE_KEY (the public feeds GitHub gets can be hours old)."""
    if not check or not os.environ.get("LINE_TOKEN") or not os.environ.get("YOUTUBE_KEY"):
        return
    videos, read, began, waited = youtube_videos(), 0, time.time(), set()
    until = lambda when: max(JOB_START + CHECK_EVERY - 30, when + 60)
    while True:
        now = time.time()
        look = {k: (w, ch) for k, (w, ch) in later.items() if w <= JOB_START + CHECK_EVERY and now < until(w)}
        if not look:
            break
        recent = any(now - 60 <= w <= now for w, _ in look.values())
        at = max(min(w for w, _ in look.values()), read + (5 if recent else 15))
        if at >= max(until(w) for w, _ in look.values()):
            break
        waited |= set(look)
        time.sleep(max(0, at - now) + 0.2)
        channels = {ch for w, ch in look.values() if w <= time.time()}
        fresh = read_youtube(channels)
        read = time.time()
        videos = [v for v in videos if v[3] not in channels] + fresh
        later = check(videos)[1]
    if waited:
        done = Future()
        done.set_result(videos)
        VIDEOS[:] = [done]  # the schedule (Watch links on the website) gets the newest reading too
        left = sorted(k for k in waited if k in later)
        print(f"youtube: waited {time.time() - began:.0f}s for {', '.join(sorted(waited))}"
              + (f"; still to come: {', '.join(left)}" if left else ""))


def setup_richmenu():
    """Install the Alert Watch menu at the bottom of the LINE chat: four buttons, the anime countdown, its timetable, what aired and F1 Live.
    Run once (workflow input richmenu); replaces any earlier menu this created."""
    auth = {"Authorization": f"Bearer {os.environ['LINE_TOKEN']}"}

    def call(method, url, body=None, ctype="application/json"):
        req = urllib.request.Request(url, data=body, method=method, headers={**auth, **({"Content-Type": ctype} if body else {})})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}")
        except urllib.error.HTTPError as e:
            print(f"FAIL line {method} {url}: {e} {e.read()[:300]}", file=sys.stderr)
            raise

    site = "https://isaraapae.github.io/alert-watch/anime/"
    menu = {"size": {"width": 2500, "height": 843}, "selected": True, "name": "Alert Watch", "chatBarText": "Menu",
            "areas": [{"bounds": {"x": 625 * n, "y": 0, "width": 625, "height": 843}, "action": {"type": "uri", "label": label, "uri": uri}}
                      for n, (label, uri) in enumerate([("Next", site), ("Timetable", site + "?v=week"), ("Aired", site + "?v=aired"), ("F1 Live", F1_PAGE)])]}
    old = [m["richMenuId"] for m in call("GET", "https://api.line.me/v2/bot/richmenu/list")["richmenus"] if m["name"] == "Alert Watch"]
    rid = call("POST", "https://api.line.me/v2/bot/richmenu", json.dumps(menu).encode())["richMenuId"]
    call("POST", f"https://api-data.line.me/v2/bot/richmenu/{rid}/content", (HERE / "richmenu.png").read_bytes(), "image/png")
    call("POST", f"https://api.line.me/v2/bot/user/all/richmenu/{rid}")
    for o in old:
        call("DELETE", f"https://api.line.me/v2/bot/richmenu/{o}")
    print(f"rich menu set: {rid} (replaced {len(old)})")


def series_slug(url):
    return [p for p in urlparse(unquote(url)).path.split("/") if p and p != "list"][-1]


def library(state):
    """library.json for the LINE "list" cards: every manga with its cover, latest chapter and where its card opens.
    Covers are fetched once per series and reused; the file is rewritten only when something changed."""
    path = HERE / "library.json"
    old = {m["url"]: m for m in (json.loads(path.read_text())["manga"] if path.exists() else [])}
    out = []
    for url in (HERE / "urls.txt").read_text().split():
        prev = old.get(url, {})
        if "cover" in prev and url not in COVER_OVERRIDES and (url not in WEBTOON_TWINS or prev.get("title")):  # a WEBTOON one: its title once
            title, image = prev.get("title"), prev["cover"]
        else:
            title, image = cover(WEBTOON_TWINS.get(url, url))  # a manga-site series also on WEBTOON shows WEBTOON's title and cover
            title = title if "webtoons.com" in WEBTOON_TWINS.get(url, url) else None  # other sites' page titles are full of extra words
            image = quote(image, safe=":/%?=&") if image and re.search(r"\.(jpe?g|png)$", urlparse(image).path, re.I) else None
        ids = state.get(url) or []
        latest = max(ids, key=lambda c: [int(x) for x in re.findall(r"\d+", c)] or [0]) if ids else ""
        # and opens there, unless the site is ahead of WEBTOON's newest free chapter
        twin = WEBTOON_TWINS.get(url)
        wt = twin if twin and (not ids or max(map(chapter_number, ids)) <= state.get("webtoon_newest", {}).get(twin, 0)) else url
        tap = WEBTOON_APP + re.search(r"title_no=(\d+)", wt)[1] if "webtoons.com" in wt else quote(url, safe=":/%")
        out.append({"url": url, "name": title or series_slug(url), "title": title, "cover": image,
                    "latest": state.get("chapter_labels", {}).get(url) or (f"Chapter {chapter_number(latest):g}" if latest else ""), "open": tap})
    if old != {m["url"]: m for m in out} or len(old) != len(out):
        path.write_text(json.dumps({"manga": out}, ensure_ascii=False, indent=1))


def manga(state, urls):
    """The manga and WEBTOON alerts, from the pages main started reading in the background."""
    state.pop("webtoon_switched", None)  # left from the automatic switch, which is gone
    state.pop("webtoon_free", None)  # replaced by webtoon_app
    state.pop("webtoon_reach", None)  # replaced by chapter_labels
    labels = state.setdefault("chapter_labels", {})  # {WEBTOON series: "Chapter 140", its newest (in the app), for the LINE list}
    newest = state.setdefault("webtoon_newest", {})  # {WEBTOON series: its newest free chapter}, for the series in WEBTOON_TWINS
    for url in urls:
        name = series_slug(url)
        try:
            found = webtoon(url) if "webtoons.com" in url else chapters(url, get(url))
        except Exception as e:
            print(f"FAIL {name}: {e}", file=sys.stderr)
            continue
        if not found:  # blocked / layout changed: keep old state, don't treat as "no chapters"
            print(f"EMPTY {name}", file=sys.stderr)
            continue
        no = "webtoons.com" in url and re.search(r"title_no=(\d+)", url)[1]
        real = {}  # a WEBTOON series' episodes as chapters, from their titles: {"EP.48": 47.0} (its own numbers count notices too)
        if no:
            try:
                real = webtoon_chapters(url, state)
                labels[url] = f"Chapter {max(real.values()) + WEBTOON_LOCKED.get(no, 0):g}"  # the app's newest, for the LINE list
            except Exception as e:  # keep the last one
                print(f"FAIL webtoon {name}: {e}", file=sys.stderr)
        twin = WEBTOON_TWINS.get(url)
        if twin:  # how far WEBTOON is, to open whichever is ahead
            try:
                tno = re.search(r"title_no=(\d+)", twin)[1]
                if tno in WEBTOON_WEEKLY:  # one more every week
                    ep, since = WEBTOON_WEEKLY[tno]
                    app = ep + max(0, int((time.time() - since) // (7 * 86400)))
                else:
                    app = max(webtoon_chapters(twin, state).values(), default=0) + WEBTOON_LOCKED.get(tno, 0)
                seen_app = state.setdefault("webtoon_app", {})  # {WEBTOON series: the app's newest at the last check}
                # a new one in the app, ahead of the manga site (one the site has came with the site's card)
                if (tno in WEBTOON_LOCKED or tno in WEBTOON_WEEKLY) and twin in seen_app and app > seen_app[twin] \
                        and app > max(map(chapter_number, found)):
                    title, image = cover(twin)
                    line = f"Chapter {app:g}"
                    notify(f"New chapter: {title or name}", line, f"{line}\nlinewebtoon://episodeList/webtoon?titleNo={tno}", image, WEBTOON_APP + tno)
                seen_app[twin] = app
                newest[twin] = app
            except Exception as e:  # keep the last count
                print(f"FAIL webtoon {name}: {e}", file=sys.stderr)
        new = sorted(set(found) - set(state[url])) if url in state else []  # first sight of a url = seed silently
        if new and twin and re.search(r"title_no=(\d+)", twin)[1] in {*WEBTOON_LOCKED, *WEBTOON_WEEKLY}:
            # the app's own card already gave every chapter up to its count (Chapter 99 at Tuesday 22:00): the manga site
            # catching up to one of them (Chapter 96 at 00:10) is no news
            old = [c for c in new if chapter_number(c) <= newest.get(twin, 0)]
            if old:
                print(f"{name}: {', '.join(f'Chapter {chapter_number(c):g}' for c in old)} already in WEBTOON's card, not sent again")
            new = [c for c in new if c not in old]
        if new:
            number = lambda c: real.get(c) or chapter_number(c)  # "ซีซั่น-1/ตอนที่-216" -> 216, WEBTOON's "EP.48" -> 47 (its title's)
            new = sorted(new, key=number)
            shown = chapter_runs(map(number, new))[:200]
            if "webtoons.com" in url:  # a cover card that opens the series straight in the WEBTOON app
                eps = sorted(int(c[3:]) for c in new)  # WEBTOON's own numbers: the card opens the first new one, so you read in order
                if no in WEBTOON_SERIES_PAGE:
                    text, tap = f"{shown}\nlinewebtoon://episodeList/webtoon?titleNo={no}", WEBTOON_APP + no
                else:
                    text = "\n".join(f"Chapter {number(f'EP.{e}'):g}\nlinewebtoon://viewer/webtoon?titleNo={no}&episodeNo={e}" for e in eps)
                    tap = f"{WEBTOON_APP}{no}&e={eps[0]}"
                title, image = cover(url)  # LINE cards only open web addresses, so the card goes via a page that hands off to the app
                notify(f"New chapter: {title or name}", labels[url] if no in WEBTOON_LOCKED and url in labels else shown, text, image, tap)
            elif twin and max(map(chapter_number, new)) <= newest.get(twin, 0):  # WEBTOON has it too: the card opens it there
                no = re.search(r"title_no=(\d+)", twin)[1]
                title, image = cover(twin)
                notify(f"New chapter: {title or name}", shown, f"{shown}\nlinewebtoon://episodeList/webtoon?titleNo={no}",
                       image, WEBTOON_APP + no)
            else:  # cover card that opens the first new chapter; the text version lists each new chapter's link
                links = {c: quote(found[c], safe=":/%?=&#") for c in new}
                text = "\n".join(f"Chapter {number(c):g}\n{links[c]}" for c in new)
                image = cover(url)[1]
                title = twin and cover(twin)[0]  # a series also on WEBTOON: its WEBTOON title, as in the LINE list
                notify(f"New chapter: {title or name}", shown, text, image and quote(image, safe=":/%?=&"), links[new[0]])
        print(f"{name}: {len(found)} chapters, {len(new)} new")
        state[url] = sorted(set(found) | set(state.get(url, [])))


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    # the manga and WEBTOON pages, read in the background while the anime and F1 checks start (manga() takes them)
    urls = (HERE / "urls.txt").read_text().split()
    twins = [WEBTOON_TWINS[u] for u in urls if u in WEBTOON_TWINS and re.search(r"title_no=(\d+)", WEBTOON_TWINS[u])[1] not in WEBTOON_WEEKLY]
    # first in the queue: the uploads both the alerts and the schedule need, and Bilibili's week (slow, seconds)
    if not VIDEOS:
        VIDEOS.append(POOL.submit(read_youtube))
    prefetch([BILIBILI_SCHEDULE])
    prefetch([webtoon_rss(u) if "webtoons.com" in u else u for u in urls] + [webtoon_rss(t) for t in twins])

    def run_f1():
        try:
            f1(state)
        except Exception as e:  # F1 results must never stop the other alerts
            print(f"FAIL f1: {e}", file=sys.stderr)
    # F1 alongside the anime and manga checks and the wait for YouTube (it can wait minutes for a session to start, which
    # must hold up no other alert); it touches only its own f1_* keys
    f1_thread = threading.Thread(target=run_f1)
    f1_thread.start()
    try:
        complete_anime()
    except Exception as e:  # a short entry still works with AniList times, so this must never stop the alerts
        print(f"FAIL completing anime: {e}", file=sys.stderr)
    youtube, later = anime(state, lambda: manga(state, urls))  # manga between the YouTube cards and the VPN feed's wait
    try:
        wait_for_youtube(youtube, later)
    except Exception as e:  # waiting must never cost the run its alerts' saved state
        print(f"FAIL waiting for YouTube: {e}", file=sys.stderr)
    f1_thread.join()
    try:
        schedule(state)
    except Exception as e:  # the countdown page must never stop the alerts
        print(f"FAIL schedule: {e}", file=sys.stderr)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    try:
        library(state)
    except Exception as e:  # the LINE list cards must never stop the alerts
        print(f"FAIL library: {e}", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        from unittest import mock
        h = ('<a href="https://x.net/manga/a/%e0%b8%95-2/">2</a><a href="https://x.net/other/">o</a>'
             '<a href="https://x.net/manga/a/%e0%b8%95-3/"> ตอนที่ 3 - อัพเดท </a><a href="https://x.net/manga/a/%e0%b8%95-3/">last</a>'
             '<li data-num="{{number}}"><a href="#/chapter-{{number}}"></a><li data-num="7"> <div>\n<a href="https://x.net/a-7/">7</a>')
        want = {"ต-2": "https://x.net/manga/a/%e0%b8%95-2/", "7": "https://x.net/a-7/"}
        assert chapters("https://x.net/manga/a/", h) == want, chapters("https://x.net/manga/a/", h)
        assert chapter_runs([343, 340, 341, 342, 344, 345]) == "Chapter 340-345"
        assert chapter_runs([340, 341, 343, 9.1]) == "Chapter 9.1, 340-341, 343"
        assert chapter_runs([216]) == "Chapter 216"
        # the starting grid: same as qualifying -> nothing; changed -> sent on the second check that sees it
        st = {"f1_final": {"9": [[63, 2, None], [1, 1, None], [16, 3, None]]}}
        q = {"session_key": 9, "session_name": "Qualifying", "date_start": "2026-11-07T18:00:00+00:00", "meeting_key": 1}
        race = {"session_name": "Race", "date_start": "2099-01-01T00:00:00+00:00"}
        g = lambda order: json.dumps([{"position": p, "driver_number": n, "lap_duration": 87.5} for p, n in enumerate(order, 1)])
        with mock.patch(__name__ + ".f1_drivers", return_value={}), mock.patch(__name__ + ".line_send", return_value=True) as send:
            with mock.patch(__name__ + ".get", return_value=g([1, 63, 16])):
                f1_grid(st, "t", q, {}, race)
            with mock.patch(__name__ + ".get", return_value=g([1, 16, 63])):
                f1_grid(st, "t", q, {}, race)
                assert not send.called and st["f1_grid_seen"]["9"]
                f1_grid(st, "t", q, {}, race)
            assert send.call_count == 1 and "63 P2 → P3" in send.call_args[0][1]["altText"], send.call_args
            assert st["f1_grid"]["9"] == [[1, 1], [16, 2], [63, 3]] and "9" not in st["f1_grid_seen"]
        rss = "<item><link>https://www.webtoons.com/th/x/ep7-a/viewer?title_no=1&amp;episode_no=7</link></item>"
        with mock.patch(__name__ + ".get", lambda u: rss if "/rss?title_no=1" in u and "://www." in u else ""):
            assert webtoon("https://m.webtoons.com/th/x/y/list?title_no=1") == {"EP.7": "https://www.webtoons.com/th/x/ep7-a/viewer?title_no=1&episode_no=7"}
        assert episode_line(5, 12, 1790866800) == "Episode 05/12\nAired 01 Oct 2026 22:00", episode_line(5, 12, 1790866800)
        assert episode_line(3, None, 1790866800).startswith("Episode 03/?")
        sat2330 = datetime(2026, 10, 3, 23, 30, tzinfo=THAI).timestamp()  # Magic Repo Man on Japanese TV
        assert datetime.fromtimestamp(thai_slot(sat2330, "Sun", "01:00"), THAI) == datetime(2026, 10, 4, 1, 0, tzinfo=THAI)
        sun2145 = datetime(2026, 10, 4, 21, 45, tzinfo=THAI).timestamp()
        assert datetime.fromtimestamp(thai_slot(sun2145, "Sun", "22:00"), THAI) == datetime(2026, 10, 4, 22, 0, tzinfo=THAI)
        assert datetime.fromtimestamp(thai_slot(sun2145, "Sun", "21:00"), THAI) == datetime(2026, 10, 11, 21, 0, tzinfo=THAI)  # never before the broadcast
        assert episode_line(2, 12, sun2145, "Ani-One Thailand").endswith("\nAni-One Thailand · 04 Oct 2026 21:45")
        tue2100 = datetime(2026, 10, 13, 21, 0, tzinfo=THAI).timestamp()  # Chitose on Japanese TV; Bilibili a week earlier
        assert datetime.fromtimestamp(release_time(tue2100, ("Bilibili", "Tue", "23:30", 7)), THAI) == datetime(2026, 10, 6, 23, 30, tzinfo=THAI)
        assert release_time(tue2100, ("Netflix", None, None, 0)) == tue2100
        v = [("[พากย์ไทย] ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ซีซั่น 2 ตอนที่ 1", "dub", 1790782200.0),
             ("ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ตอนที่ 1 [ซับไทย]", "season1", 1700000000.0),
             ("ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ซีซั่น 2 ตอนที่ 1 [ซับไทย]", "sub", 1790782200.0)]
        assert youtube_episode(v, "กลายเป็นดาบ", {1}, 1790782200) == "sub"  # Thai-sub, this season, right episode
        assert youtube_episode(v, "กลายเป็นดาบ", {2}, 1790782200) is None
        # Thai names learned from the channels' uploads (into a scratch anime.txt)
        assert thai_title("คุรุมิจัง นักสู้ FX ตอนที่ 5  [ซับไทย]【Ani-One Thailand】") == "คุรุมิจัง นักสู้ fx"
        assert thai_title("FX Fighter Kurumi-chan ตอนที่ 2 [ซับไทย]") is None
        assert thai_title("เกิดใหม่เป็นขุนนาง ภาค 3 / Tensei Kizoku 3 ตอนที่ 4 [ซับไทย]") == "เกิดใหม่เป็นขุนนาง ภาค 3"
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, mock.patch(__name__ + ".HERE", Path(tmp)):
            Path(tmp, "anime.txt").write_text("Kurumi | 1 | 0 | 1 | 0 | kurumi-chan\nSword | 2 | 0 | 1 | 0 | tensei shitara ken\nNew | 3\n")
            sh = {1: {"name": "Kurumi", "yt": "kurumi-chan", "offset": 0, "first": [1]}, 2: {"name": "Sword", "yt": "tensei shitara ken", "offset": 0, "first": [1]},
                  3: {"name": "New", "yt": "", "offset": 0, "first": [1]}}
            th = {1: ("Ani-One Thailand",), 2: ("Ani-One Thailand",), 3: ("Muse Thailand",)}
            vids = [("คุรุมิจัง นักสู้ FX ตอนที่ 5 [ซับไทย]", "a", 1000, "Ani-One Thailand"),          # nothing matches: by timing (ep 5 due at 1000)
                    ("กลายเป็นดาบ / Tensei shitara Ken ตอนที่ 3 [ซับไทย]", "b", 5000, "Ani-One Thailand"),  # matched by English: its Thai name too
                    ("อนิเมะใหม่ ตอนที่ 1 [ซับไทย]", "c", 900000, "Muse Thailand"),                     # nothing due then: not learned
                    ("อีกเรื่อง ตอนที่ 5 [ซับไทย]", "d", 1000, "Muse Thailand")]                        # due on Ani-One, posted on Muse: no
            got = learn_thai_titles(sh, vids, {(1, 5): 1100, (3, 1): 100}, th)
            assert got == [(1, "คุรุมิจัง นักสู้ fx"), (2, "กลายเป็นดาบ")], got
            assert Path(tmp, "anime.txt").read_text() == ("Kurumi | 1 | 0 | 1 | 0 | kurumi-chan|คุรุมิจัง นักสู้ fx\n"
                                                          "Sword | 2 | 0 | 1 | 0 | tensei shitara ken|กลายเป็นดาบ\nNew | 3\n")
            assert re.search(sh[1]["yt"], "คุรุมิจัง นักสู้ fx ตอนที่ 6 [ซับไทย]") and learn_thai_titles(sh, vids, {(1, 5): 1100}, th) == []
        assert f1_time(92.274) == "1:32.274" and f1_time(6120.785) == "1:42:00.785" and f1_time(61.05) == "1:01.050"
        hadjar = {"full_name": "Isack HADJAR", "team_name": "Red Bull Racing", "year": 2026,
                  "headshot_url": "https://media.formula1.com/d_driver_fallback_image.png/content/dam/fom-website/drivers/I/ISAHAD01_Isack_Hadjar/isahad01.png.transform/1col/image.png"}
        assert f1_photo(hadjar, 96).endswith("/common/f1/2026/redbullracing/isahad01/2026redbullracingisahad01right.png")
        assert "/2026/audi/nichul01/" in f1_photo({"full_name": "Nico HÜLKENBERG", "team_name": "Audi", "year": 2026}, 96)  # no link: from the name
        assert f1_name({"full_name": "Andrea Kimi ANTONELLI"}) == "A. Antonelli" and f1_name({"full_name": "Nico HÜLKENBERG"}) == "N. Hülkenberg" and f1_name({"name_acronym": "LAW"}) == "Law" and f1_name({}) == "?"
        assert f1_rows([{"position": 21, "driver_number": 5, "duration": None, "gap_to_leader": None}], {})[0][2] == "No time"  # practice, no lap set
        drv = {3: {"full_name": "Max VERSTAPPEN"}, 16: {"full_name": "Charles LECLERC"}, 10: {"full_name": "Pierre GASLY"}, 18: {"full_name": "Lance STROLL"}}
        race = [{"position": None, "driver_number": 18, "dnf": True, "duration": None, "gap_to_leader": None},
                {"position": 1, "driver_number": 3, "duration": 6120.785, "gap_to_leader": 0},
                {"position": 11, "driver_number": 10, "duration": None, "gap_to_leader": "+1 LAP"},
                {"position": 2, "driver_number": 16, "duration": 6128.12, "gap_to_leader": 7.335}]
        assert [(p, v) for p, d, v in f1_rows(race, drv)] == [("1", "1:42:00:785"), ("2", "+7.335s"), ("11", "+1 Lap"), ("NC", "DNF")]
        quali = [{"position": 1, "driver_number": 3, "duration": [93.241, 91.639, 91.373], "gap_to_leader": [0.8, 0.1, 0]},
                 {"position": 16, "driver_number": 10, "duration": [95.984, None, None], "gap_to_leader": [3.5, None, None]}]
        assert [(p, v) for p, d, v in f1_rows(quali, drv)] == [("1", "1:31.373"), ("16", "1:35.984")]
        sp = {1: {"yt": "จิโตเสะ"}}
        vids = [("ชีวิตรสโซดาของจิโตเสะคุง ตอนที่ 13.5 [ซับไทย]", "a", 100, "Ani-One Thailand"),
                ("ชีวิตรสโซดาของจิโตเสะคุง ตอนที่ 0 [ซับไทย]", "b", 100, "Ani-One Thailand"),
                ("ชีวิตรสโซดาของจิโตเสะคุง ตอนที่ 14 [ซับไทย]", "c", 100, "Ani-One Thailand"),  # regular episode
                ("ชีวิตรสโซดาของจิโตเสะคุง ตอนที่ 0.5 [พากย์ไทย]", "d", 100, "Ani-One Thailand"),  # dubbed
                ("ชีวิตรสโซดาของจิโตเสะคุง ตอนที่ 10.5 [ซับไทย]", "e", 10, "Ani-One Thailand")]  # too old
        home = ('<a href="/list/3145/#ep-2" title="Tensei Kizoku, Kantei Skill de Nariagaru 3rd Season ภาค3 ตอนที่ 02">'
                '<a href="/list/3162/#ep-2" title="Tensei Kizoku 3rd Season (พากย์ไทย) ตอนที่ 02">'
                '<a href="/list/2754/#ep-1" title="Black Clover 2nd Season ตอนที่ 01">')
        assert gojo_latest(home, {7: {"yt": "tensei kizoku"}}) == [(7, 2, GOJO + "/list/3145/#ep-2")]  # dub and other shows skipped
        series = '<title>Tensei Kizoku 3rd Season (ซับไทย) ตอนที่ 1-2</title><a href="#ep-1">1</a><a href="#ep-2">2</a>'
        assert gojo_sub_episodes(series) == {1, 2}
        assert gojo_sub_episodes(series.replace("ซับไทย", "พากย์ไทย")) == set()  # dubbed page
        feed = ('<rss version="2.0"><channel><item><title>Tensei shitara Ken deshita II Season 2 ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ (ภาค2) ตอนที่ 3 ซับไทย</title>'
                '<link>https://animeruka.com/ep/113639/</link><pubDate>Wed, 14 Oct 2026 15:51:01 +0000</pubDate></item>'
                '<item><title>Tensei shitara Ken deshita II ตอนที่ 3 พากย์ไทย</title><link>https://animeruka.com/ep/1/</link><pubDate>Wed, 14 Oct 2026 16:00:00 +0000</pubDate></item>'
                '<item><title>Edgerunners 2 ตอนที่ 1-3 ซับไทย</title><link>https://animeruka.com/ep/e/</link><pubDate>Thu, 15 Oct 2026 04:30:27 +0000</pubDate></item>'
                '<item><title>Black Clover ตอนที่ 2 ซับไทย</title><link>https://animeruka.com/ep/2/</link><pubDate>Thu, 15 Oct 2026 04:00:00 +0000</pubDate></item></channel></rss>')
        fs = {7: {"yt": "กลายเป็นดาบ|tensei shitara ken"}, 8: {"yt": "edgerunners"}}
        wed = int(datetime(2026, 10, 14, 15, 51, 1, tzinfo=timezone.utc).timestamp())
        thu = int(datetime(2026, 10, 15, 4, 30, 27, tzinfo=timezone.utc).timestamp())
        assert feed_episodes(feed, fs) == [(7, 3, "https://animeruka.com/ep/113639/", wed)] + [(8, n, "https://animeruka.com/ep/e/", thu) for n in (1, 2, 3)]
        assert specials(vids, sp, 50) == [(1, "13.5", "a", 100, "Ani-One Thailand"), (1, "0", "b", 100, "Ani-One Thailand")]
        assert [series_page(u) for u in ("https://animeruka.com/ep/cyberpunk-edgerunners-season-2-ep-10/", GOJO + "/list/3165/#ep-3",
                                         "https://suba-anime.com/yozakura-san-chi-no-daisakusen-ss2/ep-13", "https://animeruka.com/ep/113639/")] == [
            "https://animeruka.com/anime/cyberpunk-edgerunners-season-2/", GOJO + "/list/3165/",
            "https://suba-anime.com/yozakura-san-chi-no-daisakusen-ss2/", "https://animeruka.com/ep/113639/"]  # not a series' episode: stays as it is
        assert in_brave("https://www.youtube.com/watch?v=j7zFyWX6t8M") == BRAVE + "j7zFyWX6t8M"
        T = datetime(2026, 10, 4, 21, 30, tzinfo=THAI).timestamp()
        sh = {1: {"yt": "abc", "first": [1]}, 2: {"yt": "xyz", "first": [14]}}
        got = learn_release_times(sh, {1: {1: T}, 2: {1: T}}, [("abc ตอนที่ 1 [ซับไทย]", "l", T + 1800, "Ani-One Thailand"),
                                                              ("abc ตอนที่ 1 [ซับไทย]", "l", T - 2 * 86400, "Muse Thailand")],  # an early premiere: ignored
                                  [("xyz", T, "บทที่14 อัปเดตแล้ว"), ("abc", T + 3600, "บทที่1")])
        assert got == {1: ["Ani-One Thailand", "Sun", "22:00"], 2: ["Bilibili", None, None]}, got  # the earlier of two releases wins
        assert season_label({"season": "FALL", "seasonYear": 2026, "format": "TV_SHORT"}) == "Fall 2026, TV SHORT" and season_label({}) == ""
        x = ('<ProgItem><TID>8</TID><Count>15</Count><StTime>2026-10-02 00:26:00</StTime><Deleted>0</Deleted></ProgItem>'
             '<ProgItem><TID>8</TID><Count>15</Count><StTime>2026-10-02 02:00:00</StTime><Deleted>0</Deleted></ProgItem>'
             '<ProgItem><TID>8</TID><Count>16</Count><StTime>2026-10-08 23:56:00</StTime><Deleted>0</Deleted></ProgItem>')
        assert episode_total({"episodes": None, "idMal": 1}, {"mal_totals": {"1": [12, time.time()]}}) == 12  # AniList has none: MAL's
        assert episode_total({"episodes": 13, "idMal": 1}, {"mal_totals": {"1": [12, time.time()]}}) == 13  # AniList's wins
        assert episode_total({"episodes": None, "idMal": 1}, {"mal_totals": {"1": [None, time.time()]}}) is None  # neither: blank
        assert [webtoon_ep(t) for t in ("S2 Ep.12", "[Season 2] Ep.163", "Season 2 Ep.130 สไลด์เดอร์ (3)", "SS2 Ep.10 มังกร", "Ep.215", "Ep.90 ตัวตน (จบซีซัน 1)", "ประกาศ")] == [(2, 12), (2, 163), (2, 130), (2, 10), (1, 215), (1, 90), None]
        assert [chapter_number(c) for c in ("ซีซั่น-1/ตอนที่-226", "ซีซั่น-1/ตอนที่-9_1", "112 (จบซีซั่น 1)", "EP.45")] == [226, 9.1, 112, 45]
        assert "/2025/alpine/jacdoo01/" in f1_photo({"full_name": "Jack DOOHAN", "team_name": "Alpine", "year": 2024}, 96)  # no 2024 photo
        assert tv_aired(x, {8: [15]}, 1790868600) == {(8, 1): 1790868360}, tv_aired(x, {8: [15]}, 1790868600)  # earliest channel, season numbering, nothing future
        x2 = ('<ProgItem><TID>9</TID><Count>1</Count><StTime>2026-10-04 16:30:00</StTime></ProgItem>'
              '<ProgItem><TID>9</TID><Count>26</Count><StTime>2026-10-06 20:00:00</StTime></ProgItem>')
        assert set(tv_aired(x2, {9: [1, 26]}, 1791298800)) == {(9, 1)}  # the other channel's 26 is the same episode 1 (checked 7 Oct 00:00 JST)
        assert same_show("らんま1/2 (2024) 第3期", "らんま1/2(2026)") and same_show("転生したら剣でした 第2期", "転生したら剣でしたⅡ")
        assert same_show("千歳くんはラムネ瓶のなか 2クール", "千歳くんはラムネ瓶のなか(第2クール)") and same_show("アオのハコ Season２", "アオのハコ Season2")
        assert same_show("佐々木とピーちゃん シーズン２", "佐々木とピーちゃん Season2")
        assert not same_show("ジョジョの奇妙な冒険 スティール・ボール・ラン", "ジョジョの奇妙な冒険")  # a franchise name alone isn't the show
        # wait_for_youtube, on a fake clock with a fake channel: an Ani-One show's episode 5 due at T
        T, clock, cards = 1793900000, [0.0], []
        media = {"info": {"media": [{"id": 1, "episodes": 12, "coverImage": {"large": "https://x/c.jpg"}}]},
                 "web": {"airingSchedules": [{"episode": 5, "airingAt": T, "media": {"id": 1}}]}}
        vid = lambda start: ("Kurumi-chan ตอนที่ 5 [ซับไทย]", "https://www.youtube.com/watch?v=k5k5k5k5k5k", float(start), "Ani-One Thailand")

        def sleep(s):
            clock[0] += s

        def check_at(start, listed, state):  # one check starting at `start`; listed(t): the channel's uploads at time t
            clock[0] = start
            first = Future()
            first.set_result(listed(start))
            with tempfile.TemporaryDirectory() as tmp, mock.patch(__name__ + ".HERE", Path(tmp)), mock.patch(__name__ + ".VIDEOS", [first]), \
                    mock.patch(__name__ + ".JOB_START", start - 3), mock.patch(__name__ + ".anilist", lambda q, v: media), \
                    mock.patch(__name__ + ".get", mock.Mock(side_effect=OSError)), mock.patch(__name__ + ".read_feed", mock.Mock(side_effect=OSError)), \
                    mock.patch(__name__ + ".read_youtube", lambda channels=None: listed(clock[0])), \
                    mock.patch(__name__ + ".notify", lambda title, msg, links, *a: cards.append((clock[0] - T, links))), \
                    mock.patch.dict(os.environ, {"LINE_TOKEN": "x", "YOUTUBE_KEY": "y"}), mock.patch("builtins.print"), \
                    mock.patch("time.time", lambda: clock[0]), mock.patch("time.sleep", sleep):
                Path(tmp, "anime.txt").write_text("Kurumi | 1 | 0 | 1 | 0 | kurumi-chan\n")
                Path(tmp, "thai_times.txt").write_text("1 | Ani-One Thailand | - | -\n")
                wait_for_youtube(*anime(state, lambda: None))
        st = {"anime": []}
        check_at(T - 250, lambda t: [vid(T + 8)] if t >= T + 12 else [], st)  # the check before T; the video listed 12 s after T
        assert len(cards) == 1 and 12 <= cards[0][0] <= 18 and cards[0][1].endswith("k5k5k5k5k5k"), cards
        check_at(T + 50, lambda t: [vid(T + 8)], st)  # the next check: not sent again
        assert len(cards) == 1, cards
        cards.clear()
        check_at(T - 200, lambda t: [vid(T + 60)], {"anime": []})  # a premiere at T+60: its card at that time, not before
        assert len(cards) == 1 and 60 <= cards[0][0] < 61, cards
        cards.clear()
        check_at(T - 250, lambda t: [vid(T + 100)], {"anime": []})  # a premiere after the next check starts: that check's
        assert cards == [], cards
        print("ok")
    elif sys.argv[1:] == ["ping"]:
        # test: one Webtoon card of each kind, built the same way as real alerts
        for url, no in (("https://m.webtoons.com/th/fantasy/zodiac-girls/list?title_no=9274", "9274"),
                        ("https://www.webtoons.com/th/action/dead-mansion/list?title_no=6865", "6865")):
            ep = max(int(c[3:]) for c in webtoon(url))
            title, image = cover(url)
            if no in WEBTOON_SERIES_PAGE:
                kind, text, tap = "series page", f"EP.{ep}\nlinewebtoon://episodeList/webtoon?titleNo={no}", WEBTOON_APP + no
            else:
                kind, text, tap = f"opens EP.{ep}", f"EP.{ep}\nlinewebtoon://viewer/webtoon?titleNo={no}&episodeNo={ep}", f"{WEBTOON_APP}{no}&e={ep}"
            print(f"{title}: card opens {tap}")
            notify(f"New chapter: {title} (test: {kind})", f"EP.{ep}", text, image, tap)
    elif sys.argv[1:] == ["richmenu"]:
        setup_richmenu()
    elif sys.argv[1:] == ["sites"]:
        # can this machine read each anime source? (ordinary requests; a bot check counts as unreadable)
        for name, url in (("animegojo", GOJO + "/"), ("suba-anime", OTHER_PAGES[213657]), ("animeruka", "https://animeruka.com/"),
                          ("animeruka feed", "https://animeruka.com/feed/?post_type=episodes")):
            try:
                page = get(url)
                title = (re.search(r"<title>([^<]*)", page) or [None, "?"])[1].strip()
                check = "Just a moment" in page or "cf-browser-verification" in page
                print(f"sites {name}: {'BOT CHECK' if check else 'readable'} ({len(page)} bytes) {title[:60]}")
            except Exception as e:
                print(f"sites {name}: FAILED {e}")
        for name, url in FEEDS.items():
            try:
                page = read_feed(url)
                print(f"sites {name} via read_feed: readable feed ({len(page)} bytes)")
            except Exception as e:
                print(f"sites {name} via read_feed: FAILED {type(e).__name__}: {e}")
    elif sys.argv[1:] == ["f1check"]:
        # LINE checks the F1 cards (the latest race's results, standings and a live card) without sending anything
        token = os.environ["LINE_TOKEN"]
        s = [x for x in json.loads(get(f"{OPENF1}/sessions?session_name=Race&year={datetime.now(THAI).year}"))
             if datetime.fromisoformat(x["date_end"]) < datetime.now(THAI)][-1]
        key = s["session_key"]
        drivers = {d["driver_number"]: {**d, "year": s.get("year")} for d in json.loads(get(f"{OPENF1}/drivers?session_key={key}"))}
        meeting = json.loads(get(f"{OPENF1}/meetings?meeting_key={s['meeting_key']}"))[0]
        when = datetime.fromisoformat(s["date_start"]).astimezone(THAI).strftime("%a %d %b %Y · %H:%M")
        rows = f1_rows(json.loads(get(f"{OPENF1}/session_result?session_key={key}")), drivers)
        sample = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx21-ELSYx3yMPcKM.jpg"
        cards = {"anime card": {"type": "flex", "altText": "check", "contents": card("New episode: One Piece (Fall 2026, TV)",
                                                                                   "Episode 01/?\nNetflix · 15 Oct 2026 22:30", sample, "https://www.youtube.com/")},
                 "manga card": {"type": "flex", "altText": "check", "contents": card("New chapter: Reality Quest", "ตอนที่ 225", sample, "https://example.com/")},
                 "results": {"type": "flex", "altText": "check", "contents": f1_card(meeting, "Race", when, rows, key)},
                 "updated results": {"type": "flex", "altText": "check", "contents": f1_card(meeting, "Race · Updated", when, rows, key,
                                     "Result changed after the race: HIR P13 → P10 (+1 pt), PIA P7 → P13 (-6 pts)")},
                 "live": {"type": "flex", "altText": "check", "contents": f1_live_card(meeting, "Race", when, True)}}
        captured, line_send = [], lambda token, message: captured.append(message) or True   # module level: f1_standings now captures instead of sending
        f1_standings({}, token, s, meeting)
        cards["standings"] = captured[0]
        for name, message in cards.items():
            req = urllib.request.Request("https://api.line.me/v2/bot/message/validate/broadcast",
                                         data=json.dumps({"messages": [message]}, separators=(",", ":")).encode(),
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=30)
                print(f"f1check {name}: LINE accepts it ({len(json.dumps(message, separators=(',', ':')))} bytes)")
            except urllib.error.HTTPError as e:
                print(f"f1check {name}: LINE refused it: {e.read()[:600]}")
    else:
        main()
