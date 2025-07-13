import pytest
from types import SimpleNamespace
import server_bot as sb

class DummyMsg:
    def __init__(self):
        self.sent = []
    async def reply_text(self, text, **kw):
        self.sent.append(text)

class DummyUpd(SimpleNamespace):
    pass

@pytest.mark.asyncio
async def test_reset_limit_command(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "USERS_EMB", tmp_path / "u")
    monkeypatch.setattr(sb, "ADMIN_IDS", {"1"})
    uid = "1"
    (sb.USERS_EMB / uid).mkdir(parents=True)
    sb.inc_daily_gen(uid)
    assert sb.daily_gen_count(uid) == 1
    upd = DummyUpd(effective_user=SimpleNamespace(id=1), message=DummyMsg())
    await sb.cmd_reset_limit(upd, None)
    assert sb.daily_gen_count(uid) == 0
    assert "сброшен" in upd.message.sent[0]
