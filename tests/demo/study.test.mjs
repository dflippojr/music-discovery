// Node checks for the study's DOM-free logic: balanced list order, session id, export shape
// and the copy of the session kept for resuming.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import { balancedOrders, buildExport, newSessionId, restoreSession, SessionStore, sessionState, taskComplete, timestamp } from "../../src/musicdiscovery/demo/study-lib.js";
import * as lib from "../../src/musicdiscovery/demo/study-lib.js";

const schema = JSON.parse(readFileSync(resolve("src/musicdiscovery/study-ratings.schema.json"), "utf8"));

/** A seeded generator, so the tests are repeatable; the page itself uses crypto. */
function seeded(seed) {
  let state = seed >>> 0;
  return (n) => {
    state = (state + 0x6d2b79f5) >>> 0; // mulberry32
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return Math.floor((((t ^ (t >>> 14)) >>> 0) / 2 ** 32) * n);
  };
}

test("every task gets one list order and hybrid is first in half the tasks", () => {
  for (const count of [4, 5, 6]) {
    for (let seed = 1; seed <= 200; seed += 1) {
      const orders = balancedOrders(count, seeded(seed));
      assert.equal(orders.length, count);
      for (const order of orders) assert.deepEqual([...order].sort(), ["baseline", "hybrid"]);
      const hybridFirst = orders.filter((o) => o[0] === "hybrid").length;
      assert.ok(Math.abs(hybridFirst - (count - hybridFirst)) <= count % 2, `count ${count} seed ${seed}: ${hybridFirst}`);
    }
  }
});

test("the order is shuffled and the odd task out is not always the same ranker", () => {
  const first = new Set();
  const odd = new Set();
  for (let seed = 1; seed <= 200; seed += 1) {
    const orders = balancedOrders(5, seeded(seed));
    first.add(orders[0][0]);
    odd.add(orders.filter((o) => o[0] === "hybrid").length);
  }
  assert.deepEqual([...first].sort(), ["baseline", "hybrid"]);
  assert.deepEqual([...odd].sort(), [2, 3]);
});

test("session ids are 32 hex characters and timestamps are second-precision UTC", () => {
  const id = newSessionId((bytes) => bytes.forEach((_, i) => (bytes[i] = i * 17)));
  assert.match(id, /^[0-9a-f]{32}$/);
  assert.match(timestamp(), /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.equal(timestamp(new Date("2026-10-06T14:03:09.987Z")), "2026-10-06T14:03:09Z");
});

function sampleTask(rating = 1) {
  return {
    seedTrackId: 7,
    likes: ["genre:Folk"],
    dislikes: [],
    startedAt: "2026-10-06T14:00:00Z",
    completedAt: "2026-10-06T14:03:00Z",
    preferred: "A",
    lists: ["A", "B"].map((label, i) => ({
      label,
      ranker: i ? "hybrid" : "baseline",
      results: [{ trackId: 1 + i, row: 0, rating, newToMe: true }],
    })),
  };
}

test("a task is complete only with every rating and a preference", () => {
  assert.equal(taskComplete(sampleTask()), true);
  assert.equal(taskComplete({ ...sampleTask(), preferred: null }), false);
  assert.equal(taskComplete(sampleTask(null)), false);
  assert.equal(taskComplete(sampleTask(-1)), true);
});

test("the export holds only the schema's fields", () => {
  const body = buildExport({ id: "0".repeat(32), startedAt: "2026-10-06T14:00:00Z", tasks: [sampleTask()] }, "2026-10-06T14:05:00Z");
  const resolveRef = (node) => (node.$ref ? schema.$defs[node.$ref.split("/").pop()] : node);
  const allowed = (value, rawNode, path = "$") => {
    const node = resolveRef(rawNode);
    if (Array.isArray(value)) value.forEach((item) => allowed(item, node.items, `${path}[]`));
    else if (value !== null && typeof value === "object") {
      assert.equal(node.additionalProperties, false, path);
      for (const [key, inner] of Object.entries(value)) {
        assert.ok(key in node.properties, `${path}.${key} is not in the schema`);
        allowed(inner, node.properties[key], `${path}.${key}`);
      }
    }
  };
  allowed(body, schema);
  assert.deepEqual(Object.keys(body).sort(), Object.keys(schema.properties).sort());
  assert.deepEqual(Object.keys(body.tasks[0]).sort(), Object.keys(schema.$defs.task.properties).sort());
});

/** A storage double; `fail` names the methods that throw, as blocked or full storage does. */
function memoryStorage(fail = []) {
  const items = new Map();
  const guard = (name, run) => (...args) => {
    if (fail.includes(name)) throw new Error(`${name} failed`);
    return run(...args);
  };
  return {
    items,
    getItem: guard("getItem", (key) => (items.has(key) ? items.get(key) : null)),
    setItem: guard("setItem", (key, value) => items.set(key, String(value))),
    removeItem: guard("removeItem", (key) => items.delete(key)),
  };
}

const IDS = [7, 1, 2, 3];
const ORDERS = Array.from({ length: 5 }, () => ["baseline", "hybrid"]);

function sampleState({ tasks = 1, current = null } = {}) {
  const session = { id: "a".repeat(32), startedAt: "2026-10-06T13:59:00Z", tasks: Array.from({ length: tasks }, () => sampleTask()) };
  for (const task of session.tasks) task.lists.forEach((list, i) => (list.results = [{ trackId: IDS[1 + i], row: 1 + i, rating: 1, newToMe: false }]));
  return sessionState(session, ORDERS, current, tasks > 0);
}

function shownTask() {
  const task = sampleState().session.tasks[0];
  task.lists.forEach((list) => list.results.forEach((r) => (r.rating = null)));
  return { ...task, completedAt: null, preferred: null };
}

test("the session store round-trips a state and survives storage that throws", () => {
  const storage = memoryStorage();
  const store = new SessionStore(storage, "k");
  assert.equal(store.load(), null);
  assert.equal(store.save({ a: 1 }), true);
  assert.deepEqual(store.load(), { a: 1 });
  store.clear();
  assert.equal(store.load(), null);
  storage.items.set("k", "{not json");
  assert.equal(store.load(), null);

  for (const fail of [["getItem", "setItem", "removeItem"], ["setItem"]]) {
    const broken = new SessionStore(() => memoryStorage(fail), "k");
    assert.equal(broken.save({ a: 1 }), false);
    assert.equal(broken.load(), null);
    assert.doesNotThrow(() => broken.clear());
  }
  const blocked = new SessionStore(() => {
    throw new Error("SecurityError");
  });
  assert.equal(blocked.load(), null);
  assert.equal(blocked.save({}), false);
  assert.doesNotThrow(() => blocked.clear());
});

test("a stored session is restored only when it fits this catalog and protocol", () => {
  const between = sampleState();
  assert.equal(restoreSession(between, IDS), between);
  const picking = sampleState({ current: { startedAt: "2026-10-06T14:04:00Z", seedRow: null, task: null } });
  assert.equal(restoreSession(picking, IDS), picking);
  const shown = sampleState({ current: { startedAt: "2026-10-06T14:04:00Z", seedRow: 0, task: shownTask() } });
  assert.equal(restoreSession(shown, IDS), shown);
  const done = sampleState({ tasks: 5 });
  assert.equal(restoreSession(done, IDS), done);

  const bad = {
    nothing: null,
    "another version": { ...between, version: 2 },
    "short orders": { ...between, orders: ORDERS.slice(1) },
    "a ranker twice": { ...between, orders: ORDERS.map(() => ["hybrid", "hybrid"]) },
    "a bad id": { ...between, session: { ...between.session, id: "A".repeat(32) } },
    "no start time": { ...between, session: { ...between.session, startedAt: 1 } },
    "tasks not a list": { ...between, session: { ...between.session, tasks: {} } },
    "six tasks": sampleState({ tasks: 6 }),
    "an unfinished task": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], preferred: null }] } },
    "no completion time": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], completedAt: null }] } },
    "an unknown seed": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], seedTrackId: 99 }] } },
    "a bad preference": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], preferred: "C" }] } },
    "one list": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], lists: between.session.tasks[0].lists.slice(1) }] } },
    "a moved track": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], lists: between.session.tasks[0].lists.map((l) => ({ ...l, results: [{ ...l.results[0], row: 3 }] })) }] } },
    "a missing result": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], lists: between.session.tasks[0].lists.map((l) => ({ ...l, results: [null] })) }] } },
    "a result without ids": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], lists: between.session.tasks[0].lists.map((l) => ({ ...l, results: [{ rating: 1, newToMe: false }] })) }] } },
    "a row past the catalog": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], lists: between.session.tasks[0].lists.map((l) => ({ ...l, results: [{ ...l.results[0], row: 4, trackId: undefined }] })) }] } },
    "no rated flag": { ...between, rated: undefined },
    "a bad new-to-me": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], lists: between.session.tasks[0].lists.map((l) => ({ ...l, results: [{ ...l.results[0], newToMe: "yes" }] })) }] } },
    "a current task after the last": { ...done, current: picking.current },
    "no current start": { ...picking, current: { ...picking.current, startedAt: undefined } },
    "a finished current task": { ...shown, current: { ...shown.current, task: { ...shownTask(), completedAt: "2026-10-06T14:09:00Z" } } },
    "another seed row": { ...shown, current: { ...shown.current, seedRow: 1 } },
    // Each of these resumed and then exported a file the schema rejects (#144 review).
    "a like that is not a quality": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], likes: [5] }] } },
    "a free-text dislike": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], dislikes: ["anything I type"] }] } },
    "a task start that is not a time": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], startedAt: "yesterday" }] } },
    "an empty session start": { ...between, session: { ...between.session, startedAt: "" } },
    "a finished task with no results": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], lists: between.session.tasks[0].lists.map((l) => ({ ...l, results: [] })) }] } },
    "a completion that is not a time": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], completedAt: "2026-10-06 14:03" }] } },
    "too many likes": { ...between, session: { ...between.session, tasks: [{ ...between.session.tasks[0], likes: Array.from({ length: 61 }, (_, i) => `tag:t${i}`) }] } },
    "a free-text like on the task shown": { ...shown, current: { ...shown.current, task: { ...shownTask(), likes: ["genre:\nFolk"] } } },
    "a current start that is not a time": { ...picking, current: { ...picking.current, startedAt: "now" } },
    // A pattern test turns a list into text, so these got through too (#144 re-review).
    "an id in a list": { ...between, session: { ...between.session, id: ["a".repeat(32)] } },
    "an id in a nested list": { ...between, session: { ...between.session, id: [["a".repeat(32)]] } },
    "a seed row as text": { ...shown, current: { ...shown.current, seedRow: "0" } },
    "a seed row in a list": { ...shown, current: { ...shown.current, seedRow: [0] } },
  };
  const offered = Object.entries(bad).filter(([, state]) => restoreSession(state, IDS) !== null);
  assert.deepEqual(offered.map(([name]) => name), []);
});

test("a restored session is checked with the schema's own patterns", () => {
  assert.equal(lib.TIMESTAMP_PATTERN?.source, new RegExp(schema.$defs.timestamp.pattern, "u").source);
  assert.equal(lib.QUALITY_PATTERN?.source, new RegExp(schema.$defs.quality.pattern, "u").source);
  for (const kind of ["likes", "dislikes"]) assert.equal(schema.$defs.task.properties[kind].maxItems, 60, kind);
  assert.equal(schema.$defs.list.properties.results.minItems, 1);
});
