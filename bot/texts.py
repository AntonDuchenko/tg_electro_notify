"""Тексты сообщений бота (украинский — как на сайте облэнерго)."""

from __future__ import annotations

from datetime import date

from .poe import MAYBE, OFF, ON, DaySchedule, intervals

_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
_ICON = {ON: "🟢", OFF: "🔴", MAYBE: "🟡"}


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def day_title(d: date) -> str:
    return f"{d.strftime('%d.%m')} ({_WEEKDAYS[d.weekday()]})"


def render_day(sched: DaySchedule | None, queue: str, d: date) -> str:
    head = f"📅 <b>{day_title(d)}</b>, черга <b>{queue}</b>\n"
    if sched is None:
        return head + "Графіка ще немає (обленерго не опублікувало або сайт не відповідає)."
    if sched.no_outages:
        return head + "✅ Відключення не прогнозуються."
    slots = sched.slots(queue)
    if slots is None:
        return head + "У графіку немає цієї черги."

    lines = []
    off_total = 0
    for iv in intervals(slots):
        if iv.state == ON:
            continue
        mins = iv.end_min - iv.start_min
        if iv.state == OFF:
            off_total += mins
        label = "немає світла" if iv.state == OFF else "можливе відключення"
        lines.append(f"{_ICON[iv.state]} {hhmm(iv.start_min)}–{hhmm(iv.end_min)}  {label} ({mins // 60}:{mins % 60:02d})")
    if not lines:
        return head + "✅ Для вашої черги відключень немає."
    total = f"\nБез світла: ~{off_total // 60} год {off_total % 60:02d} хв"
    return head + "\n".join(lines) + "\n" + _hour_strip(slots) + total


def _hour_strip(slots: list[str]) -> str:
    """Компактная «линейка» по часам: 🟢🔴... (худшее состояние за час)."""
    per_hour = max(1, len(slots) // 24)
    rank = {ON: 0, MAYBE: 1, OFF: 2}
    icons = []
    for h in range(24):
        chunk = slots[h * per_hour:(h + 1) * per_hour] or [ON]
        icons.append(_ICON[max(chunk, key=rank.__getitem__)])
    rows = [f"{h:02d}–{h + 5:02d} " + "".join(icons[h:h + 6]) for h in range(0, 24, 6)]
    return "\n" + "\n".join(rows)


WELCOME = (
    "Привіт! Я показую графіки відключень світла Полтаваобленерго "
    "і пишу, коли графік для вашої черги змінюється.\n\n"
    "Оберіть свою чергу — її можна дізнатися на сайті poe.pl.ua "
    "або в особовому кабінеті."
)
