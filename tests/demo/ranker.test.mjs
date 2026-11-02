// The browser rankers' edge cases, on small hand-built catalogs whose every score is known.
// The Hypothesis parity law in tests/test_ranker_contract.py compares these against Python;
// this file states the expected answers themselves and needs no Python or built bundle.
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { Rankers } from "../../src/musicdiscovery/demo/ranker.js";

const SEED = 1;
const SIMILAR = "similar sound to the seed";

const encode = (typed) => btoa(String.fromCharCode(...new Uint8Array(typed.buffer)));

/** A catalog whose rows sit at `points` in a two-dimensional feature space. */
function catalog(points) {
  const n = points.length;
  return {
    count: n,
    ids: points.map((_, row) => row + 1),
    artist: points.map(() => 0),
    genre: points.map(() => 0),
    genres: ["Folk", "Rock"],
    axes: [{ name: "energy", up: "more energetic", down: "calmer" }],
    baseline: {
      dims: 2,
      projected: encode(Int16Array.from(points.flat())),
      projectedScale: 1,
      axisData: encode(new Int16Array(n)),
      axisScale: 1,
      similarityWeight: 1,
      axisWeight: 0.5,
      genreWeight: 0.5,
      tagWeight: 0.5,
      maxExplanations: 3,
    },
    hybrid: {
      counts: encode(new Uint16Array(n)),
      columns: encode(new Uint16Array(0)),
      values: encode(new Uint16Array(0)),
      valueScale: 1,
      popularity: encode(new Uint16Array(n)),
      popularityScale: 1,
      tokens: [],
      metadataWeight: 0.5,
      popularityWeight: 0.1,
      poolSize: 10,
      mmrLambda: 0.2,
      genreRedundancy: 0.5,
      varietyText: "adds variety",
    },
  };
}

const rank = (name, points, preference = {}) =>
  new Rankers(catalog(points)).rank(name, { seeds: [SEED], ...preference });

describe("baseline without signal", () => {
  test("returns nothing when no track has features", () => {
    assert.deepEqual(rank("baseline", [[0, 0], [0, 0], [0, 0]]), []);
  });

  test("returns nothing when only the seed sits at the catalog mean", () => {
    assert.deepEqual(rank("baseline", [[0, 0], [1, 0], [0, 1]]), []);
  });

  test("empty likes and dislikes ask for nothing", () => {
    assert.deepEqual(rank("baseline", [[0, 0], [1, 0]], { likes: [], dislikes: [] }), []);
  });

  test("a quality asked for is signal, so the tracks are ranked", () => {
    const results = rank("baseline", [[0, 0], [0, 0], [0, 0]], { dislikes: ["genre:Rock"] });
    assert.deepEqual(results.map((r) => r.id), [2, 3]);
  });

  test("a seed with features is signal, so the tracks are ranked", () => {
    assert.deepEqual(rank("baseline", [[1, 0], [1, 0], [0, 1]]).map((r) => r.id), [2, 3]);
  });
});

describe("explanations", () => {
  // Every other track points away from the seed, so its similarity term is negative.
  const opposed = [[1, 0], [-1, 0], [-2, 0]];

  for (const name of ["baseline", "hybrid"]) {
    test(`${name}: all-negative contributions give an empty explanation`, () => {
      const results = rank(name, opposed);
      assert.equal(results.length, 2);
      for (const result of results) {
        assert.ok(result.score < 0);
        assert.deepEqual(result.explanations, []);
        assert.deepEqual(result.explained, []);
      }
    });

    test(`${name}: contributions of exactly zero explain nothing`, () => {
      const results = rank(name, [[1, 0], [0, 1]]);
      assert.equal(results.length, 1);
      assert.ok(results[0].terms.every((term) => term.value === 0));
      assert.deepEqual(results[0].explanations, []);
      assert.deepEqual(results[0].explained, []);
    });
  }

  test("a disliked quality on top of a negative similarity is not explained either", () => {
    const [result] = rank("baseline", opposed, { dislikes: ["genre:Folk"] });
    assert.deepEqual(result.terms.map((term) => Math.sign(term.value)), [-1, -1]);
    assert.deepEqual(result.explanations, []);
  });

  test("only the terms that raised the score explain it", () => {
    const [result] = rank("baseline", opposed, { likes: ["genre:Folk"], dislikes: ["genre:Rock"] });
    assert.ok(result.terms.some((term) => term.value < 0));
    assert.deepEqual(result.explanations, ["same genre: Folk"]);
    assert.deepEqual(result.explained, ["genre:Folk"]);
  });

  test("a positive similarity still explains itself", () => {
    const [result] = rank("baseline", [[1, 0], [1, 0], [-1, 0]]);
    assert.deepEqual(result.explanations, [SIMILAR]);
  });
});
