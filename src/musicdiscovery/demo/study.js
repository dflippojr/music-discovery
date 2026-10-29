// Blind listener study. For each task the listener picks a seed and qualities, sees two
// unlabelled lists, rates every track and says which list they prefer. Which ranker made
// which list goes into the exported file, never onto the page. The session so far is kept
// in this tab's sessionStorage, so a reload or a discarded phone tab can resume it, and the
// page asks before it is left with ratings that are not exported. Nothing is sent anywhere:
// the listener downloads one JSON file and sends it to the owner.

import { Rankers } from "./ranker.js";
import { balancedOrders, buildExport, CONSENT, LABELS, LIST_SIZE, listsFilled, newSessionId, restoreSession, SessionStore, sessionState, TASK_COUNT, taskComplete, timestamp } from "./study-lib.js";
import { buildPreference, DeferredSources, downloadJson, h, makeIds, mountCatalog, QualityPicker, randomRow, resultPresentation, SeedPicker, thumbButton, trackLabel } from "./ui.js";

// Shown when a ranker has nothing for the seed and qualities picked (a seed whose sound sits at
// the catalog mean, with no quality). It names no ranker, so the lists stay blind.
const EMPTY_LIST = "This seed track gives one of the two lists nothing to show. Pick a quality or another seed track.";

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
    this.sources = new DeferredSources(data, root);
    this.store = new SessionStore(() => globalThis.sessionStorage);
    this.id = makeIds();
    this.newSession();
    root.classList.add("md-demo");
    globalThis.addEventListener("beforeunload", (event) => {
      if (!this.rated || this.exported) return;
      event.preventDefault();
      event.returnValue = ""; // browsers before Chrome 119 prompt only when this is set
    });
    const saved = restoreSession(this.store.load(), data.ids);
    if (saved) this.offerResume(saved);
    else {
      this.store.clear(); // whatever is kept is not a session this page can resume
      this.showIntro();
    }
  }

  newSession() {
    this.orders = balancedOrders(TASK_COUNT, randomRow);
    this.session = { id: newSessionId((bytes) => crypto.getRandomValues(bytes)), startedAt: timestamp(), tasks: [] };
    this.current = null;
    this.rated = false;
    this.exported = false;
  }

  /** Keep the session so far in this tab, once a task has a change worth coming back to. */
  save() {
    if (this.session.tasks.length || this.current?.task) this.store.save(sessionState(this.session, this.orders, this.current, this.rated));
  }

  show(...children) {
    this.root.replaceChildren(...children, this.sources.element, h("p", { class: "md-attribution", text: this.data.attribution }));
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

  offerResume(saved) {
    // Closing the tab now would lose the kept ratings with it, so the warning holds from here.
    this.rated = saved.rated;
    const done = saved.session.tasks.length;
    const resume = h("button", { type: "button", class: "md-button", text: "Resume my session" });
    const restart = h("button", { type: "button", class: "md-button", text: "Start over" });
    resume.addEventListener("click", () => this.resume(saved));
    restart.addEventListener("click", () => {
      this.store.clear();
      this.rated = false;
      this.showIntro();
    });
    this.show(
      this.heading("Listener study"),
      h("p", { class: "md-lead", text: `This tab has a session you have not exported yet, with ${done} of ${TASK_COUNT} tasks done. Resume it where you left off, or start over?` }),
      h("p", { class: "md-help", text: "Starting over deletes the ratings kept in this tab." }),
      h("div", { class: "md-row" }, resume, restart),
    );
  }

  resume({ session, orders, current }) {
    this.session = session;
    this.orders = orders;
    if (current?.task) this.showLists(current.task, current.seedRow);
    else if (session.tasks.length === TASK_COUNT) this.showDone();
    else this.showTask(current?.startedAt);
  }

  // --- one task -------------------------------------------------------------

  showTask(startedAt = timestamp()) {
    const index = this.session.tasks.length;
    this.current = { startedAt, seedRow: null, task: null };
    this.save();
    const next = h("button", { type: "button", class: "md-button", text: "Show the two lists", disabled: true });
    const status = h("p", { class: "md-status", role: "status" });
    // The lists are ranked as the picks change, so a task with an empty list is never shown.
    let task = null;
    const sync = () => {
      task = this.seed.row < 0 ? null : this.newTask(index, startedAt);
      next.disabled = task === null;
      status.textContent = this.seed.row >= 0 && task === null ? EMPTY_LIST : "";
    };
    this.seed = new SeedPicker(this.data, { heading: "1. Pick a seed track", onChange: sync });
    this.picker = new QualityPicker(this.data, { heading: "2. Qualities (optional)", onChange: sync });
    next.addEventListener("click", () => this.showLists(task, this.seed.row));
    this.show(
      this.heading(`Task ${index + 1} of ${TASK_COUNT}`),
      this.seed.element,
      this.picker.element,
      h("div", { class: "md-row" }, next),
      status,
    );
  }

  /** The task for the picked seed and qualities, or null when a ranker leaves its list empty. */
  newTask(index, startedAt) {
    const preference = buildPreference(this.data, this.seed.row, this.picker.qualities);
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
    return listsFilled(task) ? task : null;
  }

  showLists(task, seedRow) {
    const index = this.session.tasks.length;
    // Saved on the first change: until then, the same seed and qualities give the same lists.
    this.current = { startedAt: task.startedAt, seedRow, task };
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
      this.current = null;
      this.save();
      if (last) this.showDone();
      else this.showTask();
    });
    const lists = task.lists.map((list) => this.listSection(list, sync));
    this.show(
      this.heading(`Task ${index + 1} of ${TASK_COUNT}`),
      h("p", { class: "md-lead", text: `Seed: ${trackLabel(this.data, seedRow)}` }),
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
    const { title, header, linkBox } = resultPresentation(this.data, result.row, rank, this.sources);
    const onRate = (value) => {
      result.rating = result.rating === value ? null : value;
      this.rated = true;
      this.save();
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
    const fresh = h("input", { type: "checkbox", id: this.id(`new-${list.label}-${rank}`), checked: result.newToMe });
    fresh.addEventListener("change", () => {
      result.newToMe = fresh.checked;
      this.save();
    });
    return h(
      "li",
      { class: "md-result" },
      header,
      h("div", { class: "md-result-foot" }, linkBox, h("span", { class: "md-rates", role: "group", "aria-label": `Rate ${title}` }, up, down)),
      h("label", { class: "md-new", for: fresh.id }, fresh, "New to me", h("span", { class: "md-sr", text: ` for ${title}` })),
    );
  }

  preferenceSection(task, sync) {
    const name = this.id("prefer");
    const radios = PREFERENCES.map(({ value, text }) => {
      const input = h("input", { type: "radio", name, id: this.id(`p-${value}`), value, checked: task.preferred === value });
      input.addEventListener("change", () => {
        task.preferred = value;
        this.save();
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
      downloadJson(body, `listener-study-${this.session.id.slice(0, 8)}.json`);
      this.exported = true;
      this.store.clear();
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

function start() {
  document.querySelectorAll("[data-music-discovery-study]").forEach((root) => mountCatalog(root, Study, "study", import.meta.url));
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
else start();
