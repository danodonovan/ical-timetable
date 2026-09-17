#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["reportlab>=4.0"]
# ///
"""Render a school iCal timetable feed as one A4 PDF per week of the timetable cycle.

Reads an iCal feed (EduLink One style: one VEVENT per lesson, no recurrence
rules), works out the repeating multi-week cycle from the data, and draws a
colour-coded grid per cycle week.

Usage:
    ./timetable.py                        # feed from $ICAL_TIMETABLE_URL
    ./timetable.py --ics saved.ics        # from a local file
    ./timetable.py --outdir out --portrait
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

TZ = ZoneInfo("Europe/London")

# --------------------------------------------------------------------------- #
# iCal parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Lesson:
    start: datetime  # local time
    end: datetime  # local time
    subject: str  # "Mathematics"
    group: str  # "8X"
    code: str  # "Ma2"
    room: str  # "K15 Maths"

    @property
    def day(self) -> date:
        return self.start.date()

    @property
    def slot(self) -> tuple[str, str]:
        return self.start.strftime("%H:%M"), self.end.strftime("%H:%M")


def unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding (a CRLF followed by one space or tab)."""
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def unescape(value: str) -> str:
    out = value.replace(r"\n", "\n").replace(r"\N", "\n")
    return re.sub(r"\\([,;\\])", r"\1", out)


def parse_ics(text: str) -> list[Lesson]:
    lessons: list[Lesson] = []
    current: dict[str, str] | None = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                lesson = build_lesson(current)
                if lesson:
                    lessons.append(lesson)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        name, _, value = line.partition(":")
        current[name.split(";")[0].upper()] = unescape(value)
    lessons.sort(key=lambda le: (le.start, le.subject))
    return lessons


def parse_dt(value: str) -> datetime | None:
    """Parse the DATE-TIME forms this kind of feed uses, returning local time."""
    for fmt, utc in (("%Y%m%dT%H%M%SZ", True), ("%Y%m%dT%H%M%S", False)):
        try:
            naive = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if utc:
            return naive.replace(tzinfo=timezone.utc).astimezone(TZ)
        return naive.replace(tzinfo=TZ)
    return None


# "Mathematics 8X/Ma2" -> subject "Mathematics", group "8X", code "Ma2"
TITLE_RE = re.compile(r"^(?P<subject>.*?)\s+(?P<group>\S+)/(?P<code>\S+)$")


def build_lesson(fields: dict[str, str]) -> Lesson | None:
    start = parse_dt(fields.get("DTSTART", ""))
    end = parse_dt(fields.get("DTEND", ""))
    title = (fields.get("SUMMARY") or fields.get("DESCRIPTION") or "").strip()
    if not start or not end or not title:
        return None
    match = TITLE_RE.match(title)
    if match:
        subject, group, code = (match["subject"], match["group"], match["code"])
    else:
        subject, group, code = title, "", ""
    return Lesson(start, end, tidy_subject(subject), group, code,
                  (fields.get("LOCATION") or "").strip())


def tidy_subject(subject: str) -> str:
    """Feeds shout some subject names; title-case those but keep real casing."""
    if subject.isupper() and len(subject) > 4:
        return subject.title()
    return subject


# --------------------------------------------------------------------------- #
# Timetable structure
# --------------------------------------------------------------------------- #


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def teaching_weeks(lessons: list[Lesson]) -> list[date]:
    """Mondays of the weeks that actually contain lessons, in order."""
    return sorted({monday_of(le.day) for le in lessons})


def detect_cycle_length(lessons: list[Lesson], weeks: list[date], limit: int = 4) -> int:
    """Smallest cycle length that explains the data.

    Weeks are indexed by their position in the *teaching* sequence, not by
    calendar week: a school holiday removes a week from the cycle rather than
    shifting it, so week-of-year parity drifts out of step after any break of
    an odd number of weeks.
    """
    index = {monday: i for i, monday in enumerate(weeks)}
    best, best_score = 1, -1.0
    for length in range(1, limit + 1):
        cells: dict[tuple, collections.Counter] = collections.defaultdict(collections.Counter)
        for le in lessons:
            key = (index[monday_of(le.day)] % length, le.day.weekday(), le.slot)
            cells[key][(le.subject, le.group, le.code, le.room)] += 1
        agree = sum(c.most_common(1)[0][1] for c in cells.values())
        score = agree / sum(sum(c.values()) for c in cells.values())
        # Prefer the shortest cycle that explains ~everything.
        if score > best_score + 1e-9:
            best, best_score = length, score
        if score > 0.995:
            return length
    return best


def period_rows(lessons: list[Lesson]) -> list[dict]:
    """Rows of the grid: every distinct time slot, plus the gaps between them."""
    slots = sorted({le.slot for le in lessons})
    longest = max(slot_minutes(s) for s in slots)
    rows: list[dict] = []
    number = 0
    for i, slot in enumerate(slots):
        if i:
            gap = to_minutes(slot[0]) - to_minutes(slots[i - 1][1])
            if gap >= 10:
                rows.append({"kind": "gap", "slot": (slots[i - 1][1], slot[0]),
                             "label": "Break" if gap < 25 else "Lunch"})
        # Short slots are registration/tutor time, not a numbered teaching period.
        if slot_minutes(slot) >= longest * 0.6:
            number += 1
            label = str(number)
        else:
            label = ""
        rows.append({"kind": "period", "slot": slot, "label": label})
    return rows


def to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def slot_minutes(slot: tuple[str, str]) -> int:
    return to_minutes(slot[1]) - to_minutes(slot[0])


def build_grid(lessons: list[Lesson], weeks: list[date], cycle: int):
    """Majority-vote timetable per cycle week, plus the one-off exceptions."""
    index = {monday: i for i, monday in enumerate(weeks)}
    # Count by identity (subject/group/code/room), then keep one Lesson per winner.
    tally: dict[tuple, collections.Counter] = collections.defaultdict(collections.Counter)
    sample: dict[tuple, dict] = collections.defaultdict(dict)
    for le in lessons:
        key = (index[monday_of(le.day)] % cycle, le.day.weekday(), le.slot)
        ident = (le.subject, le.group, le.code, le.room)
        tally[key][ident] += 1
        sample[key].setdefault(ident, le)

    grid: dict[tuple, Lesson] = {}
    winners: dict[tuple, tuple] = {}
    for key, counter in tally.items():
        winners[key] = counter.most_common(1)[0][0]
        grid[key] = sample[key][winners[key]]

    exceptions = [
        (le, grid[key])
        for le in lessons
        if (key := (index[monday_of(le.day)] % cycle, le.day.weekday(), le.slot))
        and (le.subject, le.group, le.code, le.room) != winners[key]
    ]
    exceptions.sort(key=lambda pair: pair[0].start)
    return grid, exceptions


def term_gaps(weeks: list[date]) -> list[tuple[date, date, int]]:
    """Holiday gaps as (last teaching Monday, next teaching Monday, weeks off)."""
    gaps = []
    for previous, nxt in zip(weeks, weeks[1:], strict=False):
        missing = (nxt - previous).days // 7 - 1
        if missing:
            gaps.append((previous, nxt, missing))
    return gaps


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #

# Subjects are coloured by *family*, so the sheet reads as groups at a glance;
# each subject within a family gets its own tint depth of the family hue. Every
# block also carries its subject name, so colour never has to carry identity on
# its own. The eight hues are the validated categorical set from the data-viz
# reference palette (adjacent-pair CVD and normal-vision floors both pass on a
# light surface); each family takes one slot and the set is never cycled.
NEUTRAL = "#6b6a66"

FAMILIES: list[tuple[str, str, str]] = [
    # name,                 hue,        subject pattern
    ("Maths", "#2a78d6", r"math"),
    ("Science", "#1baf7a", r"science|biolog|chemist|physics\b"),
    ("English", "#4a3aa7", r"english\b|literacy\b"),
    ("Languages", "#e87ba4", (r"french\b|latin\b|spanish\b|german\b|italian\b"
                              r"|mandarin\b|greek\b|language")),
    ("Humanities", "#eda100", (r"geograph|histor|societ|sustainab|religio|philosoph"
                               r"|classic|citizenship\b|pshe\b|economic")),
    ("Arts", "#eb6834", r"art\b|arts\b|drama\b|music\b|dance\b|media\b"),
    ("Design & practical", "#008300", (r"design|technolog|food\b|textile|comput|ict\b"
                                       r"|engineer|nutrition\b")),
    ("PE", "#e34948", r"physical education\b|games\b|sport|\bpe\b|swim"),
]
ADMIN_RE = re.compile(r"\btutor\b|\bregistration\b|\bassembly\b|\bform time\b")
TINTS = (0.82, 0.72, 0.62, 0.52)


def tint(hex_colour: str, amount: float) -> Color:
    """Blend a hue towards white; `amount` is how much white."""
    base = HexColor(hex_colour)
    return Color(*(component + (1 - component) * amount
                   for component in (base.red, base.green, base.blue)))


def family_of(subject: str) -> str | None:
    lowered = subject.lower()
    for name, _, pattern in FAMILIES:
        if re.search(rf"\b(?:{pattern})", lowered):
            return name
    return None


def family_order(subjects: list[str]) -> list[tuple[str, list[str]]]:
    """(family, members) in a fixed order: named families, then the leftovers."""
    grouped: dict[str, list[str]] = collections.defaultdict(list)
    for subject in subjects:
        grouped[family_of(subject) or subject].append(subject)
    named = [name for name, _, _ in FAMILIES if name in grouped]
    rest = sorted(key for key in grouped if key not in named)
    return [(family, sorted(grouped[family])) for family in named + rest]


def assign_colours(subjects: list[str]) -> dict[str, tuple[str, Color]]:
    """subject -> (accent hex, fill colour). Tutor/admin time stays neutral."""
    groups = family_order(subjects)
    known = {name for name, _, _ in FAMILIES}
    hues = {name: hue for name, hue, _ in FAMILIES}
    spare = [hue for name, hue, _ in FAMILIES if name not in dict(groups)]
    for family, members in groups:
        if family not in known and not all(is_admin(m) for m in members):
            hues[family] = spare.pop(0) if spare else NEUTRAL

    colours: dict[str, tuple[str, Color]] = {}
    for family, members in groups:
        hue = hues.get(family, NEUTRAL)
        for i, subject in enumerate(members):
            if is_admin(subject):
                colours[subject] = (NEUTRAL, tint(NEUTRAL, 0.88))
            else:
                colours[subject] = (hue, tint(hue, TINTS[min(i, len(TINTS) - 1)]))
    return colours


def is_admin(subject: str) -> bool:
    return bool(ADMIN_RE.search(subject.lower()))


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #

INK = HexColor("#1a1a19")
INK_SOFT = HexColor("#52514e")
INK_FAINT = HexColor("#8a8983")
RULE = HexColor("#dcdbd6")
MIN_SMALL_TEXT_CONTRAST = 4.5


def relative_luminance(colour: Color) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(colour.red) + 0.7152 * channel(colour.green)
            + 0.0722 * channel(colour.blue))


def contrast(a: Color, b: Color) -> float:
    high, low = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def detail_ink(fill: Color) -> Color:
    """The softer ink where it still clears WCAG AA for small text, else full ink."""
    return INK_SOFT if contrast(INK_SOFT, fill) >= MIN_SMALL_TEXT_CONTRAST else INK
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday", "Sunday")


def fit(text: str, font: str, size: float, width: float) -> str:
    """Truncate with an ellipsis so a long room or subject cannot overflow."""
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…" if text else ""


def wrap(text: str, font: str, size: float, width: float, max_lines: int) -> list[str]:
    """Greedy word wrap; the last line absorbs any overflow and is ellipsised."""
    words = text.split()
    lines: list[str] = []
    for i, word in enumerate(words):
        if lines and stringWidth(f"{lines[-1]} {word}", font, size) <= width:
            lines[-1] = f"{lines[-1]} {word}"
        elif len(lines) < max_lines:
            lines.append(word)
        else:
            lines[-1] = " ".join([lines[-1], *words[i:]])
            break
    return [fit(line, font, size, width) for line in lines]


def draw_week(pdf: canvas.Canvas, *, title: str, subtitle: str, footer: list[str],
              rows: list[dict], grid: dict, cycle_index: int, weekdays: list[int],
              colours: dict[str, tuple[str, Color]], counts: collections.Counter,
              page: tuple[float, float]) -> None:
    width, height = page
    margin = 12 * mm
    pdf.setTitle(title)
    pdf.setFillColor(HexColor("#ffffff"))
    pdf.rect(0, 0, width, height, stroke=0, fill=1)

    # --- heading ----------------------------------------------------------- #
    y = height - margin
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(margin, y - 15, title)
    pdf.setFont("Helvetica", 9.5)
    pdf.setFillColor(INK_SOFT)
    pdf.drawRightString(width - margin, y - 13, subtitle)
    y -= 24

    # --- geometry ---------------------------------------------------------- #
    legend_columns = 4 if width > 240 * mm else 3
    legend_rows = -(-len(counts) // legend_columns)
    footer_height = len(footer) * 9 + 6
    legend_height = 12 + legend_rows * 13 + footer_height
    grid_top = y - 4
    grid_bottom = margin + legend_height
    time_col = 21 * mm
    col_width = (width - 2 * margin - time_col) / len(weekdays)

    unit = sum(3.0 if row["kind"] == "period" and slot_minutes(row["slot"]) >= 40
               else 1.0 for row in rows)
    header_height = 16.0
    body_height = grid_top - grid_bottom - header_height
    for row in rows:
        big = row["kind"] == "period" and slot_minutes(row["slot"]) >= 40
        row["height"] = body_height * (3.0 if big else 1.0) / unit

    # --- day headers ------------------------------------------------------- #
    pdf.setFillColor(HexColor("#f2f1ee"))
    pdf.rect(margin, grid_top - header_height, width - 2 * margin, header_height,
             stroke=0, fill=1)
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.setFillColor(INK)
    for i, weekday in enumerate(weekdays):
        x = margin + time_col + i * col_width
        pdf.drawCentredString(x + col_width / 2, grid_top - header_height + 5,
                              DAYS[weekday])
    pdf.setStrokeColor(RULE)
    pdf.setLineWidth(0.6)
    pdf.line(margin, grid_top - header_height, width - margin, grid_top - header_height)

    pdf.setLineWidth(0.4)
    for i in range(len(weekdays) + 1):
        x = margin + time_col + i * col_width
        pdf.line(x, grid_top - header_height, x, grid_bottom)

    # --- rows -------------------------------------------------------------- #
    y = grid_top - header_height
    for row in rows:
        y -= row["height"]
        if row["kind"] == "gap":
            pdf.setFillColor(HexColor("#f7f6f3"))
            pdf.rect(margin, y, width - 2 * margin, row["height"], stroke=0, fill=1)
            pdf.setFillColor(INK_FAINT)
            pdf.setFont("Helvetica", 7.5)
            pdf.drawString(margin + 3, y + row["height"] / 2 - 2.6,
                           f"{row['slot'][0]}–{row['slot'][1]}")
            pdf.setFont("Helvetica-Oblique", 7.5)
            pdf.drawCentredString(margin + time_col + (width - 2 * margin - time_col) / 2,
                                  y + row["height"] / 2 - 2.6, row["label"])
            continue

        start, end = row["slot"]
        pdf.setFillColor(INK)
        if row["label"]:
            pdf.setFont("Helvetica-Bold", 10)
            pdf.drawString(margin + 3, y + row["height"] - 12, f"Period {row['label']}")
            pdf.setFont("Helvetica", 8.5)
            pdf.setFillColor(INK_SOFT)
            pdf.drawString(margin + 3, y + row["height"] - 22, f"{start}–{end}")
        else:
            pdf.setFont("Helvetica", 8.5)
            pdf.setFillColor(INK_SOFT)
            pdf.drawString(margin + 3, y + row["height"] / 2 - 2.8, f"{start}–{end}")

        for i, weekday in enumerate(weekdays):
            lesson = grid.get((cycle_index, weekday, row["slot"]))
            x = margin + time_col + i * col_width
            draw_cell(pdf, lesson, x + 1, y + 1, col_width - 2, row["height"] - 2, colours)

        pdf.setStrokeColor(RULE)
        pdf.setLineWidth(0.4)
        pdf.line(margin, y, width - margin, y)

    # --- legend ------------------------------------------------------------ #
    draw_legend(pdf, margin, margin + legend_height - 12, width - 2 * margin,
                colours, counts, legend_columns)
    pdf.setFillColor(INK_FAINT)
    text_y = margin + footer_height - 12
    for line in footer:
        size = 7.0
        while size > 5.0 and stringWidth(line, "Helvetica", size) > width - 2 * margin:
            size -= 0.25
        pdf.setFont("Helvetica", size)
        pdf.drawString(margin, text_y, fit(line, "Helvetica", size, width - 2 * margin))
        text_y -= 9
    pdf.showPage()


def draw_cell(pdf, lesson, x, y, width, height, colours) -> None:
    if lesson is None:
        return
    accent, fill = colours[lesson.subject]
    pdf.setFillColor(fill)
    pdf.roundRect(x, y, width, height, 2, stroke=0, fill=1)
    pdf.setFillColor(HexColor(accent))
    pdf.rect(x, y, 2.6, height, stroke=0, fill=1)

    pad = 6.0
    inner = width - pad - 4
    short = height < 24
    size = 9.0 if short else 10.0
    max_lines = 1 if short else min(3, int((height - 2 * pad - 12) // (size + 1)))
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", size)
    lines = wrap(lesson.subject, "Helvetica-Bold", size, inner, max(1, max_lines))
    text_y = y + height - pad - size + 2 if not short else y + height / 2 + 1
    for line in lines:
        pdf.drawString(x + pad, text_y, line)
        text_y -= size + 1

    detail = " · ".join(part for part in
                        (f"{lesson.group}/{lesson.code}" if lesson.code else lesson.group,
                         lesson.room) if part)
    if not detail:
        return
    size = 7.6 if short else 8.2
    pdf.setFillColor(detail_ink(fill))
    pdf.setFont("Helvetica", size)
    if short:
        pdf.drawString(x + pad, y + height / 2 - 8, fit(detail, "Helvetica", size, inner))
        return
    detail_lines = wrap(detail, "Helvetica", size, inner, 2)
    text_y = y + pad + (len(detail_lines) - 1) * (size + 1.5)
    for line in detail_lines:
        pdf.drawString(x + pad, text_y, line)
        text_y -= size + 1.5


def draw_legend(pdf, x, top, width, colours, counts, columns) -> None:
    pdf.setFont("Helvetica-Bold", 8)
    pdf.setFillColor(INK_SOFT)
    pdf.drawString(x, top, "Subjects, grouped by colour  (lessons this week)")
    col_width = width / columns
    # Same order as the colours were handed out, so each hue family reads together.
    entries = [(subject, counts[subject])
               for _, members in family_order(sorted(counts))
               for subject in members]
    y = top - 12
    for i, (subject, count) in enumerate(entries):
        col, rowi = i % columns, i // columns
        cx = x + col * col_width
        cy = y - rowi * 13
        accent, fill = colours[subject]
        pdf.setFillColor(fill)
        pdf.setStrokeColor(RULE)
        pdf.setLineWidth(0.4)
        pdf.roundRect(cx, cy - 2.5, 17, 10, 1.5, stroke=1, fill=1)
        pdf.setFillColor(HexColor(accent))
        pdf.rect(cx + 0.2, cy - 2.3, 2.6, 9.6, stroke=0, fill=1)
        pdf.setFillColor(INK)
        pdf.setFont("Helvetica", 8)
        label = f"{subject}  ({count})"
        pdf.drawString(cx + 22, cy, fit(label, "Helvetica", 8, col_width - 28))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "timetable.py"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def cycle_label(index: int, total: int) -> str:
    if total <= 1:
        return "Timetable"
    if total == 2:
        return f"Week {'AB'[index]}"
    return f"Week {index + 1}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", default=os.environ.get("ICAL_TIMETABLE_URL"),
                        help="iCal feed URL (default: $ICAL_TIMETABLE_URL)")
    source.add_argument("--ics", type=Path, help="read a saved .ics file instead")
    parser.add_argument("--outdir", type=Path, default=Path("out"))
    parser.add_argument("--portrait", action="store_true",
                        help="A4 portrait instead of landscape")
    parser.add_argument("--cycle", type=int, help="force the cycle length in weeks")
    parser.add_argument("--swap-weeks", action="store_true",
                        help="swap which cycle week is labelled A and which B")
    parser.add_argument("--save-ics", type=Path, help="also write the raw feed here")
    args = parser.parse_args()

    if args.ics:
        text = args.ics.read_text()
    elif args.url:
        text = fetch(args.url)
    else:
        parser.error("no feed: set ICAL_TIMETABLE_URL, or pass --url / --ics")
    if args.save_ics:
        args.save_ics.write_text(text)

    lessons = parse_ics(text)
    if not lessons:
        print("No events found in the feed.", file=sys.stderr)
        return 1

    weeks = teaching_weeks(lessons)
    cycle = args.cycle or detect_cycle_length(lessons, weeks)
    grid, exceptions = build_grid(lessons, weeks, cycle)
    rows = period_rows(lessons)
    weekdays = sorted({le.day.weekday() for le in lessons})
    colours = assign_colours(sorted({le.subject for le in lessons}))

    order = list(range(cycle))
    if args.swap_weeks and cycle == 2:
        order.reverse()

    page = A4 if args.portrait else landscape(A4)
    args.outdir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(TZ).strftime("%-d %b %Y")
    group = collections.Counter(le.group for le in lessons
                                if not is_admin(le.subject)).most_common(1)[0][0]

    written = []
    for position, cycle_index in enumerate(order):
        label = cycle_label(position, cycle)
        mondays = [monday for i, monday in enumerate(weeks) if i % cycle == cycle_index]
        counts = collections.Counter(
            lesson.subject for (ci, _, _), lesson in grid.items() if ci == cycle_index)
        dates = ", ".join(monday.strftime("%-d %b") for monday in mondays)
        path = args.outdir / f"timetable-{label.lower().replace(' ', '-')}.pdf"
        pdf = canvas.Canvas(str(path), pagesize=page)
        draw_week(
            pdf,
            title=f"{label}   ·   {group}",
            subtitle=f"{len(mondays)} weeks, from {mondays[0].strftime('%-d %B %Y')}",
            footer=[f"Weeks commencing: {dates}",
                    (f"Generated {generated} from the school's EduLink iCal feed. "
                     "Room and set codes as published; check EduLink for changes.")],
            rows=rows, grid=grid, cycle_index=cycle_index, weekdays=weekdays,
            colours=colours, counts=counts, page=page,
        )
        pdf.save()
        written.append((path, label, mondays))

    report(lessons, weeks, cycle, exceptions, written)
    return 0


def report(lessons, weeks, cycle, exceptions, written) -> None:
    print(f"Parsed {len(lessons)} lessons over {len(weeks)} teaching weeks "
          f"({weeks[0]:%-d %b %Y} – {max(le.day for le in lessons):%-d %b %Y})")
    print(f"Detected a {cycle}-week repeating cycle.\n")
    for path, label, mondays in written:
        print(f"  {path}  —  {label}, {len(mondays)} weeks "
              f"(w/c {', '.join(m.strftime('%-d %b') for m in mondays)})")
    gaps = term_gaps(weeks)
    if gaps:
        print("\nHoliday gaps (no lessons):")
        for previous, nxt, missing in gaps:
            print(f"  {missing} week{'s' if missing > 1 else ''} after w/c "
                  f"{previous:%-d %b %Y}  →  resumes w/c {nxt:%-d %b %Y}")
    if exceptions:
        print("\nOne-off differences from the regular pattern:")
        for odd, usual in exceptions:
            changed = ("room", odd.room, usual.room) if odd.subject == usual.subject \
                else ("lesson", f"{odd.subject} @ {odd.room}",
                      f"{usual.subject} @ {usual.room}")
            print(f"  {odd.start:%a %-d %b %Y} {odd.start:%H:%M}  "
                  f"{changed[0]}: {changed[1]}  (usually {changed[2]})")


if __name__ == "__main__":
    raise SystemExit(main())
