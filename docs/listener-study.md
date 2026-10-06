# Listener study

A blind pilot that asks whether listeners prefer the hybrid ranker's
recommendations to the baseline's. It is a pilot, not a statistically powered
test. This document is committed before the first session; the analysis plan
below is fixed before any data is collected, and any later change is recorded
in the report next to the results.

## Participants

- 8 to 15 listeners the owner knows, invited in two batches. The second batch
  starts only after the first batch's files have been returned and checked, so
  a problem with the page or the instructions can be fixed once.
- Adults who listen to music regularly; no other screening.
- Each participant does one session of 5 tasks, about 15 minutes, on a laptop or
  phone, in the study page (`study.html` in the demo bundle).

## What a session looks like

For each task the listener picks a seed track and, optionally, qualities they
want more or less of. The page shows two lists of 5 tracks, labelled only A and
B. One comes from the baseline ranker and one from the hybrid ranker; which is
which is chosen at random per task, with the hybrid list first in half the tasks
(the fifth task is a coin flip), and is never shown. The listener opens each
track's source page to listen, gives every track a thumbs up or down, ticks
"New to me" if they had not heard it, and says which list they prefer overall
(A, B or no preference). The page shows no ranker names, scores or explanations.

## Consent

Shown on the page before the first task and sent with the invitation:

> This is a short study of two ways of recommending music. You will do 5 tasks.
> In each, you pick a track, get two lists of tracks, listen as much as you
> like, rate each track, and say which list you prefer. Taking part is
> voluntary and you can stop at any time by closing the page; nothing is saved
> until you download the file at the end. The file holds a random session id,
> your choices and ratings and the times you made them. It does not hold your
> name, email address, IP address or anything you type. You then send the file
> to the person who invited you. The results will be reported only in
> aggregate, with no way to tell who rated what. By sending the file you agree
> to it being used this way. If you change your mind afterwards, tell the
> person who invited you and your file will be deleted, as long as the report
> has not yet been written.

## What is recorded

One JSON file per session, described by
`src/musicdiscovery/study-ratings.schema.json`. Only these fields exist; the
schema rejects any other:

- a random 32-character session id made in the browser (not derived from
  anything about the person or device) and the start and export times;
- per task: the task number, the seed track id, the liked and disliked qualities
  (taken from the page's fixed choices), the start and finish times;
- per list: its label (A or B), which ranker made it (hidden from the
  participant) and, for each shown track, its id, the thumbs up or down and the
  "new to me" box;
- per task: the overall preference (A, B or none).

No names, emails, IP addresses, free text, user agent or device details are
collected. The page makes no network requests and has no analytics.

## Returning files

The participant clicks "Export my ratings" on the last screen, which downloads
one file, and sends it to the owner by whatever channel they normally use
(email attachment or message). Nothing is uploaded and there is no backend.

## Data handling

- Returned files are saved only under the git-ignored `ratings/` directory (or
  another directory outside the repository). They are never committed.
- Because the owner knows the participants and receives the file from them, the
  file's sender is known to the owner even though the file holds no identity.
  The owner does not record who sent which file, and deletes the message
  holding it once the file is saved.
- Files are kept until the report is written and then deleted, unless a
  participant has asked for theirs to be deleted sooner.
- The report holds aggregates only: no session ids, seeds or track-level
  ratings.
- If a participant volunteers anything identifying in a message, it is not
  copied into any file in the repository.

## Analysis plan

Fixed before any session. The command `musicdiscovery analyze-study <dir>`
implements it and writes `reports/listener-study-<date>.md`.

1. **Validation.** Every file must match the schema, hold two different
   rankers labelled A then B in every task, and have a unique session id.
   If any file fails, the command stops with a message naming the file and no
   report is written; the file is fixed or excluded by the owner, and the
   exclusion is noted in the report.
2. **Primary outcome: overall preference.** Over all tasks from all sessions,
   count tasks where the hybrid list was preferred and where the baseline list
   was preferred. "No preference" tasks are counted and reported but left out of
   the test. Report the hybrid's preference share with a 95% Wilson interval and
   a two-sided exact sign test against an even split (p = 0.5). Tasks are
   treated as independent trials, which they are not quite; see Limits.
3. **Thumbs-up rate.** For each ranker, the share of shown tracks that received
   a thumbs up, with a 95% Wilson interval.
4. **New to me and liked.** For each ranker, the share of shown tracks that
   were both marked "new to me" and given a thumbs up, with a 95% Wilson
   interval. This is the discovery measure: a good list contains tracks the
   listener had not heard and likes.
5. **No other tests.** There is no multiple-comparison correction because there
   is one primary outcome; items 3 and 4 are descriptive. Nothing is dropped
   because it contradicts expectations, and subgroups are not examined (too few
   participants).
6. **How the result is read.** A p-value under 0.05 is described as consistent
   with a preference, not as proof, and a p-value above it as no evidence of a
   difference. Either way the report calls the study a pilot and repeats its
   Limits.

## Limits

The same text is written into every report.

- The study is a pilot with a small convenience sample of acquaintances of the
  owner, not a statistically powered test; its intervals are wide and say little
  about people in general.
- The catalog is a small, fixed set of openly licensed tracks and is biased
  towards the genres it holds, so a good list is limited by what exists.
- Listeners judge from each track's source page, so novelty, presentation and
  how quickly they listened all affect ratings.
- Each session is minutes long: it says nothing about long-term satisfaction
  or whether people would come back.
- Ratings from one listener are not independent, but the intervals treat them
  as if they were, so they are tighter than they should be.
- Listeners saw their own seeds and chosen qualities, so the two lists were
  compared on different tasks across people, not on one fixed set of queries.

## Running the study

1. `musicdiscovery ingest` and `musicdiscovery export-demo build/demo` (see
   [demo.md](demo.md)); serve or vendor the bundle, and give participants the
   address of `study.html`.
2. Collect the files into `ratings/`.
3. `musicdiscovery analyze-study ratings` writes the report.
