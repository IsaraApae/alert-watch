// Alert Watch commands: add or remove series by messaging your Alert Watch LINE account.
// LINE sends each message to this script (its web-app address is the LINE webhook). The script edits
// urls.txt / anime.txt in the alert-watch GitHub repository and replies; the watcher picks the change up
// at its next check. Settings live in Project Settings -> Script Properties:
//   LINE_TOKEN    channel access token from the LINE Developers console
//   GITHUB_TOKEN  fine-grained token for IsaraApae/alert-watch with Contents: Read and write
//   OWNER_ID      your LINE user ID, so only you can use the commands (message Alert Watch once to see it)

const REPO = "IsaraApae/alert-watch";
const BRANCH = "main";
const KNOWN_SITES = ["manga-lc.net", "black-manga.com", "speed-manga.net", "slow-manga.net", "webtoons.com"];
const HELP = [
  "Commands:",
  "add <link>  -  add a manga or Webtoon series",
  "anime <title>  -  add an anime",
  "remove <name>  -  stop tracking (part of the name or link is enough)",
  "list  -  everything you track, as cover cards",
].join("\n");

function doPost(e) {
  const props = PropertiesService.getScriptProperties();
  const events = JSON.parse(e.postData.contents).events || [];
  for (const ev of events) {
    if (ev.type !== "message" || ev.message.type !== "text") continue;
    const owner = props.getProperty("OWNER_ID");
    const uid = ev.source && ev.source.userId;
    let answer;
    if (!owner) {
      answer = `Your LINE ID is:\n${uid}\n\nAdd it in the script's Script Properties as OWNER_ID, then send "help".`;
    } else if (uid !== owner) {
      continue;  // anyone else who messages Alert Watch is ignored
    } else {
      const lock = LockService.getScriptLock();  // two quick messages must not overwrite each other's edit
      lock.waitLock(20000);
      try {
        answer = handle(ev.message.text.trim());
      } catch (err) {
        answer = "That didn't work: " + err.message;
      } finally {
        lock.releaseLock();
      }
    }
    if (typeof answer === "string") reply(ev.replyToken, [{ type: "text", text: answer.slice(0, 4900) }]);
    else if (!reply(ev.replyToken, answer.messages)) reply(ev.replyToken, [{ type: "text", text: answer.fallback.slice(0, 4900) }]);  // LINE refused the cards
  }
  return ContentService.createTextOutput("ok");
}

function handle(text) {
  const words = text.split(/\s+/);
  const cmd = words[0].toLowerCase(), arg = words.slice(1).join(" ").trim();
  if (cmd === "add") return addManga(arg);
  if (cmd === "anime") return addAnime(arg);
  if (cmd === "remove") return remove(arg);
  if (cmd === "list") return list();
  return HELP;
}

function addManga(url) {
  if (!/^https?:\/\/\S+$/.test(url)) return "Send it like this:\nadd https://manga-lc.net/manga/reborn-rich/";
  const f = readFile("urls.txt");
  const lines = f.text.split("\n").filter(l => l.trim());
  if (lines.some(l => l.trim() === url)) return "Already tracked:\n" + seriesName(url);
  writeFile("urls.txt", lines.concat(url).join("\n") + "\n", f.sha, "Add series from LINE");
  const host = url.split("/")[2].replace(/^(www|m)\./, "");
  const untested = KNOWN_SITES.indexOf(host) < 0 ? "\n\nThis site hasn't been tested. If the watcher can't read it, it's skipped." : "";
  const note = `Within 10 minutes it records what's out now; alerts start from the next new chapter.${untested}`;
  return {
    messages: [{ type: "flex", altText: "Added " + seriesName(url), contents: bubble(seriesName(url), "Added", pageCover(url), null, "kilo") },
               { type: "text", text: note }],
    fallback: `Added: ${seriesName(url)}\n\n${note}`,
  };
}

function addAnime(title) {
  if (!title) return "Send it like this:\nanime FX Senshi Kurumi-chan";
  const query = "query($s:String){Page(perPage:6){media(search:$s,type:ANIME,sort:[START_DATE_DESC]){id status season seasonYear format coverImage{large} title{romaji english}}}}";
  const res = UrlFetchApp.fetch("https://graphql.anilist.co", {
    method: "post", contentType: "application/json", muteHttpExceptions: true,
    payload: JSON.stringify({ query: query, variables: { s: title } }),
  });
  const data = JSON.parse(res.getContentText()).data;
  const media = data && data.Page ? data.Page.media : [];
  // the newest airing or upcoming match, so "Blue Box" means the current season
  const pick = media.find(m => m.status === "RELEASING" || m.status === "NOT_YET_RELEASED") || media[0];
  if (!pick) return `Couldn't find "${title}" on AniList. Try the English or Japanese title.`;
  const name = (pick.title.english || pick.title.romaji).replace(/\|/g, "/");
  const f = readFile("anime.txt");
  if (f.text.split("\n").some(l => (l.split("|")[1] || "").trim() === String(pick.id))) return "Already tracked: " + name;
  writeFile("anime.txt", f.text.replace(/\n*$/, "\n") + `${name} | ${pick.id}\n`, f.sha, "Add anime from LINE");
  const season = pick.season ? pick.season[0] + pick.season.slice(1).toLowerCase() + " " + pick.seasonYear : "";
  const about = [season, pick.format].filter(Boolean).join(", ");
  const note = `If an episode aired in the past week, you'll get an alert for it at the next check. Wrong show? Send: remove ${name}`;
  return {
    messages: [{ type: "flex", altText: "Added " + name, contents: bubble(name, "Added · " + about, pick.coverImage && pick.coverImage.large, null, "kilo") },
               { type: "text", text: note }],
    fallback: `Added: ${name}\n(${about}) anilist.co/anime/${pick.id}\n\n${note}`,
  };
}

function remove(word) {
  if (!word) return "Send it like this:\nremove reborn-rich";
  const w = word.toLowerCase(), files = [];
  for (const path of ["urls.txt", "anime.txt"]) {
    const f = readFile(path), lines = f.text.split("\n");
    files.push({ path: path, f: f, lines: lines, hits: lines.filter(l => l.trim() && !l.startsWith("#") && label(l).toLowerCase().includes(w)) });
  }
  const hits = [].concat.apply([], files.map(x => x.hits));
  if (!hits.length) return `Nothing matches "${word}". Send "list" to see the names.`;
  if (hits.length > 1) return `More than one matches "${word}":\n` + hits.map(l => "- " + label(l)).join("\n") + "\nSend a longer part of the name.";
  const x = files.find(x => x.hits.length);
  writeFile(x.path, x.lines.filter(l => l !== x.hits[0]).join("\n"), x.f.sha, "Remove series from LINE");
  const name = label(x.hits[0]);
  return {
    messages: [{ type: "flex", altText: "Removed " + name, contents: bubble(name, "Removed", knownCover(x.hits[0]), null, "kilo") }],
    fallback: "Removed: " + name,
  };
}

function list() {
  const lib = published("library.json"), sched = published("schedule.json"), now = Date.now();
  const manga = lib.manga.map(m => bubble(m.name, m.latest ? "Latest " + m.latest : "Waiting for first check", m.cover, m.open));
  const anime = sched.shows.map(s => {
    const next = s.episodes.find(e => e.at * 1000 > now), last = s.episodes.filter(e => e.at * 1000 <= now).pop();
    const sub = next ? `Next EP ${pad(next.ep)} · ${Utilities.formatDate(new Date(next.at * 1000), "Asia/Bangkok", "EEE d MMM HH:mm")}`
                     : last ? `Latest EP ${pad(last.ep)}` : "No date yet";
    return bubble(s.name, sub, s.cover, null);
  });
  const rows = chunks(manga, 12).map((b, i, all) => carousel(`Manga ${i + 1}/${all.length}`, b))
    .concat(chunks(anime, 12).map((b, i, all) => carousel(`Anime ${i + 1}/${all.length}`, b)));
  const messages = rows.length <= 5 ? rows : rows.slice(0, 4).concat([{ type: "text", text: listText() }]);
  return { messages: messages, fallback: listText() };
}

function bubble(name, sub, image, uri, size) {
  const card = { type: "bubble", size: size || "micro",
    body: { type: "box", layout: "vertical", spacing: "xs", paddingAll: "10px", contents: [
      { type: "text", text: name, size: "xs", weight: "bold", wrap: true, maxLines: 2 },
      { type: "text", text: sub, size: "xxs", color: "#8E8E93", wrap: true, maxLines: 2 } ] } };
  if (image && /^https:\/\/\S+\.(jpe?g|png)(\?\S*)?$/i.test(image))  // LINE cards show JPEG/PNG over https only
    card.hero = { type: "image", url: image, size: "full", aspectRatio: "3:4", aspectMode: "cover" };
  if (uri) card.action = { type: "uri", label: "open", uri: uri };
  return card;
}
// cover of a series being added: the page's preview image, or the one in its layout (some sites)
function pageCover(url) {
  try {
    const page = UrlFetchApp.fetch(url, { muteHttpExceptions: true, headers: { "User-Agent": "Mozilla/5.0" } }).getContentText();
    const m = page.match(/<meta property="og:image" content="([^"]+)"/) || page.match(/<div class="(?:summary_image|thumb)"[^>]*>[\s\S]*?<img[^>]+?(?:data-src|src)="([^"]+)"/);
    return m ? encodeURI(decodeURI(m[1])) : null;
  } catch (e) { return null; }
}
// cover of a tracked series, from what the watcher publishes (library.json for manga, schedule.json for anime)
function knownCover(line) {
  try {
    if (/^\s*https?:/.test(line)) { const m = published("library.json").manga.find(x => x.url === line.trim()); return m && m.cover; }
    const s = published("schedule.json").shows.find(x => x.name === label(line)); return s && s.cover;
  } catch (e) { return null; }
}
function carousel(alt, bubbles) { return { type: "flex", altText: alt, contents: { type: "carousel", contents: bubbles } }; }
function chunks(a, n) { const out = []; for (let i = 0; i < a.length; i += n) out.push(a.slice(i, i + n)); return out; }
function pad(n) { return String(n).padStart(2, "0"); }
function published(path) {  // files the watcher publishes in the repository (public, so no token needed)
  return JSON.parse(UrlFetchApp.fetch(`https://raw.githubusercontent.com/${REPO}/${BRANCH}/${path}?t=${Date.now()}`).getContentText());
}

function listText() {
  const manga = readFile("urls.txt").text.split("\n").filter(l => l.trim()).map(l => "- " + label(l));
  const anime = readFile("anime.txt").text.split("\n").filter(l => l.trim() && !l.startsWith("#")).map(l => "- " + label(l));
  return `Manga (${manga.length})\n${manga.join("\n")}\n\nAnime (${anime.length})\n${anime.join("\n")}`;
}

// a line's display name: the series slug for a link, the name before "|" for an anime
function label(line) {
  return /^\s*https?:/.test(line) ? seriesName(line.trim()) : line.split("|")[0].trim();
}
function seriesName(url) {
  let path = url.split("?")[0];
  try { path = decodeURIComponent(path); } catch (e) {}
  const parts = path.split("/").filter(p => p && p !== "list");
  return parts[parts.length - 1];
}

function github(method, path, payload) {
  const options = {
    method: method, muteHttpExceptions: true,
    headers: { Authorization: "Bearer " + PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN"), Accept: "application/vnd.github+json" },
  };
  if (payload) { options.contentType = "application/json"; options.payload = JSON.stringify(payload); }
  const res = UrlFetchApp.fetch(`https://api.github.com/repos/${REPO}/contents/${path}` + (payload ? "" : `?ref=${BRANCH}`), options);
  if (res.getResponseCode() >= 300) throw new Error(`GitHub answered ${res.getResponseCode()} (check GITHUB_TOKEN)`);
  return JSON.parse(res.getContentText());
}
function readFile(path) {
  const f = github("get", path);
  return { sha: f.sha, text: Utilities.newBlob(Utilities.base64Decode(f.content.replace(/\n/g, ""))).getDataAsString("UTF-8") };
}
function writeFile(path, text, sha, message) {
  github("put", path, { message: message, branch: BRANCH, sha: sha, content: Utilities.base64Encode(text, Utilities.Charset.UTF_8) });
}

function reply(token, messages) {
  const res = UrlFetchApp.fetch("https://api.line.me/v2/bot/message/reply", {
    method: "post", contentType: "application/json", muteHttpExceptions: true,
    headers: { Authorization: "Bearer " + PropertiesService.getScriptProperties().getProperty("LINE_TOKEN") },
    payload: JSON.stringify({ replyToken: token, messages: messages }),
  });
  return res.getResponseCode() === 200;
}
