"""Formatting helpers for the Weio Ledger CLI views."""
from __future__ import annotations

from datetime import date, datetime


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _short_date(value) -> str:
    if not value:
        return ""
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date().isoformat()
        except ValueError:
            pass
    return text[:10]


def _is_overdue(entry: dict) -> bool:
    due = entry.get("due_date")
    if not due:
        return False
    try:
        return date.fromisoformat(str(due)[:10]) < date.today()
    except ValueError:
        return False


def _clip(value, width: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "."


def _table(headers: list[str], rows: list[list[str]], widths: list[int]) -> str:
    if not rows:
        return ""
    lines = []
    header = "  ".join(_clip(h, w).ljust(w) for h, w in zip(headers, widths))
    lines.append(header.rstrip())
    lines.append("  ".join("-" * w for w in widths).rstrip())
    for row in rows:
        lines.append("  ".join(_clip(v, w).ljust(w) for v, w in zip(row, widths)).rstrip())
    return "\n".join(lines)


def format_rollup(data: dict) -> str:
    totals = data.get("totals") or {}
    scope = data.get("scope") or "ledger"
    lines = [
        f"Ledger ({scope})",
        f"Open: {_fmt_int(totals.get('open'))}  "
        f"Overdue: {_fmt_int(totals.get('overdue'))}  "
        f"Proposed: {_fmt_int(totals.get('proposed'))}",
    ]

    departments = data.get("departments") or []
    if departments:
        rows = [
            [d.get("department") or "(none)", _fmt_int(d.get("open")), _fmt_int(d.get("overdue"))]
            for d in departments[:12]
        ]
        lines.extend(["", "Departments", _table(["department", "open", "overdue"], rows, [24, 6, 7])])

    people = data.get("people") or []
    if people:
        rows = [
            [
                p.get("name") or "(unassigned)",
                p.get("department") or "",
                _fmt_int(p.get("open")),
                _fmt_int(p.get("overdue")),
            ]
            for p in people[:12]
        ]
        lines.extend(["", "People", _table(["person", "department", "open", "overdue"], rows, [22, 18, 6, 7])])
    return "\n".join(line for line in lines if line is not None)


def format_entries(entries: list[dict], *, title: str = "Entries") -> str:
    if not entries:
        return f"{title}\nNo ledger entries found."
    rows = []
    for e in entries:
        state = e.get("state") or ""
        if _is_overdue(e) and state == "open":
            state = "overdue"
        rows.append([
            e.get("kind") or "",
            state,
            e.get("priority") or "",
            e.get("assignee_name") or "",
            e.get("department") or "",
            _short_date(e.get("due_date")),
            e.get("title") or "",
        ])
    return "\n".join([
        title,
        _table(
            ["kind", "state", "prio", "assignee", "department", "due", "title"],
            rows,
            [12, 9, 5, 18, 16, 10, 44],
        ),
    ])


def format_ledger_view(rollup: dict | None, entries: list[dict], *, entries_title: str = "Open entries") -> str:
    sections = []
    if rollup:
        sections.append(format_rollup(rollup))
    sections.append(format_entries(entries, title=entries_title))
    return "\n\n".join(sections)
