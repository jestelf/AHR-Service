import pytest, os, tempfile, io, wave, contextlib, types, sys

# ---- lightweight stub for voice_module before importing server_bot ----
class DummyVM:
    def __init__(self, *a, **k):
        self.storage = tempfile.gettempdir()
        self._params = {}

    def get_user_params(self, uid):
        return self._params.get(uid, {"temperature": 0.5, "speed": 1.0})

    def set_user_params(self, uid, **kw):
        p = self.get_user_params(uid)
        p.update(kw)
        if "speed" in p:
            p["speed"] = min(3.0, p["speed"])
        self._params[uid] = p

    def create_embedding(self, path, uid):
        dst = os.path.join(self.storage, f"emb_{uid}.wav")
        open(dst, "wb").write(b"RIFF")
        return dst

    def synthesize(self, uid, text, embedding_file=None):
        dst = os.path.join(self.storage, f"tts_{uid}.wav")
        open(dst, "wb").write(b"RIFF" + b"0" * 1000)
        return dst

sys.modules['voice_module'] = types.ModuleType('voice_module')
sys.modules['voice_module'].VoiceModule = DummyVM

from server_bot import app as flask_app, VOICE

@pytest.fixture(scope="session")
def client():
    flask_app.config["TESTING"] = True
    return flask_app.test_client()

@pytest.fixture
def silence_wav(tmp_path):
    """1-секундный WAV 16 kHz silence."""
    path = tmp_path / "silence.wav"
    with contextlib.closing(wave.open(path, "w")) as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16_000)
        f.writeframes(b"\x00\x00" * 16_000)
    return path

@pytest.fixture(scope="session")
def fake_user():
    return "42"
