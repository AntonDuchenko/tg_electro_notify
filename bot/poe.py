"""Получение и разбор графиков погодинних відключень (ГПВ) с сайта Полтаваобленерго.

Сайт отдаёт HTML-фрагмент по POST-запросу на /customs/newgpv-info.php.
В ответе один или несколько блоков ``div.gpvinfodetail`` (обычно «сегодня» и
«завтра»). В каждом блоке дата в теге ``<b>`` («8 листопада 2026 року») и,
если отключения есть, таблица ``table.turnoff-scheduleui-table``:

* ``td.turnoff-scheduleui-table-queue``    — номер очереди (1..6);
* ``td.turnoff-scheduleui-table-subqueue`` — номер подочереди (1..2);
* ячейки с классами ``light_1`` / ``light_2`` / ``light_3`` — «свет есть» /
  «отключено» / «возможно отключение», по одной на интервал (обычно 30 минут).

Структура восстановлена по чужим открытым парсерам, а не проверена вживую:
если сайт поменяет разметку, ``parse_html`` вернёт пустой список, и бот
сохранит последний удачный график.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

URL = "https://www.poe.pl.ua/customs/newgpv-info.php"
REFERER = "https://www.poe.pl.ua/disconnection/power-outages/"
USER_AGENT = "tg_electro_notify/1.0 (+https://github.com/AntonDuchenko/tg_electro_notify)"

QUEUES = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]

ON, OFF, MAYBE = "on", "off", "maybe"
_CLASS_TO_STATE = {"light_1": ON, "light_2": OFF, "light_3": MAYBE}

_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})", re.I)
NO_OUTAGES_MARK = "не прогнозується"


@dataclass
class DaySchedule:
    day: date
    # "1.1" -> ["on", "off", ...]; пусто, если отключений нет
    queues: dict[str, list[str]] = field(default_factory=dict)
    no_outages: bool = False
    note: str = ""

    def slots(self, queue: str) -> list[str] | None:
        if self.no_outages:
            return [ON] * 48
        return self.queues.get(queue)

    def fingerprint(self, queue: str) -> str:
        """Хэш графика конкретной очереди — для поиска изменений."""
        payload = json.dumps({"no": self.no_outages, "s": self.slots(queue)})
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class Interval:
    start_min: int  # минуты от начала суток
    end_min: int
    state: str


def parse_ua_date(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    return date(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))


def parse_html(html: str) -> list[DaySchedule]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[DaySchedule] = []
    for block in soup.select("div.gpvinfodetail"):
        day = None
        for b in block.find_all("b"):
            day = parse_ua_date(b.get_text(" ", strip=True))
            if day:
                break
        if day is None:
            continue
        text = block.get_text(" ", strip=True)
        sched = DaySchedule(day=day)
        table = block.select_one("table.turnoff-scheduleui-table")
        if table is not None:
            sched.queues = _parse_table(table)
        if not sched.queues and NO_OUTAGES_MARK in text:
            sched.no_outages = True
        if not sched.queues and not sched.no_outages:
            # Незнакомый формат — не выдумываем «света нет/есть».
            log.warning("Блок за %s без таблицы и без пометки «не прогнозується»", day)
            continue
        sched.note = text.split("Черга")[0].strip()[:500]
        result.append(sched)
    return result


def _parse_table(table) -> dict[str, list[str]]:
    queues: dict[str, list[str]] = {}
    body = table.find("tbody") or table
    current_queue: str | None = None
    for row in body.find_all("tr"):
        q_cell = row.select_one("td.turnoff-scheduleui-table-queue")
        if q_cell is not None:
            current_queue = _digits(q_cell.get_text())
        s_cell = row.select_one("td.turnoff-scheduleui-table-subqueue")
        states = []
        for td in row.find_all("td"):
            for cls in td.get("class", []):
                if cls in _CLASS_TO_STATE:
                    states.append(_CLASS_TO_STATE[cls])
                    break
        if not states:
            continue
        if current_queue and s_cell is not None:
            key = f"{current_queue}.{_digits(s_cell.get_text())}"
        else:
            # Запасной вариант: строки идут по порядку 1.1, 1.2, 2.1 ...
            idx = len(queues)
            key = QUEUES[idx] if idx < len(QUEUES) else f"?{idx}"
        queues[key] = states
    return queues


def _digits(text: str) -> str:
    m = re.search(r"\d+", text)
    return m.group(0) if m else text.strip()


def intervals(slots: list[str]) -> list[Interval]:
    """Склеивает соседние ячейки с одинаковым состоянием в интервалы."""
    if not slots:
        return []
    step = 1440 // len(slots)
    out: list[Interval] = []
    for i, st in enumerate(slots):
        if out and out[-1].state == st:
            out[-1].end_min = (i + 1) * step
        else:
            out.append(Interval(i * step, (i + 1) * step, st))
    return out


async def fetch(client: httpx.AsyncClient, day: date) -> str:
    resp = await client.post(
        URL,
        data={"seldate": json.dumps({"date_in": day.strftime("%d-%m-%Y")})},
        headers={
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


async def fetch_schedules(day: date) -> list[DaySchedule]:
    async with httpx.AsyncClient() as client:
        return parse_html(await fetch(client, day))
