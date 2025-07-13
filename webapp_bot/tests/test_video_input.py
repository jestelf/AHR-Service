import pytest
from types import SimpleNamespace
import server_bot as sb

class DummyBot:
    async def get_file(self, file_id):
        class F:
            async def download_to_drive(self, dest):
                open(dest, "wb").write(b"vid")
        return F()

class DummyMsg:
    def __init__(self):
        self.voice = None
        self.audio = None
        self.video = SimpleNamespace(file_id="v1")
        self.video_note = None
        self.sent = []
    async def reply_text(self, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(chat_id=1, message_id=1)

@pytest.mark.asyncio
async def test_video_to_wav(monkeypatch, tmp_path):
    uid = "55"
    monkeypatch.setattr(sb, "USERS_EMB", tmp_path / "u")
    (sb.USERS_EMB / uid).mkdir(parents=True)
    sb.ACTIVE_SLOTS[uid] = 0
    monkeypatch.setattr(sb, "tariff_info", lambda u: {"slots": 1})
    monkeypatch.setattr(sb, "auto_delete_enabled", lambda u: False)

    called = {}
    def fake_create(path, user):
        called["path"] = path
        open(path, "wb").write(b"RIFF")
        return path
    monkeypatch.setattr(sb.VOICE, "create_embedding", fake_create)

    used = {}
    def fake_from_file(src):
        used["src"] = src
        class S:
            def set_channels(self, *a): return self
            def set_frame_rate(self, *a): return self
            def set_sample_width(self, *a): return self
            def export(self, dst, format):
                open(dst, 'wb').write(b'RIFF')
                return dst
        return S()
    monkeypatch.setattr(sb, "AudioSegment", SimpleNamespace(from_file=fake_from_file))

    ctx = SimpleNamespace(bot=DummyBot())
    msg = DummyMsg()
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                          effective_message=msg,
                          message=msg)
    await sb.tg_voice(upd, ctx)
    assert "src" in used
    assert called["path"].endswith(".wav")
