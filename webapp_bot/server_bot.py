# server_bot.py — единый процесс
# ────────────────────────────────────────────────────────────────────────────────
# • Flask-сайт              http://localhost:5000
# • LocalTunnel-туннель     https://<sub>.loca.lt
# • Telegram-бот            (команда /start даёт ин-лайн-меню слотов)
# • PatentTTS-проверка      POST /audio_check
# • Anti-scam-классификатор (classifier.py)
# • XTTS-clone / synthesis  (voice_module.py)
# • Логи users_emb/<id>/message.log  +  strikes / blacklist
# • Тарифы/лимиты           tariffs_db.json   (free/base/vip/premium)
# -------------------------------------------------------------------------------
# Запуск:
#   1)  export BOT_TOKEN="123456:ABC-DEF…"   # либо .env
#   2)  (опц.) export XTTS_MODEL_DIR="D:/prdja"
#   3)  python server_bot.py
# ────────────────────────────────────────────────────────────────────────────────

import os
import re
import json
import shutil
import subprocess
import threading
import tempfile
import asyncio
import concurrent.futures
from pathlib import Path
from datetime import datetime, date
from types import MethodType

from dotenv import load_dotenv
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
    send_from_directory,
    Response,
)
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# для безопасной отправки аудио
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from audio_checker import predict
from classifier import get_classifier
from voice_module import VoiceModule
from pydub import AudioSegment
from bot_extra_commands import cmd_help, cmd_about, cmd_stats, cmd_feedback, cmd_history

# ───────────────────────── конфигурация
load_dotenv()  #   читаем .env

BOT_TOKEN = os.getenv("BOT_TOKEN")

LT_SUBDOMAIN = os.getenv("LT_SUBDOMAIN", "audiohighres")
LT_CMD_ENV = os.getenv("LT_CMD")

XTTS_MODEL_DIR = Path(os.getenv("XTTS_MODEL_DIR", "D:/prdja"))

# все «постоянные» файлы / папки теперь настраиваются извне
SETTINGS_DB = os.getenv("SETTINGS_DB", "user_settings.json")
TARIFFS_DB = os.getenv("TARIFFS_DB", "tariffs_db.json")
AUTH_FILE = os.getenv("AUTH_FILE", "authorized_users.txt")
STRIKES_DB = os.getenv("STRIKES_DB", "user_strikes.json")
BL_FILE = os.getenv("BLACKLIST_FILE", "blacklist.txt")
USERS_EMB = Path(os.getenv("USERS_EMB_DIR", "users_emb"))

# числа приводим к нужному типу
MAX_STRIKES = int(os.getenv("MAX_STRIKES", "5"))
ALERT_THRESH = float(os.getenv("ALERT_THRESH", "0.50"))

WEBAPP_URL = os.getenv("WEBAPP_URL")
ADMIN_IDS = {i for i in os.getenv("ADMIN_IDS", "").split(",") if i.isdigit()}


def is_admin(uid: str) -> bool:
    """True, если пользователь в списке администраторов."""
    return uid in ADMIN_IDS


# ───────── тарифы
TARIFF_DEFS = {
    "free": {"slots": 1, "daily_gen": 5},
    "base": {"slots": 3, "daily_gen": 20},
    "vip": {"slots": 6, "daily_gen": 60},
    "premium": {"slots": 12, "daily_gen": 9999},
}

# ───────── ensure files / dirs
for path, default in [
    (SETTINGS_DB, {}),
    (TARIFFS_DB, {}),
    (STRIKES_DB, {}),
    (AUTH_FILE, None),
]:
    if not Path(path).exists():
        with open(path, "w", encoding="utf-8") as f:
            if default is not None:
                json.dump(default, f)
USERS_EMB.mkdir(exist_ok=True)
Path(BL_FILE).touch(exist_ok=True)


# ───────────────────────── helpers
# ───────── тарифы: вспомогательные функции  ← вставить здесь
def set_tariff_safe(uid: str, name: str) -> str:
    """
    Валидирует имя тарифа и записывает его в tariffs_db.json.
    Возвращает фактически установленный план (или прежний, если ошибка).
    """
    if name not in TARIFF_DEFS:  # неизвестный план
        return get_tariff(uid)  # ничего не меняем
    db = load_json(TARIFFS_DB)
    rec = db.get(uid)
    if isinstance(rec, str):
        rec = {"plan": name, "bonus_gen": 0}
    else:
        rec = rec or {"bonus_gen": 0}
        rec["plan"] = name
    db[uid] = rec
    save_json(TARIFFS_DB, db)
    return name


# ---- какие поля из user_settings.json можно отдавать в VoiceModule
ALLOWED_TTS_KEYS = {
    "temperature",
    "top_k",
    "top_p",
    "repetition_penalty",
    "length_penalty",
    "speed",
}


def apply_user_settings(uid: str) -> None:
    """
    Берём сохранённые настройки пользователя, отфильтровываем
    только TTS-параметры и передаём их в VoiceModule.
    """
    raw = load_json(SETTINGS_DB).get(uid)
    if not raw:
        return
    overrides = {k: raw[k] for k in ALLOWED_TTS_KEYS if k in raw}
    if overrides:
        VOICE.set_user_params(uid, **overrides)


def load_json(p: str) -> dict:
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_json(p: str, d: dict) -> None:
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def toggle_filter(uid: str) -> bool:
    """Переключить антискам-фильтр. Возвращает новое состояние."""
    db = load_json(SETTINGS_DB)
    cfg = db.get(uid, {})
    state = not cfg.get("filter_off", False)
    cfg["filter_off"] = state
    db[uid] = cfg
    save_json(SETTINGS_DB, db)
    return state

def is_blacklisted(uid: str) -> bool:
    with open(BL_FILE, encoding="utf-8") as f:
        return uid in {l.strip() for l in f}


def add_black(uid: str):
    if not is_blacklisted(uid):
        with open(BL_FILE, "a", encoding="utf-8") as f:
            f.write(uid + "\n")


def add_strike(uid: str) -> int:
    d = load_json(STRIKES_DB)
    d[uid] = d.get(uid, 0) + 1
    save_json(STRIKES_DB, d)
    return d[uid]

def _tariff_record(uid: str) -> dict:
    """Возвращает запись из tariffs_db.json, создавая при необходимости."""
    db = load_json(TARIFFS_DB)
    rec = db.get(uid)
    if isinstance(rec, str):
        rec = {"plan": rec, "bonus_gen": 0}
    if not rec:
        rec = {"plan": "free", "bonus_gen": 0}
    if "bonus_gen" not in rec:
        rec["bonus_gen"] = 0
    db[uid] = rec
    save_json(TARIFFS_DB, db)
    return rec


def get_tariff(uid: str) -> str:
    return _tariff_record(uid).get("plan", "free")


def set_tariff(uid: str, name: str) -> None:
    db = load_json(TARIFFS_DB)
    rec = db.get(uid)
    if isinstance(rec, str):
        rec = {"plan": name, "bonus_gen": 0}
    else:
        rec = rec or {"bonus_gen": 0}
        rec["plan"] = name
    db[uid] = rec
    save_json(TARIFFS_DB, db)


def add_daily_gen(uid: str, amount: int) -> int:
    """Увеличивает bonus_gen и возвращает новое значение."""
    db = load_json(TARIFFS_DB)
    rec = db.get(uid)
    if isinstance(rec, str):
        rec = {"plan": rec, "bonus_gen": 0}
    rec = rec or {"plan": "free", "bonus_gen": 0}
    rec["bonus_gen"] = rec.get("bonus_gen", 0) + int(amount)
    db[uid] = rec
    save_json(TARIFFS_DB, db)
    return rec["bonus_gen"]


def tariff_info(uid: str) -> dict:
    rec = _tariff_record(uid)
    base = TARIFF_DEFS[rec.get("plan", "free")].copy()
    base["daily_gen"] += rec.get("bonus_gen", 0)
    return base


def daily_gen_count(uid: str) -> int:
    meta = USERS_EMB / uid / "gen_meta.json"
    if not meta.exists():
        return 0
    d = load_json(meta)
    if d.get("date") != date.today().isoformat():
        return 0
    return d.get("count", 0)


def inc_daily_gen(uid: str) -> None:
    meta = USERS_EMB / uid / "gen_meta.json"
    d = load_json(meta)
    today = date.today().isoformat()
    if d.get("date") != today:
        d = {"date": today, "count": 0}
    d["count"] = d.get("count", 0) + 1
    save_json(meta, d)


def reset_daily_gen(uid: str) -> None:
    """Сбросить счётчик дневных генераций пользователя."""
    meta = USERS_EMB / uid / "gen_meta.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    save_json(meta, {"date": date.today().isoformat(), "count": 0})


def log_line(uid: str, line: str):
    folder = USERS_EMB / uid
    folder.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(folder / "message.log", "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")


AUTO_DEL_KEY = "auto_delete"
DEL_DELAY = 5.0


async def _maybe_delete(ctx, chat_id: int, msg_id: int, delay: float = 0.0) -> None:
    """Безопасное удаление сообщения Telegram."""
    if not ctx or not getattr(ctx, "bot", None):
        return
    try:
        if delay:
            await asyncio.sleep(delay)
        await ctx.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except TelegramError:
        pass


def auto_delete_enabled(uid: str) -> bool:
    """Проверяем флаг автоудаления из user_settings.json."""
    return load_json(SETTINGS_DB).get(uid, {}).get(AUTO_DEL_KEY, False)


ABBR = {
    "Безопасные сообщения": "БС",
    "Родственник в беде": "РВБ",
    "Выигрыши/лотереи/подарки": "ВЛ",
    "Госорганы и службы": "ГОС",
    "Инвестиции и заработок": "ИЗ",
    "Курьерские и почтовые обманы": "КПО",
    "Мошенники от имени банков": "МБ",
    "Поддельная служба поддержки": "ПСП",
    "Призывы к действию": "ПД",
    "Социальные схемы": "СС",
}

# ───────────────────────── XTTS
VOICE = VoiceModule(model_dir=XTTS_MODEL_DIR, storage_dir=USERS_EMB)


def _userdir_patch(self, uid: str) -> Path:
    d = USERS_EMB / uid
    d.mkdir(parents=True, exist_ok=True)
    return d


VOICE._user_dir = MethodType(_userdir_patch, VOICE)  # type: ignore
VOICE.users_root = USERS_EMB  # type: ignore
voice_pool = concurrent.futures.ThreadPoolExecutor(1)


# ───────────────────────── LocalTunnel
def _lt_cmd() -> str:
    if LT_CMD_ENV and Path(LT_CMD_ENV).is_file():
        return LT_CMD_ENV
    path = shutil.which("lt")
    if not path:
        raise RuntimeError("LocalTunnel CLI не найден.")
    return path


def start_lt(port: int = 5000) -> str:
    proc = subprocess.Popen(
        [_lt_cmd(), "--port", str(port), "--subdomain", LT_SUBDOMAIN],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        print(line.strip())
        m = re.search(r"https://[a-z0-9\-]+\.loca\.lt", line)
        if m:
            return m.group(0)
    raise RuntimeError("LT URL not received")


# ───────────────────────── Flask
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True

ACTIVE_SLOTS: dict[str, int] = {}


# ───────────────────────── WebApp reply-клавиатура
def build_webapp_keyboard() -> ReplyKeyboardMarkup:
    """
    Отдаёт ReplyKeyboardMarkup с одной кнопкой «⚙️ Настройки».
    Если WEBAPP_URL задан, кнопка открывает Web-App.
    """
    btn = (
        KeyboardButton("⚙️ Настройки", web_app=WebAppInfo(url=WEBAPP_URL))
        if WEBAPP_URL
        else KeyboardButton("⚙️ Настройки")
    )
    return ReplyKeyboardMarkup([[btn]], resize_keyboard=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        app.static_folder, "favicon.ico", mimetype="image/vnd.microsoft.icon"
    )


@app.route("/telegram_auth", methods=["POST"])
def telegram_auth():
    d = request.get_json(force=True, silent=True)
    if not d or "id" not in d:
        return jsonify(status="error", message="Нет ID"), 400
    uid = str(d["id"])
    with open(AUTH_FILE, "a+", encoding="utf-8") as f:
        f.seek(0)
        ids = {l.strip() for l in f}
        if uid not in ids:
            f.write(uid + "\n")
    return jsonify(status="success"), 200


@app.route("/save_user_settings", methods=["POST"])
def save_settings():
    p = request.get_json(force=True, silent=True)
    if not p or "userId" not in p or "settings" not in p:
        return jsonify(status="error", message="bad payload"), 400
    db = load_json(SETTINGS_DB)
    db[str(p["userId"])] = p["settings"]
    save_json(SETTINGS_DB, db)
    return jsonify(status="success"), 200


@app.route("/set_user_tariff", methods=["POST"])
def set_user_tariff():
    """
    Payload: {"userId": 123, "plan": "vip"}
    """
    p = request.get_json(force=True, silent=True)
    if not p or "userId" not in p or "plan" not in p:
        return jsonify(status="error", message="bad payload"), 400
    uid = str(p["userId"])
    plan = p["plan"]
    new = set_tariff_safe(uid, plan)
    return jsonify(status="success", plan=new), 200


@app.route("/get_user_settings")
def get_settings():
    uid = request.args.get("userId")
    if not uid:
        return jsonify(status="error", message="need userId"), 400
    db = load_json(SETTINGS_DB)
    if uid in db:
        return jsonify(status="success", settings=db[uid]), 200
    return jsonify(status="not_found"), 404


@app.route("/audio_check", methods=["POST"])
def audio_check():
    if "audio" not in request.files:
        return jsonify(status="error", message="no file"), 400
    f = request.files["audio"]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.close()
    try:
        print("⏳ Анализ аудио…")
        f.save(tmp.name)
        res = predict(tmp.name)
        status = "безопасно" if "BINARY: real" in res else "опасно"
        print(f"✅ Результат: {status}")
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500
    finally:
        try:
            os.remove(tmp.name)
        except:
            pass
    return jsonify(status="ok", result=res), 200


# ───────────────────────── voice-routes
@app.route("/voice/embed", methods=["POST"])
def voice_embed():
    form = request.form
    if "audio" not in request.files or "userId" not in form or "slot" not in form:
        return jsonify(status="error", message="need audio, userId & slot"), 400
    uid, slot = str(form["userId"]), int(form["slot"])
    slots_allowed = tariff_info(uid)["slots"]
    if not (0 <= slot < slots_allowed):
        return jsonify(status="error", message=f"slot {slot} out of range"), 403

    user_dir = USERS_EMB / uid
    before = set(user_dir.glob("speaker_embedding_*.npz"))

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")
    tmp.close()
    request.files["audio"].save(tmp.name)
    try:
        VOICE.create_embedding(tmp.name, uid)
    finally:
        try:
            os.remove(tmp.name)
        except:
            pass

    after = set(user_dir.glob("speaker_embedding_*.npz"))
    new = after - before
    if not new:
        return jsonify(status="error", message="no new embedding"), 500

    new_file = new.pop()
    target = user_dir / f"speaker_embedding_{slot}.npz"
    if target.exists():
        target.unlink()
    new_file.rename(target)
    return jsonify(status="ok"), 200


@app.route("/voice/tts", methods=["POST"])
def voice_tts():
    d = request.get_json(force=True, silent=True)
    if not d or "userId" not in d or "text" not in d or "slot" not in d:
        return jsonify(status="error", message="need userId, text & slot"), 400
    uid, text, slot = str(d["userId"]), d["text"], int(d["slot"])

    if daily_gen_count(uid) >= tariff_info(uid)["daily_gen"]:
        return jsonify(status="error", message="daily limit"), 403

    emb = USERS_EMB / uid / f"speaker_embedding_{slot}.npz"
    if not emb.exists():
        return jsonify(status="error", message="slot empty"), 404

    # применяем личные настройки до синтеза
    apply_user_settings(uid)

    # явно указываем VOICE, какой embedding-файл использовать
    VOICE.user_embedding[uid] = emb  # type: ignore

    try:
        wav_path = Path(VOICE.synthesize(uid, text))
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500

    if not wav_path.exists() or not wav_path.is_file():
        return jsonify(status="error", message="synthesis failed"), 500

    inc_daily_gen(uid)
    # отдаем настоящий WAV
    return send_file(
        wav_path.resolve(),
        as_attachment=True,
        download_name=wav_path.name,
        mimetype="audio/wav",
    )


# ───────────────────────── Telegram-handlers
def build_slot_keyboard(uid: str) -> InlineKeyboardMarkup:
    slots = tariff_info(uid)["slots"]
    files = list((USERS_EMB / uid).glob("speaker_embedding_*.npz"))
    active = ACTIVE_SLOTS.get(uid)
    kb = []
    for i in range(slots):
        if i < len(files):
            text = f"{'✅ ' if active==i else ''}Слот {i+1}"
            data = f"slot:{i}"
        else:
            text = f"{'✅ ' if active==i else ''}➕ Пусто {i+1}"
            data = f"new:{i}"
        kb.append([InlineKeyboardButton(text, callback_data=data)])
    return InlineKeyboardMarkup(kb)


async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(upd.effective_user.id)
    if is_blacklisted(uid):
        return

    # ── «жёсткий» сброс Web-App  ───────────────────────────────────
    if ctx.args and ctx.args[0].lower() == "reset":
        # кнопка-ссылка открывает Web-App c query-param  ?reset=1
        reset_kb = ReplyKeyboardMarkup(
            [
                [
                    KeyboardButton(
                        "Очистить Web-App",
                        web_app=WebAppInfo(url=f"{WEBAPP_URL}?reset=1"),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await upd.message.reply_text(
            "Нажмите кнопку ниже — Web-App перезапустится без сохранённых данных.",
            reply_markup=reset_kb,
        )
        return  # дальше /start не продолжаем

    # ── регистрация пользователю (если впервые)
    with open(AUTH_FILE, "a+", encoding="utf-8") as f:
        f.seek(0)
        known = {l.strip() for l in f}
        if uid not in known:
            f.write(uid + "\n")

    # ── тариф: если ещё не задан – free
    if uid not in load_json(TARIFFS_DB):
        set_tariff(uid, "free")

    # ── отдаём клавиатуры
    await upd.message.reply_text(
        "Ваши голосовые слоты:", reply_markup=build_slot_keyboard(uid)
    )

    await upd.message.reply_text(
        "Откройте Web-App для гибких настроек:", reply_markup=build_webapp_keyboard()
    )


async def cb_handler(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    uid = str(q.from_user.id)
    cmd, arg = q.data.split(":", 1)

    # ────────── слоты  ──────────
    if cmd in {"slot", "new"}:
        idx = int(arg)
        ACTIVE_SLOTS[uid] = idx
        if cmd == "slot":
            await q.answer("Слот выбран")
            await q.edit_message_text(
                f"Слот {idx+1} выбран.", reply_markup=build_slot_keyboard(uid)
            )
        else:
            await q.answer()
            await q.edit_message_text("Отправьте новое голосовое сообщение для слепка.")
        return

    # ────────── тарифы (только админ) ──────────
    if cmd == "plan":
        if not is_admin(uid):
            await q.answer("Недостаточно прав", show_alert=True)
            return
        new = set_tariff_safe(uid, arg)
        await q.answer()
        await q.edit_message_text(
            f"🎫 Тариф установлен: *{new}*",
            reply_markup=build_tariff_keyboard(new),
            parse_mode="Markdown",
        )


async def handle_web_app(upd: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = str(upd.effective_user.id)
    if is_blacklisted(uid):
        return

    try:
        payload = json.loads(upd.message.web_app_data.data)
    except Exception:
        await upd.message.reply_text("❌ bad JSON")
        return

    act = payload.get("action")

    if act == "save_settings":
        db = load_json(SETTINGS_DB)
        db[uid] = payload.get("settings", {})
        save_json(SETTINGS_DB, db)
        await upd.message.reply_text("✅ Настройки сохранены.")

    elif act == "set_tariff":
        if not is_admin(uid):
            await upd.message.reply_text("⛔ Только админ может менять тариф.")
            return
        plan = payload.get("plan")
        new = set_tariff_safe(uid, plan)
        await upd.message.reply_text(
            f"🎫 Тариф установлен: *{new}*", parse_mode="Markdown"
        )

    else:
        await upd.message.reply_text("❌ unknown action")


async def tg_voice(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(upd.effective_user.id)
    if is_blacklisted(uid):
        return
    slot = ACTIVE_SLOTS.get(uid)
    msg = upd.effective_message
    if slot is None:
        await msg.reply_text("Выберите слот через /start")
        return

    v = msg.voice or msg.audio or msg.video or msg.video_note
    if not v:
        return

    allowed = tariff_info(uid)["slots"]
    if not (0 <= slot < allowed):
        await msg.reply_text(f"Слот {slot+1} вне диапазона.")
        return

    m = await upd.message.reply_text("⏳ Обрабатываю запись…")
    if auto_delete_enabled(uid):
        await _maybe_delete(ctx, m.chat_id, m.message_id, DEL_DELAY)


    user_dir = USERS_EMB / uid
    before = set(user_dir.glob("speaker_embedding_*.npz"))

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")
    tmp.close()
    await (await ctx.bot.get_file(v.file_id)).download_to_drive(tmp.name)
    file_path = tmp.name
    if msg.video or msg.video_note:
        wav_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        wav_tmp.close()
        (
            AudioSegment.from_file(tmp.name)
            .set_channels(1)
            .set_frame_rate(16_000)
            .set_sample_width(2)
            .export(wav_tmp.name, format="wav")
        )
        os.remove(tmp.name)
        file_path = wav_tmp.name
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(voice_pool, VOICE.create_embedding, file_path, uid)
    os.remove(file_path)

    after = set(user_dir.glob("speaker_embedding_*.npz"))
    new = after - before
    if not new:
        err = await upd.message.reply_text("Ошибка создания слепка.")
        if auto_delete_enabled(uid):
            await _maybe_delete(ctx, upd.effective_chat.id, upd.message.message_id)
            await _maybe_delete(ctx, err.chat_id, err.message_id, DEL_DELAY)
        return

    new_file = new.pop()
    target = user_dir / f"speaker_embedding_{slot}.npz"
    if target.exists():
        target.unlink()
    new_file.rename(target)
    done = await upd.message.reply_text(
        "🗣️ Слепок создан.", reply_markup=build_slot_keyboard(uid)
    )
    if auto_delete_enabled(uid):
        await _maybe_delete(ctx, upd.effective_chat.id, upd.message.message_id)
        await _maybe_delete(ctx, done.chat_id, done.message_id, DEL_DELAY)


async def tg_text(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = upd.effective_message
    if not msg or not msg.text:
        return

    uid = str(upd.effective_user.id)
    txt = msg.text.strip()

    if is_blacklisted(uid):
        return

    settings = load_json(SETTINGS_DB).get(uid, {})
    if not settings.get("filter_off"):
        tmp = await upd.message.reply_text("⏳ Анализирую текст…")
        if auto_delete_enabled(uid):
            await _maybe_delete(ctx, tmp.chat_id, tmp.message_id, DEL_DELAY)
        clf = get_classifier()
        scores = await clf.analyse(txt)
        comp = ";".join(f"{ABBR[k]}{scores.get(k, 0) * 100:04.1f}" for k in ABBR)
        log_line(uid, f"{txt} ({comp})")

        safe = scores.get("Безопасные сообщения", 0)
        top_lbl, top_p = max(scores.items(), key=lambda kv: kv[1])

        warn = None
        if top_lbl != "Безопасные сообщения" and top_p >= ALERT_THRESH:
            warn = f"«{top_lbl}» {top_p * 100:.0f}%"
        elif safe < 0.50 and top_p < ALERT_THRESH:
            parts = [
                f"{l} {p * 100:.0f}%"
                for l, p in scores.items()
                if l != "Безопасные сообщения" and p > 0.05
            ]
            if parts:
                warn = "; ".join(parts)
        res = await upd.message.reply_text(
            "Результат: безопасно" if not warn else "Результат: опасно"
        )
        if auto_delete_enabled(uid):
            await _maybe_delete(ctx, res.chat_id, res.message_id, DEL_DELAY)
            await _maybe_delete(ctx, upd.effective_chat.id, upd.message.message_id)
        if warn:
            s = add_strike(uid)
            if s >= MAX_STRIKES:
                add_black(uid)
                ban = await upd.message.reply_text("🚫 Заблокировано.")
                if auto_delete_enabled(uid):
                    await _maybe_delete(ctx, upd.effective_chat.id, upd.message.message_id)
                    await _maybe_delete(ctx, ban.chat_id, ban.message_id, DEL_DELAY)
                return
            warn_msg = await upd.message.reply_text(f"⚠️ {warn}. Strike {s}/{MAX_STRIKES}.")
            if auto_delete_enabled(uid):
                await _maybe_delete(ctx, upd.effective_chat.id, upd.message.message_id)
                await _maybe_delete(ctx, warn_msg.chat_id, warn_msg.message_id, DEL_DELAY)
            return
    else:
        log_line(uid, txt)

    # ---------- обычный TTS ----------
    slot = ACTIVE_SLOTS.get(uid)
    if slot is None:
        return

    if daily_gen_count(uid) >= tariff_info(uid)["daily_gen"]:
        lm = await upd.message.reply_text("Дневной лимит генераций исчерпан.")
        if auto_delete_enabled(uid):
            await _maybe_delete(ctx, upd.effective_chat.id, upd.message.message_id)
            await _maybe_delete(ctx, lm.chat_id, lm.message_id, DEL_DELAY)
        return

    emb = USERS_EMB / uid / f"speaker_embedding_{slot}.npz"
    if not emb.exists():
        sl = await upd.message.reply_text(f"Слот {slot+1} пуст. Выберите занятый слот.")
        if auto_delete_enabled(uid):
            await _maybe_delete(ctx, upd.effective_chat.id, upd.message.message_id)
            await _maybe_delete(ctx, sl.chat_id, sl.message_id, DEL_DELAY)
        return

    apply_user_settings(uid)
    VOICE.user_embedding[uid] = emb  # type: ignore
    proc = await upd.message.reply_text("⏳ Генерирую речь…")
    if auto_delete_enabled(uid):
        await _maybe_delete(ctx, proc.chat_id, proc.message_id, DEL_DELAY)

    loop = asyncio.get_running_loop()
    try:
        wav_path = Path(
            await loop.run_in_executor(voice_pool, VOICE.synthesize, uid, txt)
        )
    except Exception as e:
        log_line(uid, f"TTS ERROR: {e}")
        return

    with open(str(wav_path), "rb") as f:
        audio_msg = await ctx.bot.send_audio(
            chat_id=upd.effective_chat.id,
            audio=InputFile(f, filename=wav_path.name),
            title="TTS",
        )
    inc_daily_gen(uid)
    done = await upd.message.reply_text("✅ Готово")
    if auto_delete_enabled(uid):
        await _maybe_delete(ctx, upd.effective_chat.id, upd.message.message_id)
        await _maybe_delete(ctx, audio_msg.chat_id, audio_msg.message_id, DEL_DELAY)
        await _maybe_delete(ctx, done.chat_id, done.message_id, DEL_DELAY)


def build_tariff_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for p in TARIFF_DEFS:
        mark = "✅ " if p == current else ""
        rows.append(
            [InlineKeyboardButton(f"{mark}{p.title()}", callback_data=f"plan:{p}")]
        )
    return InlineKeyboardMarkup(rows)


async def cmd_tariff(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(upd.effective_user.id)
    if not is_admin(uid):
        await upd.message.reply_text("⛔ Это админ-команда.")
        return

    plan = get_tariff(uid)
    txt = f"Текущий тариф пользователя – *{plan}*\n" "Выберите новый план:"
    await upd.message.reply_text(
        txt, reply_markup=build_tariff_keyboard(plan), parse_mode="Markdown"
    )


async def cmd_filter(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(upd.effective_user.id)
    state = toggle_filter(uid)
    msg = "Антискам-фильтр выключен." if state else "Антискам-фильтр включен."
    await upd.message.reply_text(msg)


async def cmd_add_limit(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(upd.effective_user.id)
    if not is_admin(uid):
        await upd.message.reply_text("⛔ Это админ-команда.")
        return

    if not ctx.args or not ctx.args[0].isdigit():
        await upd.message.reply_text("Используйте: /add_limit <n>")
        return
    n = int(ctx.args[0])
    add_daily_gen(uid, n)
    await upd.message.reply_text(f"Добавлено {n} к дневному лимиту.")


def run_flask():
    app.run(port=5000, debug=False, use_reloader=False)


def main():
    if not BOT_TOKEN or not re.fullmatch(r"\d+:[\w-]{35}", BOT_TOKEN):
        raise RuntimeError("❌ BOT_TOKEN отсутствует или некорректен.")
    threading.Thread(target=run_flask, daemon=True).start()
    print("🌐 Flask на :5000")
    lt_url = start_lt()
    print("✅", lt_url)

    # если .env не задаёт URL, берём LT-домен
    global WEBAPP_URL
    if not WEBAPP_URL:
        WEBAPP_URL = lt_url.rstrip("/") + "/"

    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CallbackQueryHandler(cb_handler))
    app_tg.add_handler(CommandHandler("tariff", cmd_tariff))
    app_tg.add_handler(CommandHandler("filter", cmd_filter))
    app_tg.add_handler(CommandHandler("add_limit", cmd_add_limit))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("about", cmd_about))
    app_tg.add_handler(CommandHandler("stats", cmd_stats))
    app_tg.add_handler(CommandHandler("feedback", cmd_feedback))
    app_tg.add_handler(CommandHandler("history", cmd_history))
    app_tg.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app)
    )
    voice_f = filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE
    app_tg.add_handler(MessageHandler(voice_f, tg_voice))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_text))
    edited = filters.UpdateType.EDITED_MESSAGE
    app_tg.add_handler(MessageHandler(edited & filters.TEXT, tg_text))
    edited_all = filters.UpdateType.EDITED_MESSAGE | filters.UpdateType.EDITED_CHANNEL_POST
    app_tg.add_handler(MessageHandler(edited_all & voice_f, tg_voice))
    print("🤖 Bot up.")
    app_tg.run_polling()


if __name__ == "__main__":
    main()
