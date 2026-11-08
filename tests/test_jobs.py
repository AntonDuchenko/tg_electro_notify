import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import bot.main as m
from bot.poe import parse_html
from bot.storage import Storage

HTML = (Path(__file__).parent / "fixtures" / "sample.html").read_text(encoding="utf-8")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def make_ctx(tmp_path):
    st = Storage(str(tmp_path / "db.sqlite3"))
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={"storage": st}))
    return ctx, st, bot


def test_check_notifies_once_then_on_change(tmp_path, monkeypatch):
    ctx, st, bot = make_ctx(tmp_path)
    st.set_queue(1, "1.1")
    st.set_queue(2, "2.1")
    monkeypatch.setattr(m, "now", lambda: datetime(2026, 11, 8, 6, 0, tzinfo=m.TZ))
    html = {"v": HTML}

    async def fake_fetch(day):
        return parse_html(html["v"])
    monkeypatch.setattr(m.poe, "fetch_schedules", fake_fetch)

    asyncio.run(m.job_check(ctx))
    assert len(bot.sent) == 4  # 2 пользователя x 2 дня, «опубликовано»
    bot.sent.clear()
    asyncio.run(m.job_check(ctx))
    assert bot.sent == []  # ничего не поменялось
    html["v"] = HTML.replace("light_2", "light_1", 1)  # поменялась только 1.1
    asyncio.run(m.job_check(ctx))
    assert [c for c, _ in bot.sent] == [1] and "змінився" in bot.sent[0][1]


def test_fetch_error_keeps_old_data(tmp_path, monkeypatch):
    ctx, st, bot = make_ctx(tmp_path)
    for s in parse_html(HTML):
        st.save_schedule(s)

    async def boom(day):
        raise ConnectionError("reset")
    monkeypatch.setattr(m.poe, "fetch_schedules", boom)
    monkeypatch.setattr(m, "now", lambda: datetime(2026, 11, 8, 6, 0, tzinfo=m.TZ))
    asyncio.run(m.job_check(ctx))
    assert st.get_schedule(datetime(2026, 11, 8).date()).queues["1.1"][14] == "off"


def test_remind_before_outage_once(tmp_path, monkeypatch):
    ctx, st, bot = make_ctx(tmp_path)
    st.set_queue(1, "1.1")
    for s in parse_html(HTML):
        st.save_schedule(s)
    monkeypatch.setattr(m, "REMIND_BEFORE_MIN", 30)
    monkeypatch.setattr(m, "now", lambda: datetime(2026, 11, 8, 6, 40, tzinfo=m.TZ))
    asyncio.run(m.job_remind(ctx))
    asyncio.run(m.job_remind(ctx))
    assert len(bot.sent) == 1 and "07:00–11:00" in bot.sent[0][1]
    # 11:00 — продолжение отключения («можливе»), а не новое: не напоминаем
    monkeypatch.setattr(m, "now", lambda: datetime(2026, 11, 8, 10, 45, tzinfo=m.TZ))
    asyncio.run(m.job_remind(ctx))
    assert len(bot.sent) == 1


def test_build_app():
    app = m.build_app("123:ABC", ":memory:")
    assert len(app.job_queue.jobs()) == 2
