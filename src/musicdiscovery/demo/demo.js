// Demo page: pick a seed track, choose liked and disliked qualities, read explained
// recommendations from the hybrid or baseline ranker, and rate them. Everything runs
// in the browser from catalog.json; ratings never leave the page unless exported.

import { Rankers } from "./ranker.js";
import { buildPreference, DeferredSources, h, makeIds, QualityPicker, SeedPicker, thumbButton } from "./ui.js";

const RANKER_LABELS = {
  hybrid: "Hybrid",
  baseline: "Baseline",
};
const RANKER_HINTS = {
  hybrid: "Sound, genres and tags, popularity, and a nudge towards variety.",
  baseline: "Sound similarity to the seed plus the qualities you pick.",
};

class Demo {
  constructor(root, data) {
    this.root = root;
    this.data = data;
    this.sources = new DeferredSources(data);
    this.rankers = new Rankers(data);
    this.ranker = "hybrid";
    this.ratings = new Map(); // context key -> rating record
    this.painters = new Map();
    this.seed = new SeedPicker(data, { heading: "1. Seed track", onChange: () => this.render() });
    this.picker = new QualityPicker(data, { heading: "2. Qualities (optional)", onChange: () => this.render() });
    this.build();
    this.render();
  }

  // --- layout ---------------------------------------------------------------

  build() {
    this.id = makeIds();
    this.excludeSeedArtist = h("input", { type: "checkbox", id: this.id("exclude-seed-artist") });
    this.excludeSeedArtist.addEventListener("change", () => this.render());
    this.picker.element.append(h("div", { class: "md-row" }, this.excludeSeedArtist, h("label", { for: this.id("exclude-seed-artist"), text: "Exclude the seed artist" })));
    this.root.classList.add("md-demo");
    this.root.replaceChildren(
      h("p", { class: "md-lead", text: "Start from a track you like, say what you want more or less of, and see why each track was picked." }),
      this.seed.element,
      this.picker.element,
      this.buildRankerToggle(),
      this.buildResults(),
      this.buildNote(),
      h("p", { class: "md-attribution", text: this.data.attribution }),
    );
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

  // --- results --------------------------------------------------------------

  render() {
    if (this.seed.row < 0) {
      this.results.replaceChildren();
      this.status.textContent = "Pick a seed track to see recommendations.";
      return;
    }
    const preference = buildPreference(this.data, this.seed.row, this.picker.qualities);
    preference.excludeSeedArtist = this.excludeSeedArtist.checked;
    const picks = this.rankers.rank(this.ranker, preference, 10);
    const context = JSON.stringify([this.ranker, preference]);
    this.status.textContent = picks.length
      ? `${picks.length} recommendations from the ${RANKER_LABELS[this.ranker].toLowerCase()} ranker for ${this.seed.labels[this.seed.row]}.`
      : "No recommendations available with these preferences. Try unchecking Exclude the seed artist or picking another seed track.";
    this.painters = new Map();
    this.sources.slots = [];
    this.results.replaceChildren(...picks.map((pick, i) => this.resultItem(pick, i + 1, preference, context)));
  }

  resultItem(pick, rank, preference, context) {
    const { data } = this;
    const row = pick.row;
    const title = data.titles[row] || `Track ${pick.id}`;
    const genre = data.genre[row] >= 0 ? data.genres[data.genre[row]] : "";
    const links = [];
    if (data.licenses[data.license[row]]) {
      links.push(h("a", { href: data.licenses[data.license[row]], target: "_blank", rel: "noopener noreferrer", text: "License" }));
    }
    const linkBox = h("span", { class: "md-links" }, links);
    this.sources.fill(linkBox, row);
    const key = `${context}|${pick.id}`;
    const onRate = (value) => this.rate(key, value, pick, preference);
    const up = thumbButton(1, "Thumbs up", title, onRate);
    const down = thumbButton(-1, "Thumbs down", title, onRate);
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
      h("div", { class: "md-result-foot" }, linkBox, h("span", { class: "md-rates", role: "group", "aria-label": `Rate ${title}` }, up, down)),
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
        exclude_seed_artist: preference.excludeSeedArtist,
      });
    }
    this.painters.get(key)?.();
    this.exportButton.disabled = this.ratings.size === 0;
    const count = this.ratings.size;
    this.status.textContent = `${count} rating${count === 1 ? "" : "s"} kept in this page.`;
  }

  exportRatings() {
    const body = { format: "music-discovery-ratings", version: 2, ratings: [...this.ratings.values()] };
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
    demo.sources.load(new URL(root.dataset.sources ?? "sources.json", url));
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
