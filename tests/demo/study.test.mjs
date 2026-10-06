// Node checks for the study's DOM-free logic: balanced list order, session id, export shape.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import { balancedOrders, buildExport, newSessionId, taskComplete, timestamp } from "../../src/musicdiscovery/demo/study-lib.js";

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
