// JavaScript port of the baseline and hybrid rankers (src/musicdiscovery/ranking.py
// and hybrid.py). It reads the bundle written by `musicdiscovery export-demo`.
// A Node test checks that it returns the same top 10 as Python on fixed queries.

const Z_FALLBACK_AXES = ["brightness", "energy", "noisiness"];

function decode(blob, Type) {
  const text = atob(blob);
  const buffer = new ArrayBuffer(text.length);
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < text.length; i += 1) bytes[i] = text.charCodeAt(i);
  return new Type(buffer);
}

function scaled(ints, scale) {
  const out = new Float64Array(ints.length);
  for (let i = 0; i < ints.length; i += 1) out[i] = ints[i] * scale;
  return out;
}

/** Parse "genre:Folk", "tag:punk", "more:energy" or "less:energy". */
export function parseQuality(text, axisNames = Z_FALLBACK_AXES) {
  const split = text.indexOf(":");
  const head = text.slice(0, split).trim().toLowerCase();
  const name = text.slice(split + 1).trim();
  if (split < 0 || !name) throw new Error(`cannot read quality ${text}`);
  if (head === "genre" || head === "tag") return { kind: head, name, direction: 1 };
  if (head === "more" || head === "less") {
    const axis = name.toLowerCase();
    if (!axisNames.includes(axis)) throw new Error(`unknown axis ${name}`);
    return { kind: "axis", name: axis, direction: head === "more" ? 1 : -1 };
  }
  throw new Error(`unknown quality kind ${head}`);
}

/** Likes count +1, dislikes -1; an axis dislike becomes a flipped like. */
function signedQualities(likes, dislikes) {
  const signed = likes.map((q) => [q, 1]);
  for (const q of dislikes) {
    if (q.kind === "axis") signed.push([{ ...q, direction: -q.direction }, 1]);
    else signed.push([q, -1]);
  }
  return signed;
}

function sumTerms(terms, n) {
  const total = new Float64Array(n);
  for (const term of terms) {
    for (let i = 0; i < n; i += 1) total[i] += term.values[i];
  }
  return total;
}

/** Row order is track id order, so a lower row wins ties like a lower id. */
function byScoreThenRow(scores) {
  return (a, b) => scores[b] - scores[a] || a - b;
}

export class Rankers {
  constructor(data) {
    this.data = data;
    this.n = data.count;
    const base = data.baseline;
    this.axisNames = data.axes.map((a) => a.name);
    this.axes = Object.fromEntries(data.axes.map((a) => [a.name, a]));

    const projected = scaled(decode(base.projected, Int16Array), base.projectedScale);
    this.dims = base.dims;
    this.projected = projected;
    this.unit = new Float64Array(projected.length);
    for (let row = 0; row < this.n; row += 1) {
      let norm = 0;
      for (let d = 0; d < this.dims; d += 1) norm += projected[row * this.dims + d] ** 2;
      norm = Math.sqrt(norm) || 1;
      for (let d = 0; d < this.dims; d += 1) {
        this.unit[row * this.dims + d] = projected[row * this.dims + d] / norm;
      }
    }
    this.axisZ = scaled(decode(base.axisData, Int16Array), base.axisScale);

    const hybrid = data.hybrid;
    const counts = decode(hybrid.counts, Uint16Array);
    this.indptr = new Int32Array(this.n + 1);
    for (let row = 0; row < this.n; row += 1) {
      this.indptr[row + 1] = this.indptr[row] + counts[row];
    }
    this.columns = decode(hybrid.columns, Uint16Array);
    this.values = scaled(decode(hybrid.values, Uint16Array), hybrid.valueScale);
    this.popularity = scaled(decode(hybrid.popularity, Uint16Array), hybrid.popularityScale);
    this.tokenId = new Map(hybrid.tokens.map((token, i) => [token, i]));
    this.genreIndex = new Map(data.genres.map((g, i) => [g, i]));
  }

  hasTag(tag) {
    const id = this.tokenId.get(`t:${tag}`);
    const out = new Uint8Array(this.n);
    if (id === undefined) return out;
    for (let row = 0; row < this.n; row += 1) {
      for (let j = this.indptr[row]; j < this.indptr[row + 1]; j += 1) {
        if (this.columns[j] === id) out[row] = 1;
      }
    }
    return out;
  }

  hasGenre(name) {
    const index = this.genreIndex.get(name);
    const out = new Uint8Array(this.n);
    if (index === undefined) return out;
    for (let row = 0; row < this.n; row += 1) out[row] = this.data.genre[row] === index ? 1 : 0;
    return out;
  }

  /** Each score term per track, and which tracks may be shown. */
  scoreTerms(preference) {
    const { n, dims, data } = this;
    const base = data.baseline;
    const seedRows = preference.seeds.map((id) => {
      const row = data.ids.indexOf(id);
      if (row < 0) throw new Error(`unknown track id ${id}`);
      return row;
    });

    const centre = new Float64Array(dims);
    for (const row of seedRows) {
      for (let d = 0; d < dims; d += 1) centre[d] += this.projected[row * dims + d] / seedRows.length;
    }
    const norm = Math.sqrt(centre.reduce((sum, v) => sum + v * v, 0));
    const similarity = new Float64Array(n);
    if (norm) {
      for (let row = 0; row < n; row += 1) {
        let dot = 0;
        for (let d = 0; d < dims; d += 1) dot += this.unit[row * dims + d] * (centre[d] / norm);
        similarity[row] = base.similarityWeight * dot;
      }
    }
    const terms = [{ name: "similarity", text: "similar sound to the seed", values: similarity }];

    const likes = (preference.likes ?? []).map((q) => parseQuality(q, this.axisNames));
    const dislikes = (preference.dislikes ?? []).map((q) => parseQuality(q, this.axisNames));
    for (const [quality, sign] of signedQualities(likes, dislikes)) {
      terms.push(this.qualityTerm(quality, sign));
    }

    const allowed = new Uint8Array(n).fill(1);
    for (const row of seedRows) allowed[row] = 0;
    if (preference.excludeSeedArtist) {
      const seedArtists = new Set(seedRows.map((row) => data.artist[row]));
      for (let row = 0; row < n; row += 1) if (seedArtists.has(data.artist[row])) allowed[row] = 0;
    }
    return { terms, allowed };
  }

  qualityTerm(quality, sign) {
    const { n, data } = this;
    const base = data.baseline;
    const values = new Float64Array(n);
    if (quality.kind === "axis") {
      const axis = this.axes[quality.name];
      const column = this.axisNames.indexOf(quality.name);
      const up = quality.direction > 0;
      for (let row = 0; row < n; row += 1) {
        values[row] = base.axisWeight * quality.direction * this.axisZ[row * this.axisNames.length + column];
      }
      return {
        name: `axis:${quality.name}${up ? "+" : "-"}`,
        text: `${up ? axis.up : axis.down}, as asked`,
        values,
      };
    }
    const isGenre = quality.kind === "genre";
    const weight = isGenre ? base.genreWeight : base.tagWeight;
    const matches = isGenre ? this.hasGenre(quality.name) : this.hasTag(quality.name);
    for (let row = 0; row < n; row += 1) values[row] = sign * weight * matches[row];
    return {
      name: `${quality.kind}:${quality.name}`,
      text: isGenre ? `same genre: ${quality.name}` : `tagged ${quality.name}`,
      values,
    };
  }

  build(row, terms) {
    const parts = terms.map((t) => ({ name: t.name, text: t.text, value: t.values[row] }));
    const ordered = [...parts].sort((a, b) => b.value - a.value);
    let explained = ordered.filter((t) => t.value > 0).slice(0, this.data.baseline.maxExplanations);
    if (!explained.length) explained = ordered.slice(0, 1);
    return {
      row,
      id: this.data.ids[row],
      score: parts.reduce((sum, t) => sum + t.value, 0),
      terms: parts,
      explanations: explained.map((t) => t.text),
    };
  }

  rankBaseline(preference, k) {
    const { terms, allowed } = this.scoreTerms(preference);
    const scores = sumTerms(terms, this.n);
    const candidates = [];
    for (let row = 0; row < this.n; row += 1) if (allowed[row]) candidates.push(row);
    candidates.sort(byScoreThenRow(scores));
    return candidates.slice(0, k).map((row) => this.build(row, terms));
  }

  metadataSimilarity(seedRows) {
    const { n, columns, values, indptr } = this;
    const centre = new Float64Array(this.tokenId.size);
    for (const row of seedRows) {
      for (let j = indptr[row]; j < indptr[row + 1]; j += 1) centre[columns[j]] += values[j];
    }
    const norm = Math.sqrt(centre.reduce((sum, v) => sum + v * v, 0));
    const out = new Float64Array(n);
    if (!norm) return out;
    for (let row = 0; row < n; row += 1) {
      let total = 0;
      for (let j = indptr[row]; j < indptr[row + 1]; j += 1) total += values[j] * (centre[columns[j]] / norm);
      out[row] = total;
    }
    return out;
  }

  rankHybrid(preference, k) {
    const { n, data } = this;
    const config = data.hybrid;
    const { terms: baseTerms, allowed } = this.scoreTerms(preference);
    const seedRows = preference.seeds.map((id) => data.ids.indexOf(id));
    const terms = baseTerms.map((t) => (t.name === "similarity" ? { ...t, name: "audio" } : t));

    const metadata = this.metadataSimilarity(seedRows).map((v) => config.metadataWeight * v);
    terms.push({ name: "metadata", text: "similar genres and tags", values: metadata });
    const popularity = this.popularity.map((v) => config.popularityWeight * v);
    terms.push({ name: "popularity", text: "popular with listeners", values: popularity });
    const relevance = sumTerms(terms, n);

    const candidates = [];
    for (let row = 0; row < n; row += 1) if (allowed[row]) candidates.push(row);
    candidates.sort(byScoreThenRow(relevance));
    const pool = candidates.slice(0, Math.max(config.poolSize, k));
    const { picks, variety } = this.mmr(pool, relevance, k);

    return picks.map((row) => {
      const rowTerms = [...terms];
      if ((variety.get(row) ?? 0) > 0) {
        const values = new Float64Array(n);
        values[row] = variety.get(row);
        rowTerms.push({ name: "variety", text: config.varietyText, values });
      }
      return this.build(row, rowTerms);
    });
  }

  /** Greedy maximal marginal relevance over the pool, which is in relevance order. */
  mmr(pool, relevance, k) {
    const config = this.data.hybrid;
    const lam = config.mmrLambda;
    const scores = pool.map((row) => relevance[row]);
    const low = Math.min(...scores);
    const span = Math.max(...scores) - low;
    const rel = scores.map((s) => (span ? (s - low) / span : 0));
    const artistTaken = new Set();
    const genreCount = new Map();
    const picks = [];
    const variety = new Map();
    let remaining = pool.map((_, i) => i);

    const redundancy = (i) => {
      const row = pool[i];
      const sameArtist = artistTaken.has(this.data.artist[row]) ? 1 : 0;
      const genre = this.data.genre[row];
      const share = picks.length && genre >= 0 ? (genreCount.get(genre) ?? 0) / picks.length : 0;
      const g = config.genreRedundancy;
      return (1 - g) * sameArtist + g * share;
    };

    while (remaining.length && picks.length < k) {
      let best = remaining[0];
      let bestGain = -Infinity;
      for (const i of remaining) {
        const gain = (1 - lam) * rel[i] - lam * redundancy(i);
        if (gain > bestGain) {
          bestGain = gain;
          best = i;
        }
      }
      const top = remaining[0];
      if (best !== top) variety.set(pool[best], lam * (redundancy(top) - redundancy(best)));
      const row = pool[best];
      picks.push(row);
      remaining = remaining.filter((i) => i !== best);
      artistTaken.add(this.data.artist[row]);
      const genre = this.data.genre[row];
      if (genre >= 0) genreCount.set(genre, (genreCount.get(genre) ?? 0) + 1);
    }
    return { picks, variety };
  }

  /** Rank with "baseline" or "hybrid"; `preference` is {seeds, likes, dislikes}. */
  rank(name, preference, k = 10) {
    if (name === "baseline") return this.rankBaseline(preference, k);
    if (name === "hybrid") return this.rankHybrid(preference, k);
    throw new Error(`unknown ranker ${name}`);
  }
}
