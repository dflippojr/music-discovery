// Ranks with ranker.js for tests/test_ranker_contract.py, which checks it against
// Python on generated catalogs. Each stdin line is {catalog, queries}, where a
// query is {name, preference, k}; each stdout line is the matching list of
// rankings ({id, score, explanations} per pick) or {error} per query.
import { createInterface } from "node:readline";
import { Rankers } from "../../src/musicdiscovery/demo/ranker.js";

for await (const line of createInterface({ input: process.stdin })) {
  const { catalog, queries } = JSON.parse(line);
  const rankers = new Rankers(catalog);
  const replies = queries.map(({ name, preference, k }) => {
    try {
      return rankers.rank(name, preference, k).map(({ id, score, explanations }) => ({ id, score, explanations }));
    } catch (error) {
      return { error: String(error) };
    }
  });
  process.stdout.write(`${JSON.stringify(replies)}\n`);
}
