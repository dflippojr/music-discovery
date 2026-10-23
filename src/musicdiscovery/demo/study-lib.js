// Logic of the blind listener study that needs no DOM: which ranker's list is shown
// first in each task, the session id, the copy kept for resuming, and the exported file. The file layout is
// described by study-ratings.schema.json; docs/listener-study.md explains the study.

export const TASK_COUNT = 5; // the protocol allows four to six
export const LIST_SIZE = 5;
export const FORMAT = "music-discovery-study";
export const FORMAT_VERSION = 1;
export const RANKERS = ["baseline", "hybrid"];
export const LABELS = ["A", "B"];
export const SESSION_KEY = "music-discovery-study-session";
export const SESSION_VERSION = 1;
const PREFERRED = new Set([null, "A", "B", "none"]);

// Shown before the first task; docs/listener-study.md carries the same text (a test checks).
export const CONSENT =
  "This is a short study of two ways of recommending music. You will do 5 tasks. In each, you pick a track, get two lists of tracks, listen as much as you like, rate each track, and say which list you prefer. Taking part is voluntary and you can stop at any time by closing the page. So that a reload does not lose your work, your choices and ratings so far are kept in this browser tab's own storage (sessionStorage) and nowhere else; they are deleted when you download the file at the end, when you choose to start over instead of resuming, or when you close the tab for good (a browser that reopens a closed tab, or restores its tabs after a restart, can bring them back with it). The file holds a random session id, your choices and ratings and the times you made them. It does not hold your name, email address, IP address or anything you type. You then send the file to the person who invited you. The results will be reported only in aggregate, with no way to tell who rated what. By sending the file you agree to it being used this way. If you change your mind afterwards, tell the person who invited you and your file will be deleted, as long as the report has not yet been written.";

/**
 * For each task, which ranker is shown as list A. Hybrid is first in half the tasks
 * (the odd task out is a coin flip), in a random order, so neither ranker gets the
 * first position more than once more than the other. `randomBelow(n)` returns a
 * uniform integer in [0, n).
 */
export function balancedOrders(count, randomBelow) {
  const hybridFirst = Math.floor(count / 2) + (count % 2 ? randomBelow(2) : 0);
  const orders = Array.from({ length: count }, (_, i) => (i < hybridFirst ? ["hybrid", "baseline"] : ["baseline", "hybrid"]));
  for (let i = count - 1; i > 0; i -= 1) {
    const j = randomBelow(i + 1);
    [orders[i], orders[j]] = [orders[j], orders[i]];
  }
  return orders;
}

/** 32 lowercase hex characters from `fill`, which fills a Uint8Array with random bytes. */
export function newSessionId(fill) {
  const bytes = new Uint8Array(16);
  fill(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** An ISO 8601 UTC time to the second, e.g. 2026-10-06T14:03:09Z. */
export function timestamp(date = new Date()) {
  return `${date.toISOString().slice(0, 19)}Z`;
}

/** Whether every result in the task has a rating and a preference has been chosen. */
export function taskComplete(task) {
  return task.preferred !== null && task.lists.every((list) => list.results.every((r) => r.rating === 1 || r.rating === -1));
}

/**
 * The exported file: only the random session id, task inputs, shown track ids with
 * their ratings, which ranker produced each list, the overall preference and times.
 */
export function buildExport(session, exportedAt = timestamp()) {
  return {
    format: FORMAT,
    version: FORMAT_VERSION,
    session_id: session.id,
    started_at: session.startedAt,
    exported_at: exportedAt,
    tasks: session.tasks.map((task, i) => ({
      task: i + 1,
      seed_track_id: task.seedTrackId,
      likes: task.likes,
      dislikes: task.dislikes,
      started_at: task.startedAt,
      completed_at: task.completedAt,
      lists: task.lists.map((list) => ({
        label: list.label,
        ranker: list.ranker,
        results: list.results.map((r) => ({ track_id: r.trackId, rating: r.rating, new_to_me: r.newToMe })),
      })),
      preferred: task.preferred,
    })),
  };
}

/**
 * The in-progress session kept in `storage` (a Storage, or a function returning one,
 * since merely reading `sessionStorage` throws when site data is blocked) so a reload
 * or a discarded tab can resume. Every storage call is guarded: when storage fails,
 * the session simply lives in memory only, as it would without this copy.
 */
export class SessionStore {
  constructor(storage, key = SESSION_KEY) {
    this.storage = storage;
    this.key = key;
  }

  store() {
    return typeof this.storage === "function" ? this.storage() : this.storage;
  }

  /** The stored value, or null when there is none or it cannot be read or parsed. */
  load() {
    try {
      const text = this.store().getItem(this.key);
      return text ? JSON.parse(text) : null;
    } catch {
      return null;
    }
  }

  /** Returns whether the state was written. */
  save(state) {
    try {
      this.store().setItem(this.key, JSON.stringify(state));
      return true;
    } catch {
      return false;
    }
  }

  clear() {
    try {
      this.store().removeItem(this.key);
    } catch {
      // Nothing was stored, or storage is blocked: either way nothing is left to delete.
    }
  }
}

/**
 * What the store keeps: the session so far, the list order of every task, the task on
 * screen, and whether any track was ever rated (the leave-page warning holds from the
 * first rating until export, even if that rating is withdrawn).
 */
export function sessionState(session, orders, current, rated) {
  return { version: SESSION_VERSION, session, orders, current, rated };
}

// The rules of study-ratings.schema.json that a stored session must also keep, so a
// resumed session cannot export a file the schema rejects (a test checks they match).
export const TIMESTAMP_PATTERN = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/u;
export const QUALITY_PATTERN = /^(genre|tag|more|less):[^\n]{1,60}$/u;
const MAX_QUALITIES = 60;

const isTime = (value) => typeof value === "string" && TIMESTAMP_PATTERN.test(value);
const isQualities = (value) => Array.isArray(value) && value.length <= MAX_QUALITIES && value.every((q) => typeof q === "string" && QUALITY_PATTERN.test(q));

function validOrders(orders) {
  return Array.isArray(orders) && orders.length === TASK_COUNT && orders.every((o) => Array.isArray(o) && o.length === RANKERS.length && RANKERS.every((r) => o.includes(r)));
}

function validResult(result, ids) {
  if (typeof result !== "object" || result === null || !Number.isInteger(result.row) || result.row < 0 || result.row >= ids.length) return false;
  return ids[result.row] === result.trackId && [null, 1, -1].includes(result.rating) && typeof result.newToMe === "boolean";
}

/** Whether `task` is well formed for this catalog, with lists in the order `order`. */
function validTask(task, order, ids) {
  if (!task || !ids.includes(task.seedTrackId) || !isQualities(task.likes) || !isQualities(task.dislikes)) return false;
  if (!isTime(task.startedAt) || !PREFERRED.has(task.preferred) || !Array.isArray(task.lists) || task.lists.length !== LABELS.length) return false;
  return task.lists.every((list, i) => list?.label === LABELS[i] && list.ranker === order[i] && Array.isArray(list.results) && list.results.length >= 1 && list.results.length <= LIST_SIZE && list.results.every((r) => validResult(r, ids)));
}

/**
 * A stored state checked against the catalog's track `ids`, or null when it is not one
 * this page wrote for this catalog (an old version, another catalog, or a damaged value).
 * `current` is null between tasks, holds only `startedAt` while the seed is picked, and
 * holds the task and its seed row once the lists are shown.
 */
export function restoreSession(state, ids) {
  if (state?.version !== SESSION_VERSION || typeof state.rated !== "boolean") return null;
  const { session, orders, current } = state;
  if (!validOrders(orders) || !/^[0-9a-f]{32}$/.test(session?.id) || !isTime(session.startedAt) || !Array.isArray(session.tasks)) return null;
  const { tasks } = session;
  if (tasks.length > TASK_COUNT || !tasks.every((task, i) => validTask(task, orders[i], ids) && taskComplete(task) && isTime(task.completedAt))) return null;
  if (current === null) return state;
  if (tasks.length === TASK_COUNT || !isTime(current?.startedAt)) return null;
  if (current.task === null) return state;
  const task = current.task;
  return validTask(task, orders[tasks.length], ids) && task.completedAt === null && ids[current.seedRow] === task.seedTrackId ? state : null;
}
