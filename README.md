# Alert Watch

Sends a LINE message (official account **Alert Watch**) when a tracked manga chapter, Webtoon episode or anime episode comes out, and after every Formula 1 session. It runs on GitHub Actions, so it works with your Mac off.

- **Website:** https://isaraapae.github.io/alert-watch/. It has a countdown to the next episode, the next 7 days, and the episodes that aired in the past week with their Watch buttons (**Next episode**), plus a Monday–Sunday **Weekly timetable** (`?v=week`). Times are Thai time.
- **F1 Live:** https://isaraapae.github.io/f1-live/, its own site and repo ([IsaraApae/f1-live](https://github.com/IsaraApae/f1-live)): live timing, the track on a real map, calendar, standings, replays and lap battles.
- **LINE menu:** three buttons: **Next episode**, **Timetable** and **F1 Live**. The menu image is [richmenu.png](richmenu.png).

## What gets an alert

| Kind | Card opens |
|---|---|
| Manga ([urls.txt](urls.txt)) | The new chapter; for the 15 series also on WEBTOON Thai, the series in the WEBTOON app when WEBTOON has that chapter free |
| Webtoon ([urls.txt](urls.txt)) | The episode in the WEBTOON app; for Enrolling in the Transcendent Academy and Zodiac Girls, the series page; for Transcendent Academy the card and LINE list give the app's newest episode ("Chapter 140": the 138 free plus the 2 locked behind coins or ads, `WEBTOON_LOCKED` in `watch.py`) |
| Anime ([anime.txt](anime.txt)) | Muse/Ani-One shows: the Thai-sub video in Brave. Other shows (Netflix, Disney+, Bilibili, online): the [animegojo](https://animegojos.com) episode page, once it's up with Thai subs |
| Anime specials | The Thai-sub video in Brave. Specials are episode 0 or ".5" episodes posted by Muse Thailand or Ani-One Thailand, sent once a YouTube premiere starts. |

Manga and anime cards use the anime website's look: a white rounded card on its soft lavender background, the cover tall with rounded corners, a small red label (NEW EPISODE, NEW CHAPTER, WATCH NOW, NEW SPECIAL), the title, the details in grey, and a **Watch** (anime) or **Read** (manga) button. If LINE ever refuses a card, the alert is sent as plain text instead.

Anime alerts use the time the **Thai-sub** episode comes out (never dubbed, never พากย์ไทย), with the platform name and the episode count ("Episode 03/12"): AniList's, or MyAnimeList's when AniList has none (read from the show's MAL page at most every 6 hours, `mal_totals` in `ci-state.json`); no count anywhere means none is shown, never a guess. If a YouTube channel posts late, the alert waits up to an hour; if the video still isn't up, a "Watch now" card follows when it is.

## F1 Live

The F1 site moved to its own repo, [IsaraApae/f1-live](https://github.com/IsaraApae/f1-live), published at https://isaraapae.github.io/f1-live/. The LINE cards link there: "Open F1 Live" and the menu's **F1 Live** button to `?live` (its Live tab), "Watch replay" to `?replay=<session key>`. The old address, `f1.html` on this repo's `gh-pages` branch, now forwards to the new site, so older cards still work.

## Live session card

When an F1 session starts, LINE gets one card, "LIVE NOW", with a button that opens the F1 Live page (`f1_live` in `ci-state.json`). The check before the start waits for it, so the card comes at the start time; if the session starts more than a few minutes after a check, the next check sends it (up to 30 minutes in). How long a check can wait: the workflow notes when its job began (`JOB_START`), and watch.py keeps 2.5 minutes of the job's 9 for everything else.

## Formula 1 results

A card with the official F1 logo after every session of each Grand Prix weekend: **Practice 1, 2 and 3, Sprint Qualifying, Sprint, Qualifying and Race**, sent once each as soon as the session finishes (`f1_sessions` in `ci-state.json`). The order comes from Formula 1's own live timing feed (free, the one the F1 Live site uses): it counts as final when the feed says so, or 3 minutes after the chequered flag, and a check that finds a session about to finish waits for it. OpenF1 refuses free requests during a session (HTTP 401), so the weekend's schedule and Grand Prix details are saved whenever it answers (`f1_schedule`, `f1_meetings`), and if the feed can't be read, the card comes from OpenF1 once it opens again.

The cards use the F1 Live site's light look: white panels on light grey and the site's driver tags (the car number on the team colour, the three-letter name tinted with it).

- Header: the F1 logo, the Grand Prix with its flag, the session, and the Thai start time.
- The top 3 as a podium (P2, P1 in the middle, P3) with their photos on their team colour, P1 in red, each with their driver tag and time.
- Everyone else as the page's timing table: position, driver tag, name and time, in striped rows.
- A red **Watch replay** button that opens the session's replay on the F1 Live page.
- The time column shows:
  - practice: best lap, then the gap to the fastest;
  - qualifying and sprint qualifying: the lap from the last part reached (Q3 for the top 10);
  - sprint and race: total time, then the gap, laps down, or DNF/DNS/DSQ. An unclassified driver is shown as NC in the position column.

After each race, the **Drivers' and Teams' Championships** follow as two swipeable cards in the same style, with driver photos and team logos in their own colours (as on the site in light mode). Each line shows the position, the change since before the race (+1 in green, -1 in red), and the points with how many were gained (`f1_standings` in `ci-state.json`).

`python3 watch.py f1check` (the **f1check** option when running the workflow by hand) has LINE check the cards, built from the latest race, without sending anything; LINE takes at most 30 KB a card, and a drivers' standings card that would be bigger is sent without faces.

Times on the cards are Thai time, and race lengths are written as "1h 42m 00.785s". Results come from Formula 1's live timing feed straight after a session, otherwise (and standings and team colours) from the free [OpenF1](https://openf1.org) API. Driver photos are formula1.com's portraits for the current season, cropped to the face. OpenF1's own photo links are last season's, in old team suits, and rookies have none.

## Editing the lists

You can manage the lists by messaging Alert Watch on LINE:

| Command | What it does |
|---|---|
| `add <link>` | Track a manga or Webtoon series |
| `anime <title>` | Track an anime (searched on AniList) |
| `thai <Thai name>` | Add the Thai title of the anime just added, so its Thai-sub uploads are found |
| `remove <name>` | Stop tracking a series |
| `list` | Show everything tracked |
| `help` | Show the commands |

Every manga card and the LINE list say "Chapter 216": the manga site's chapter, or for WEBTOON the chapter in the episode's title (WEBTOON's own episode numbers also count notices).

You can also edit the files directly:

- [urls.txt](urls.txt): one series link per line.
  - **Manga also on WEBTOON Thai** (`WEBTOON_TWINS` in `watch.py`): 15 of the manga-site series, found on WEBTOON Thai by their Thai titles (Reborn Rich, Bad Guy, Reality Quest, The Boy of Death (ตั๋วโกงชีวิต), Killer Peter (นักฆ่าย้อนวัย), Juvenile Prison (เยาวชนคนคุก), +99 Wooden Stick, The Ultimate Shut-In, The Delusional Hunter, Secretly More Powerful than the Hero, A Sense of Amusement (สนามเด็กล่า), Genius Grandson of the Loan Shark King, What a Bountiful Harvest, Demon Lord!, The Regressed Son of a Duke Is an Assassin (ลูกชายคนเล็กของดยุกคือมือสังหาร), Life Changer (คอลเซ็นเตอร์เปลี่ยนชีวิต)). They're alerted when the manga site posts a chapter, the fastest source the watcher can read, with their WEBTOON title. The card and the LINE list open the series in the WEBTOON app when WEBTOON has that chapter free ("Chapter 112"), and the manga site when the site is ahead. WEBTOON's count is its newest free episode on its website plus the episodes the app has locked behind coins or ads, as counted in the app in Oct 2026 (`WEBTOON_LOCKED` in `watch.py`: +1 to +3; update it if WEBTOON changes how many it locks). Reborn Rich's and Bad Guy's free episodes on WEBTOON's website stop at Ep.10 and Ep.55, so their app counts are counted by the week instead (`WEBTOON_WEEKLY`): Reborn Rich Ep.208 and Bad Guy Ep.310 on Wed 14 Oct 2026, one more every Wednesday at 22:00 Thai time, each with a card ("Reborn Rich, Chapter 209") (`webtoon_newest` in `ci-state.json`), counted on through the seasons the way the manga sites count: Juvenile Prison's "S2 Ep.12" is chapter 112, since season 1 ended at Ep.100 (each season's start is learned once from WEBTOON's episode list, `webtoon_seasons`). The locked episodes ("35 เหรียญ หรือดูโฆษณา") exist only in the WEBTOON app, which the watcher can't read, hence the fixed counts. For these series (all but Reality Quest and Life Changer, which have no locked count), a card also comes when the app gets one more episode (WEBTOON frees one more; Reborn Rich and Bad Guy: every Wednesday 22:00), giving the app's newest chapter: "+99 ท่อนไม้พร้อมบวก, Chapter 218" (216 free + 2); it comes only when the app is ahead of the manga site, since a chapter the site has came with the site's card (so Bad Guy, with the site at 322 and the app at 311, gets none yet) (`webtoon_app` in `ci-state.json`), and it opens the series' page in the WEBTOON app.
- [anime.txt](anime.txt): `name | AniList id | Syoboi TID (0 = none) | first episode number(s) | episode offset | YouTube keywords`. A short `name | AniList id` line is filled in automatically.
- [thai_times.txt](thai_times.txt): `AniList id | platform | weekday | HH:MM [| days before Japan]` sets when each show comes out with Thai subs. Use `- | -` when only the platform is known. This file overrides the release times the watcher works out on its own (stored in `thai_times_auto.json`).

## How it runs

- [cron-job.org](https://cron-job.org) starts [watch.yml](.github/workflows/watch.yml) every 10 minutes through GitHub's dispatch API. GitHub's own schedule never fired for this repo.
- [watch.py](watch.py) checks the sources, sends the LINE alerts, and commits `ci-state.json` (what has been sent), `schedule.json` (website data) and `library.json` (data for the LINE `list` command).
- Sources:
  - Manga: the sites' pages.
  - Webtoon: RSS feeds.
  - Japanese TV times: [Syoboi Calendar](https://cal.syoboi.jp).
  - Online-only anime: AniList.
  - **When AniList or Syoboi is down** (on 21 Oct 2026 both were: AniList switched its API off for everyone, and Syoboi's server was offline), the watcher carries on with their last good answers, saved as `anilist_media` and `tv_times` in `ci-state.json` (or, before anything is saved, the episodes in `schedule.json`). Alerts, Watch links and the website keep working; the run logs `FAIL anilist: ... (using its last saved answer)`. New shows and changed air times wait until the site is back, and release times aren't learned meanwhile.
  - Thai-sub videos: the YouTube Data API (each channel's 50 newest uploads).
  - Thai-sub episodes on [animegojo](https://animegojos.com): used for the shows not on Muse/Ani-One (Netflix, Disney+, Bilibili, online). A series counts only if its page says (ซับไทย), never พากย์ไทย. These shows get **no card at the scheduled time** — one card (and a website Watch button) is sent only once animegojo has the episode, within ~8 days of release. Episodes released together (a whole Netflix season at once) are grouped into one card, e.g. "Episode 01-10"; if animegojo uploads them in parts, each part gets its own card as it appears ("Episode 01-03", then "Episode 04-07", ...). If a site continues a new cour on an older page (`GOJO_SHIFT` in `watch.py`; Yozakura Cour 2 appears as ตอนที่ 13+), the numbers are mapped back.
  - **Three sites race: anime-waku, animeruka and animegojo.** Each episode gets one card, from whichever site posted it first, and the card opens that site's episode page. Every card shows when the episode aired (its platform's Thai release time, or Japanese TV), not when the site posted it. anime-waku and animeruka are read from their episode feeds (`FEEDS` in `watch.py`), ซับไทย posts only, which give each upload's exact time. animegojo shows no upload times, so an episode first seen there is taken as uploaded halfway between the last check and this one (`first_source` marks the order `estimated`). Ordinary requests only: if a site answers with Cloudflare's bot check (as anime-waku and animeruka have for GitHub's servers), the run logs `FAIL anime-waku: HTTP 403 (Cloudflare bot check)` and the other sites carry on. Run the workflow with **sites** to see which ones GitHub can read. On the website, each episode shows the show's official platform (Netflix, Bilibili, Muse Thailand...), never the site its Watch button opens; Yozakura, with no Thai platform, shows none.
  - **Mission: Yozakura Family Season 2 Cour 2** is the only show also checked on [suba-anime](https://suba-anime.com/yozakura-san-chi-no-daisakusen-ss2/) (`OTHER_PAGES` in `watch.py`), and only until one of the three sites above carries it: then suba-anime is dropped for good (`suba_dropped` in `ci-state.json`) and the three race for it like any other show. Until then suba-anime races with them. Its page lists the whole season, so Cour 2's episode 1 is its ตอนที่ 13 (animegojo too: `GOJO_SHIFT`); episodes marked (ยังไม่มา) aren't out yet. It's checked from a day before each episode is due, with ordinary requests only (if the site ever puts up a bot check, the run reports it rather than working around it). 
  - For animegojo shows, each show's ซับไทย page is remembered (`gojo_pages` in `ci-state.json`). Besides animegojo's fast-moving home page, the watcher checks that page from a day before each episode is due, so an episode is caught even after it scrolls off the home page; the website's Watch buttons use the same page.
  - A show with no saved page (a new season, like Edgerunners 2 or Yozakura Cour 2) is found by searching animegojo (`gojo_search` in `ci-state.json`). Pages already there at the first search are older seasons and never count; a page counts once it is new or gains an episode that is due, and is then saved.
  - Watch links are saved in `ci-state.json` (`watch_links`) once found, so website buttons stay after a video drops out of the channel's newest uploads.
  - The TV schedule is read from a month back, so a rerun on another channel is never taken for an episode's first broadcast.
  - Each episode is alerted once, from whichever source had it first. `first_source` in `ci-state.json` records the source, link, detection time, publication time (when known), and whether the order is uncertain.
- Repository secrets: `LINE_TOKEN` and `YOUTUBE_KEY`. Set them with `gh secret set NAME -R IsaraApae/alert-watch`.
- Workflow inputs:
  - **ping** sends a test card.
  - **richmenu** installs the LINE menu.
  - **f1check** has LINE check every kind of card (anime, manga, and the F1 results, standings and live cards, built from the latest race) without sending anything.
  - **sites** checks which anime sources GitHub can read right now (animegojo, suba-anime and the two episode feeds), and Formula 1's live timing feed.
- `python3 watch.py test` runs the self-checks.
- [line/Code.gs](line/Code.gs) is the LINE command webhook. It runs on Google Apps Script with these script properties: `LINE_TOKEN`, `GITHUB_TOKEN`, `OWNER_ID`. After you change it, redeploy it with **Manage deployments → New version**.
- The `gh-pages` branch holds the website:
  - `index.html`;
  - `w.html`, which opens a link in the WEBTOON app;
  - `b.html`, which opens a link in Brave;
  - `covers/`;
  - `f1.html`, which forwards to the F1 Live site (old links);
  - `f1-logo.png`, the official F1 logo used in the F1 LINE cards.
