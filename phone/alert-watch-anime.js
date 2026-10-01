// Alert Watch (anime) for the Scriptable app on iPhone.
// Checks anime-waku, animeruka and animegojoo for new episodes labelled ซับไทย (not พากย์ไทย) of the shows below,
// and shows one iPhone notification per episode, from the first site listing it. Run it from Shortcuts automations.
// The phone has to do this: the three sites block cloud servers.

const SITES = [  // priority order: on a tie the earlier site's link is used
  { name: "anime-waku", url: "https://anime-waku.com/anime/" },
  { name: "animeruka", url: "https://animeruka.com/anime/" },
  { name: "animegojoo", url: "https://animegojoo.com/" },
];

// [name, keyword regex on the site's title (lowercase, romaji or English), season number (0 = any), AniList id, episodes before this entry]
const SHOWS = [
  ["As a Reincarnated Aristocrat Season 3", "tensei kizoku.*kantei|reincarnated aristocrat|appraisal skill", 3, 185756, 0],
  ["Reincarnated as a Sword Season 2", "tensei shitara ken|reincarnated as a sword", 2, 159042, 0],
  ["The Ramparts of Ice Season 2", "koo?ri no jouheki|ramparts of ice", 2, 213805, 0],
  ["The Apothecary Diaries Season 3", "kusuriya no hitorigoto|apothecary diaries", 3, 195516, 0],
  ["Tokyo Revengers Season 4", "tokyo revengers.*(santen|season ?4|4th season|ภาค ?4|ซีซั่น ?4)", 0, 178083, 0],
  ["Even the Student Council Has Its Holes!", "seitokai ni mo ana wa aru|student council has its holes", 0, 191656, 0],
  ["Ranma 1/2 Season 3", "ranma", 3, 209872, 0],
  ["Magic Repo Man", "kashita maryoku|revo barai|magic repo man", 0, 202250, 0],
  ["Blue Box Season 2", "ao no hako|blue box", 2, 189123, 0],
  ["HOTEL INHUMANS Season 2", "hotel inhumans", 2, 199426, 0],
  ["The Detective Is Already Dead Season 2", "tantei wa mou|detective is already dead", 2, 152677, 0],
  ["Sasaki and Peeps Season 2", "sasaki to pii|sasaki and peeps", 2, 176314, 0],
  ["The Iceblade Sorcerer Shall Rule the World Season 2", "hyouken no majutsushi|iceblade sorcerer", 2, 212503, 0],
  ["Mission: Yozakura Family Season 2 Cour 2", "yozakura-?san chi no daisakusen|yozakura family", 2, 213657, 0],
  ["Cyberpunk: Edgerunners 2", "edgerunners ?2|edgerunners.*(season ?2|ภาค ?2|ซีซั่น ?2)", 0, 195539, 0],
  ["JoJo Part 7: Steel Ball Run 2nd-3rd Stage", "steel ball run", 0, 210482, 1],
  ["Chitose Is in the Ramune Bottle 2nd Cour", "chitose-?kun wa ramune|ramune bottle", 0, 198727, 0],
];

const SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";
const MIN_GAP_MINUTES = 10;  // automations can fire close together; don't hit the sites more often than this

// --- parsing: (title, audio label, latest released episode, link) for every card on a listing page ---
function cards(site, html) {
  const out = [];
  let m;
  if (site === "anime-waku") {  // all in the title: "Name (ภาค3) ตอนที่ 1-3 ซับไทย ยังไม่จบ"
    const re = /<a href="([^"]+\/anime\/[^"]+)"><div class="see[^"]*"><\/div><h3><div class="movie-title">([^<]*)/g;
    while ((m = re.exec(html))) {
      const e = /ตอนที่ ?(\d+)(?:-(\d+))?(.*)/.exec(m[2]);
      if (!e) continue;
      const ongoing = e[3].includes("ยังไม่จบ");  // an ongoing show's range ends on the next, not-yet-released episode
      out.push([m[2].slice(0, e.index), e[3], Number(e[2] || e[1]) - (ongoing ? 1 : 0), m[1]]);
    }
  } else if (site === "animeruka") {  // labels before the link; finished shows carry no episode number
    const re = /features-type">([^<]*)<\/span>\s*<span class="features-status">([^<]*)<\/span>\s*<a href="([^"]+)">[\s\S]{0,200}?movie-title">([^<]*)/g;
    while ((m = re.exec(html))) {
      const e = /ตอนที่ ?(\d+)/.exec(m[2]);
      if (e) out.push([m[4], m[1], Number(e[1]), m[3]]);
    }
  } else if (site === "animegojoo") {  // the link goes straight to the episode
    const re = /<a href="(\/list\/[^"]+)" title="([^"]*)">[\s\S]{0,400}?<span class="badge [^"]*">([^<]*)<\/span>\s*<span class="ep-tag">EP (\d+)<\/span>/g;
    while ((m = re.exec(html))) out.push([m[2].replace(/ ตอนที่ \d+$/, ""), m[3], Number(m[4]), "https://animegojoo.com" + m[1]]);
  }
  return out.map(([title, audio, ep, url]) => ({ title: decode(title), audio, ep, url }));
}

function decode(s) {
  return s.replace(/&#8211;/g, "-").replace(/&#8217;/g, "'").replace(/&amp;/g, "&");
}

function isShow(title, keywords, season) {
  const t = title.toLowerCase();
  if (!new RegExp(keywords).test(t)) return false;
  if (!season) return true;
  const roman = { 2: "ii", 3: "iii", 4: "iv" }[season] || "$^";
  const marker = new RegExp(`season ?${season}\\b|\\b${season}(st|nd|rd|th) season|ภาค ?${season}(?!\\d)|ซีซั่น ?${season}(?!\\d)|\\bss ?${season}\\b|\\b${roman}\\b`);
  return marker.test(t);
}

// pages: [{site, html}]; state.seen: {show name: highest episode already reported}. Returns alerts and updates state.
function detect(pages, state) {
  const best = {};
  for (const { site, html } of pages) {
    for (const c of cards(site, html)) {
      if (!c.audio.includes("ซับไทย") || c.audio.includes("พากย์ไทย")) continue;  // only entries labelled ซับไทย alone
      for (const [name, keywords, season, aid, offset] of SHOWS) {
        const floor = best[name] ? best[name].ep : (state.seen[name] || 0);
        if (isShow(c.title, keywords, season) && c.ep > floor) best[name] = { name, ep: c.ep, url: c.url, site, aid, offset };  // strict >: earlier site wins a tie
      }
    }
  }
  // ponytail: one counter per show, so a second cour that restarts at episode 1 on a site would be missed; track per entry if that happens
  const alerts = Object.values(best);
  for (const a of alerts) state.seen[a.name] = a.ep;
  return alerts;
}

function thaiNow() {
  return new Date().toLocaleString("en-GB", { timeZone: "Asia/Bangkok", day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

// --- Scriptable side ---
async function totalEpisodes(aid, offset) {
  try {
    const r = new Request("https://graphql.anilist.co");
    r.method = "POST";
    r.headers = { "Content-Type": "application/json" };
    r.body = JSON.stringify({ query: "query($i:Int){Media(id:$i){episodes}}", variables: { i: aid } });
    const n = (await r.loadJSON()).data.Media.episodes;
    return n ? String(n + offset).padStart(2, "0") : "?";
  } catch (e) {
    return "?";
  }
}

async function main() {
  const fm = FileManager.local();
  const path = fm.joinPath(fm.documentsDirectory(), "alert-watch-anime.json");
  const state = fm.fileExists(path) ? JSON.parse(fm.readString(path)) : { seen: {}, seeded: false, last: 0 };
  if (Date.now() - state.last < MIN_GAP_MINUTES * 60000) return;
  state.last = Date.now();

  const pages = [];
  for (const s of SITES) {
    try {
      const r = new Request(s.url);
      r.timeoutInterval = 20;
      r.headers = { "User-Agent": SAFARI };  // anime-waku refuses requests that don't name a browser; this is the phone's own Safari
      pages.push({ site: s.name, html: await r.loadString() });
    } catch (e) {
      console.log(`FAIL ${s.name}: ${e}`);
    }
  }
  const alerts = detect(pages, state);
  if (state.seeded) {
    for (const a of alerts) {
      const n = new Notification();
      n.title = `New episode: ${a.name}`;
      n.body = `Episode ${String(a.ep).padStart(2, "0")}/${await totalEpisodes(a.aid, a.offset)} ซับไทย\nFound ${thaiNow()} on ${a.site}`;
      n.openURL = a.url;
      await n.schedule();
    }
  }
  console.log(`${pages.length}/${SITES.length} sites read, ${alerts.length} ${state.seeded ? "new" : "recorded silently (first run)"}`);
  state.seeded = true;
  fm.writeString(path, JSON.stringify(state));
}

if (typeof Script !== "undefined") {
  main().then(() => Script.complete());
} else {
  module.exports = { cards, isShow, detect, SITES, SHOWS };  // lets the logic be tested outside the phone
}
