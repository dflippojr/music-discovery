// Node checks for the demo's feedback log: event shape, append, cap, clear, JSON Lines
// and staying usable when storage throws.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import { buildEvent, FeedbackLog, LOG_KEY, MAX_EVENTS } from "../../src/musicdiscovery/demo/feedback-log.js";

const schema = JSON.parse(readFileSync(resolve("src/musicdiscovery/feedback-log.schema.json"), "utf8"));

/** A Storage stand-in; `fail` names the methods that throw. */
function memoryStorage(fail = []) {
  const items = new Map();
  const guard = (name) => {
    if (fail.includes(name)) throw new Error(`${name} blocked`);
  };
  return {
    items,
    getItem: (key) => (guard("getItem"), items.has(key) ? items.get(key) : null),
    setItem: (key, value) => (guard("setItem"), items.set(key, String(value))),
    removeItem: (key) => (guard("removeItem"), items.delete(key)),
  };
}

const picks = [
  { id: 101, explained: ["audio", "genre:Folk"] },
  { id: 102, explained: ["metadata"] },
  { id: 103 },
];
const preference = { seeds: [100], likes: ["genre:Folk"], dislikes: ["more:energy"], excludeSeedArtist: true };
const sample = (n = 0) => ({ n });

test("an event records the query, the shown list, the rated track and its explanation terms", () => {
  const event = buildEvent({ ranker: "hybrid", preference, picks, pick: picks[1], rating: -1, time: "2026-10-12T10:00:00Z" });
  assert.deepEqual(event, {
    version: 1,
    time: "2026-10-12T10:00:00Z",
    ranker: "hybrid",
    seed_track_ids: [100],
    likes: ["genre:Folk"],
    dislikes: ["more:energy"],
    exclude_seed_artist: true,
    shown: [{ track_id: 101, rank: 1 }, { track_id: 102, rank: 2 }, { track_id: 103, rank: 3 }],
    track_id: 102,
    rank: 2,
    rating: -1,
    explanation_terms: ["metadata"],
  });
  assert.deepEqual(Object.keys(event).sort(), [...schema.required].sort());
  const bare = buildEvent({ ranker: "baseline", preference: { seeds: [100] }, picks, pick: picks[2], rating: 0 });
  assert.match(bare.time, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.deepEqual([bare.likes, bare.dislikes, bare.exclude_seed_artist, bare.explanation_terms], [[], [], false, []]);
});

test("appends persist and a new log reads them back", () => {
  const storage = memoryStorage();
  const log = new FeedbackLog(storage);
  assert.equal(log.size, 0);
  assert.equal(log.append(sample(1)), true);
  assert.equal(log.append(sample(2)), true);
  assert.deepEqual(JSON.parse(storage.items.get(LOG_KEY)), [sample(1), sample(2)]);
  const again = new FeedbackLog(() => storage);
  assert.deepEqual(again.events(), [sample(1), sample(2)]);
  assert.equal(again.persistent, true);
});

test("another tab's events are kept when appending", () => {
  const storage = memoryStorage();
  const first = new FeedbackLog(storage);
  const second = new FeedbackLog(storage);
  first.append(sample(1));
  second.append(sample(2));
  assert.deepEqual(second.events(), [sample(1), sample(2)]);
});

test("the log is capped and drops the oldest events first", () => {
  assert.equal(MAX_EVENTS, 2000);
  const log = new FeedbackLog(memoryStorage(), { max: 3 });
  for (let n = 1; n <= 5; n += 1) log.append(sample(n));
  assert.deepEqual(log.events(), [sample(3), sample(4), sample(5)]);
});

test("clear empties the log and storage", () => {
  const storage = memoryStorage();
  const log = new FeedbackLog(storage);
  log.append(sample(1));
  log.clear();
  assert.equal(log.size, 0);
  assert.equal(storage.items.has(LOG_KEY), false);
  assert.equal(new FeedbackLog(storage).size, 0);
});

test("the download is one JSON object per line", () => {
  const log = new FeedbackLog(memoryStorage());
  assert.equal(log.toJsonl(), "");
  log.append(sample(1));
  log.append({ text: "a\nb" });
  assert.equal(log.toJsonl(), '{"n":1}\n{"text":"a\\nb"}\n');
});

test("corrupt or unexpected stored values read as an empty log and are replaced", () => {
  for (const value of ["{not json", '{"n":1}', "[1, null, {\"n\":2}]"]) {
    const storage = memoryStorage();
    storage.items.set(LOG_KEY, value);
    const log = new FeedbackLog(storage);
    assert.equal(log.persistent, true);
    log.append(sample(3));
    assert.deepEqual(JSON.parse(storage.items.get(LOG_KEY)).at(-1), sample(3));
  }
  const storage = memoryStorage();
  storage.items.set(LOG_KEY, '[1, null, {"n":2}]');
  assert.deepEqual(new FeedbackLog(storage).events(), [sample(2)]);
});

test("when storage cannot be reached at all the log lives in memory", () => {
  const log = new FeedbackLog(() => {
    throw new Error("SecurityError: access denied");
  });
  assert.equal(log.persistent, false);
  assert.equal(log.append(sample(1)), false);
  assert.equal(log.append(sample(2)), false);
  assert.deepEqual(log.events(), [sample(1), sample(2)]);
  log.clear();
  assert.equal(log.size, 0);
});

test("when reads fail the log lives in memory", () => {
  const log = new FeedbackLog(memoryStorage(["getItem"]));
  assert.equal(log.persistent, false);
  log.append(sample(1));
  assert.deepEqual(log.events(), [sample(1)]);
});

test("a full quota keeps the newer half, and a failing store falls back to memory", () => {
  const storage = memoryStorage();
  const log = new FeedbackLog(storage);
  for (let n = 1; n <= 4; n += 1) log.append(sample(n));
  const setItem = storage.setItem;
  let calls = 0;
  storage.setItem = (key, value) => {
    calls += 1;
    if (calls === 1) throw new Error("QuotaExceededError");
    setItem(key, value);
  };
  assert.equal(log.append(sample(5)), true);
  assert.deepEqual(log.events(), [sample(3), sample(4), sample(5)]);
  assert.deepEqual(JSON.parse(storage.items.get(LOG_KEY)), [sample(3), sample(4), sample(5)]);

  storage.setItem = () => {
    throw new Error("QuotaExceededError");
  };
  assert.equal(log.append(sample(6)), false);
  assert.equal(log.persistent, false);
  assert.deepEqual(log.events(), [sample(3), sample(4), sample(5), sample(6)]);
});

test("a failing clear still empties the page's copy", () => {
  const storage = memoryStorage(["removeItem"]);
  const log = new FeedbackLog(storage);
  log.append(sample(1));
  log.clear();
  assert.equal(log.size, 0);
  assert.equal(log.persistent, false);
});
