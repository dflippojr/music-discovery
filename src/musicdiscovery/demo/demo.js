// Demo page: pick a seed track, choose liked and disliked qualities, read explained
// recommendations from the hybrid or baseline ranker, see how each was scored, and
// rate them. Everything runs in the browser from catalog.json; ratings never leave
// the page unless exported, and the feedback log stays in this browser's storage.

import { buildEvent, FeedbackLog, MAX_EVENTS } from "./feedback-log.js";
import { Rankers } from "./ranker.js";
import { buildPreference, DeferredSources, downloadJson, downloadText, h, makeIds, mountCatalog, QualityPicker, resultPresentation, SeedPicker, thumbButton } from "./ui.js";

const RANKER_LABELS = {
  hybrid: "Hybrid",
  baseline: "Baseline",
};
const RANKER_HINTS = {
  hybrid: "Sound, genres and tags, popularity, and a nudge towards variety.",
  baseline: "Sound similarity to the seed plus the qualities you pick.",
};
const TERM_LABELS = {
  similarity: "Sounds like the seed",
  audio: "Sounds like the seed",
  metadata: "Shares genres and tags with the seed",
  popularity: "Popular with listeners",
  variety: "Adds variety to the list",
};
const QUALITY_LABELS = { genre: "Genre you picked", tag: "Tag you picked", axis: "Sound you picked" };
const LOG_FILE = "music-discovery-feedback.jsonl";
const LINK_KEYS = ["seed", "like", "dislike", "ranker", "exclude"];
const IGNORED_LINK = "Some settings in this link are not available here and were ignored.";

/** Plain language for a score term name such as "audio", "genre:Folk" or "axis:energy+". */
function termLabel(data, name) {
  if (TERM_LABELS[name]) return TERM_LABELS[name];
  const split = name.indexOf(":");
  const kind = name.slice(0, split);
  let value = name.slice(split + 1);
  if (kind === "axis") {
    const axis = data.axes.find((a) => a.name === value.slice(0, -1));
    value = (value.endsWith("+") ? axis?.up : axis?.down) ?? value;
  }
  return QUALITY_LABELS[kind] ? `${QUALITY_LABELS[kind]}: ${value}` : name;
}

/** A score to two decimals with an explicit sign; a true minus sign reads better aloud. */
function signed(value) {
  const text = Math.abs(value).toFixed(2);
  if (text === "0.00") return text;
  return `${value > 0 ? "+" : "\u2212"}${text}`;
}

function entries(count) {
  return `${count.toLocaleString("en")} entr${count === 1 ? "y" : "ies"}`;
}

class Demo {
  constructor(root, data) {
    this.root = root;
    this.data = data;
    this.sources = new DeferredSources(data, root);
    this.rankers = new Rankers(data);
    this.ranker = "hybrid";
    this.ratings = new Map(); // context key -> rating record
    this.painters = new Map();
    this.log = new FeedbackLog(() => globalThis.localStorage);
    this.seed = new SeedPicker(data, { heading: "1. Seed track", onChange: () => this.render() });
    this.picker = new QualityPicker(data, { heading: "2. Qualities (optional)", onChange: () => this.render() });
    // Links to results live in the URL fragment, which only a page's single demo can own.
    this.linkable = document.querySelectorAll("[data-music-discovery]").length === 1;
    this.linked = false; // whether the fragment currently holds this demo's state
    this.build();
    const ignored = this.linkable && this.restoreLink();
    this.render();
    if (ignored) this.status.textContent = `${this.status.textContent} ${IGNORED_LINK}`;
  }

  // --- links to results -----------------------------------------------------

  /**
   * Apply the settings a `#seed=…&like=…` fragment names that this page can show, without
   * rendering. Returns true if any of them had to be ignored. Fragments without these keys,
   * such as an ordinary in-page anchor, are left alone.
   */
  restoreLink() {
    const params = new URLSearchParams(location.hash.slice(1));
    if (!LINK_KEYS.some((key) => params.has(key))) return false;
    let ignored = false;
    const seeds = params.getAll("seed");
    const row = seeds.length === 1 ? this.data.ids.findIndex((id) => String(id) === seeds[0]) : -1;
    if (row >= 0) {
      this.seed.select(row);
      this.linked = true;
    } else ignored = true;
    const named = new Set();
    for (const [key, state] of [["like", "like"], ["dislike", "dislike"]]) {
      for (const quality of params.getAll(key)) {
        // A quality named twice keeps its first state, so the list matches the earlier mention.
        if (named.has(quality) || !this.picker.setQuality(quality, state)) ignored = true;
        named.add(quality);
      }
    }
    const ranker = params.get("ranker");
    if (ranker !== null) {
      if (Object.hasOwn(RANKER_LABELS, ranker)) {
        this.ranker = ranker;
        this.rankerInputs[ranker].checked = true;
      } else ignored = true;
    }
    const exclude = params.get("exclude");
    if (exclude === "1" || exclude === "0") this.excludeSeedArtist.checked = exclude === "1";
    else if (exclude !== null) ignored = true;
    return ignored;
  }

  /** Replace the fragment with the state behind `preference`, or clear it when there is none. */
  writeLink(preference) {
    if (!this.linkable || (!preference && !this.linked)) return;
    let hash = "";
    if (preference) {
      const params = new URLSearchParams({ seed: String(preference.seeds[0]) });
      for (const like of preference.likes) params.append("like", like);
      for (const dislike of preference.dislikes) params.append("dislike", dislike);
      params.append("ranker", this.ranker);
      if (preference.excludeSeedArtist) params.append("exclude", "1");
      hash = `#${params}`;
    }
    this.linked = Boolean(preference);
    // replaceState, not pushState: Back still leaves the page rather than stepping through settings.
    history.replaceState(history.state, "", `${location.pathname}${location.search}${hash}`);
  }

  async copyLink() {
    const href = location.href;
    this.linkField.value = href;
    try {
      await navigator.clipboard.writeText(href);
      this.linkBox.hidden = true;
      this.status.textContent = "Link copied.";
    } catch {
      // No clipboard (insecure context, refused permission, older browser): let the visitor copy it.
      this.linkBox.hidden = false;
      this.linkField.focus();
      this.linkField.select();
      this.status.textContent = "The link could not be copied automatically. It is selected in the field below.";
    }
  }

  // --- layout ---------------------------------------------------------------

  build() {
    this.id = makeIds();
    this.excludeSeedArtist = h("input", { type: "checkbox", id: this.id("exclude-seed-artist") });
    this.excludeSeedArtist.addEventListener("change", () => this.render());
    this.picker.element.append(h("label", { class: "md-check", for: this.id("exclude-seed-artist") }, this.excludeSeedArtist, "Exclude the seed artist"));
    this.root.classList.add("md-demo");
    this.root.replaceChildren(
      h("p", { class: "md-lead", text: "Start from a track you like, say what you want more or less of, and see why each track was picked." }),
      this.seed.element,
      this.picker.element,
      this.buildRankerToggle(),
      this.buildResults(),
      this.buildLog(),
      this.buildNote(),
      h("p", { class: "md-attribution", text: this.data.attribution }),
    );
  }

  buildRankerToggle() {
    this.rankerInputs = {};
    const radios = Object.keys(RANKER_LABELS).map((name) => {
      const input = h("input", { type: "radio", name: this.id("ranker"), id: this.id(`r-${name}`), value: name, checked: name === this.ranker });
      this.rankerInputs[name] = input;
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
    this.copyButton = h("button", { type: "button", class: "md-button", text: "Copy link to these results", disabled: true });
    this.copyButton.addEventListener("click", () => this.copyLink());
    this.linkField = h("input", { type: "text", class: "md-input", id: this.id("link"), readonly: true, spellcheck: "false" });
    this.linkBox = h("div", { class: "md-link-box", hidden: true }, h("label", { for: this.id("link"), class: "md-label", text: "Link to these results" }), this.linkField);
    return h(
      "section",
      { class: "md-section", "aria-labelledby": this.id("res-h") },
      h("h2", { id: this.id("res-h"), text: "Recommendations" }),
      this.status,
      this.sources.element,
      this.results,
      h("div", { class: "md-row" }, this.exportButton, h("span", { class: "md-help", text: "Ratings stay in this page. Export downloads them as a JSON file." })),
      this.linkable ? [h("div", { class: "md-row" }, this.copyButton), this.linkBox] : null,
    );
  }

  buildLog() {
    this.logStatus = h("p", { class: "md-log-status", role: "status", tabindex: "-1" });
    this.downloadLogButton = h("button", { type: "button", class: "md-button", text: "Download feedback log" });
    this.downloadLogButton.addEventListener("click", () => this.downloadLog());
    this.clearLogButton = h("button", { type: "button", class: "md-button", text: "Clear feedback log" });
    this.clearLogButton.addEventListener("click", () => this.askClearLog(true));
    const confirm = h("button", { type: "button", class: "md-button", text: "Yes, clear the log" });
    confirm.addEventListener("click", () => this.clearLog());
    this.cancelClearButton = h("button", { type: "button", class: "md-button", text: "Keep the log" });
    this.cancelClearButton.addEventListener("click", () => this.askClearLog(false));
    this.confirmQuestion = h("p", { id: this.id("clear-q") });
    this.confirmBox = h(
      "div",
      { class: "md-confirm", role: "group", "aria-labelledby": this.id("clear-q"), hidden: true },
      this.confirmQuestion,
      h("div", { class: "md-row" }, confirm, this.cancelClearButton),
    );
    const section = h(
      "section",
      { class: "md-section", "aria-labelledby": this.id("log-h") },
      h("h2", { id: this.id("log-h"), text: "Feedback log" }),
      h("p", {
        class: "md-help",
        text: `Every thumbs up or down, and every rating you take back, also adds an entry to a feedback log kept in this browser only, with the list it was given on. It holds up to ${MAX_EVENTS.toLocaleString("en")} entries, dropping the oldest first. Nothing is sent anywhere: download the log to keep or share it.`,
      }),
      this.logStatus,
      h("div", { class: "md-row" }, this.downloadLogButton, this.clearLogButton),
      this.confirmBox,
    );
    this.paintLog();
    return section;
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
    this.linkBox.hidden = true;
    this.copyButton.disabled = this.seed.row < 0;
    if (this.seed.row < 0) {
      this.results.replaceChildren();
      this.status.textContent = "Pick a seed track to see recommendations.";
      this.writeLink(null);
      return;
    }
    const preference = buildPreference(this.data, this.seed.row, this.picker.qualities);
    preference.excludeSeedArtist = this.excludeSeedArtist.checked;
    this.writeLink(preference);
    const picks = this.rankers.rank(this.ranker, preference, 10);
    const context = JSON.stringify([this.ranker, preference]);
    this.status.textContent = picks.length
      ? `${picks.length} recommendations from the ${RANKER_LABELS[this.ranker].toLowerCase()} ranker for ${this.seed.labels[this.seed.row]}.`
      : "No recommendations available with these preferences. Try unchecking Exclude the seed artist or picking another seed track.";
    this.painters = new Map();
    this.results.replaceChildren(...picks.map((pick, i) => this.resultItem(pick, i + 1, preference, context, picks)));
  }

  resultItem(pick, rank, preference, context, picks = [pick]) {
    const { title, header, linkBox } = resultPresentation(this.data, pick.row, rank, this.sources);
    const key = `${context}|${pick.id}`;
    const onRate = (value) => this.rate(key, value, pick, preference, picks);
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
      header,
      h("ul", { class: "md-why", "aria-label": "Why this track" }, pick.explanations.map((text) => h("li", { text }))),
      this.scoreDetail(pick, title),
      h("div", { class: "md-result-foot" }, linkBox, h("span", { class: "md-rates", role: "group", "aria-label": `Rate ${title}` }, up, down)),
    );
  }

  /** A closed disclosure listing every score term with its signed contribution and a bar. */
  scoreDetail(pick, title) {
    const terms = [...(pick.terms ?? [])].sort((a, b) => b.value - a.value);
    const largest = Math.max(...terms.map((t) => Math.abs(t.value)), 1e-9);
    const rows = terms.map((term) => {
      const fill = h("span", { class: "md-bar-fill", "data-sign": term.value < 0 ? "negative" : "positive" });
      // CSSOM, not a style attribute, so the page keeps its strict CSP.
      fill.style.width = `${(50 * Math.abs(term.value)) / largest}%`;
      return h(
        "li",
        { class: "md-term" },
        h("span", { class: "md-term-name", text: termLabel(this.data, term.name) }),
        h("span", { class: "md-term-value", text: signed(term.value) }),
        h("span", { class: "md-bar", "aria-hidden": "true" }, fill),
      );
    });
    const score = terms.reduce((sum, t) => sum + t.value, 0);
    return h(
      "details",
      { class: "md-score" },
      h("summary", {}, "How this was scored", h("span", { class: "md-sr", text: `: ${title}` })),
      h("p", { class: "md-help", text: `Score ${signed(score)}, the sum of these terms. Positive terms raise a track and negative ones lower it; bars are relative to the largest term here.` }),
      h("ul", { class: "md-terms", "aria-label": `Score terms for ${title}` }, rows),
    );
  }

  rate(key, value, pick, preference, picks = [pick]) {
    const withdrawn = this.ratings.get(key)?.rating === value;
    if (withdrawn) this.ratings.delete(key);
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
    this.log.append(buildEvent({ ranker: this.ranker, preference, picks, pick, rating: withdrawn ? 0 : value }));
    this.paintLog();
  }

  paintLog() {
    const count = this.log.size;
    this.logStatus.textContent = this.log.persistent
      ? `${entries(count)} in this browser's feedback log.`
      : `${entries(count)} in the feedback log. This browser is not letting the page save it, so it lasts only until you leave the page; download it first.`;
    this.downloadLogButton.disabled = count === 0;
    this.clearLogButton.disabled = count === 0;
  }

  downloadLog() {
    downloadText(this.log.toJsonl(), LOG_FILE, "application/x-ndjson");
    this.logStatus.textContent = `Downloaded ${entries(this.log.size)} as ${LOG_FILE}.`;
  }

  askClearLog(open) {
    this.confirmBox.hidden = !open;
    if (open) {
      this.confirmQuestion.textContent = `Clear all ${entries(this.log.size)} from this browser's feedback log? This cannot be undone.`;
      this.cancelClearButton.focus();
    } else {
      this.clearLogButton.focus();
    }
  }

  clearLog() {
    this.log.clear();
    this.confirmBox.hidden = true;
    this.paintLog();
    this.logStatus.textContent = "Feedback log cleared.";
    this.logStatus.focus();
  }

  exportRatings() {
    const body = { format: "music-discovery-ratings", version: 2, ratings: [...this.ratings.values()] };
    downloadJson(body, "music-discovery-ratings.json");
    this.status.textContent = `Exported ${body.ratings.length} rating${body.ratings.length === 1 ? "" : "s"}.`;
  }
}

function start() {
  document.querySelectorAll("[data-music-discovery]").forEach((root) => mountCatalog(root, Demo, "demo", import.meta.url));
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
else start();
