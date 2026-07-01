// Fetches Apple Music's "most played albums" chart, filters to full albums,
// and finds each one's Discogs master + best physical pressing. Writes
// chart-links.json so every device running the app can read one shared
// result instead of each independently querying Discogs (whose API key here
// is a single credential shared by every install of the app).
//
// Matching is an independent, deliberately simple fuzzy-match heuristic —
// not a copy of the app's own internal matcher — since a small amount of
// mismatch here just means one chart album backfills later, not a
// user-facing correctness issue.

import { writeFile } from "node:fs/promises";

const FEED_URL = "https://rss.applemarketingtools.com/api/v2/us/music/most-played/100/albums.json";
const DISCOGS_BASE = "https://api.discogs.com";
const USER_AGENT = "VinylChartLinksSync/1.0";
const MIN_TRACK_COUNT = 5;
// Apple's own feed caps at 100, so this just takes the whole filtered chart —
// this job runs once/day from one place, so there's no per-device fan-out
// concern here (unlike the client, which used to search Discogs itself).
const MAX_CANDIDATE_POOL_SIZE = 100;
// Discogs' limit is 60 req/min = 1/sec sustained. 500ms was wrong here — a
// gap between EVERY request (search, then pressing) is 2 req/sec = 120/min,
// which tripped a 429 partway through a 100-album run. No UX pressure on a
// background daily job, so just stay safely under the real limit.
const REQUEST_GAP_MS = 1050;

const DISCOGS_KEY = process.env.DISCOGS_CONSUMER_KEY;
const DISCOGS_SECRET = process.env.DISCOGS_CONSUMER_SECRET;
const DISCOGS_TOKEN = process.env.DISCOGS_API_TOKEN;

if (!DISCOGS_TOKEN && !(DISCOGS_KEY && DISCOGS_SECRET)) {
  console.error("Missing Discogs credentials (DISCOGS_API_TOKEN or DISCOGS_CONSUMER_KEY/SECRET env vars).");
  process.exit(1);
}

function discogsAuthHeader() {
  if (DISCOGS_TOKEN) return `Discogs token=${DISCOGS_TOKEN}`;
  return `Discogs key=${DISCOGS_KEY}, secret=${DISCOGS_SECRET}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function discogsGet(path, query) {
  const url = new URL(DISCOGS_BASE + path);
  for (const [k, v] of Object.entries(query)) url.searchParams.set(k, v);
  const res = await fetch(url, {
    headers: { Authorization: discogsAuthHeader(), "User-Agent": USER_AGENT },
  });
  if (!res.ok) {
    console.warn(`Discogs ${path} -> HTTP ${res.status}`);
    return null;
  }
  return res.json();
}

// --- Simple, independent fuzzy matching ---

function fold(s) {
  return (s || "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

// Drops a trailing "(...)"/"[...]" annotation and anything after the first
// " - " separator, e.g. "Album (Deluxe) - Remastered" -> "Album".
function simplifyTitle(raw) {
  let s = raw.replace(/[([][^)\]]*[)\]]/g, " ");
  const dash = s.indexOf(" - ");
  if (dash !== -1) s = s.slice(0, dash);
  return fold(s);
}

// Drops everything from the first collaborator marker onward.
function simplifyArtist(raw) {
  const cut = raw.search(/\s(feat\.?|ft\.?|featuring|&|and)\s/i);
  return fold(cut === -1 ? raw : raw.slice(0, cut));
}

function isReasonableMatch(hit, title, artist, year) {
  const hay = fold(hit.title || "");
  if (!hay || !title) return false;
  const titleOk = hay === title || hay.includes(title) || title.includes(hay);
  if (!titleOk) return false;
  if (artist && !hay.includes(artist)) return false;
  if (year && hit.year) {
    const hitYear = parseInt(hit.year, 10);
    if (!Number.isNaN(hitYear) && Math.abs(hitYear - year) > 6) return false;
  }
  return true;
}

function bestMaster(hits, title, artist, year) {
  const masters = hits.filter((h) => h.type === "master" && isReasonableMatch(h, title, artist, year));
  masters.sort((a, b) => a.id - b.id);
  return masters[0] || null;
}

// --- Discogs lookups ---

async function searchMasters(artist, title) {
  const fielded = await discogsGet("/database/search", {
    type: "master",
    artist,
    release_title: title,
    per_page: "50",
  });
  if (fielded === null) return null;
  if (fielded.results?.length) return fielded.results;

  const term = `${artist} ${title}`.trim();
  if (!term) return [];
  const fallback = await discogsGet("/database/search", { type: "master", q: term, per_page: "50" });
  if (fallback === null) return null;
  return fallback.results || [];
}

async function popularPressing(masterID) {
  const resp = await discogsGet(`/masters/${masterID}/versions`, {
    per_page: "50",
    sort: "have",
    sort_order: "desc",
    page: "1",
  });
  const versions = resp?.versions || [];
  const vinyl = versions.find((v) => v.major_formats?.includes("Vinyl"));
  if (vinyl) return vinyl;
  return versions.find((v) => !(v.major_formats?.includes("File"))) || null;
}

// --- Apple Music chart + iTunes album filter ---

async function fetchChartCandidates() {
  const feedRes = await fetch(FEED_URL);
  if (!feedRes.ok) throw new Error(`Apple feed HTTP ${feedRes.status}`);
  const feed = await feedRes.json();
  const results = feed.feed?.results || [];

  const allIDs = results.map((r) => r.id).filter(Boolean);
  const itunesRes = await fetch(`https://itunes.apple.com/lookup?id=${allIDs.join(",")}`);
  const itunesData = itunesRes.ok ? await itunesRes.json() : { results: [] };
  const fullAlbumIDs = new Set(
    (itunesData.results || [])
      .filter((item) => item.collectionType === "Album" && (item.trackCount || 0) >= MIN_TRACK_COUNT)
      .map((item) => String(item.collectionId))
  );

  return results
    .filter((r) => fullAlbumIDs.has(r.id))
    .map((r) => ({
      id: r.id,
      name: r.name,
      artistName: r.artistName,
      year: r.releaseDate ? parseInt(String(r.releaseDate).slice(0, 4), 10) : undefined,
    }));
}

// --- Main ---

async function main() {
  const candidates = await fetchChartCandidates();
  const pool = candidates.slice(0, MAX_CANDIDATE_POOL_SIZE);
  console.log(`Chart candidates: ${candidates.length}, pool: ${pool.length}`);

  const links = {};
  let matched = 0;

  for (const album of pool) {
    const title = simplifyTitle(album.name);
    const artist = simplifyArtist(album.artistName);

    const hits = await searchMasters(artist, title);
    if (hits === null) {
      console.warn(`Search failed for "${album.name}" -- likely rate-limited, stopping batch early.`);
      break;
    }
    await sleep(REQUEST_GAP_MS);

    const master = bestMaster(hits, title, artist, album.year);
    if (!master) {
      console.log(`No match: "${album.name}" by ${album.artistName}`);
      continue;
    }

    const masterID = master.master_id || master.id;
    const entry = { masterID };

    const version = await popularPressing(masterID);
    await sleep(REQUEST_GAP_MS);
    if (version) entry.releaseID = version.id;

    links[album.id] = entry;
    matched += 1;
    console.log(`Linked: "${album.name}" -> master ${masterID}${version ? ` / release ${version.id}` : ""}`);
  }

  console.log(`Matched ${matched}/${pool.length}`);

  const output = { generatedAt: new Date().toISOString(), links };
  await writeFile("chart-links.json", JSON.stringify(output, null, 2) + "\n");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
