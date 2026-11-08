from datetime import date
from pathlib import Path

from bot.poe import MAYBE, OFF, ON, intervals, parse_html, parse_ua_date
from bot.texts import render_day

HTML = (Path(__file__).parent / "fixtures" / "sample.html").read_text(encoding="utf-8")


def test_parse_two_days():
    days = parse_html(HTML)
    assert [d.day for d in days] == [date(2026, 11, 8), date(2026, 11, 9)]
    today, tomorrow = days
    assert len(today.queues) == 12
    assert set(today.queues) == {f"{q}.{s}" for q in range(1, 7) for s in (1, 2)}
    assert all(len(v) == 48 for v in today.queues.values())
    assert tomorrow.no_outages and tomorrow.slots("1.1") == [ON] * 48


def test_intervals():
    today = parse_html(HTML)[0]
    got = [(i.start_min, i.end_min, i.state) for i in intervals(today.slots("1.1")) if i.state != ON]
    assert got == [(420, 660, OFF), (660, 720, MAYBE), (1320, 1440, OFF)]


def test_fingerprint_changes_only_for_affected_queue():
    a = parse_html(HTML)[0]
    b = parse_html(HTML.replace('light_2', 'light_1', 1))[0]  # первый «off» у 1.1 стал «on»
    assert a.fingerprint("1.1") != b.fingerprint("1.1")
    assert a.fingerprint("2.1") == b.fingerprint("2.1")


def test_unknown_markup_is_ignored():
    assert parse_html("<html><body>Технічні роботи</body></html>") == []
    assert parse_html('<div class="gpvinfodetail"><b>8 листопада 2026</b> щось нове</div>') == []


def test_ua_date():
    assert parse_ua_date("на 1 січня 2027 року") == date(2027, 1, 1)
    assert parse_ua_date("сьогодні") is None


def test_render():
    today = parse_html(HTML)[0]
    text = render_day(today, "1.1", today.day)
    assert "07:00–11:00" in text and "22:00–24:00" in text and "11:00–12:00" in text
    assert "~6 год 00 хв" in text
    assert "відключень немає" in render_day(today, "2.1", today.day)
    assert "не прогнозуються" in render_day(parse_html(HTML)[1], "1.1", date(2026, 11, 9))
    assert "Графіка ще немає" in render_day(None, "1.1", today.day)
