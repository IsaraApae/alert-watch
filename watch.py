#!/usr/bin/env python3
"""Notify (macOS) when a watched manga page lists a chapter it didn't list last run."""
import html, json, os, re, shutil, subprocess, sys, time, urllib.request
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


def cover(url):
    """(title, cover image) from a series page's preview tags, or (None, None)."""
    try:
        tags = dict(re.findall(r'<meta property="og:(title|image)" content="([^"]+)"', get(url)))
        return tags.get("title"), tags.get("image")
    except Exception as e:
        print(f"FAIL cover {url}: {e}", file=sys.stderr)
        return None, None


WEBTOON_APP = "https://isaraapae.github.io/manga-watch/w.html?t="  # redirect page on the gh-pages branch; opens linewebtoon://


THAI = timezone(timedelta(hours=7))


def episode_line(ep, total, aired_at):
    when = datetime.fromtimestamp(aired_at, THAI).strftime("%d %b %Y %H:%M")
    return f"Episode {ep:02d}/{f'{total:02d}' if total else '?'}\nAired {when}"


JST = timezone(timedelta(hours=9))


def tv_aired(xml, first_ep, now):
    """{(TV-schedule id, season episode): earliest broadcast} from Syoboi Calendar's ProgLookup XML, for episodes already on air."""
    out = {}
    for item in re.findall(r"<ProgItem\b.*?</ProgItem>", xml, re.S):
        p = dict(re.findall(r"<(\w+)>([^<]*)</\1>", item))
        if p.get("Deleted") == "1" or not p.get("Count", "").isdigit() or int(p["TID"]) not in first_ep:
            continue
        t = datetime.strptime(p["StTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).timestamp()
        key = (int(p["TID"]), int(p["Count"]) - first_ep[int(p["TID"])] + 1)  # the TV schedule keeps counting across seasons
        if t <= now and (key not in out or t < out[key]):
            out[key] = t
    return out


YT_CHANNELS = ["UCn8hjQOnGYR1AZtYYMYP5jQ", "UCw2bdNSXh4x6e0NCduVoxMQ"]  # Muse Thailand, Ani-One Thailand: official uploads


def youtube_videos():
    """[(title, link, published timestamp)] from the official channels' public feeds (latest 15 each)."""
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
                out.append((html.unescape(t[1]), l[1], datetime.fromisoformat(p[1]).timestamp()))
    return out


def youtube_episode(videos, pattern, eps, aired):
    """Link to a [ซับไทย] upload of one of these episode numbers, posted around broadcast (not an older season's video)."""
    for title, link, published in videos:
        t = title.lower()
        if "ซับไทย" in t and "พากย์ไทย" not in t and re.search(pattern, t) and published >= aired - 6 * 3600 \
                and any(re.search(rf"(?:ตอนที่|ep\.?|episode)\s*0*{e}(?!\d)", t) for e in eps):
            return link
    return None


def anime(state):
    """Notify when a show in anime.txt airs a new episode: the Japanese TV schedule (Syoboi Calendar, earliest channel)
    for TV shows, AniList's airing schedule for online-only ones. anime.txt: name | AniList id | TV-schedule id (0 = none)
    | first episode number in the TV schedule | episodes before this AniList entry (added to the count)."""
    rows = [l.split(" | ") for l in (HERE / "anime.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    shows = {int(r[1]): {"name": r[0], "tid": int(r[2]), "first": int(r[3]), "offset": int(r[4]), "yt": r[5] if len(r) > 5 else ""} for r in rows}
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
    new = sorted((t, i, ep) for (i, ep), t in aired.items() if f"{i}:{ep}" not in seen)
    videos = youtube_videos() if new and not seed else []
    for t, i, ep in new:
        if not seed:
            s, m = shows[i], info.get(i, {})
            total = m.get("episodes")
            line = episode_line(ep + s["offset"], total and total + s["offset"], t)
            parts = [f"{m['season'].title()} {m['seasonYear']}" if m.get("season") and m.get("seasonYear") else "", (m.get("format") or "").replace("_", " ")]
            label = f" ({', '.join(p for p in parts if p)})" if any(parts) else ""  # e.g. "Fall 2026, TV"
            # if an official channel already has this episode up with Thai subs, the card opens that video; otherwise no link
            yt = s["yt"] and youtube_episode(videos, s["yt"], {ep, ep + s["offset"], ep + max(s["first"], 1) - 1}, t)
            notify(f"New episode: {s['name']}{label}", line, line + (f"\n{yt}" if yt else ""), (m.get("coverImage") or {}).get("large"), yt or None)
        seen.append(f"{i}:{ep}")
    print(f"anime: {len(shows)} watched, {len(new)} {'seeded' if seed else 'new'}")


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    anime(state)
    for url in (HERE / "urls.txt").read_text().split():
        name = [p for p in urlparse(unquote(url)).path.split("/") if p and p != "list"][-1]  # series slug
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
                title, image = cover(url)  # LINE cards only open web addresses, so the card goes via a page that hands off to the app
                notify(f"New chapter: {title or name}", shown, shown + "\nlinewebtoon://episodeList/webtoon?titleNo=" + no, image, WEBTOON_APP + no)
            else:  # cover card that opens the series page (encoded so LINE accepts it)
                link, image = quote(url, safe=":/%"), cover(url)[1]
                notify(f"New chapter: {name}", shown, shown + "\n" + link, image and quote(image, safe=":/%?=&"), link)
        print(f"{name}: {len(found)} chapters, {len(new)} new")
        state[url] = sorted(set(found) | set(state.get(url, [])))
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


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
        v = [("[พากย์ไทย] ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ซีซั่น 2 ตอนที่ 1", "dub", 1790782200.0),
             ("ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ตอนที่ 1 [ซับไทย]", "season1", 1700000000.0),
             ("ซวยเหลือหลายเกิดใหม่กลายเป็นดาบ ซีซั่น 2 ตอนที่ 1 [ซับไทย]", "sub", 1790782200.0)]
        assert youtube_episode(v, "กลายเป็นดาบ", {1}, 1790782200) == "sub"  # Thai-sub, this season, right episode
        assert youtube_episode(v, "กลายเป็นดาบ", {2}, 1790782200) is None
        x = ('<ProgItem><TID>8</TID><Count>15</Count><StTime>2026-10-02 00:26:00</StTime><Deleted>0</Deleted></ProgItem>'
             '<ProgItem><TID>8</TID><Count>15</Count><StTime>2026-10-02 02:00:00</StTime><Deleted>0</Deleted></ProgItem>'
             '<ProgItem><TID>8</TID><Count>16</Count><StTime>2026-10-08 23:56:00</StTime><Deleted>0</Deleted></ProgItem>')
        assert tv_aired(x, {8: 15}, 1790868600) == {(8, 1): 1790868360}, tv_aired(x, {8: 15}, 1790868600)  # earliest channel, season numbering, nothing future
        print("ok")
    elif sys.argv[1:] == ["ping"]:
        # replays the real alert for Reincarnated as a Sword Season 2 episode 1 (aired 30 Sep 2026 22:30 Thai)
        q = json.dumps({"query": "{Media(id:159042){episodes season seasonYear format coverImage{large}}}"}).encode()
        r = urllib.request.Request("https://graphql.anilist.co", data=q, headers={"Content-Type": "application/json", "User-Agent": UA})
        m = json.loads(urllib.request.urlopen(r, timeout=30).read())["data"]["Media"]
        line = episode_line(1, m["episodes"], 1790782200)
        yt = youtube_episode(youtube_videos(), "กลายเป็นดาบ|tensei shitara ken|reincarnated as a sword", {1}, 1790782200)
        print("youtube link:", yt)
        notify(f"New episode: Reincarnated as a Sword Season 2 ({m['season'].title()} {m['seasonYear']}, {m['format']})",
               line, line + (f"\n{yt}" if yt else ""), m["coverImage"]["large"], yt)
    else:
        main()
