# Alert Watch

Sends a LINE message (official account **Alert Watch**) when a tracked manga chapter, Webtoon episode or anime episode comes out, and after every Formula 1 session. It runs on GitHub Actions, so it works with your Mac off.

- **Website:** https://isaraapae.github.io/alert-watch/. It has a countdown to the next episode, the next 7 days, and the episodes that aired in the past week with their Watch buttons (**Next episode**), plus a Monday–Sunday **Weekly timetable** (`?v=week`). Times are Thai time.
- **F1 Live page:** https://isaraapae.github.io/alert-watch/f1.html, with free live timing and replays (see below).
- **LINE menu:** three buttons: **Next episode**, **Timetable** and **F1 Live**. The menu image is [richmenu.png](richmenu.png).

## What gets an alert

| Kind | Card opens |
|---|---|
| Manga ([urls.txt](urls.txt)) | The new chapter |
| Webtoon ([urls.txt](urls.txt)) | The episode in the WEBTOON app; for Enrolling in the Transcendent Academy and Zodiac Girls, the series page |
| Anime ([anime.txt](anime.txt)) | Muse/Ani-One shows: the Thai-sub video in Brave. Other shows (Netflix, Disney+, Bilibili, online): the [animegojo](https://animegojos.com) episode page, once it's up with Thai subs |
| Anime specials | The Thai-sub video in Brave. Specials are episode 0 or ".5" episodes posted by Muse Thailand or Ani-One Thailand, sent once a YouTube premiere starts. |

Anime alerts use the time the **Thai-sub** episode comes out (never dubbed, never พากย์ไทย), with the platform name. If a YouTube channel posts late, the alert waits up to an hour; if the video still isn't up, a "Watch now" card follows when it is.

## F1 Live page

[isaraapae.github.io/alert-watch/f1.html](https://isaraapae.github.io/alert-watch/f1.html) has free live timing in any browser, in a dark navy (or light) style like the broadcast timing screens, and the LINE menu's **F1 Live** button opens it. Your browser connects straight to Formula 1's live timing feed, so there's no server.

- **Header:** the country flag, session, track status (clear, yellow, safety car, VSC, red flag), lap, time left, air and track temperature, wind. In qualifying, a countdown: "Q1 ends in 12:34" (red in the last 2 minutes), then "Q2 starts in 6:52" during the 7-minute break (timed from race control's chequered flag), and so on to "Finished"; replays use the real start and flag times.
- **Timing tower:** the position, then each driver's car number and name as a team-colour tag (with **+5s**, **DT** or **SG** while a penalty is still to be served), interval (a green outline when catching), gap, last and best lap (purple for the fastest), sectors with mini sectors, speed trap, tyre stints, pit stops and places gained. **Settings** has a switch for each column (with Show all and Hide extras).
- **Track map:** the circuit with numbered corners and the start line, and the track drawn in three colours, one per sector (S1 red, S2 blue, S3 yellow, split by the fastest sector times), and every car as a dot in its team colour on the racing line, with a label (car number in the team colour, name on a dark box) always straight above it, so labels never move on their own (when cars are nose to tail they can overlap; the leader's is on top). Cars move at 60 frames a second: in replays exactly where their lap timing puts them, with the replay clock advancing every frame. Live positions are an estimate from the timing loops (exact positions are only on F1 TV); replays place cars from their lap times. Sectors under a yellow flag flash yellow; under a safety car or red flag the whole track is outlined.
- **Next session:** between sessions, a banner with the next session, its start time and a countdown, plus a button to replay the last session. It hides while a session is running.
- **Qualifying:** the part (Q1, Q2, Q3), a red line under the last safe place (Q1: 16th of 22, Q2: 10th), and knocked-out drivers greyed at the bottom. Replays work out the parts from race control's session starts and rank each part by its own times; until a driver sets a time in Q2 or Q3, they keep their place from the part before.
- **Follow a driver:** tap a driver in the timing table, on the map or in the race positions chart. Their row is highlighted, their dot grows while the others fade, the charts dim everyone else, and a **Following** card shows their photo, team, position, gap to the leader, gaps to the cars ahead and behind, places gained, last and best lap, tyre and its age, pit stops (or a penalty to serve), sectors and top speed. Tap them again or ✕ to stop; it's remembered in the browser.
- **Battles within 1 second** (races): cars within 1 s of the car ahead get an amber bar in the timing table and an amber interval, are ringed in amber and linked on the map, and the timing header counts them.
- **Fastest sectors.**
- **Race positions** (races and sprints): a lap-by-lap chart from the grid, a line per driver in team colours (the second driver of each team dashed). Tap a driver to highlight them.
- **Tyre strategy:** a bar per driver in running order, one block per set of tyres in its compound's colour and as long as the laps it ran (used sets striped).
- **Pit stops** (races): each stop's lap, driver, tyres before and after, and the time the car stood still (about 2–5 s), with the pit lane time underneath; newest first, the quickest in purple. F1's free feed only gives the pit lane time, so the time stood still is an estimate (shown with ≈): the lane time minus the drive through the pit lane, taken as the race's quickest lane time less a 2.2 s stop. Checked against F1's real times for the 2026 Singapore race: 23 of 25 stops within 0.5 s (17 within 0.3 s). Stops more than 15 s (red flags, repairs) show the lane time only.
- **Penalties:** time penalties, drive-throughs and stop-and-gos (active or served), stewards' investigations and what came of them, black-and-white flags and deleted lap times per driver; "No active penalties" when there are none. Read from race control's messages.
- **Race control:** every message with an icon (the flag's colour, safety car, stewards, track limits, DRS, chequered flag), its kind, lap and time, driver tags in place of "CAR 81 (PIA)", and filter chips (All, Flags, Safety car, Stewards, Track limits).
- **Layout:** on a computer, the timing table, race positions, tyre strategy and team radio on the left; the track map, fastest sectors, championship, pit stops, penalties and race control on the right. On a phone, everything in one column. **Settings → Panels** moves any panel up or down, puts it in the left or right column (on a computer), or hides it (all but Timing); Reset puts it back. Saved in the browser.
- **Live championship:** drivers (photos, team colours) and teams (logos), worked out live during sprints and races from the standings before them.
- **Team radio:** every message F1 publishes (for a race usually about 30, from the drivers shown on TV), as a **race timeline**: grouped under the lap they came on (practice and qualifying: by time of day), each with a dot in the team colour, the time, the driver's tag and name, a waveform-style progress bar and a play button. Filter chips per driver, **Auto-play** for new messages and **Play next**. Live, laps come from the lap counter while the page is open and from race control's lap numbers before that. F1 publishes no transcripts, and its audio can't be read by other sites or by cloud servers, so there's no transcript text.
- **Gaps in replays:** a car is "+1 Lap" only when the leader finished an extra lap before that car crossed the line; the interval is the time since the car ahead last crossed the line (never a lap or more); retired cars show no gap; and once the winner takes the flag, positions and gaps come from the official result. Checked for every driver in six 2026 races (Singapore, Britain, Austria, Hungary, Netherlands, Azerbaijan): no car shown lapped while less than a lap behind, and the lapped drivers at the finish match the official results.
- **Replay:** any finished session since 2023, from [OpenF1](https://openf1.org), picked by season and weekend (with flags and short session buttons). Play or pause it, skip 30 s, change speed (1×–32×), or drag the timeline: it shows safety-car (amber), VSC (striped) and red-flag (red) periods and lap marks, with a bubble showing the time and lap while you drag. The clock shows the race time, the lap and the time of day. If OpenF1 is busy (it allows 30 requests a minute), the page waits and tries again.
- **Track status:** live, straight from F1's feed. In replays it is worked out from race control's messages (safety car, VSC and VSC ending, red flag, yellow sectors, track clear); checked against F1's own recorded status for the 2026 Singapore race, it matched at 417 of 419 moments.
- **Appearance:** Dark (default, navy), Light or Device, in Settings.

The live code was tested by playing F1's own recording of the 2026 Singapore race and qualifying through a local copy of the live connection (`f1.html?feed=ws://localhost:PORT` points the page at a local test feed; only localhost is accepted).

The page is published on the `gh-pages` branch as `f1.html`. Circuits come from MultiViewer's circuit API, flags from OpenF1, and photos and logos from formula1.com.

## Live session card

From 15 minutes before an F1 session starts to 30 minutes after, LINE gets one card, "LIVE NOW" (or "STARTING SOON"), with a button that opens the F1 Live page (`f1_live` in `ci-state.json`).

## Formula 1 results

A card with the official F1 logo after every session of each Grand Prix weekend: **Practice 1, 2 and 3, Sprint Qualifying, Sprint, Qualifying and Race**, sent once each when the results come out (`f1_sessions` in `ci-state.json`).

- Dark F1 header: the Grand Prix with its flag, the session, and the Thai start time.
- The top 3 with their photos on their team colour.
- Everyone else in order: position, team-colour stripe, photo, name, team and time.
- The time column shows:
  - practice: best lap, then the gap to the fastest;
  - qualifying and sprint qualifying: the lap from the last part reached (Q3 for the top 10);
  - sprint and race: total time, then the gap, laps down, or DNF/DNS/DSQ. An unclassified driver is shown as NC in the position column.

After each race, the **Drivers' and Teams' Championships** follow as two swipeable cards, with driver photos and team logos. Each line shows the position, the change since before the race (+1 in green, -1 in red), and the points with how many were gained (`f1_standings` in `ci-state.json`).

Times on the cards are Thai time, and race lengths are written as "1h 42m 00.785s". Results, standings and team colours come from the free [OpenF1](https://openf1.org) API. Driver photos are formula1.com's portraits for the current season, cropped to the face. OpenF1's own photo links are last season's, in old team suits, and rookies have none.

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

You can also edit the files directly:

- [urls.txt](urls.txt): one series link per line.
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
  - Thai-sub videos: the YouTube Data API (each channel's 50 newest uploads).
  - Thai-sub episodes on [animegojo](https://animegojos.com): used for the shows not on Muse/Ani-One (Netflix, Disney+, Bilibili, online). A series counts only if its page says (ซับไทย), never พากย์ไทย. These shows get **no card at the scheduled time** — one card (and a website Watch button) is sent only once animegojo has the episode, within ~8 days of release. Episodes released together (a whole Netflix season at once) are grouped into one card, e.g. "Episode 01-10"; if animegojo uploads them in parts, each part gets its own card as it appears ("Episode 01-03", then "Episode 04-07", ...). If animegojo continues a new cour on an older page (`GOJO_SHIFT` in `watch.py`; Yozakura Cour 2 may appear as ตอนที่ 13+), the numbers are mapped back. anime-waku and animeruka block GitHub's servers with Cloudflare, so they aren't checked.
  - For animegojo shows, each show's ซับไทย page is remembered (`gojo_pages` in `ci-state.json`). Besides animegojo's fast-moving home page, the watcher checks that page from a day before each episode is due, so an episode is caught even after it scrolls off the home page; the website's Watch buttons use the same page.
  - A show with no saved page (a new season, like Edgerunners 2 or Yozakura Cour 2) is found by searching animegojo (`gojo_search` in `ci-state.json`). Pages already there at the first search are older seasons and never count; a page counts once it is new or gains an episode that is due, and is then saved.
  - Watch links are saved in `ci-state.json` (`watch_links`) once found, so website buttons stay after a video drops out of the channel's newest uploads.
  - The TV schedule is read from a month back, so a rerun on another channel is never taken for an episode's first broadcast.
  - Each episode is alerted once, from whichever source had it first. `first_source` in `ci-state.json` records the source, link, detection time, publication time (when known), and whether the order is uncertain.
- Repository secrets: `LINE_TOKEN` and `YOUTUBE_KEY`. Set them with `gh secret set NAME -R IsaraApae/alert-watch`.
- Workflow inputs:
  - **ping** sends a test card.
  - **richmenu** installs the LINE menu.
- `python3 watch.py test` runs the self-checks.
- [line/Code.gs](line/Code.gs) is the LINE command webhook. It runs on Google Apps Script with these script properties: `LINE_TOKEN`, `GITHUB_TOKEN`, `OWNER_ID`. After you change it, redeploy it with **Manage deployments → New version**.
- The `gh-pages` branch holds the website:
  - `index.html`;
  - `w.html`, which opens a link in the WEBTOON app;
  - `b.html`, which opens a link in Brave;
  - `covers/`;
  - `f1.html`, the F1 Live page;
  - `f1-logo.png`, the official F1 logo used in the F1 LINE cards.
