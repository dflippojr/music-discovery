// Blind listener study. For each task the listener picks a seed and qualities, sees two
// unlabelled lists, rates every track and says which list they prefer. Which ranker made
// which list is kept in memory only and goes into the exported file, never onto the page.
// Nothing is sent anywhere: the listener downloads one JSON file and sends it to the owner.

import { Rankers } from "./ranker.js";
import { balancedOrders, buildExport, CONSENT, LABELS, LIST_SIZE, newSessionId, TASK_COUNT, taskComplete, timestamp } from "./study-lib.js";
import { buildPreference, h, makeIds, QualityPicker, randomRow, SeedPicker, thumbButton } from "./ui.js";

const PREFERENCES = [
  { value: "A", text: "List A" },
  { value: "B", text: "List B" },
  { value: "none", text: "No preference" },
];

class Study {
  constructor(root, data) {
    this.root = root;
    this.data = data;
    this.rankers = new Rankers(data);
    this.orders = balancedOrders(TASK_COUNT, randomRow);
    this.session = { id: newSessionId((bytes) => crypto.getRandomValues(bytes)), startedAt: timestamp(), tasks: [] };
    this.id = makeIds();
    root.classList.add("md-demo");
    this.showIntro();
  }

  show(...children) {
    this.root.replaceChildren(...children, h("p", { class: "md-attribution", text: this.data.attribution }));
    this.root.querySelector("h2")?.focus();
  }

  heading(text) {
    return h("h2", { class: "md-study-heading", tabindex: "-1", text });
  }

  showIntro() {
    const start = h("button", { type: "button", class: "md-button", text: "I agree, start" });
    start.addEventListener("click", () => this.showTask());
    this.show(
      this.heading("Listener study"),
      h("p", { class: "md-lead", text: CONSENT }),
      h("p", { class: "md-help", text: `Each list has ${LIST_SIZE} tracks. You do not know how either list was made. Open a track's source page to listen, then rate it; tick “New to me” if you had not heard it before.` }),
      h("div", { class: "md-row" }, start),
    );
  }

  // --- one task -------------------------------------------------------------

  showTask() {
    const index = this.session.tasks.length;
    const startedAt = timestamp();
    const next = h("button", { type: "button", class: "md-button", text: "Show the two lists", disabled: true });
    const sync = () => {
      next.disabled = this.seed.row < 0;
    };
    this.seed = new SeedPicker(this.data, { heading: "1. Pick a seed track", onChange: sync });
    this.picker = new QualityPicker(this.data, { heading: "2. Qualities (optional)", onChange: () => {} });
    next.addEventListener("click", () => this.showLists(index, startedAt));
    this.show(
      this.heading(`Task ${index + 1} of ${TASK_COUNT}`),
      this.seed.element,
      this.picker.element,
      h("div", { class: "md-row" }, next),
    );
  }

  showLists(index, startedAt) {
    const preference = buildPreference(this.data, this.seed.row, this.picker.qualities);
    const seedLabel = this.seed.labels[this.seed.row];
    const task = {
      seedTrackId: preference.seeds[0],
      likes: preference.likes,
      dislikes: preference.dislikes,
      startedAt,
      completedAt: null,
      preferred: null,
      lists: this.orders[index].map((ranker, i) => ({
        label: LABELS[i],
        ranker,
        results: this.rankers.rank(ranker, preference, LIST_SIZE).map((pick) => ({ trackId: pick.id, row: pick.row, rating: null, newToMe: false })),
      })),
    };
    const last = index === TASK_COUNT - 1;
    const next = h("button", { type: "button", class: "md-button", text: last ? "Finish" : "Next task", disabled: true });
    const status = h("p", { class: "md-status", role: "status" });
    const sync = () => {
      const unrated = task.lists.reduce((sum, list) => sum + list.results.filter((r) => r.rating === null).length, 0);
      next.disabled = !taskComplete(task);
      if (unrated) status.textContent = `${unrated} track${unrated === 1 ? "" : "s"} still need a thumbs up or down.`;
      else status.textContent = task.preferred ? "Ready for the next step." : "Now choose the list you prefer.";
    };
    next.addEventListener("click", () => {
      task.completedAt = timestamp();
      this.session.tasks.push(task);
      if (last) this.showDone();
      else this.showTask();
    });
    const lists = task.lists.map((list) => this.listSection(list, sync));
    this.show(
      this.heading(`Task ${index + 1} of ${TASK_COUNT}`),
      h("p", { class: "md-lead", text: `Seed: ${seedLabel}` }),
      ...lists,
      this.preferenceSection(task, sync),
      status,
      h("div", { class: "md-row" }, next),
    );
    sync();
  }

  listSection(list, sync) {
    const id = this.id(`list-${list.label}`);
    const items = list.results.map((result, i) => this.resultItem(list, result, i + 1, sync));
    return h("section", { class: "md-section", "aria-labelledby": id }, h("h3", { id, class: "md-study-list", text: `List ${list.label}` }), h("ol", { class: "md-results" }, items));
  }

  resultItem(list, result, rank, sync) {
    const { data } = this;
    const row = result.row;
    const title = data.titles[row] || `Track ${result.trackId}`;
    const genre = data.genre[row] >= 0 ? data.genres[data.genre[row]] : "";
    const links = [];
    if (data.sources[row]) links.push(h("a", { href: data.sources[row], target: "_blank", rel: "noopener noreferrer", text: "Source page (FMA)" }));
    if (data.licenses[data.license[row]]) links.push(h("a", { href: data.licenses[data.license[row]], target: "_blank", rel: "noopener noreferrer", text: "License" }));
    const onRate = (value) => {
      result.rating = result.rating === value ? null : value;
      paint();
      sync();
    };
    const up = thumbButton(1, "Thumbs up", title, onRate);
    const down = thumbButton(-1, "Thumbs down", title, onRate);
    const paint = () => {
      up.setAttribute("aria-pressed", String(result.rating === 1));
      down.setAttribute("aria-pressed", String(result.rating === -1));
    };
    paint();
    const fresh = h("input", { type: "checkbox", id: this.id(`new-${list.label}-${rank}`) });
    fresh.addEventListener("change", () => {
      result.newToMe = fresh.checked;
    });
    return h(
      "li",
      { class: "md-result" },
      h("div", { class: "md-result-head" }, h("span", { class: "md-rank", "aria-hidden": "true", text: String(rank) }), h("div", {}, h("p", { class: "md-title", text: title }), h("p", { class: "md-meta", text: [data.artists[data.artist[row]], genre].filter(Boolean).join(" · ") }))),
      h("div", { class: "md-result-foot" }, h("span", { class: "md-links" }, links), h("span", { class: "md-rates", role: "group", "aria-label": `Rate ${title}` }, up, down)),
      h("div", { class: "md-new" }, fresh, h("label", { for: fresh.id }, "New to me", h("span", { class: "md-sr", text: ` for ${title}` }))),
    );
  }

  preferenceSection(task, sync) {
    const name = this.id("prefer");
    const radios = PREFERENCES.map(({ value, text }) => {
      const input = h("input", { type: "radio", name, id: this.id(`p-${value}`), value });
      input.addEventListener("change", () => {
        task.preferred = value;
        sync();
      });
      return h("div", { class: "md-radio" }, input, h("label", { for: input.id }, h("span", { class: "md-radio-name", text })));
    });
    return h("fieldset", { class: "md-section md-fieldset" }, h("legend", { text: "Which list do you prefer overall?" }), radios);
  }

  // --- finish ---------------------------------------------------------------

  showDone() {
    const exportButton = h("button", { type: "button", class: "md-button", text: "Export my ratings" });
    const status = h("p", { class: "md-status", role: "status" });
    exportButton.addEventListener("click", () => {
      const body = buildExport(this.session);
      const url = URL.createObjectURL(new Blob([`${JSON.stringify(body, null, 2)}\n`], { type: "application/json" }));
      const link = h("a", { href: url, download: `listener-study-${this.session.id.slice(0, 8)}.json` });
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      status.textContent = "Downloaded. Please send that file to the person who invited you.";
    });
    this.show(
      this.heading("All done, thank you"),
      h("p", { class: "md-lead", text: "Download your ratings as one file and send it to the person who invited you. It contains no name, email address or free text." }),
      h("div", { class: "md-row" }, exportButton),
      status,
    );
  }
}

async function mount(root) {
  const url = new URL(root.dataset.catalog ?? "catalog.json", import.meta.url);
  root.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    root.study = new Study(root, await response.json());
  } catch (error) {
    root.replaceChildren(h("p", { class: "md-error", role: "alert", text: `The study could not load its catalog (${error.message}).` }));
  } finally {
    root.removeAttribute("aria-busy");
  }
}

function start() {
  document.querySelectorAll("[data-music-discovery-study]").forEach(mount);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
else start();
