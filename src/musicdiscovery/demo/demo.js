// Demo page: pick a seed track, choose liked and disliked qualities, read explained
// recommendations from the hybrid or baseline ranker, and rate them. Everything runs
// in the browser from catalog.json; ratings never leave the page unless exported.

import { Rankers } from "./ranker.js";

const MAX_SUGGESTIONS = 8;
const RANKER_LABELS = {
  hybrid: "Hybrid",
  baseline: "Baseline",
};
const RANKER_HINTS = {
  hybrid: "Sound, genres and tags, popularity, and a nudge towards variety.",
  baseline: "Sound similarity to the seed plus the qualities you pick.",
};
const NEXT_STATE = { neutral: "like", like: "dislike", dislike: "neutral" };
const STATE_WORDS = { neutral: "not selected", like: "liked", dislike: "disliked" };
const STATE_MARKS = { neutral: "", like: "+", dislike: "−" };
const SVG_NS = "http://www.w3.org/2000/svg";
const THUMB_PATH =
  "M2 21h4V9H2v12zm20-11a2 2 0 0 0-2-2h-6.3l1-4.6v-.3a1.5 1.5 0 0 0-.44-1.06L13.2 1 6.6 7.6A2 2 0 0 0 6 9v10a2 2 0 0 0 2 2h9a2 2 0 0 0 1.84-1.22l3-7A2 2 0 0 0 22 12v-2z";

let uid = 0;

/** A uniformly random row; crypto keeps the choice free of Math.random. */
function randomRow(count) {
  const limit = 2 ** 32 - (2 ** 32 % count);
  const draw = new Uint32Array(1);
  do crypto.getRandomValues(draw);
  while (draw[0] >= limit);
  return draw[0] % count;
}

function capitalise(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === false || value === null || value === undefined) continue;
    if (name === "class") el.className = value;
    else if (name === "text") el.textContent = value;
    else el.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) el.append(child);
  }
  return el;
}

function thumbIcon(down) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", down ? "md-icon md-icon-down" : "md-icon");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", THUMB_PATH);
  svg.append(path);
  return svg;
}

class Demo {
  constructor(root, data) {
    this.root = root;
    this.data = data;
    this.rankers = new Rankers(data);
    this.ranker = "hybrid";
    this.seedRow = -1;
    this.qualities = new Map(); // "genre:Rock" -> {kind, name, direction, state}
    this.ratings = new Map(); // context key -> rating record
    this.suggestions = [];
    this.painters = new Map();
    this.active = -1;
    this.labels = data.ids.map((_, row) => this.label(row));
    this.searchable = this.labels.map((text) => text.toLowerCase());
    this.build();
    this.render();
  }

  label(row) {
    const { titles, artists, artist, ids } = this.data;
    return `${titles[row] || `Track ${ids[row]}`} — ${artists[artist[row]] || "Unknown artist"}`;
  }

  // --- layout ---------------------------------------------------------------

  build() {
    const n = ++uid;
    this.id = (name) => `md-${name}-${n}`;
    this.root.classList.add("md-demo");
    this.root.replaceChildren(
      h("p", { class: "md-lead", text: "Start from a track you like, say what you want more or less of, and see why each track was picked." }),
      this.buildSeed(),
      this.buildQualities(),
      this.buildRankerToggle(),
      this.buildResults(),
      this.buildNote(),
      h("p", { class: "md-attribution", text: this.data.attribution }),
    );
  }

  buildSeed() {
    this.input = h("input", {
      id: this.id("seed"),
      class: "md-input",
      type: "text",
      role: "combobox",
      autocomplete: "off",
      spellcheck: "false",
      "aria-autocomplete": "list",
      "aria-expanded": "false",
      "aria-controls": this.id("list"),
    });
    this.list = h("ul", { id: this.id("list"), class: "md-suggestions", role: "listbox", "aria-label": "Matching tracks", hidden: true });
    const random = h("button", { type: "button", class: "md-button", text: "Pick a random track" });
    random.addEventListener("click", () => this.pickSeed(randomRow(this.data.count)));
    this.input.addEventListener("input", () => this.suggest());
    this.input.addEventListener("keydown", (event) => this.onSeedKey(event));
    this.input.addEventListener("blur", () => this.closeList());
    this.input.addEventListener("focus", () => {
      if (this.input.value && this.seedRow < 0) this.suggest();
    });
    return h(
      "section",
      { class: "md-section", "aria-labelledby": this.id("seed-h") },
      h("h2", { id: this.id("seed-h"), text: "1. Seed track" }),
      h("label", { for: this.id("seed"), class: "md-label", text: "Search by title or artist" }),
      h("div", { class: "md-combo" }, this.input, this.list),
      h("div", { class: "md-row" }, random),
    );
  }

  buildQualities() {
    const group = (title, items) =>
      h("div", { class: "md-chip-group", role: "group", "aria-label": title }, h("h3", { class: "md-subhead", text: title }), h("div", { class: "md-chips" }, items));
    const axisChips = this.data.axes.flatMap((axis) => [
      this.chip({ kind: "axis", name: axis.name, direction: 1 }, capitalise(axis.up)),
      this.chip({ kind: "axis", name: axis.name, direction: -1 }, capitalise(axis.down)),
    ]);
    return h(
      "section",
      { class: "md-section", "aria-labelledby": this.id("q-h") },
      h("h2", { id: this.id("q-h"), text: "2. Qualities (optional)" }),
      h("p", { class: "md-help", text: "Select a quality once to like it, twice to dislike it, a third time to clear it." }),
      group("Sound", axisChips),
      group("Genres", this.data.chips.genres.map((g) => this.chip({ kind: "genre", name: g, direction: 1 }, g))),
      group("Popular tags", this.data.chips.tags.map((t) => this.chip({ kind: "tag", name: t, direction: 1 }, t))),
    );
  }

  chip(quality, text) {
    const key = quality.kind === "axis" ? `axis:${quality.name}:${quality.direction}` : `${quality.kind}:${quality.name}`;
    const mark = h("span", { class: "md-chip-mark", "aria-hidden": "true" });
    const state = h("span", { class: "md-sr" });
    const button = h("button", { type: "button", class: "md-chip", "data-state": "neutral" }, mark, h("span", { text }), state);
    const entry = { ...quality, state: "neutral" };
    const paint = () => {
      button.dataset.state = entry.state;
      mark.textContent = STATE_MARKS[entry.state];
      state.textContent = `, ${STATE_WORDS[entry.state]}`;
    };
    paint();
    button.addEventListener("click", () => {
      entry.state = NEXT_STATE[entry.state];
      if (entry.state === "neutral") this.qualities.delete(key);
      else this.qualities.set(key, entry);
      paint();
      this.render();
    });
    return button;
  }

  buildRankerToggle() {
    const radios = Object.keys(RANKER_LABELS).map((name) => {
      const input = h("input", { type: "radio", name: this.id("ranker"), id: this.id(`r-${name}`), value: name, checked: name === this.ranker });
      input.addEventListener("change", () => {
        this.ranker = name;
        this.render();
      });
      return h(
        "div",
        { class: "md-radio" },
        input,
        h("label", { for: this.id(`r-${name}`) }, h("span", { class: "md-radio-name", text: RANKER_LABELS[name] }), h("span", { class: "md-radio-hint", text: RANKER_HINTS[name] })),
      );
    });
    return h("fieldset", { class: "md-section md-fieldset" }, h("legend", { text: "3. Ranker" }), radios);
  }

  buildResults() {
    this.status = h("p", { class: "md-status", role: "status" });
    this.results = h("ol", { class: "md-results", "aria-labelledby": this.id("res-h") });
    this.exportButton = h("button", { type: "button", class: "md-button", text: "Export my ratings", disabled: true });
    this.exportButton.addEventListener("click", () => this.exportRatings());
    return h(
      "section",
      { class: "md-section", "aria-labelledby": this.id("res-h") },
      h("h2", { id: this.id("res-h"), text: "Recommendations" }),
      this.status,
      this.results,
      h("div", { class: "md-row" }, this.exportButton, h("span", { class: "md-help", text: "Ratings stay in this page. Export downloads them as a JSON file." })),
    );
  }

  buildNote() {
    return h(
      "section",
      { class: "md-section md-note", "aria-labelledby": this.id("note-h") },
      h("h2", { id: this.id("note-h"), text: "What this demo can and cannot show" }),
      h("ul", {}, [
        h("li", { text: "It can show how two rankers use the same seed and qualities, and why each track was picked." }),
        h("li", { text: "It cannot play audio: tracks link to their Free Music Archive pages, so you judge from the page, not the sound." }),
        h("li", { text: "It cannot show which ranker listeners prefer. That needs the offline metrics and the blind listener study, not this page." }),
        h("li", { text: "The catalog is a small fixed set of openly licensed tracks, so many good songs are simply missing." }),
        h("li", { text: "Nothing is sent anywhere: no server, accounts or analytics." }),
      ]),
    );
  }

  // --- seed typeahead -------------------------------------------------------

  suggest() {
    this.seedRow = -1;
    const query = this.input.value.trim().toLowerCase();
    this.suggestions = [];
    if (query) {
      const starts = [];
      const contains = [];
      for (let row = 0; row < this.searchable.length; row += 1) {
        const at = this.searchable[row].indexOf(query);
        if (at === 0) starts.push(row);
        else if (at > 0) contains.push(row);
        if (starts.length >= MAX_SUGGESTIONS) break;
      }
      this.suggestions = [...starts, ...contains].slice(0, MAX_SUGGESTIONS);
    }
    this.active = this.suggestions.length ? 0 : -1;
    this.list.replaceChildren(
      ...this.suggestions.map((row, i) => {
        const option = h("li", { id: this.id(`o${i}`), role: "option", class: "md-option", text: this.labels[row], "aria-selected": String(i === this.active) });
        option.addEventListener("mousedown", (event) => {
          event.preventDefault();
          this.pickSeed(row);
        });
        return option;
      }),
    );
    const open = this.suggestions.length > 0;
    this.list.hidden = !open;
    this.input.setAttribute("aria-expanded", String(open));
    this.syncActive();
    this.render();
  }

  syncActive() {
    [...this.list.children].forEach((option, i) => option.setAttribute("aria-selected", String(i === this.active)));
    if (this.active >= 0) this.input.setAttribute("aria-activedescendant", this.id(`o${this.active}`));
    else this.input.removeAttribute("aria-activedescendant");
  }

  closeList() {
    this.list.hidden = true;
    this.input.setAttribute("aria-expanded", "false");
    this.input.removeAttribute("aria-activedescendant");
  }

  onSeedKey(event) {
    const count = this.suggestions.length;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!count) return;
      event.preventDefault();
      if (this.list.hidden) {
        this.list.hidden = false;
        this.input.setAttribute("aria-expanded", "true");
      }
      this.active = (this.active + (event.key === "ArrowDown" ? 1 : -1) + count) % count;
      this.syncActive();
    } else if (event.key === "Enter" && !this.list.hidden && this.active >= 0) {
      event.preventDefault();
      this.pickSeed(this.suggestions[this.active]);
    } else if (event.key === "Escape") {
      this.closeList();
    }
  }

  pickSeed(row) {
    this.seedRow = row;
    this.input.value = this.labels[row];
    this.suggestions = [];
    this.closeList();
    this.render();
  }

  // --- results --------------------------------------------------------------

  preference() {
    const likes = [];
    const dislikes = [];
    for (const q of this.qualities.values()) {
      const dir = q.kind === "axis" ? (q.direction > 0 ? "more" : "less") : q.kind;
      const text = q.kind === "axis" ? `${dir}:${q.name}` : `${q.kind}:${q.name}`;
      (q.state === "like" ? likes : dislikes).push(text);
    }
    return { seeds: [this.data.ids[this.seedRow]], likes, dislikes };
  }

  render() {
    if (this.seedRow < 0) {
      this.results.replaceChildren();
      this.status.textContent = "Pick a seed track to see recommendations.";
      return;
    }
    const preference = this.preference();
    const picks = this.rankers.rank(this.ranker, preference, 10);
    const context = JSON.stringify([this.ranker, preference]);
    this.status.textContent = `${picks.length} recommendations from the ${RANKER_LABELS[this.ranker].toLowerCase()} ranker for ${this.labels[this.seedRow]}.`;
    this.painters = new Map();
    this.results.replaceChildren(...picks.map((pick, i) => this.resultItem(pick, i + 1, preference, context)));
  }

  resultItem(pick, rank, preference, context) {
    const { data } = this;
    const row = pick.row;
    const title = data.titles[row] || `Track ${pick.id}`;
    const genre = data.genre[row] >= 0 ? data.genres[data.genre[row]] : "";
    const links = [];
    if (data.sources[row]) {
      links.push(h("a", { href: data.sources[row], target: "_blank", rel: "noopener noreferrer", text: "Source page (FMA)" }));
    }
    if (data.licenses[data.license[row]]) {
      links.push(h("a", { href: data.licenses[data.license[row]], target: "_blank", rel: "noopener noreferrer", text: "License" }));
    }
    const key = `${context}|${pick.id}`;
    const rate = (value, label) => {
      const button = h("button", { type: "button", class: "md-rate" }, thumbIcon(value < 0), h("span", { text: label }), h("span", { class: "md-sr", text: ` for ${title}` }));
      button.addEventListener("click", () => this.rate(key, value, pick, preference));
      return button;
    };
    const up = rate(1, "Thumbs up");
    const down = rate(-1, "Thumbs down");
    const paint = () => {
      const current = this.ratings.get(key)?.rating;
      up.setAttribute("aria-pressed", String(current === 1));
      down.setAttribute("aria-pressed", String(current === -1));
    };
    paint();
    this.painters.set(key, paint);
    return h(
      "li",
      { class: "md-result" },
      h("div", { class: "md-result-head" }, h("span", { class: "md-rank", "aria-hidden": "true", text: String(rank) }), h("div", {}, h("p", { class: "md-title", text: title }), h("p", { class: "md-meta", text: [data.artists[data.artist[row]], genre].filter(Boolean).join(" · ") }))),
      h("ul", { class: "md-why", "aria-label": "Why this track" }, pick.explanations.map((text) => h("li", { text }))),
      h("div", { class: "md-result-foot" }, h("span", { class: "md-links" }, links), h("span", { class: "md-rates", role: "group", "aria-label": `Rate ${title}` }, up, down)),
    );
  }

  rate(key, value, pick, preference) {
    if (this.ratings.get(key)?.rating === value) this.ratings.delete(key);
    else {
      this.ratings.set(key, {
        track_id: pick.id,
        rating: value,
        ranker: this.ranker,
        seed_track_id: preference.seeds[0],
        likes: preference.likes,
        dislikes: preference.dislikes,
      });
    }
    this.painters.get(key)?.();
    this.exportButton.disabled = this.ratings.size === 0;
    const count = this.ratings.size;
    this.status.textContent = `${count} rating${count === 1 ? "" : "s"} kept in this page.`;
  }

  exportRatings() {
    const body = { format: "music-discovery-ratings", version: 1, ratings: [...this.ratings.values()] };
    const url = URL.createObjectURL(new Blob([`${JSON.stringify(body, null, 2)}\n`], { type: "application/json" }));
    const link = h("a", { href: url, download: "music-discovery-ratings.json" });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    this.status.textContent = `Exported ${body.ratings.length} rating${body.ratings.length === 1 ? "" : "s"}.`;
  }
}

async function mount(root) {
  const url = new URL(root.dataset.catalog ?? "catalog.json", import.meta.url);
  root.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const demo = new Demo(root, await response.json());
    root.demo = demo;
  } catch (error) {
    root.replaceChildren(h("p", { class: "md-error", role: "alert", text: `The demo could not load its catalog (${error.message}).` }));
  } finally {
    root.removeAttribute("aria-busy");
  }
}

function start() {
  document.querySelectorAll("[data-music-discovery]").forEach(mount);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
else start();
