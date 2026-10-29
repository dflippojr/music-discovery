// The demo's local feedback capture log, with no DOM: one event per thumbs action,
// kept in this browser's localStorage and downloaded as JSON Lines. Nothing here
// touches the network. feedback-log.schema.json describes one line;
// `musicdiscovery feedback-report` reads the downloaded file.

import { timestamp } from "./study-lib.js";

export const LOG_KEY = "music-discovery-feedback-log";
export const LOG_VERSION = 1;
export const MAX_EVENTS = 2000;

/**
 * One thumbs action: what was asked for, the whole list as shown (track ids and
 * ranks), the rated track, its rating (1, -1, or 0 when a rating is withdrawn)
 * and the score terms behind the explanations it showed.
 */
export function buildEvent({ ranker, preference, picks, pick, rating, time = timestamp() }) {
  return {
    version: LOG_VERSION,
    time,
    ranker,
    seed_track_ids: [...preference.seeds],
    likes: [...(preference.likes ?? [])],
    dislikes: [...(preference.dislikes ?? [])],
    exclude_seed_artist: Boolean(preference.excludeSeedArtist),
    shown: picks.map((p, i) => ({ track_id: p.id, rank: i + 1 })),
    track_id: pick.id,
    rank: picks.indexOf(pick) + 1,
    rating,
    explanation_terms: [...(pick.explained ?? [])],
  };
}

/**
 * A capped list of events in `storage` (a Storage, or a function returning one,
 * since merely reading `localStorage` throws when site data is blocked). Every
 * storage call is guarded: if storage fails, events are kept in memory for this
 * page only and `persistent` turns false. On a full quota the page keeps every
 * event and stores only the newest `stored` of them; `storageFull` says so.
 */
export class FeedbackLog {
  constructor(storage, { key = LOG_KEY, max = MAX_EVENTS } = {}) {
    this.storage = storage;
    this.key = key;
    this.max = max;
    this.persistent = true;
    this.memory = this.load();
    this.stored = this.memory.length;
    this.storageFull = false;
  }

  store() {
    return typeof this.storage === "function" ? this.storage() : this.storage;
  }

  /** The stored events; a corrupt value reads as empty and is replaced on the next save. */
  load() {
    let text;
    try {
      text = this.store().getItem(this.key);
    } catch {
      this.persistent = false;
      return [];
    }
    try {
      const events = text ? JSON.parse(text) : [];
      return Array.isArray(events) ? events.filter((e) => e && typeof e === "object") : [];
    } catch {
      return [];
    }
  }

  /** Every event, oldest first. */
  events() {
    return [...this.memory];
  }

  get size() {
    return this.memory.length;
  }

  /** Add one event, dropping the oldest beyond the cap; returns whether it was saved to storage. */
  append(event) {
    // Re-read first so two open tabs do not overwrite each other's events. While
    // storage holds only the newest events, the re-read would drop the older ones.
    if (this.persistent && !this.storageFull) {
      const stored = this.load();
      if (this.persistent) this.memory = stored;
    }
    this.memory = [...this.memory, event].slice(-this.max);
    return this.save(this.memory);
  }

  save(events) {
    if (!this.persistent) return false;
    // Usually a full quota: store the newer half, then the newer half of that, so a
    // reload keeps the newest events. The page's copy keeps them all. If not even
    // one event fits, stop saving.
    for (let keep = events; keep.length > 0; keep = keep.length > 1 ? keep.slice(Math.floor(keep.length / 2)) : []) {
      try {
        this.store().setItem(this.key, JSON.stringify(keep));
        this.stored = keep.length;
        this.storageFull = keep.length < events.length;
        return true;
      } catch {
        // Try fewer events.
      }
    }
    this.persistent = false;
    return false;
  }

  clear() {
    this.memory = [];
    this.stored = 0;
    this.storageFull = false;
    if (!this.persistent) return;
    try {
      this.store().removeItem(this.key);
    } catch {
      this.persistent = false;
    }
  }

  /** The log as JSON Lines, one event per line. */
  toJsonl() {
    return this.memory.map((event) => `${JSON.stringify(event)}\n`).join("");
  }
}
