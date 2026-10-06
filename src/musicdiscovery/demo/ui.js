// Pieces shared by the demo page and the study page: DOM helper, seed typeahead,
// quality chips and thumb buttons. No state beyond what each piece owns.

const MAX_SUGGESTIONS = 8;
const NEXT_STATE = { neutral: "like", like: "dislike", dislike: "neutral" };
const STATE_WORDS = { neutral: "not selected", like: "liked", dislike: "disliked" };
const STATE_MARKS = { neutral: "", like: "+", dislike: "−" };
const SVG_NS = "http://www.w3.org/2000/svg";
const THUMB_PATH =
  "M2 21h4V9H2v12zm20-11a2 2 0 0 0-2-2h-6.3l1-4.6v-.3a1.5 1.5 0 0 0-.44-1.06L13.2 1 6.6 7.6A2 2 0 0 0 6 9v10a2 2 0 0 0 2 2h9a2 2 0 0 0 1.84-1.22l3-7A2 2 0 0 0 22 12v-2z";

let uid = 0;

/** A function that makes element ids unique to one component instance. */
export function makeIds() {
  const n = ++uid;
  return (name) => `md-${name}-${n}`;
}

/** A uniformly random integer below count; crypto keeps the choice free of Math.random. */
export function randomRow(count) {
  const limit = 2 ** 32 - (2 ** 32 % count);
  const draw = new Uint32Array(1);
  do crypto.getRandomValues(draw);
  while (draw[0] >= limit);
  return draw[0] % count;
}

export function capitalise(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function h(tag, attrs = {}, ...children) {
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

export function thumbIcon(down) {
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

export function trackLabel(data, row) {
  const { titles, artists, artist, ids } = data;
  return `${titles[row] || `Track ${ids[row]}`} — ${artists[artist[row]] || "Unknown artist"}`;
}

/** The preference object the rankers take, from a seed row and a picker's qualities. */
export function buildPreference(data, seedRow, qualities) {
  const likes = [];
  const dislikes = [];
  for (const q of qualities.values()) {
    const dir = q.kind === "axis" ? (q.direction > 0 ? "more" : "less") : q.kind;
    const text = q.kind === "axis" ? `${dir}:${q.name}` : `${q.kind}:${q.name}`;
    (q.state === "like" ? likes : dislikes).push(text);
  }
  return { seeds: [data.ids[seedRow]], likes, dislikes };
}

/** Seed section: a combobox with typeahead and a random pick. `onChange` fires on any change. */
export class SeedPicker {
  constructor(data, { heading, onChange }) {
    this.data = data;
    this.onChange = onChange;
    this.row = -1;
    this.suggestions = [];
    this.active = -1;
    this.labels = data.ids.map((_, row) => trackLabel(data, row));
    this.searchable = this.labels.map((text) => text.toLowerCase());
    this.id = makeIds();
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
    random.addEventListener("click", () => this.pick(randomRow(data.count)));
    this.input.addEventListener("input", () => this.suggest());
    this.input.addEventListener("keydown", (event) => this.onKey(event));
    this.input.addEventListener("blur", () => this.closeList());
    this.input.addEventListener("focus", () => {
      if (this.input.value && this.row < 0) this.suggest();
    });
    this.element = h(
      "section",
      { class: "md-section", "aria-labelledby": this.id("seed-h") },
      h("h2", { id: this.id("seed-h"), text: heading }),
      h("label", { for: this.id("seed"), class: "md-label", text: "Search by title or artist" }),
      h("div", { class: "md-combo" }, this.input, this.list),
      h("div", { class: "md-row" }, random),
    );
  }

  suggest() {
    this.row = -1;
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
          this.pick(row);
        });
        return option;
      }),
    );
    const open = this.suggestions.length > 0;
    this.list.hidden = !open;
    this.input.setAttribute("aria-expanded", String(open));
    this.syncActive();
    this.onChange();
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

  onKey(event) {
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
      this.pick(this.suggestions[this.active]);
    } else if (event.key === "Escape") {
      this.closeList();
    }
  }

  pick(row) {
    this.row = row;
    this.input.value = this.labels[row];
    this.suggestions = [];
    this.closeList();
    this.onChange();
  }
}

/** Quality chips: each cycles not selected, liked, disliked. `qualities` holds the chosen ones. */
export class QualityPicker {
  constructor(data, { heading, onChange }) {
    this.onChange = onChange;
    this.qualities = new Map(); // "genre:Rock" -> {kind, name, direction, state}
    this.id = makeIds();
    const group = (title, items) =>
      h("div", { class: "md-chip-group", role: "group", "aria-label": title }, h("h3", { class: "md-subhead", text: title }), h("div", { class: "md-chips" }, items));
    const axisChips = data.axes.flatMap((axis) => [
      this.chip({ kind: "axis", name: axis.name, direction: 1 }, capitalise(axis.up)),
      this.chip({ kind: "axis", name: axis.name, direction: -1 }, capitalise(axis.down)),
    ]);
    this.element = h(
      "section",
      { class: "md-section", "aria-labelledby": this.id("q-h") },
      h("h2", { id: this.id("q-h"), text: heading }),
      h("p", { class: "md-help", text: "Select a quality once to like it, twice to dislike it, a third time to clear it." }),
      group("Sound", axisChips),
      group("Genres", data.chips.genres.map((g) => this.chip({ kind: "genre", name: g, direction: 1 }, g))),
      group("Popular tags", data.chips.tags.map((t) => this.chip({ kind: "tag", name: t, direction: 1 }, t))),
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
      this.onChange();
    });
    return button;
  }
}

/** A thumbs up or down button for `title`; `onClick` gets the value (1 or -1). */
export function thumbButton(value, label, title, onClick) {
  const button = h("button", { type: "button", class: "md-rate" }, thumbIcon(value < 0), h("span", { text: label }), h("span", { class: "md-sr", text: ` for ${title}` }));
  button.addEventListener("click", () => onClick(value));
  return button;
}
