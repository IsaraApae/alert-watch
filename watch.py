#!/usr/bin/env python3
"""Notify (macOS) when a watched manga page lists a chapter it didn't list last run."""
import json, os, re, shutil, subprocess, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

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


def get(url):
    req = urllib.request.Request(quote(url, safe=":/%?&="), headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def notify(title, msg, links):
    if shutil.which("osascript"):  # absent on the GitHub runner
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title {json.dumps(title)}"])
    token = os.environ.get("LINE_TOKEN")  # phone push: LINE official account broadcast; only set on GitHub
    if token:
        body = json.dumps({"messages": [{"type": "text", "text": f"{title}\n{links}"[:4500]}]}).encode()
        req = urllib.request.Request("https://api.line.me/v2/bot/message/broadcast", data=body,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            print(f"FAIL line: {e} {getattr(e, 'read', lambda: b'')()[:300]}", file=sys.stderr)
            if "ping" in sys.argv:
                sys.exit(1)


THAI = timezone(timedelta(hours=7))


def episode_line(ep, total, aired_at):
    when = datetime.fromtimestamp(aired_at, THAI).strftime("%d %b %Y %H:%M")
    return f"Episode {ep:02d}/{f'{total:02d}' if total else '?'}\nAired {when}"


def anime(state):
    """Notify when AniList's airing schedule shows a new episode of a show in anime.txt (name | AniList id | optional episode offset)."""
    rows = [l.split(" | ") for l in (HERE / "anime.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    names = {int(r[1]): r[0] for r in rows}
    offset = {int(r[1]): int(r[2]) for r in rows if len(r) > 2}  # episodes that aired before this AniList entry starts counting
    pm = {int(r[1]) for r in rows if len(r) > 3 and r[3].strip() == "pm"}  # AniList lists these in the morning by mistake

    def aired_at(media_id, t):  # moves a wrongly-AM time to PM; stops on its own once AniList is corrected
        return t + 43200 if media_id in pm and datetime.fromtimestamp(t, timezone(timedelta(hours=9))).hour < 12 else t
    now = int(time.time())
    query = ("query($ids:[Int],$a:Int,$b:Int){Page(perPage:50){airingSchedules(mediaId_in:$ids,airingAt_greater:$a,"
             "airingAt_lesser:$b,sort:TIME){episode airingAt media{id episodes season seasonYear format}}}}")
    body = json.dumps({"query": query, "variables": {"ids": list(names), "a": now - 8 * 86400, "b": now}}).encode()
    req = urllib.request.Request("https://graphql.anilist.co", data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        aired = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]["Page"]["airingSchedules"]
    except Exception as e:
        print(f"FAIL anilist: {e}", file=sys.stderr)
        return
    seed = "anime" not in state  # first run: record the past week's episodes silently
    seen = state.setdefault("anime", [])
    aired = [a for a in aired if aired_at(a["media"]["id"], a["airingAt"]) <= now]
    new = [a for a in aired if f"{a['media']['id']}:{a['episode']}" not in seen]
    for a in new:
        if not seed:
            off, total = offset.get(a["media"]["id"], 0), a["media"]["episodes"]
            line = episode_line(a["episode"] + off, total and total + off, aired_at(a["media"]["id"], a["airingAt"]))
            m = a["media"]  # broadcast season and format, e.g. "Fall 2026, TV"
            parts = [f"{m['season'].title()} {m['seasonYear']}" if m["season"] and m["seasonYear"] else "", (m["format"] or "").replace("_", " ")]
            label = f" ({', '.join(p for p in parts if p)})" if any(parts) else ""
            notify(f"New episode: {names[m['id']]}{label}", line.replace("\n", " - "), line)
        seen.append(f"{a['media']['id']}:{a['episode']}")
    print(f"anime: {len(names)} watched, {len(new)} {'seeded' if seed else 'new'}")


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    anime(state)
    for url in (HERE / "urls.txt").read_text().split():
        name = unquote(url).rstrip("/").rsplit("/", 1)[-1]
        try:
            found = chapters(url, get(url))
        except Exception as e:
            print(f"FAIL {name}: {e}", file=sys.stderr)
            continue
        if not found:  # blocked / layout changed: keep old state, don't treat as "no chapters"
            print(f"EMPTY {name}", file=sys.stderr)
            continue
        new = sorted(set(found) - set(state[url])) if url in state else []  # first sight of a url = seed silently
        if new:
            notify(f"New chapter: {name}", ", ".join(new)[:200], "\n".join(f"{c}\n{found[c]}" for c in new))
        print(f"{name}: {len(found)} chapters, {len(new)} new")
        state[url] = sorted(set(found) | set(state.get(url, [])))
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        h = ('<a href="https://x.net/manga/a/%e0%b8%95-2/">2</a><a href="https://x.net/other/">o</a>'
             '<a href="https://x.net/manga/a/%e0%b8%95-3/"> ตอนที่ 3 - อัพเดท </a><a href="https://x.net/manga/a/%e0%b8%95-3/">last</a>'
             '<li data-num="{{number}}"><a href="#/chapter-{{number}}"></a><li data-num="7"> <div>\n<a href="https://x.net/a-7/">7</a>')
        want = {"ต-2": "https://x.net/manga/a/%e0%b8%95-2/", "7": "https://x.net/a-7/"}
        assert chapters("https://x.net/manga/a/", h) == want, chapters("https://x.net/manga/a/", h)
        assert episode_line(5, 12, 1790866800) == "Episode 05/12\nAired 01 Oct 2026 22:00", episode_line(5, 12, 1790866800)
        assert episode_line(3, None, 1790866800).startswith("Episode 03/?")
        print("ok")
    elif sys.argv[1:] == ["ping"]:
        notify("manga-watch test", "Phone notifications are working", "https://manga-lc.net/")
    else:
        main()
