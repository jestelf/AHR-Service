import pytest
from types import SimpleNamespace
import server_bot as sb

class DummyBot:
    def __init__(self):
        self.deleted = []
    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
    async def send_audio(self, chat_id, audio, title):
        return SimpleNamespace(chat_id=chat_id, message_id=50)

class DummyMsg:
    def __init__(self, chat_id=1):
        self.chat_id = chat_id
        self.message_id = 10
    async def reply_text(self, text, **kw):
        return SimpleNamespace(chat_id=self.chat_id, message_id=11)

class DummyUpd(SimpleNamespace):
    pass

@pytest.mark.asyncio
async def test_delete_message_called(monkeypatch, tmp_path):
    uid = "99"
    sb.ACTIVE_SLOTS.clear()
    sb.ACTIVE_SLOTS[uid] = 0
    # prepare embedding file
    monkeypatch.setattr(sb, "USERS_EMB", tmp_path / "u")
    emb_dir = sb.USERS_EMB / uid
    emb_dir.mkdir(parents=True)
    (emb_dir / "speaker_embedding_0.npz").write_bytes(b"0")
    out = tmp_path / "out.wav"
    out.write_bytes(b"RIFF")
    monkeypatch.setattr(sb.VOICE, "synthesize", lambda uid, txt: out)
    monkeypatch.setattr(sb, "apply_user_settings", lambda u: None)
    monkeypatch.setattr(sb, "tariff_info", lambda u: {"slots":1, "daily_gen":5})
    monkeypatch.setattr(sb, "daily_gen_count", lambda u: 0)
    monkeypatch.setattr(sb, "auto_delete_enabled", lambda u: True)
    monkeypatch.setattr(sb, "load_json", lambda p: {uid: {"filter_off": True}})

    ctx = SimpleNamespace(bot=DummyBot(), args=[])
    upd = DummyUpd(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=1),
        message=DummyMsg(),
    )
    await sb.tg_text(upd, ctx)
    assert (1, 10) in ctx.bot.deleted
