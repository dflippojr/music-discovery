// Browser rankers must give the same top 10 as Python on the fixed parity queries.
// Run `python scripts/build_demo_fixture.py <dir>` first; DEMO_BUNDLE points at <dir>.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

const bundle = process.env.DEMO_BUNDLE ?? ".demo-fixture";
const source = join("src", "musicdiscovery", "demo", "ranker.js");
const { Rankers } = await import(pathToFileURL(resolve(source)).href);
const read = (name) => JSON.parse(readFileSync(join(bundle, name), "utf8"));
const rankers = new Rankers(read("catalog.json"));
const parity = read("parity.json");

test("the bundle ships the tested ranker", () => {
  assert.equal(readFileSync(join(bundle, "ranker.js"), "utf8"), readFileSync(source, "utf8"));
});

test("there are parity queries with likes and dislikes", () => {
  assert.ok(parity.queries.length >= 20);
  assert.ok(parity.queries.some((q) => q.dislikes.length));
});

for (const name of ["baseline", "hybrid"]) {
  test(`${name} top 10 matches Python`, () => {
    for (const query of parity.queries) {
      const preference = { seeds: [query.seed], likes: query.likes, dislikes: query.dislikes };
      const got = rankers.rank(name, preference, 10).map((r) => r.id);
      assert.deepEqual(got, query.expected[name], JSON.stringify(query));
    }
  });
}

test("results explain themselves", () => {
  const query = parity.queries[0];
  for (const name of ["baseline", "hybrid"]) {
    for (const r of rankers.rank(name, { seeds: [query.seed], likes: query.likes }, 10)) {
      assert.ok(r.explanations.length >= 1 && r.explanations.length <= 3);
    }
  }
});

test("bad input is rejected", () => {
  assert.throws(() => rankers.rank("baseline", { seeds: [-1] }), /unknown track id/);
  assert.throws(() => rankers.rank("baseline", { seeds: [parity.queries[0].seed], likes: ["more:tempo"] }), /unknown axis/);
  assert.throws(() => rankers.rank("nope", { seeds: [1] }), /unknown ranker/);
});
