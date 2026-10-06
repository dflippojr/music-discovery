// Logic of the blind listener study that needs no DOM: which ranker's list is shown
// first in each task, the session id, and the exported file. The file layout is
// described by study-ratings.schema.json; docs/listener-study.md explains the study.

export const TASK_COUNT = 5; // the protocol allows four to six
export const LIST_SIZE = 5;
export const FORMAT = "music-discovery-study";
export const FORMAT_VERSION = 1;
export const RANKERS = ["baseline", "hybrid"];
export const LABELS = ["A", "B"];

// Shown before the first task; docs/listener-study.md carries the same text (a test checks).
export const CONSENT =
  "This is a short study of two ways of recommending music. You will do 5 tasks. In each, you pick a track, get two lists of tracks, listen as much as you like, rate each track, and say which list you prefer. Taking part is voluntary and you can stop at any time by closing the page; nothing is saved until you download the file at the end. The file holds a random session id, your choices and ratings and the times you made them. It does not hold your name, email address, IP address or anything you type. You then send the file to the person who invited you. The results will be reported only in aggregate, with no way to tell who rated what. By sending the file you agree to it being used this way. If you change your mind afterwards, tell the person who invited you and your file will be deleted, as long as the report has not yet been written.";

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
