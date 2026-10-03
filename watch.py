#!/usr/bin/env python3
"""Notify (macOS) when a watched manga page lists a chapter it didn't list last run."""
import html, json, os, re, shutil, subprocess, sys, time, unicodedata, urllib.error, urllib.request
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


def get(url):
    req = urllib.request.Request(quote(url, safe=":/%?&="), headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def card(title, msg, image, uri=None):
    """A small LINE card: cover on the left, title and chapter on the right; tapping anywhere opens uri, if given."""
    text = lambda t, **k: {"type": "text", "text": t, "size": "sm", "wrap": True, **k}
    return {"type": "bubble", "size": "kilo", **({"action": {"type": "uri", "label": "open", "uri": uri}} if uri else {}),
            "body": {"type": "box", "layout": "horizontal", "spacing": "md", "contents": [
                {"type": "image", "url": image, "size": "sm", "aspectMode": "cover", "aspectRatio": "1:1", "flex": 0},
                {"type": "box", "layout": "vertical", "contents": [text(title, weight="bold"), text(msg, color="#777777")]}]}}


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
        req = urllib.request.Request("https://api.line.me/v2/bot/message/broadcast", data=json.dumps({"messages": [message]}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
            return
        except Exception as e:
            print(f"FAIL line ({message['type']}): {e} {getattr(e, 'read', lambda: b'')()[:300]}", file=sys.stderr)
    if "ping" in sys.argv:
        sys.exit(1)


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
    """thai_times.txt: {AniList id: (where, weekday, "HH:MM")} for shows whose Thai-sub release is later than Japanese TV."""
    auto = HERE / "thai_times_auto.json"  # learned by the watcher; thai_times.txt (yours) overrides it
    out = {int(k): tuple(v) for k, v in (json.loads(auto.read_text()) if auto.exists() else {}).items()}
    path = HERE / "thai_times.txt"
    for line in path.read_text().splitlines() if path.exists() else []:
        r = [x.strip() for x in line.split("|")]
        if line.startswith("#") or len(r) != 4 or not r[0].isdigit():
            continue
        if r[2] == r[3] == "-":  # platform known, release time not: keep the Japanese time, but name where to watch
            out[int(r[0])] = (r[1], None, None)
        elif r[2][:3].title() in WEEKDAYS and re.fullmatch(r"\d{1,2}[:.]\d{2}", r[3]):
            out[int(r[0])] = (r[1], r[2][:3].title(), r[3].replace(".", ":"))
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
    """[(title, link, published timestamp, channel)] from the official channels' public feeds (latest 15 each)."""
    out = []
    for cid in YT_CHANNELS:
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


def schedule():
    """Write schedule.json for the countdown page: every show's episodes from a week ago to two months ahead,
    with the same times and numbering as the alerts. Rewritten only when something changed, so it isn't committed every run."""
    shows, now = anime_list(), int(time.time())
    q = "query($ids:[Int]){Page(perPage:50){media(id_in:$ids){id episodes season seasonYear format coverImage{large} airingSchedule(perPage:50){nodes{episode airingAt}}}}}"
    try:
        info = {m["id"]: m for m in anilist(q, {"ids": list(shows)})["Page"]["media"]}
        by_tid = {s["tid"]: i for i, s in shows.items() if s["tid"]}
        span = f"{datetime.fromtimestamp(now - 8 * 86400, JST):%Y%m%d_%H%M%S}-{datetime.fromtimestamp(now + 60 * 86400, JST):%Y%m%d_%H%M%S}"
        xml = get(f"https://cal.syoboi.jp/db.php?Command=ProgLookup&TID={','.join(map(str, by_tid))}&Range={span}")
        tv = tv_aired(xml, {tid: shows[i]["first"] for tid, i in by_tid.items()}, now + 60 * 86400)
    except Exception as e:  # keep the previous file rather than publish a half-empty one
        print(f"FAIL schedule: {e}", file=sys.stderr)
        return
    videos = youtube_videos()
    thai = thai_times()
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
            t = thai_slot(t, *thai[i][1:]) if i in thai and thai[i][1] else t
            if now - 8 * 86400 <= t <= now + 60 * 86400:
                yt = t <= now and s["yt"] and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t)
                rows.append({"ep": ep + s["offset"], "at": t, **({"watch": in_brave(yt)} if yt else {})})
        total = m.get("episodes")
        out.append({"name": s["name"], "label": season_label(m), "cover": (m.get("coverImage") or {}).get("large"),
                    "total": total and total + s["offset"], "where": thai[i][0] if i in thai else None, "episodes": rows})
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
    body = json.dumps({"query": query, "variables": {"ids": list(shows), "web": web_only or [0], "a": now - 8 * 86400, "b": now}}).encode()
    req = urllib.request.Request("https://graphql.anilist.co", data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]
    except Exception as e:
        print(f"FAIL anilist: {e}", file=sys.stderr)
        return
    info = {m["id"]: m for m in data["info"]["media"]}
    aired = {(a["media"]["id"], a["episode"]): a["airingAt"] for a in data["web"]["airingSchedules"] if a["airingAt"] <= now}
    by_tid = {s["tid"]: i for i, s in shows.items() if s["tid"]}
    if by_tid:
        span = f"{datetime.fromtimestamp(now - 8 * 86400, JST):%Y%m%d_%H%M%S}-{datetime.fromtimestamp(now, JST):%Y%m%d_%H%M%S}"
        try:
            xml = get(f"https://cal.syoboi.jp/db.php?Command=ProgLookup&TID={','.join(map(str, by_tid))}&Range={span}")
            first = {tid: shows[i]["first"] for tid, i in by_tid.items()}
            aired.update({(by_tid[tid], ep): t for (tid, ep), t in tv_aired(xml, first, now).items()})
        except Exception as e:
            print(f"FAIL tv schedule: {e}", file=sys.stderr)
    seed = "anime" not in state  # first run: record the past week's episodes silently
    seen = state.setdefault("anime", [])
    thai = thai_times()  # shows released later with Thai subs: alert at that time instead of the Japanese broadcast
    aired = {k: (thai_slot(t, *thai[k[0]][1:]) if k[0] in thai and thai[k[0]][1] else t) for k, t in aired.items()}
    aired = {k: t for k, t in aired.items() if t <= now}
    new = sorted((t, i, ep) for (i, ep), t in aired.items() if f"{i}:{ep}" not in seen)
    videos = youtube_videos() if new and not seed else []
    for t, i, ep in new:
        if not seed:
            s, m = shows[i], info.get(i, {})
            total = m.get("episodes")
            line = episode_line(ep + s["offset"], total and total + s["offset"], t, thai[i][0] if i in thai else None)
            label = f" ({season_label(m)})" if season_label(m) else ""  # e.g. "Fall 2026, TV"
            # if an official channel already has this episode up with Thai subs, the card opens that video; otherwise no link
            yt = s["yt"] and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"][0], 1) - 1}, t)
            if not yt and i in thai and thai[i][0] in YT_CHANNELS.values() and now - t < 30 * 60:
                continue  # the channel's video can take minutes to reach its feed: retry at the next check, up to 30 minutes
            notify(f"New episode: {s['name']}{label}", line, line + (f"\n{yt}" if yt else ""), (m.get("coverImage") or {}).get("large"), yt and in_brave(yt))
        seen.append(f"{i}:{ep}")
    print(f"anime: {len(shows)} watched, {len(new)} {'seeded' if seed else 'new'}")


def setup_richmenu():
    """Install the Alert Watch menu at the bottom of the LINE chat: two buttons opening the countdown site.
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
    menu = {"size": {"width": 2500, "height": 843}, "selected": True, "name": "Alert Watch", "chatBarText": "Anime schedule",
            "areas": [{"bounds": {"x": 0, "y": 0, "width": 1250, "height": 843}, "action": {"type": "uri", "label": "Next episode", "uri": site}},
                      {"bounds": {"x": 1250, "y": 0, "width": 1250, "height": 843}, "action": {"type": "uri", "label": "Timetable", "uri": site + "?v=week"}}]}
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
        schedule()
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
        v = [("[พากย์ไทย] ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ซีซั่น 2 ตอนที่ 1", "dub", 1790782200.0),
             ("ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ตอนที่ 1 [ซับไทย]", "season1", 1700000000.0),
             ("ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ซีซั่น 2 ตอนที่ 1 [ซับไทย]", "sub", 1790782200.0)]
        assert youtube_episode(v, "กลายเป็นดาบ", {1}, 1790782200) == "sub"  # Thai-sub, this season, right episode
        assert youtube_episode(v, "กลายเป็นดาบ", {2}, 1790782200) is None
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
    else:
        main()
