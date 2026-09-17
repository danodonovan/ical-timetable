# AGENTS.md

## What this is

One script, `timetable.py`, that turns a school's EduLink One iCal feed into a
printable A4 PDF per week of the timetable cycle — colour-coded by subject, with
lesson times, set codes and rooms. The current feed is a Year 8 (`8X`) timetable
for the 2026/27 academic year.

Scope is deliberately small: fetch, work out the structure, draw two sheets to
pin up. There is no web app, no database, no scheduling.

```
timetable.py     the whole thing (~660 lines, sectioned: parse / structure / colour / draw / main)
README.md        how to run it, for a human
.envrc           ICAL_TIMETABLE_URL — SECRET, gitignored (see below)
out/             generated PDFs; disposable, regenerate rather than edit
```

## Running it

Needs [`uv`](https://docs.astral.sh/uv/). The script carries a PEP 723 inline
header declaring `reportlab`, so it is self-bootstrapping — do **not** add a
`pyproject.toml`, `requirements.txt` or virtualenv.

```sh
./timetable.py                                   # live feed -> out/
./timetable.py --ics feed.ics --outdir /tmp/x    # offline, repeatable
./timetable.py --save-ics feed.ics               # keep a copy of the feed
```

Iterate against a saved `.ics` rather than hammering the school's server.

To check a visual change, render the PDF and actually look at it:

```sh
sips -s format png --resampleWidth 1800 out/timetable-week-a.pdf --out /tmp/a.png
```

Lint with `uvx ruff check --select E,F,W,B,ISC --line-length 100 timetable.py`
(clean as of the last change). There are no tests; the feed itself is the
fixture, and `report()` printing the expected 2-week cycle and 2 exceptions is
the regression signal.

## The feed — what's actually in it

Established by inspecting the whole year (1103 events). Worth not re-deriving:

- **Properties present:** `DTSTART`, `DTEND`, `SUMMARY`, `DESCRIPTION`,
  `LOCATION`, `UID`, `DTSTAMP`. Nothing else.
- `DESCRIPTION` is byte-identical to `SUMMARY` on every event. Ignore it.
- **No recurrence rules.** One `VEVENT` per lesson, 1103 of them.
- **No teacher names, no `CATEGORIES`, no `ORGANIZER`,** no homework or
  assessment data, no term-date or holiday events. The only hint of a teacher is
  the tutor group code `MW/Tp2` — `MW` is probably someone's initials.
- `SUMMARY` packs three fields: `"Mathematics 8X/Ma2"` → subject, group, set
  code. Parsed by `TITLE_RE`; falls back to treating the whole string as the
  subject if it doesn't match.
- `LOCATION` carries room *and* its name: `"K15 Maths"`, `"E11 Catering"`,
  `"Hums Central Room"`.
- Times are **correct UTC** (`...Z`) — winter events really are stamped an hour
  later than summer ones, so converting to `Europe/London` is right and there is
  no DST bug to work around. Never treat the wall-clock time as local.

## Timetable structure — established facts

- **Day is 09:00–15:20 local, every day, all year.** Five 60-minute periods plus
  a 20-minute tutor period; break 11:00–11:20, lunch 12:40–13:20. No early or
  late lessons anywhere in the feed.
- **Two-week cycle, and it does _not_ follow calendar-week parity.** A holiday
  removes a week from the cycle rather than shifting it, so a one-week half-term
  flips ISO-week parity. `teaching_weeks()` indexes weeks by position in the
  *teaching* sequence instead; on that basis the 2-week cycle explains every one
  of the 1103 events bar two.
- **Which week the school calls "A" is not in the feed.** The labels are a
  guess: `--swap-weeks` flips them, and each sheet's footer lists its actual
  weeks commencing so a human can check.
- **The only two exceptions all year** (both room changes, both first week of
  term): Thu 3 Sep 2026 Drama in `G6 Dance` not `E15 Black Comedy`; Tue 8 Sep
  2026 PE at `Swimming pool` not `Playground 3`. `build_grid()` majority-votes
  each cell and returns these as `exceptions` for `report()` to print. If a run
  starts reporting many more, the cycle detection has gone wrong — look there
  first, not at the drawing code.
- Holiday gaps are derived, not hardcoded (`term_gaps()`).

## Design decisions — settled, don't re-litigate

- **Structure is derived from the data, not hardcoded.** Period times, day
  columns, subject list and cycle length all come from the feed
  (`period_rows()`, `detect_cycle_length()`). It should survive next year's
  timetable without edits.
- **Colour is by subject _family_, one hue each, tint depth separating subjects
  within a family** (`FAMILIES`, `TINTS`). The eight hues are the validated
  colourblind-safe categorical set from the `dataviz` skill's reference palette
  — they pass adjacent-pair CVD and normal-vision separation on a light surface.
  Don't invent a ninth hue, and don't swap one without re-running
  `scripts/validate_palette.js` from that skill.
- **Every block is text-labelled**, so colour never carries identity alone. That
  is what relieves the palette's sub-3:1 contrast warning.
- **Text contrast is computed, not eyeballed.** `detail_ink()` picks the softer
  ink only where it still clears 4.5:1 against that cell's fill. Worst case is
  currently 11.2:1 for body text. If you deepen `TINTS`, that check keeps small
  text legible automatically — but verify.
- **Family matching uses word-boundary regexes** (`family_of()`). Substring
  matching bit twice: `"physic"` pulled *Physical Education* into Science, and
  `" pe"` pulled *Tutor Period* into PE. Keep the `\b` anchors.
- **Landscape A4 by default**; `--portrait` also works and is tested, with the
  legend dropping to 3 columns and long names wrapping to 3 lines.
- Pale-but-coloured fills with a full-strength accent bar, chosen to be
  ink-friendly on a home printer.

## Conventions

- **`.envrc` holds the feed URL and is a secret** — it is a signed EduLink link
  that exposes one child's timetable. It is gitignored; keep it that way, never
  paste the URL into a commit, log line, issue or PDF, and redact it in any
  output you show.
- Commits: Angular style (`feat(render): ...`), atomic, imperative, lower-case,
  no attribution trailers.
- The repo has no commits yet as of writing. `.gitignore` already covers the
  generated and local-only files: `.envrc`, `out`, `**/*.pyc`, `.DS_Store`.
