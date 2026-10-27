"""
DALYN weekly report builder.

Reads DALYN's log files for one Mon-Fri week and writes the weekly Word report
(landscape, totals grid, by-day email breakdown, attachment breakdown).

Usage:
    python dalyn_weekly_report.py                         # last full Mon-Fri week, logs in .\\logs
    python dalyn_weekly_report.py --week 2026-10-19       # any date in the week you want
    python dalyn_weekly_report.py --logs "C:\\path\\to\\logs" --out "C:\\Reports"
    python dalyn_weekly_report.py --from-json counts.json  # skip the logs, use numbers you already have

Needs: pip install python-docx

How it counts (same rules as the hand-built reports):
  - One email = one Graph message ID, counted on each day it was tagged, by its
    last tag that day. A day where its last tag is "Waiting for STAC" doesn't
    count; it counts on the day it gets a final tag. Emails started but never
    tagged (pass broke off) are left out.
  - Auto-filed = that tag includes "DALYN: Filed" (moved to the done folder).
  - Attachments = the count in the email's outcome line ("SINGLE (1 attachment)").
  - Entered = every file named in a "saved email N attachment M" line, counted
    once per attachment, on the day it was saved. "Check STAC first" saves were
    never confirmed, so they are not counted as entered.
  - Type/Subtype vs PLS RVW comes from the "under X/Y" part of the save line.
Check each day against STAC's StacUploader Statistics report before sending.
Its end date is exclusive: for Mon 10/19 to Fri 10/23 enter 10/19 to 10/24.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

LOG_NAME = re.compile(r"^dalyn\.log(\.\d{4}-\d{2}-\d{2})?$")

RE_PASS = re.compile(r"\| dalyn\.main \| Pass:")
RE_MSG = re.compile(r"\| dalyn\.main \| (\S+@\S+) message \d+ of \d+ \(email (\d+)\), received (\S+)")
RE_ID = re.compile(r"\| dalyn\.graph_client \| Message (\S+): ")
RE_SAVED = re.compile(r"\| dalyn\.stac \| (\S+): saved (email \d+ attachment \d+(?:, email \d+ attachment \d+)*) under (.*)$")
RE_TAG = re.compile(r"\| dalyn\.tagging \| Email (\d+): tagged (.*)$")
RE_OUT = re.compile(r"\| dalyn\.main \| Email (\d+): \w+ (?:\((\d+) attachments?\) )?-> ")


# ---------------------------------------------------------------- counting

def week_bounds(any_day: dt.date) -> list[dt.date]:
    """Return Monday to Friday of the week holding any_day."""
    monday = any_day - dt.timedelta(days=any_day.weekday())
    return [monday + dt.timedelta(days=i) for i in range(5)]


def last_full_week(today: dt.date) -> dt.date:
    """Return the Monday of the most recent Mon-Fri week that has fully ended."""
    monday = today - dt.timedelta(days=today.weekday())
    if today.weekday() < 5:  # in the middle of a work week, use the previous one
        monday -= dt.timedelta(days=7)
    return monday


def find_logs(folder: str) -> list[str]:
    """Return the DALYN log files in folder, oldest first by their first line."""
    files = [f for f in glob.glob(os.path.join(folder, "*")) if LOG_NAME.match(os.path.basename(f))]
    if not files:
        sys.exit(f"No dalyn.log files found in {folder}")

    def first_ts(path: str) -> str:
        """Return the timestamp on the file's first line."""
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readline()[:19]
    return sorted(files, key=first_ts)


def count_week(log_files: list[str], days: list[dt.date]) -> dict:
    """Count emails, attachments and saves per day for the given days.

    Returns {"days": [...], "reasons": {...}, "notes": {...}}, the same shape
    --from-json reads.
    """
    cur = {}                     # email number in this pass -> message id
    pending = None
    mailbox = {}                 # id -> (mailbox, received)
    started = set()
    last = defaultdict(dict)     # day -> id -> {"tag": ..., "n": attachments in last outcome}
    saves = {}                   # (id, att_no) -> (day, "TYPE/SUBTYPE"), first save only

    for path in log_files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line_no, ln in enumerate(fh, 1):
                ln = ln.rstrip("\n")
                day = ln[:10]
                if RE_PASS.search(ln):
                    cur = {}
                    pending = None
                    continue
                m = RE_MSG.search(ln)
                if m:
                    pending = (m.group(2), m.group(1), m.group(3))
                    # Some emails are tagged without a "Message <id>:" line ever
                    # being written, like the one tagged Interrupted on 2026-10-19.
                    # Key them by where they appear until the real ID shows up.
                    stand_in = f"{path}:{line_no}"
                    cur[m.group(2)] = stand_in
                    mailbox[stand_in] = (m.group(1), m.group(3))
                    continue
                m = RE_ID.search(ln)
                if m and pending:
                    num, mb, rcv = pending
                    cur[num] = m.group(1)
                    mailbox[m.group(1)] = (mb, rcv)
                    started.add(m.group(1))
                    pending = None
                    continue
                m = RE_OUT.search(ln)
                if m and m.group(1) in cur:
                    last[day].setdefault(cur[m.group(1)], {})["n"] = int(m.group(2) or 0)
                    continue
                m = RE_SAVED.search(ln)
                if m:
                    for num, att in re.findall(r"email (\d+) attachment (\d+)", m.group(2)):
                        if num in cur:
                            saves.setdefault((cur[num], att), (day, m.group(3).strip()))
                    continue
                m = RE_TAG.search(ln)
                if m and m.group(1) in cur:
                    last[day].setdefault(cur[m.group(1)], {})["tag"] = m.group(2)

    out = {"days": []}
    reasons = Counter()
    cross = 0
    tagged_ever = {i for d in last.values() for i, v in d.items() if "tag" in v}
    for d in days:
        key = d.isoformat()
        c = Counter()
        # an email counts on each day it was tagged, by its last tag that day,
        # unless that tag is "Waiting for STAC" (it counts later, at its final tag)
        todays = {i: v for i, v in last.get(key, {}).items()
                  if "tag" in v and "Waiting for STAC" not in v["tag"]}
        for i, v in todays.items():
            c["emails"] += 1
            c["attachments"] += v.get("n", 0)
            if "DALYN: Filed" in v["tag"]:
                c["autofiled"] += 1
            else:
                reasons[v["tag"].split(",")[0].replace("DALYN:", "").strip()] += 1
        for (_, _), (sday, typ) in saves.items():
            if sday == key:
                c["entered"] += 1
                c["pls" if typ.upper().startswith("PLS RVW") else "typed"] += 1
        groups = defaultdict(set)
        for i in todays:
            groups[mailbox[i][1]].add(i)
        cross += sum(len(g) - 1 for g in groups.values() if len({mailbox[i][0] for i in g}) > 1)
        if c["emails"] or c["entered"]:
            out["days"].append({"day": f"{d:%a} {d.month}/{d.day}",
                                "emails": c["emails"], "autofiled": c["autofiled"],
                                "attachments": c["attachments"], "entered": c["entered"],
                                "typed": c["typed"], "pls": c["pls"]})
    out["reasons"] = dict(reasons.most_common())
    out["notes"] = {"possible_cross_mailbox_duplicates": cross,
                    "started_never_tagged": len(started - tagged_ever)}
    return out


# ---------------------------------------------------------------- report

def build_docx(data: dict, start: dt.date, end: dt.date, out_path: str) -> tuple[int, int, int, int, int, int]:
    """Write the weekly Word report and return the week's totals.

    Returns (emails, auto-filed, attachments, entered, typed, PLS RVW).
    """
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    days = data["days"]
    if not days:
        sys.exit("No emails found for that week.")
    E = sum(d["emails"] for d in days)
    A = sum(d["autofiled"] for d in days)
    ATT = sum(d["attachments"] for d in days)
    ENT = sum(d["entered"] for d in days)
    TYP = sum(d["typed"] for d in days)
    PLS = sum(d["pls"] for d in days)

    def pct(a: int, b: int) -> str:
        """Return a as a percent of b, one decimal place."""
        return f"{100 * a / b:.1f}%" if b else "0.0%"

    def n(x: int) -> str:
        """Return x with thousands commas."""
        return f"{x:,}"

    best = max(days, key=lambda d: d["autofiled"] / d["emails"])
    full = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday"}
    rng = f"{start:%b} {start.day} to {end.day}" if start.month == end.month else f"{start:%b} {start.day} to {end:%b} {end.day}"

    doc = Document()
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Inches(11), Inches(8.5)
    sec.left_margin = sec.right_margin = Inches(1)
    sec.top_margin = sec.bottom_margin = Inches(0.7)
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")

    def para(text: str, bold: bool = False, after: int = 6):
        """Add a paragraph with the given space after, in points."""
        p = doc.add_paragraph()
        r = p.add_run(text)
        r.bold = bold
        p.paragraph_format.space_after = Pt(after)
        return p

    def set_borders(cell, val: str = "single", sz: int = 4) -> None:
        """Set all four borders of a cell; val "nil" removes them."""
        tcPr = cell._tc.get_or_add_tcPr()
        for old in tcPr.findall(qn("w:tcBorders")):
            tcPr.remove(old)
        b = OxmlElement("w:tcBorders")
        for edge in ("top", "left", "bottom", "right"):
            e = OxmlElement(f"w:{edge}")
            e.set(qn("w:val"), val)
            if val != "nil":
                e.set(qn("w:sz"), str(sz))
                e.set(qn("w:color"), "000000")
            b.append(e)
        tcPr.append(b)

    def fixed(table, widths: list[float]) -> None:
        """Pin a table's column widths, in inches, so Word does not autofit them."""
        tblPr = table._tbl.tblPr
        lay = OxmlElement("w:tblLayout")
        lay.set(qn("w:type"), "fixed")
        tblPr.append(lay)
        table.autofit = False
        for gc, w in zip(table._tbl.tblGrid.findall(qn("w:gridCol")), widths):
            gc.set(qn("w:w"), str(int(w * 1440)))
        for row in table.rows:
            for c, w in enumerate(widths):
                row.cells[c].width = Inches(w)

    def write(cell, text: str, bold: bool = False, size: float = 12, center: bool = False) -> None:
        """Replace a cell's text with one run."""
        cell.text = ""
        p = cell.paragraphs[0]
        r = p.add_run(text)
        r.bold = bold
        r.font.size = Pt(size)
        if center:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    para(f"DALYN Weekly Report, {rng}", bold=True)
    para(f"DALYN filed {n(A)} of {n(E)} emails ({pct(A, E)}) into live STAC, "
         f"{start:%a %b} {start.day} to {end:%a %b} {end.day}, with no person touching them. "
         f"{full[best['day'][:3]]}'s filing rate was the best of the week at {pct(best['autofiled'], best['emails'])}.",
         after=12)
    para("Totals for the week", bold=True, after=4)

    t = doc.add_table(rows=4, cols=4)
    t.style = "Table Grid"
    fixed(t, [2.25] * 4)
    grid = [
        ["Emails processed", "Emails auto-filed", "Emails left in Inbox after 2 days", ""],
        [n(E), f"{n(A)} ({pct(A, E)})", f"{n(E - A)} ({pct(E - A, E)})", ""],
        ["Attachments processed in Inbox", "Attachments auto-filed", "Attachment images reviewed (PLS RVW)", "Attachments left in Inbox"],
        [f"{n(ATT)} (100%)", f"{n(TYP)} ({pct(TYP, ATT)})", f"{n(PLS)} ({pct(PLS, ATT)})", f"{n(ATT - ENT)} ({pct(ATT - ENT, ATT)})"],
    ]
    for r, row in enumerate(grid):
        t.rows[r].height = Inches(0.45)
        for c, v in enumerate(row):
            write(t.cell(r, c), v, bold=r in (0, 2))

    para("", after=6)

    # One 9-column table: 4 email columns, a blank gap, 4 attachment columns.
    erows = [["Day", "Emails", "Auto-filed", "Staff manual review"]]
    arows = [["Total Attachments", "Attachments Uploaded", "Attachments Classified", "Attachments Marked PLS RVW"]]
    for d in days:
        man = d["emails"] - d["autofiled"]
        erows.append([d["day"], n(d["emails"]), f"{n(d['autofiled'])} ({pct(d['autofiled'], d['emails'])})", f"{n(man)} ({pct(man, d['emails'])})"])
        arows.append([n(d["attachments"]), f"{n(d['entered'])} ({pct(d['entered'], d['attachments'])})",
                      f"{n(d['typed'])} ({pct(d['typed'], d['entered'])})", f"{n(d['pls'])} ({pct(d['pls'], d['entered'])})"])
    erows.append(["Week", n(E), f"{n(A)} ({pct(A, E)})", f"{n(E - A)} ({pct(E - A, E)})"])
    arows.append([n(ATT), f"{n(ENT)} ({pct(ENT, ATT)})", f"{n(TYP)} ({pct(TYP, ENT)})", f"{n(PLS)} ({pct(PLS, ENT)})"])

    nrows = len(erows) + 1
    widths = [0.9, 0.7, 1.3, 1.25, 0.25, 1.0, 1.2, 1.2, 1.2]
    bt = doc.add_table(rows=nrows, cols=9)
    fixed(bt, widths)
    last = nrows - 1

    # title row
    left = bt.cell(0, 0).merge(bt.cell(0, 3))
    right = bt.cell(0, 5).merge(bt.cell(0, 8))
    write(left, "BY DAY EMAIL BREAKDOWN", bold=True, center=True)
    write(right, "ATTACHMENT BREAKDOWN", bold=True, center=True)
    for cell in (left, right, bt.cell(0, 4)):
        set_borders(cell, "nil")
    bt.rows[0].height = Inches(0.4)

    for i in range(len(erows)):
        r = i + 1
        bt.rows[r].height = Inches(0.6 if i == 0 else 0.3)
        for c in range(9):
            cell = bt.cell(r, c)
            if c == 4:
                write(cell, "")
                set_borders(cell, "nil")
                continue
            text = erows[i][c] if c < 4 else arows[i][c - 5]
            write(cell, text, bold=(r == last), size=10.5)
            set_borders(cell, "single", 18 if r == last else 4)

    # keep the trailing paragraph Word requires after a table from pushing a blank page
    tail = doc.add_paragraph()
    tail.paragraph_format.space_after = Pt(0)
    tail.paragraph_format.space_before = Pt(0)
    for r in tail.runs: r.font.size = Pt(1)
    tail.style.font.size = Pt(12)
    doc.save(out_path)
    return E, A, ATT, ENT, TYP, PLS


# ---------------------------------------------------------------- main

def main() -> None:
    """Parse the arguments, count the week, write the report and print a summary."""
    ap =argparse.ArgumentParser(description="Build the DALYN weekly Word report.")
    ap.add_argument("--week", help="any date in the week (YYYY-MM-DD). Default: last full Mon-Fri week")
    ap.add_argument("--logs", default="logs", help="folder holding dalyn.log and dalyn.log.YYYY-MM-DD")
    ap.add_argument("--out", default=".", help="folder to write the .docx into")
    ap.add_argument("--from-json", help="build from a counts JSON instead of the logs")
    ap.add_argument("--save-json", action="store_true", help="also write the counts next to the report")
    args = ap.parse_args()

    if args.week:
        monday = week_bounds(dt.date.fromisoformat(args.week))[0]
    else:
        monday = last_full_week(dt.date.today())
    days = week_bounds(monday)
    start, end = days[0], days[-1]

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        logs = find_logs(args.logs)
        data = count_week(logs, days)

    name = f"DALYN Weekly Report {start:%b} {start.day}-{end:%b} {end.day}.docx" if start.month != end.month \
        else f"DALYN Weekly Report {start:%b} {start.day}-{end.day}.docx"
    out_path = os.path.join(args.out, name)
    E, A, ATT, ENT, TYP, PLS = build_docx(data, start, end, out_path)

    if args.save_json:
        with open(out_path.replace(".docx", ".json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    print(f"Week {start} to {end}")
    for d in data["days"]:
        print(f"  {d['day']:<10} emails {d['emails']:>5}  auto-filed {d['autofiled']:>5}  "
              f"attachments {d['attachments']:>5}  entered {d['entered']:>5}  type {d['typed']:>5}  pls {d['pls']:>4}")
    print(f"  {'Week':<10} emails {E:>5}  auto-filed {A:>5}  attachments {ATT:>5}  entered {ENT:>5}  type {TYP:>5}  pls {PLS:>4}")
    if data.get("reasons"):
        print("Manual review reasons:", ", ".join(f"{k} {v}" for k, v in data["reasons"].items()))
    notes = data.get("notes", {})
    if notes.get("possible_cross_mailbox_duplicates"):
        print(f"Heads up: {notes['possible_cross_mailbox_duplicates']} email(s) look like the same email in two mailboxes (counted twice).")
    if notes.get("started_never_tagged"):
        print(f"Left out: {notes['started_never_tagged']} email(s) started but never tagged.")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
