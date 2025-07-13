import pytest
from types import SimpleNamespace
import server_bot as sb

class DummyMsg:
    def __init__(self, text):
        self.text = text
        self.sent = []
        self.voice = None
        self.audio = None
        self.video_note = None
    async def reply_text(self, text, **kw):
        self.sent.append(text)

class DummyBot:
    def __init__(self):
        self.calls = 0
    async def send_audio(self, *a, **kw):
        self.calls += 1

@pytest.mark.asyncio
async def test_edited_message_triggers_tts(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "USERS_EMB", tmp_path / "u")
    monkeypatch.setattr(sb, "SETTINGS_DB", str(tmp_path / "s.json"))
    (tmp_path / "s.json").write_text("{}")
    uid = "1"
    (sb.USERS_EMB / uid).mkdir(parents=True)
    (sb.USERS_EMB / uid / "speaker_embedding_0.npz").write_bytes(b"x")
    sb.ACTIVE_SLOTS[uid] = 0

    bot = DummyBot()
    ctx = SimpleNamespace(bot=bot)
    msg = DummyMsg("hi")
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        edited_message=msg,
        effective_message=msg,
    )
    class DummyClf:
        async def analyse(self, text):
            return {"Безопасные сообщения": 1.0}
    monkeypatch.setattr(sb, "get_classifier", lambda: DummyClf())
    sb.VOICE.user_embedding = {}

    await sb.tg_text(upd, ctx)
    assert bot.calls == 1
