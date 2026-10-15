#!/usr/bin/env python3
"""Notify (macOS) when a watched manga page lists a chapter it didn't list last run."""
import email.utils, html, json, os, re, shutil, subprocess, sys, time, unicodedata, urllib.error, urllib.request
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


def webtoon(url):
    """LINE Webtoon: {"EP.216": link} from the series' RSS feed, which lists its latest free episodes."""
    rss = get(url.replace("://m.webtoons.com", "://www.webtoons.com").replace("/list?", "/rss?"))
    return {f"EP.{n}": link.replace("&amp;", "&") for link, n in re.findall(r"<link>([^<]*episode_no=(\d+))</link>", rss)}

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper()
except ImportError:
    _scraper = None
       
SLOW = set()  # sites that timed out this run: skipped for the rest of it, so one dead site can't hold the run up for 15 minutes


def get(url):
    host = urlparse(url).netloc
    if host in SLOW:
        raise TimeoutError(f"{host} skipped: it timed out earlier in this run")
    req = urllib.request.Request(quote(url, safe=":/%?&="), headers={"User-Agent": UA})
    try:
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
    """A race's total time, written so it can't be read as a time of day: 6120.785 -> "1h 42m 00.785s"."""
    h, rem = divmod(sec, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h)}h {int(m):02d}m {sec:06.3f}s" if h else f"{int(m)}m {sec:06.3f}s"


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
            val = f"+{gap:.3f}"
        else:
            val = str(gap or "").replace("LAPS", "Laps").replace("LAP", "Lap")
        rows.append((pos, drivers.get(x["driver_number"], {}), val))
    return rows


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
    # c_thumb,g_face: cropped to the face; d_: formula1.com's blank driver if the portrait is missing, never a broken image
    return (f"https://media.formula1.com/image/upload/c_thumb,g_face,z_0.6,w_{px},h_{px}/d_common:f1:{year}:fallback:driver:"
            f"{year}fallbackdriverright.webp/common/f1/{year}/{team}/{code}/{year}{team}{code}right.png")


def f1_logo(team, year):
    """A team's white logo for the season as PNG, for the cards' dark background (as the F1 Live page uses in dark mode),
    from formula1.com (Red Bull Racing: .../2026/redbullracing/2026redbullracinglogowhite)."""
    slug = re.sub(r"[^a-z0-9]", "", (team or "").lower())
    return f"https://media.formula1.com/image/upload/c_fit,w_96,h_96/common/f1/{year}/{slug}/{year}{slug}logowhite.png" if slug else None


def f1_name(d):
    """"Andrea Kimi ANTONELLI" -> "A. Antonelli"."""
    words = (d.get("full_name") or d.get("name_acronym") or "?").split()
    last = [w for w in words if w.isupper() and len(w) > 1] or words[-1:]
    first = [w for w in words if w not in last]
    return f"{first[0][0]}. " * bool(first) + " ".join(last).title()


# the F1 Live page's look: navy, team-colour driver tags (car number on the team colour, the name tinted with it)
F1C = {"bg": "#0B0B10", "panel": "#15151E", "row": "#181821", "line": "#262633", "text": "#F2F2F6", "dim": "#8D8D9C",
       "red": "#E10600", "green": "#22C55E", "purple": "#B14CF0"}


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
        {"type": "box", "layout": "vertical", "backgroundColor": f1_mix(c, F1C["panel"], .24), "width": "38px",
         "contents": [t(d.get("name_acronym"), color=f1_mix(c, "#FFFFFF", .55))]}]}


def f1_head(meeting, title, sub):
    """The dark header: F1 logo, the Grand Prix and its flag, a big title, a dim line under it."""
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


def f1_card(meeting, session, when, rows, replay_key=None):
    """A results card in the F1 Live page's style: the top 3 with faces on their team colour, then the timing table:
    position, driver tag, name and time, rows striped like the page; a button opens the session's replay."""
    t = lambda text, **k: {"type": "text", "text": text, **k}
    colour = lambda d: "#" + (d.get("team_colour") or "777777")

    def podium(pos, d, val):
        photo = f1_photo(d, 300)
        return {"type": "box", "layout": "vertical", "flex": 1, "spacing": "xs", "alignItems": "center", "contents": [
            {"type": "box", "layout": "vertical", "cornerRadius": "8px", "backgroundColor": colour(d), "width": "100%", "contents": [
                {"type": "image", "url": photo, "aspectMode": "cover", "aspectRatio": "1:1", "size": "full"} if photo else
                {"type": "box", "layout": "vertical", "height": "80px", "contents": []}]},
            t(f"P{pos}", size="sm", weight="bold", color=F1C["red"] if pos == "1" else F1C["text"], margin="sm"),
            f1_tag(d), t(val, size="xxs", color=F1C["dim"], align="center", wrap=True)]}

    def row(i, pos, d, val):
        return {"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center", "paddingAll": "5px",
                **({"backgroundColor": F1C["row"]} if i % 2 else {}), "contents": [
            t(pos, size="xs", weight="bold", color=F1C["text"], align="end", flex=0, gravity="center"),
            f1_tag(d),
            t(f1_name(d), size="xxs", color=F1C["dim"], flex=1, gravity="center"),
            t(val, size="xs", weight="bold", color=F1C["text"], align="end", flex=0, gravity="center")]}

    table = [{"type": "box", "layout": "horizontal", "paddingStart": "8px", "paddingEnd": "8px", "paddingBottom": "4px", "contents": [
        t("POS · DRIVER", size="xxs", weight="bold", color=F1C["dim"], flex=1), t("TIME", size="xxs", weight="bold", color=F1C["dim"], flex=0)]}]
    table += [row(i, *r) for i, r in enumerate(rows[3:])]
    body = [{"type": "box", "layout": "horizontal", "spacing": "md", "contents": [podium(*r) for r in rows[:3]]},
            {"type": "separator", "margin": "lg", "color": F1C["line"]},
            {"type": "box", "layout": "vertical", "margin": "md", "contents": table}]
    return {"type": "bubble", "size": "mega", "header": f1_head(meeting, session, when),
            "body": {"type": "box", "layout": "vertical", "backgroundColor": F1C["panel"], "contents": body},
            **({"footer": f1_button("Watch replay", f"{F1_PAGE}?replay={replay_key}")} if replay_key else {})}


def f1_standings_card(title, sub_title, rows, colour_of):
    """A championship table in the page's style: position, change since before the race (+1 green, -1 red),
    face or team logo, name and team, points and points gained."""
    t = lambda text, **k: {"type": "text", "text": text, **k}

    def row(i, pos, move, colour, photo, name, team, points, gained):
        mark = t(f"+{move}", color=F1C["green"]) if move > 0 else t(str(move), color=F1C["red"]) if move < 0 else t("–", color=F1C["dim"])
        return {"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center", "paddingAll": "4px",
                **({"backgroundColor": F1C["row"]} if i % 2 else {}), "contents": [
            t(str(pos), size="xs", weight="bold", color=F1C["text"], align="end", flex=2),
            {**mark, "size": "xxs", "weight": "bold", "align": "center", "flex": 2},
            {"type": "box", "layout": "vertical", "width": "3px", "height": "26px", "backgroundColor": colour, "contents": []},
            *([{"type": "image", "url": photo, "size": "26px", "aspectRatio": "1:1", "flex": 0,
                **({"aspectMode": "fit"} if "logo" in photo else {"aspectMode": "cover", "backgroundColor": colour})}] if photo else []),
            {"type": "box", "layout": "vertical", "flex": 12, "contents": [t(name, size="xs", weight="bold", color=F1C["text"]),
                                                                         *([t(team, size="xxs", color=F1C["dim"])] if team else [])]},
            {"type": "box", "layout": "vertical", "flex": 4, "contents": [
                t(f"{points:g}", size="xs", weight="bold", color=F1C["text"], align="end"),
                t(f"+{gained:g}" if gained else "–", size="xxs", color=F1C["green"] if gained else F1C["dim"], align="end")]}]}

    return {"type": "bubble", "size": "mega", "header": f1_head({"meeting_name": sub_title}, title, "Points after the race · change since before it"),
            "body": {"type": "box", "layout": "vertical", "backgroundColor": F1C["panel"], "contents": [row(i, *r) for i, r in enumerate(rows)]}}


def f1_standings(state, token, s, meeting):
    """After a race: the drivers' and teams' championships as two swipeable cards, sent once (when OpenF1 has them)."""
    sent = state.setdefault("f1_standings", [])
    key = str(s["session_key"])
    if key in sent:
        return
    try:
        champ_d = json.loads(get(f"{OPENF1}/championship_drivers?session_key={key}"))
        champ_t = json.loads(get(f"{OPENF1}/championship_teams?session_key={key}"))
        if not champ_d or not champ_t:
            return  # not published yet: tried again on the next check
        drivers = {d["driver_number"]: {**d, "year": s.get("year")} for d in json.loads(get(f"{OPENF1}/drivers?session_key={key}"))}
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
    rank = lambda c: c.get("position_current") or 99
    move = lambda c: (c.get("position_start") or c.get("position_current") or 0) - (c.get("position_current") or 0)
    gained = lambda c: (c.get("points_current") or 0) - (c.get("points_start") or 0)
    drows = [(c["position_current"], move(c), "#" + (drivers[c["driver_number"]].get("team_colour") or "999999"),
              f1_photo(drivers[c["driver_number"]], 96), f1_name(drivers[c["driver_number"]]),
              drivers[c["driver_number"]].get("team_name") or "", c.get("points_current") or 0, gained(c)) for c in sorted(champ_d, key=rank)]
    trows = [(c["position_current"], move(c), colour.get(c["team_name"], "#999999"), f1_logo(c["team_name"], s.get("year") or datetime.now(THAI).year), c["team_name"], "",
              c.get("points_current") or 0, gained(c)) for c in sorted(champ_t, key=rank)]
    name = meeting.get("meeting_name") or "Formula 1"
    top = lambda rows: ", ".join(f"{r[0]} {r[4]} {r[6]:g}" for r in rows[:3])
    if not token:  # on the Mac: show what would be sent
        print(f"F1 standings after {name} | drivers: {top(drows)} | teams: {top(trows)}")
        sent.append(key)
        return
    dcard = f1_standings_card("Drivers' Championship", f"After the {name}", drows, colour)
    if len(json.dumps(dcard, separators=(",", ":"))) > 29000:  # LINE takes at most 30 KB a card: without faces it always fits
        dcard = f1_standings_card("Drivers' Championship", f"After the {name}", [r[:3] + (None,) + r[4:] for r in drows], colour)
    card = {"type": "flex", "altText": f"F1 standings after {name}: {top(drows)}"[:400], "contents": {"type": "carousel", "contents": [
        dcard, f1_standings_card("Teams' Championship", f"After the {name}", trows, colour)]}}
    plain = {"type": "text", "text": f"F1 standings after the {name}\n\nDrivers\n" + "\n".join(
        f"{r[0]} ({'+' if r[1] > 0 else ''}{r[1] or '–'}) {r[4]} {r[6]:g}" for r in drows) + "\n\nTeams\n" + "\n".join(
        f"{r[0]} ({'+' if r[1] > 0 else ''}{r[1] or '–'}) {r[4]} {r[6]:g}" for r in trows)}
    if line_send(token, card) or line_send(token, plain):
        sent.append(key)
    del sent[:-50]


F1_PAGE = "https://isaraapae.github.io/alert-watch/f1.html"


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


def f1_live(state, token, sessions, now):
    """Tell LINE when a session goes live (from 15 minutes before its start to 30 minutes after), once."""
    sent = state.setdefault("f1_live", [])
    for s in sessions:
        start, key = datetime.fromisoformat(s["date_start"]).timestamp(), str(s["session_key"])
        if key in sent or not start - 15 * 60 <= now <= start + 30 * 60:
            continue
        try:
            meeting = (json.loads(get(f"{OPENF1}/meetings?meeting_key={s['meeting_key']}")) or [{}])[0]
        except Exception:
            meeting = {}
        live = now >= start
        when = datetime.fromtimestamp(start, THAI).strftime("%a %d %b · %H:%M") + ("" if live else f" · in {max(1, round((start - now) / 60))} min")
        name = meeting.get("meeting_name") or s.get("country_name") or "Formula 1"
        if not token:   # on the Mac: show what would be sent
            print(f"F1 live: {s['session_name']} · {name} · {when}")
        else:
            card = {"type": "flex", "altText": f"F1 {s['session_name']} · {name} is {'live' if live else 'starting'}: open F1 Live",
                    "contents": f1_live_card(meeting, s["session_name"], when, live)}
            plain = {"type": "text", "text": f"F1 {s['session_name']} · {name} is {'live now' if live else 'starting soon'}\n{F1_PAGE}"}
            if not (line_send(token, card) or line_send(token, plain)):
                continue
        sent.append(key)
    del sent[:-100]


def f1(state):
    """Send every Formula 1 session's order once it is published: Practice 1-3, Sprint Qualifying, Sprint, Qualifying, Race."""
    token, now = os.environ.get("LINE_TOKEN"), time.time()
    state.pop("f1", None)  # the earlier race-only records
    since = datetime.fromtimestamp(now - 3 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        sessions = json.loads(get(f"{OPENF1}/sessions?date_end>={since}"))
    except Exception as e:
        print(f"FAIL f1 sessions: {e}", file=sys.stderr)
        return
    f1_live(state, token, sessions, now)
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
            except Exception as e:
                print(f"FAIL f1 meeting: {e}", file=sys.stderr)
                continue
            f1_standings(state, token, s, m)
        key = str(s["session_key"])
        if key in sent:
            continue
        try:
            results = json.loads(get(f"{OPENF1}/session_result?session_key={key}"))
            if not results:
                continue  # not published yet: tried again on the next check
            drivers = {d["driver_number"]: {**d, "year": s.get("year")} for d in json.loads(get(f"{OPENF1}/drivers?session_key={key}"))}
            meeting = (json.loads(get(f"{OPENF1}/meetings?meeting_key={s['meeting_key']}")) or [{}])[0]
        except Exception as e:
            print(f"FAIL f1 {s['session_name']}: {e}", file=sys.stderr)
            continue
        rows = f1_rows(results, drivers)
        name, session = meeting.get("meeting_name") or s.get("country_name") or "Formula 1", s["session_name"]
        when = datetime.fromisoformat(s["date_start"]).astimezone(THAI).strftime("%a %d %b %Y · %H:%M")
        top = ", ".join(f"{p} {d.get('name_acronym', '?')}" for p, d, _ in rows[:3])
        if not token:  # on the Mac: show what would be sent
            print(f"F1 {session} · {name} · {when} | {top}")
            sent.append(key)
            continue
        plain = {"type": "text", "text": f"F1 {session} · {name}\n{when}\n" + "\n".join(f"{p} {f1_name(d)} ({d.get('team_name', '')}) {v}" for p, d, v in rows)}
        card = {"type": "flex", "altText": f"F1 {session} · {name}: {top}"[:400], "contents": f1_card(meeting, session, when, rows, key)}
        if line_send(token, card) or line_send(token, plain):
            sent.append(key)
            if session == "Race":
                f1_standings(state, token, s, meeting)
    del sent[:-100]


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


WEBTOON_APP = "https://isaraapae.github.io/alert-watch/w.html?t="  # redirect page on the gh-pages branch; opens linewebtoon://
WEBTOON_SERIES_PAGE = {"6227", "9274"}  # Enrolling in the Transcendent Academy, Zodiac Girls: cards open the series page, not the episode
BRAVE = "https://isaraapae.github.io/alert-watch/b.html?v="  # redirect page on the gh-pages branch; opens a YouTube video in Brave


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


def youtube_videos():
    """[(title, link, published timestamp, channel)] for the official channels' newest uploads. With a YOUTUBE_KEY, from
    YouTube's data service, current within seconds; otherwise (or if that fails) from the public feeds, which GitHub
    can get as a copy hours old."""
    out = []
    key = os.environ.get("YOUTUBE_KEY")
    for cid in YT_CHANNELS:
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


GOJO = "https://animegojos.com"  # anime-waku and animeruka are read from their feeds (FEEDS) when they let this machine in


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

# shows also followed on another site: {AniList id: its series page}, only until anime-waku, animeruka or animegojo
# carries the show (suba_dropped in the state); from then on those three compete for it. suba-anime was the only place to
# watch Yozakura Season 2 in Thailand; its page numbers the season 1-24, so Cour 2's episode 1 is its 13 (GOJO_SHIFT).
OTHER_PAGES = {213657: "https://suba-anime.com/yozakura-san-chi-no-daisakusen-ss2/"}


def suba_episodes(page):
    """{episode number: its page} on a suba-anime series page; an episode marked (ยังไม่มา), not out yet, has no link there."""
    return {int(n): link for link, n, text in re.findall(r'<a href="([^"#]+/ep-(\d+))"[^>]*>([^<]*)', page) if "ยังไม่มา" not in text}


def site_episodes(url):
    """{episode number: link} for a series page on animegojo (ซับไทย only) or suba-anime."""
    page = get(url)
    if "suba-anime.com" in url:
        return suba_episodes(page)
    return {n: f"{url.split('#')[0]}#ep-{n}" for n in gojo_sub_episodes(page)}


site_name = lambda url: "suba-anime" if "suba-anime.com" in url else "animegojo"

# anime-waku and animeruka: each site's RSS feed of new episodes, which (unlike animegojo) gives every upload's time.
# If a site can't be read, the run reports it and carries on with the other sites.
FEEDS = {"anime-waku": "https://anime-waku.com/feed/?post_type=episodes", "animeruka": "https://animeruka.com/feed/?post_type=episodes"}


def read_feed(url):
    """A site's RSS feed; an error when the site answers with anything else."""
    if _scraper:
        page = _scraper.get(quote(url, safe=":/%?&="), timeout=20).text
    else:
        page = get(url)
    if "<rss" not in page[:1000]:
        raise RuntimeError("answered with something that is not its feed")
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


def season_label(m):
    """AniList media -> "Fall 2026, TV"."""
    parts = [f"{m['season'].title()} {m['seasonYear']}" if m.get("season") and m.get("seasonYear") else "", (m.get("format") or "").replace("_", " ")]
    return ", ".join(p for p in parts if p)


def anilist(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request("https://graphql.anilist.co", data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]


def schedule(state=None):
    """Write schedule.json for the countdown page: every show's episodes from a week ago to two months ahead,
    with the same times and numbering as the alerts. Rewritten only when something changed, so it isn't committed every run."""
    shows, now = anime_list(), int(time.time())
    q = "query($ids:[Int]){Page(perPage:50){media(id_in:$ids){id episodes season seasonYear format coverImage{large} airingSchedule(perPage:50){nodes{episode airingAt}}}}}"
    try:
        info = {m["id"]: m for m in anilist(q, {"ids": list(shows)})["Page"]["media"]}
        by_tid = {s["tid"]: i for i, s in shows.items() if s["tid"]}
        span = f"{datetime.fromtimestamp(now - 30 * 86400, JST):%Y%m%d_%H%M%S}-{datetime.fromtimestamp(now + 60 * 86400, JST):%Y%m%d_%H%M%S}"  # from a month back: a rerun must not pass for an episode's first broadcast
        xml = get(f"https://cal.syoboi.jp/db.php?Command=ProgLookup&TID={','.join(map(str, by_tid))}&Range={span}")
        tv = tv_aired(xml, {tid: shows[i]["first"] for tid, i in by_tid.items()}, now + 60 * 86400)
    except Exception as e:  # keep the previous file rather than publish a half-empty one
        print(f"FAIL schedule: {e}", file=sys.stderr)
        return
    videos = youtube_videos()
    thai = thai_times()
    state = {} if state is None else state
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
    out, jp = [], {}
    for i, s in shows.items():
        m = info.get(i, {})
        if s["tid"]:
            eps = {ep: t for (tid, ep), t in tv.items() if tid == s["tid"] and ep >= 1}
        else:
            eps = {n["episode"]: n["airingAt"] for n in (m.get("airingSchedule") or {}).get("nodes", [])}
        jp[i] = dict(eps)
        rows = []
        for ep, t in sorted(eps.items()):
            t = release_time(t, thai[i]) if i in thai else t
            if now - 8 * 86400 <= t <= now + 60 * 86400:
                yt = t <= now and s["yt"] and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t)
                rec = state.get("first_source", {}).get(f"{i}:{ep}", {})  # the animegojo episode page, if that's where it was found first
                watch = (in_brave(yt) if yt else None) or links.get(f"{i}:{ep}") or (rec["url"] if rec.get("first") in ("animegojo", "suba-anime", *FEEDS) else None)
                if not watch and t <= now and (str(i) in pages or i in OTHER_PAGES) and not (i in thai and thai[i][0] in YT_CHANNELS.values()):
                    shift, have = on_gojo(i)  # aired, no button yet: the episode on its animegojo (or suba-anime) page, if it's up there
                    watch = have.get(ep + s["offset"] + shift)
                if watch:
                    links[f"{i}:{ep}"] = watch
                rows.append({"ep": ep + s["offset"], "at": t, **({"watch": watch} if watch else {})})
        rows += [{"ep": n, "at": p, "watch": in_brave(link)} for link, (a, n, p) in kept.items() if a == i]
        rows.sort(key=lambda r: r["at"])
        total = m.get("episodes")
        out.append({"name": s["name"], "label": season_label(m), "cover": (m.get("coverImage") or {}).get("large"),
                    "total": total and total + s["offset"], "where": thai[i][0] if i in thai else site_name(OTHER_PAGES[i]) if i in OTHER_PAGES and str(i) not in state.get("suba_dropped", {}) else None,
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
    learned = {str(k): v for k, v in learn_release_times(shows, jp, videos, cards).items()}
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


def anime(state):
    """Notify when a show in anime.txt airs a new episode: the Japanese TV schedule (Syoboi Calendar, earliest channel)
    for TV shows, AniList's airing schedule for online-only ones. anime.txt: name | AniList id | TV-schedule id (0 = none)
    | first episode number in the TV schedule | episodes before this AniList entry (added to the count)."""
    shows = anime_list()
    now = int(time.time())
    query = ("query($ids:[Int],$web:[Int],$a:Int,$b:Int){info:Page(perPage:50){media(id_in:$ids){id episodes season seasonYear format coverImage{large}}}"
             " web:Page(perPage:50){airingSchedules(mediaId_in:$web,airingAt_greater:$a,airingAt_lesser:$b){episode airingAt media{id}}}}")
    web_only = [i for i, s in shows.items() if not s["tid"]]
    body = json.dumps({"query": query, "variables": {"ids": list(shows), "web": web_only or [0], "a": now - 8 * 86400, "b": now + 8 * 86400}}).encode()
    req = urllib.request.Request("https://graphql.anilist.co", data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]
    except Exception as e:
        print(f"FAIL anilist: {e}", file=sys.stderr)
        return
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
        except Exception as e:
            print(f"FAIL tv schedule: {e}", file=sys.stderr)
    seed = "anime" not in state  # first run: record the past week's episodes silently
    seen = state.setdefault("anime", [])
    thai = thai_times()  # shows released later with Thai subs: alert at that time instead of the Japanese broadcast
    gojo_only = lambda i: not (i in thai and thai[i][0] in YT_CHANNELS.values())  # not on Muse/Ani-One: wait for animegojo, no scheduled card
    aired = {k: (release_time(t, thai[k[0]]) if k[0] in thai else t) for k, t in aired.items()}
    aired = {k: t for k, t in aired.items() if t >= now - 8 * 86400}  # the past week only (older episodes were alerted then)
    due = dict(aired)  # every episode due from a week ago to a week ahead: a number outside it is not trusted
    aired = {k: t for k, t in aired.items() if t <= now}
    pending = state.setdefault("watch_later", {})  # episodes alerted before their Thai-sub video showed up: {key: release}
    videos = youtube_videos() if not seed else []  # every check, for animegojo and the specials below
    log = state.setdefault("first_source", {})  # per episode: where it was first seen with Thai subs, link, times, alerted
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
    # every site that has an episode competes, and the fastest one gets the episode's one card: anime-waku and animeruka
    # by their feeds' upload times; animegojo and suba-anime show none, so an episode first seen on them in this check is
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
    # suba-anime is for Yozakura only, and only until one of the three sites carries it: from then on those three compete
    dropped = state.setdefault("suba_dropped", {})  # {AniList id: the site that had it}
    for t, site, i, n, link in hits:
        if i in OTHER_PAGES and str(i) not in dropped and to_ep(i, n) is not None:
            dropped[str(i)] = site
            print(f"{shows[i]['name']}: found on {site}, so suba-anime is no longer checked")
    for i, url in (OTHER_PAGES.items() if not seed else []):
        if i not in shows or not gojo_only(i) or str(i) in dropped:
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
        total = m.get("episodes")
        tot = f"{total + s['offset']:02d}" if total else "?"
        a, b = batch[0] + s["offset"], batch[-1] + s["offset"]
        link = on_site[batch[0]][0]
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
    new = sorted((t, i, ep) for (i, ep), t in aired.items() if f"{i}:{ep}" not in seen)
    for t, i, ep in new:
        if not seed:
            s, m = shows[i], info.get(i, {})
            if gojo_only(i):
                continue  # wait for animegojo (handled above): no card at the scheduled time, no video link exists
            total = m.get("episodes")
            line = episode_line(ep + s["offset"], total and total + s["offset"], t, thai[i][0] if i in thai else None)
            label = f" ({season_label(m)})" if season_label(m) else ""  # e.g. "Fall 2026, TV"
            # if an official channel already has this episode up with Thai subs, the card opens that video; otherwise no link
            yt = s["yt"] and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t)
            if not yt and i in thai and thai[i][0] in YT_CHANNELS.values() and now - t < 60 * 60:
                continue  # its channel may post a little late: wait up to an hour so the alert is one card with the link
            if not yt and s["yt"]:  # the video may still come later: send a "Watch now" card then
                pending[f"{i}:{ep}"] = t
            notify(f"New episode: {s['name']}{label}", line, line + (f"\n{yt}" if yt else ""), (m.get("coverImage") or {}).get("large"), yt and in_brave(yt))
            v = next((v for v in videos if v[1] == yt), None)
            log[f"{i}:{ep}"] = {"title": s["name"], "episode": ep + s["offset"], "version": "ซับไทย", "first": v[3] if v else (thai[i][0] if i in thai else "Japanese TV"),
                                "url": yt or None, "detected": now, "published": v and int(v[2]), "scheduled": int(t), "notified": True,
                                **({} if v else {"order": "uncertain"})}  # no video: the platform's time is a schedule, not a verified release
        seen.append(f"{i}:{ep}")
    for key in list(log)[:-300]:
        del log[key]
    for key, t in list(pending.items()):
        i, ep = map(int, key.split(":"))
        s = shows.get(i)
        yt = s and videos and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t)
        if yt:
            where = next((ch for title, link, p, ch in videos if link == yt), "YouTube")
            line = f"Episode {ep + s['offset']:02d} with Thai subs is up on {where}"
            m = info.get(i, {})
            notify(f"Watch now: {s['name']}", line, f"{line}\n{yt}", (m.get("coverImage") or {}).get("large"), in_brave(yt))
        if yt or not s or now - t > 2 * 86400:  # sent, show removed, or no video within 2 days
            del pending[key]
    sent = state.setdefault("specials", [])  # links of specials already alerted
    for i, n, link, published, ch in specials(videos, shows, now - 2 * 86400):
        if link not in sent and published <= now:  # a premiere: alert once it plays
            m = info.get(i, {})
            line = f"Special episode {n}\n{ch} · {datetime.fromtimestamp(published, THAI):%d %b %Y %H:%M}"
            notify(f"New special: {shows[i]['name']}", line, f"{line}\n{link}", (m.get("coverImage") or {}).get("large"), in_brave(link))
            sent.append(link)
    del sent[:-50]
    print(f"anime: {len(shows)} watched, {len(new)} {'seeded' if seed else 'new'}, {len(pending)} waiting for a Thai-sub video")


def setup_richmenu():
    """Install the Alert Watch menu at the bottom of the LINE chat: three buttons, the anime countdown, its timetable and F1 Live.
    Run once (workflow input richmenu); replaces any earlier menu this created."""
    auth = {"Authorization": f"Bearer {os.environ['LINE_TOKEN']}"}

    def call(method, url, body=None, ctype="application/json"):
        req = urllib.request.Request(url, data=body, method=method, headers={**auth, **({"Content-Type": ctype} if body else {})})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}")
        except urllib.error.HTTPError as e:
            print(f"FAIL line {method} {url}: {e} {e.read()[:300]}", file=sys.stderr)
            raise

    site = "https://isaraapae.github.io/alert-watch/"
    menu = {"size": {"width": 2500, "height": 843}, "selected": True, "name": "Alert Watch", "chatBarText": "Menu",
            "areas": [{"bounds": {"x": 0, "y": 0, "width": 833, "height": 843}, "action": {"type": "uri", "label": "Next episode", "uri": site}},
                      {"bounds": {"x": 833, "y": 0, "width": 834, "height": 843}, "action": {"type": "uri", "label": "Timetable", "uri": site + "?v=week"}},
                      {"bounds": {"x": 1667, "y": 0, "width": 833, "height": 843}, "action": {"type": "uri", "label": "F1 Live", "uri": site + "f1.html"}}]}
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
        if "cover" in prev and url not in COVER_OVERRIDES:
            title, image = prev.get("title"), prev["cover"]
        else:
            title, image = cover(url)
            title = title if "webtoons.com" in url else None  # other sites' page titles are full of extra words
            image = quote(image, safe=":/%?=&") if image and re.search(r"\.(jpe?g|png)$", urlparse(image).path, re.I) else None
        ids = state.get(url) or []
        latest = max(ids, key=lambda c: [int(x) for x in re.findall(r"\d+", c)] or [0]) if ids else ""
        tap = WEBTOON_APP + re.search(r"title_no=(\d+)", url)[1] if "webtoons.com" in url else quote(url, safe=":/%")
        out.append({"url": url, "name": title or series_slug(url), "title": title, "cover": image,
                    "latest": re.sub(r"^ซี่?ซั่น-1/", "", latest), "open": tap})
    if old != {m["url"]: m for m in out} or len(old) != len(out):
        path.write_text(json.dumps({"manga": out}, ensure_ascii=False, indent=1))


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    try:
        complete_anime()
    except Exception as e:  # a short entry still works with AniList times, so this must never stop the alerts
        print(f"FAIL completing anime: {e}", file=sys.stderr)
    anime(state)
    try:
        f1(state)
    except Exception as e:  # F1 results must never stop the other alerts
        print(f"FAIL f1: {e}", file=sys.stderr)
    try:
        schedule(state)
    except Exception as e:  # the countdown page must never stop the alerts
        print(f"FAIL schedule: {e}", file=sys.stderr)
    for url in (HERE / "urls.txt").read_text().split():
        name = series_slug(url)
        try:
            found = webtoon(url) if "webtoons.com" in url else chapters(url, get(url))
        except Exception as e:
            print(f"FAIL {name}: {e}", file=sys.stderr)
            continue
        if not found:  # blocked / layout changed: keep old state, don't treat as "no chapters"
            print(f"EMPTY {name}", file=sys.stderr)
            continue
        new = sorted(set(found) - set(state[url])) if url in state else []  # first sight of a url = seed silently
        if new:
            shown = ", ".join(re.sub(r"^ซี่?ซั่น-1/", "", c) for c in new)[:200]  # every series is on season 1; a later season would still show
            if "webtoons.com" in url:  # a cover card that opens the series straight in the WEBTOON app
                no = re.search(r"title_no=(\d+)", url)[1]
                eps = sorted(int(c[3:]) for c in new)  # "EP.216" -> 216; the card opens the first new one, so you read in order
                if no in WEBTOON_SERIES_PAGE:
                    text, tap = f"{shown}\nlinewebtoon://episodeList/webtoon?titleNo={no}", WEBTOON_APP + no
                else:
                    text = "\n".join(f"EP.{e}\nlinewebtoon://viewer/webtoon?titleNo={no}&episodeNo={e}" for e in eps)
                    tap = f"{WEBTOON_APP}{no}&e={eps[0]}"
                title, image = cover(url)  # LINE cards only open web addresses, so the card goes via a page that hands off to the app
                notify(f"New chapter: {title or name}", shown, text, image, tap)
            else:  # cover card that opens the first new chapter; the text version lists each new chapter's link
                links = {c: quote(found[c], safe=":/%?=&#") for c in new}
                order = sorted(new, key=lambda c: [int(x) for x in re.findall(r"\d+", c)] or [0])
                text = "\n".join(f"{re.sub(r'^ซี่?ซั่น-1/', '', c)}\n{links[c]}" for c in order)
                image = cover(url)[1]
                notify(f"New chapter: {name}", shown, text, image and quote(image, safe=":/%?=&"), links[order[0]])
        print(f"{name}: {len(found)} chapters, {len(new)} new")
        state[url] = sorted(set(found) | set(state.get(url, [])))
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
        assert f1_time(92.274) == "1:32.274" and f1_time(6120.785) == "1:42:00.785" and f1_time(61.05) == "1:01.050"
        hadjar = {"full_name": "Isack HADJAR", "team_name": "Red Bull Racing", "year": 2026,
                  "headshot_url": "https://media.formula1.com/d_driver_fallback_image.png/content/dam/fom-website/drivers/I/ISAHAD01_Isack_Hadjar/isahad01.png.transform/1col/image.png"}
        assert f1_photo(hadjar, 96).endswith("/common/f1/2026/redbullracing/isahad01/2026redbullracingisahad01right.png")
        assert "/2026/audi/nichul01/" in f1_photo({"full_name": "Nico HÜLKENBERG", "team_name": "Audi", "year": 2026}, 96)  # no link: from the name
        assert f1_name({"full_name": "Andrea Kimi ANTONELLI"}) == "A. Antonelli" and f1_name({"full_name": "Nico HÜLKENBERG"}) == "N. Hülkenberg"
        drv = {3: {"full_name": "Max VERSTAPPEN"}, 16: {"full_name": "Charles LECLERC"}, 10: {"full_name": "Pierre GASLY"}, 18: {"full_name": "Lance STROLL"}}
        race = [{"position": None, "driver_number": 18, "dnf": True, "duration": None, "gap_to_leader": None},
                {"position": 1, "driver_number": 3, "duration": 6120.785, "gap_to_leader": 0},
                {"position": 11, "driver_number": 10, "duration": None, "gap_to_leader": "+1 LAP"},
                {"position": 2, "driver_number": 16, "duration": 6128.12, "gap_to_leader": 7.335}]
        assert [(p, v) for p, d, v in f1_rows(race, drv)] == [("1", "1h 42m 00.785s"), ("2", "+7.335"), ("11", "+1 Lap"), ("NC", "DNF")]
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
                '<link>https://anime-waku.com/ep/113639/</link><pubDate>Wed, 14 Oct 2026 15:51:01 +0000</pubDate></item>'
                '<item><title>Tensei shitara Ken deshita II ตอนที่ 3 พากย์ไทย</title><link>https://anime-waku.com/ep/1/</link><pubDate>Wed, 14 Oct 2026 16:00:00 +0000</pubDate></item>'
                '<item><title>Edgerunners 2 ตอนที่ 1-3 ซับไทย</title><link>https://animeruka.com/ep/e/</link><pubDate>Thu, 15 Oct 2026 04:30:27 +0000</pubDate></item>'
                '<item><title>Black Clover ตอนที่ 2 ซับไทย</title><link>https://anime-waku.com/ep/2/</link><pubDate>Thu, 15 Oct 2026 04:00:00 +0000</pubDate></item></channel></rss>')
        fs = {7: {"yt": "กลายเป็นดาบ|tensei shitara ken"}, 8: {"yt": "edgerunners"}}
        wed = int(datetime(2026, 10, 14, 15, 51, 1, tzinfo=timezone.utc).timestamp())
        thu = int(datetime(2026, 10, 15, 4, 30, 27, tzinfo=timezone.utc).timestamp())
        assert feed_episodes(feed, fs) == [(7, 3, "https://anime-waku.com/ep/113639/", wed)] + [(8, n, "https://animeruka.com/ep/e/", thu) for n in (1, 2, 3)]
        assert specials(vids, sp, 50) == [(1, "13.5", "a", 100, "Ani-One Thailand"), (1, "0", "b", 100, "Ani-One Thailand")]
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
        assert tv_aired(x, {8: [15]}, 1790868600) == {(8, 1): 1790868360}, tv_aired(x, {8: [15]}, 1790868600)  # earliest channel, season numbering, nothing future
        x2 = ('<ProgItem><TID>9</TID><Count>1</Count><StTime>2026-10-04 16:30:00</StTime></ProgItem>'
              '<ProgItem><TID>9</TID><Count>26</Count><StTime>2026-10-06 20:00:00</StTime></ProgItem>')
        assert set(tv_aired(x2, {9: [1, 26]}, 1791298800)) == {(9, 1)}  # the other channel's 26 is the same episode 1 (checked 7 Oct 00:00 JST)
        assert same_show("らんま1/2 (2024) 第3期", "らんま1/2(2026)") and same_show("転生したら剣でした 第2期", "転生したら剣でしたⅡ")
        assert same_show("千歳くんはラムネ瓶のなか 2クール", "千歳くんはラムネ瓶のなか(第2クール)") and same_show("アオのハコ Season２", "アオのハコ Season2")
        assert same_show("佐々木とピーちゃん シーズン２", "佐々木とピーちゃん Season2")
        assert not same_show("ジョジョの奇妙な冒険 スティール・ボール・ラン", "ジョジョの奇妙な冒険")  # a franchise name alone isn't the show
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
        # can this machine read each anime source?
        for name, url in (("animegojo", GOJO + "/"), ("suba-anime", OTHER_PAGES[213657])):
            try:
                page = get(url)
                title = (re.search(r"<title>([^<]*)", page) or [None, "?"])[1].strip()
                print(f"sites {name}: readable ({len(page)} bytes) {title[:60]}")
            except Exception as e:
                print(f"sites {name}: FAILED {e}")
        for name, url in FEEDS.items():
            try:
                page = read_feed(url)
                print(f"sites {name}: readable feed ({len(page)} bytes)")
            except Exception as e:
                print(f"sites {name}: FAILED {type(e).__name__}: {e}")
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
