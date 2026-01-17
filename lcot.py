# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Dict, Any, List

import telebot
from telebot.types import Message, InlineKeyboardMarkup as K, InlineKeyboardButton as B
from logging import getLogger

import FunPayAPI
import FunPayAPI.types
from tg_bot import static_keyboards as skb

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "Lots by Time (LCOT)"
VERSION = "0.2.0"
DESCRIPTION = "Копии лота под разные длительности с пересчетом цены"
CREDITS = "@pen1t"
UUID = "d9a2c6f2-0d5a-49a8-9f96-0cc66ad4a1d3"
SETTINGS_PAGE = False

logger = getLogger("FPC.lcot")

# ---- STATE KEYS
STATE_WAIT_LOT  = f"{UUID}|STATE|WAIT_LOT"
STATE_WAIT_DURS = f"{UUID}|STATE|WAIT_DURS"
STATE_WAIT_DISC = f"{UUID}|STATE|WAIT_DISC"

# ---- CALLBACKS
CBT_CREATE = f"{UUID}|CB|CREATE"
CBT_CANCEL = f"{UUID}|CB|CANCEL"

# ---- SESSION STORAGE
SESSION: Dict[int, Dict[str, Any]] = {}

# ---------- helpers

def _fmt_price(v: float) -> str:
    return f"{float(v):.6f}"

def _guess_created_id(cardinal, fields: dict) -> int | None:
    """Пробует определить ID созданного лота."""
    for k in ("offer_id", "id"):
        v = (fields.get(k) or "").strip()
        if isinstance(v, str) and v.isdigit() and int(v) > 0:
            return int(v)

    ru = (fields.get("fields[summary][ru]") or "").strip()
    en = (fields.get("fields[summary][en]") or "").strip()
    titles = {t for t in (ru, en) if t}

    try:
        prof = getattr(cardinal, "profile", None) or cardinal.account.get_user(cardinal.account.id)
        lots = []
        if prof:
            if hasattr(prof, "get_lots"):
                lots = prof.get_lots() or []
        for it in lots:
            try:
                descr = (getattr(it, "description", "") or getattr(it, "title", "") or "").strip()
                if descr and descr in titles:
                    return int(getattr(it, "id"))
            except Exception:
                continue
    except Exception:
        pass
    return None

def _parse_durations(text: str) -> List[float]:
    """Парсит длительности и возвращает часы."""
    durs: List[float] = []
    s = (text or "").lower()
    tokens = re.split(r"[,\s;]+", s.strip())
    for tok in tokens:
        if not tok:
            continue
        m = re.fullmatch(
            r"(\d+(?:[.,]\d+)?)(?:\s*("
            r"h|ч|ч\.|час|часа|часов|"
            r"d|д|дн|день|дня|дней"
            r"))?", tok)
        if not m:
            continue
        val = float(m.group(1).replace(",", "."))
        unit = (m.group(2) or "h").strip()

        if unit in ("h", "ч", "ч.", "час", "часа", "часов"):
            hours = val
        elif unit in ("d", "д", "дн", "день", "дня", "дней"):
            hours = val * 24.0
        else:
            continue

        if hours > 0:
            durs.append(round(hours, 2))

    return sorted(list(dict.fromkeys(durs)))

def _en_duration_phrase(hours: float) -> str:
    """Форматирует длительность на английском."""
    if abs(hours - round(hours)) < 1e-9:
        h = int(round(hours))
        if h >= 24 and h % 24 == 0:
            d = h // 24
            return f"{d} {'day' if d == 1 else 'days'}"
    h_str = _hours_str(hours)
    unit = "hour" if abs(hours - 1.0) < 1e-9 else "hours"
    if not float(hours).is_integer():
        unit = "hours"
    return f"{h_str} {unit}"

def _ru_days_phrase(hours: float) -> str | None:
    """Возвращает дни, если кратно 24 часам."""
    if abs(hours - round(hours)) < 1e-9:
        h = int(round(hours))
        if h >= 24 and h % 24 == 0:
            d = h // 24
            return f"{d} " + _ru_num_word(d, ("день", "дня", "дней"))
    return None

def _ru_duration_phrase(hours: float) -> str:
    """Форматирует длительность по-русски."""
    days = _ru_days_phrase(hours)
    if days:
        return days
    return _ru_hours_phrase(hours)

def _ru_num_word(n: float, forms: tuple[str, str, str]) -> str:
    """Подбирает правильную форму слова для числа."""
    try:
        f = float(n)
    except Exception:
        f = n
    if isinstance(f, float) and not f.is_integer():
        return forms[1]
    n = int(round(f)) % 100
    if 11 <= n <= 19:
        return forms[2]
    n1 = n % 10
    if n1 == 1:
        return forms[0]
    if 2 <= n1 <= 4:
        return forms[1]
    return forms[2]

def _ru_hours_phrase(hours: float) -> str:
    h_str = _hours_str(hours)
    word = _ru_num_word(hours, ("час", "часа", "часов"))
    return f"{h_str} {word}"

def _fmt_short_duration(hours: float) -> str:
    """Короткий формат длительности для предпросмотра."""
    if abs(hours - round(hours)) < 1e-9 and int(hours) >= 24 and int(hours) % 24 == 0:
        d = int(hours) // 24
        return f"{d} д"
    return f"{_hours_str(hours)} ч"

def _hours_str(hours: float) -> str:
    return (str(hours).rstrip("0").rstrip(".") if isinstance(hours, float) else str(hours))

def _replace_hours_in_title(title: str, hours: float, locale: str = "ru", allow_insert: bool = True) -> str:
    """Нормализует упоминание длительности в тексте."""
    if not title:
        return title

    h_str = _hours_str(hours)
    t = title

    HY = r"[-\u2010-\u2015\u2212\uFE58\uFE63\uFF0D\u2011]"

    if locale.lower() == "ru":
        target_phrase = f"на {_ru_duration_phrase(hours)}"

        ru_hours = _ru_hours_phrase(hours)

        patterns = [
            (rf"(\b1\s*(?:ч|час(?:а|ов)?)?\.?\s*(?:{HY}|=)\s*)\d+(?:[.,]\d+)?\s*(?:ч(?:\.|ас(?:а|ов)?)?)\b",
             lambda m: f"{m.group(1)}{ru_hours}"),
            (r"\b(?:на|от)\s*\d+(?:[.,]\d+)?\s*(?:ч(?:\.|ас(?:а|ов)?)?)\b",
             lambda m: target_phrase),

            (r"\b(?:на|от)\s*\d+(?:[.,]\d+)?\s*(?:дн(?:я|ей)?|день)\b",
             lambda m: target_phrase),

            (r"\b\d+(?:[.,]\d+)?\s*час(?:а|ов)?\b(?:\s*аренды)?",
             lambda m: target_phrase),

            (r"(?:•\s*)?аренда\s*\d+(?:[.,]\d+)?\s*ч\.?\b",
             lambda m: target_phrase),
        ]

        for pat, repl in patterns:
            new_t, n = re.subn(pat, repl, t, flags=re.IGNORECASE)
            if n:
                return new_t

        if not allow_insert:
            return t

        insert = f" {target_phrase} "
        if "⏱" in t:
            return re.sub(r"\s*⏱", insert + "⏱", t, count=1)
        if target_phrase not in t:
            return (t + insert).strip()
        return t

    else:
        target_phrase = f"for {_en_duration_phrase(hours)}"

        patterns = [
            (r"\b(?:for|from)\s*\d+(?:[.,]\d+)?\s*(?:h|hr|hrs|hour|hours)\b",
             lambda m: target_phrase),
            (r"\b(?:for|from)\s*\d+(?:[.,]\d+)?\s*(?:d|day|days)\b",
             lambda m: target_phrase),
            (r"(?:•\s*)?rental\s*\d+(?:[.,]\d+)?\s*(?:h|hr|hrs)\b",
             lambda m: target_phrase),
            (r"(?:•\s*)?rental\s*\d+(?:[.,]\d+)?\s*(?:d|day|days)\b",
             lambda m: target_phrase),
            (r"\b\d+(?:[.,]\d+)?\s*(?:hour|hours|day|days)\b(?:\s*rental)?",
             lambda m: target_phrase),
        ]

        for pat, repl in patterns:
            new_t, n = re.subn(pat, repl, t, flags=re.IGNORECASE)
            if n:
                return new_t

        if not allow_insert:
            return t

        insert = f" • {target_phrase} "
        if "⏱" in t:
            return re.sub(r"\s*⏱", insert + "⏱", t, count=1)
        if target_phrase not in t:
            return (t + insert).strip()
        return t

def _build_preview_lines(base_title_ru: str, durs: List[float], price_1h: float, disc: float) -> List[str]:
    lines = []
    for h in durs:
        pr = price_1h * h
        if disc:
            pr *= (1 - disc / 100.0)
        pr_i = int(round(pr))
        label = _fmt_short_duration(h)
        if disc:
            lines.append(f"• {label} → {pr_i} (−{disc:.0f}%)")
        else:
            lines.append(f"• {label} → {pr_i}")
    return lines

# ---------- plugin init

def init_commands(cardinal: Cardinal, *args):
    if not cardinal.telegram:
        return
    tg = cardinal.telegram
    bot = tg.bot

    def cmd_lcot(m: Message):
        msg = bot.send_message(
            m.chat.id,
            "📦 Пришлите ID исходного лота. Можно несколько через запятую или пробел. Пример: `301, 305 402`.",
            parse_mode="Markdown",
            reply_markup=skb.CLEAR_STATE_BTN()
        )
        tg.set_state(m.chat.id, msg.id, m.from_user.id, STATE_WAIT_LOT)

    def handle_lot_id(m: Message):
        tg.clear_state(m.chat.id, m.from_user.id, True)
        raw = (m.text or "").strip()
        ids = []
        for t in re.split(r"[,\s;]+", raw):
            t = t.strip()
            if t.isdigit():
                ids.append(int(t))
        ids = list(dict.fromkeys(ids))
        if not ids:
            bot.send_message(m.chat.id, "❌ Не вижу ID. Пришлите один или несколько через запятую или пробел.")
            return

        bases = {}
        for lot_id in ids:
            try:
                base_lf: FunPayAPI.types.LotFields = cardinal.account.get_lot_fields(lot_id)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
                bot.send_message(m.chat.id, f"❌ Не смог получить данные лота #{lot_id}.")
                return

            fields = dict(base_lf.fields)
            title_ru = fields.get("fields[summary][ru]") or getattr(base_lf, "title_ru", "") or ""
            title_en = fields.get("fields[summary][en]") or getattr(base_lf, "title_en", "") or ""
            price_str = fields.get("price") or ""
            try:
                price_1h = float(price_str.replace(",", "."))
            except Exception:
                price_1h = float(getattr(base_lf, "price", 0.0) or 0.0)

            bases[lot_id] = {
                "fields": fields,
                "title_ru": title_ru,
                "title_en": title_en,
                "price_1h": price_1h
            }

        first_id = ids[0]
        SESSION[m.chat.id] = {
            "lot_ids": ids,
            "bases": bases,
            "price_1h": bases[first_id]["price_1h"],
            "title_ru": bases[first_id]["title_ru"],
            "title_en": bases[first_id]["title_en"],
            "durs": [],
            "disc": 0.0
        }

        msg = bot.send_message(
            m.chat.id,
            "⏱ Укажите длительности через запятую.\n"
            "Примеры: `6`, `0.5`, `6h` / `6ч`, `12 часов`, `1d` / `1д`, `7д`.\n\n"
            f"Базовая цена (за 1 час, лот #{first_id}): *{int(round(SESSION[m.chat.id]['price_1h']))}*",
            parse_mode="Markdown",
            reply_markup=skb.CLEAR_STATE_BTN()
        )
        tg.set_state(m.chat.id, msg.id, m.from_user.id, STATE_WAIT_DURS)

    def handle_durations(m: Message):
        tg.clear_state(m.chat.id, m.from_user.id, True)
        sess = SESSION.get(m.chat.id)
        if not sess:
            bot.send_message(m.chat.id, "❌ Сессия не найдена. Запустите /lcot заново.")
            return

        durs = _parse_durations(m.text)
        if not durs:
            bot.send_message(m.chat.id, "❌ Не понял длительности. Пример: `0.5, 1, 2, 3`", parse_mode="Markdown")
            return

        sess["durs"] = durs
        SESSION[m.chat.id] = sess

        msg = bot.send_message(
            m.chat.id,
            "💸 Укажите скидку в процентах от базовой цены.\n"
            "Можно так: `10` или `10%` (диапазон 0–90).",
            parse_mode="Markdown",
            reply_markup=skb.CLEAR_STATE_BTN()
        )
        tg.set_state(m.chat.id, msg.id, m.from_user.id, STATE_WAIT_DISC)

    def handle_discount(m: Message):
        tg.clear_state(m.chat.id, m.from_user.id, True)
        sess = SESSION.get(m.chat.id)
        if not sess:
            bot.send_message(m.chat.id, "❌ Сессия не найдена. Запустите /lcot заново.")
            return
        raw = (m.text or "").strip()
        raw = raw.replace(",", ".")
        m_pct = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", raw)
        if not m_pct:
            bot.send_message(m.chat.id, "❌ Скидка должна быть от 0 до 90.")
            return
        disc = float(m_pct.group(1))
        if disc < 0 or disc > 90:
            bot.send_message(m.chat.id, "❌ Скидка должна быть от 0 до 90.")
            return
        sess["disc"] = disc
        SESSION[m.chat.id] = sess

        lot_ids = sess.get("lot_ids") or [sess.get("lot_id")]
        first_id = lot_ids[0]
        lines = _build_preview_lines(sess["title_ru"], sess["durs"], sess["price_1h"], disc)

        total = len(lot_ids) * len(sess["durs"])
        kb = K()
        kb.row(B("✅ Создать", callback_data=CBT_CREATE))
        kb.row(B("❌ Отмена", callback_data=CBT_CANCEL))
        bot.send_message(
            m.chat.id,
            "🧾 *Предпросмотр*\n"
            f"Лоты: `{', '.join('#'+str(i) for i in lot_ids)}`\n"
            f"Будет создано: *{total}* шт.\n"
            f"Скидка: *{disc:.0f}%*\n"
            f"_В предпросмотре цена считается по лоту #{first_id}; при создании берутся цены каждого исходника._\n\n"
            + "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=kb
        )

    def cb_cancel(call: telebot.types.CallbackQuery):
        SESSION.pop(call.message.chat.id, None)
        try:
            bot.edit_message_text("Действие отменено.", call.message.chat.id, call.message.id)
        except Exception:
            pass
        
    def cb_create(call: telebot.types.CallbackQuery):
        chat_id = call.message.chat.id
        sess = SESSION.get(chat_id)
        if not sess:
            bot.answer_callback_query(call.id, "Сессия не найдена.")
            return

        try:
            bot.edit_message_text("⏳ Создаю лоты...", chat_id, call.message.id)
        except Exception:
            pass

        created, failed = 0, 0
        created_details = []

        disc = float(sess.get("disc", 0.0))
        durs = list(sess.get("durs") or [])

        lot_ids = sess.get("lot_ids") or [sess.get("lot_id")]
        bases = sess.get("bases")

        single_base_fields = dict(sess.get("base_fields") or {})
        single_title_ru = sess.get("title_ru") or ""
        single_title_en = sess.get("title_en") or ""
        try:
            single_price_1h = float(sess.get("price_1h") or 0.0)
        except Exception:
            single_price_1h = 0.0

        for src_id in lot_ids:
            if bases and src_id in bases:
                base = bases[src_id]
                base_fields = dict(base["fields"])
                title_ru = base.get("title_ru", "")
                title_en = base.get("title_en", "")
                try:
                    price_1h = float(base.get("price_1h") or 0.0)
                except Exception:
                    price_1h = 0.0
            else:
                base_fields = dict(single_base_fields)
                title_ru = single_title_ru
                title_en = single_title_en
                price_1h = single_price_1h

            for h in durs:
                try:
                    fields = dict(base_fields)

                    price = price_1h * float(h)
                    if disc:
                        price *= (1 - disc / 100.0)
                    fields["price"] = _fmt_price(price)

                    if title_ru:
                        fields["fields[summary][ru]"] = _replace_hours_in_title(title_ru, h, "ru")
                    if title_en:
                        fields["fields[summary][en]"] = _replace_hours_in_title(title_en, h, "en")

                    desc_ru = fields.get("fields[desc][ru]")
                    if desc_ru:
                        fields["fields[desc][ru]"] = _replace_hours_in_title(desc_ru, h, "ru", allow_insert=False)
                    desc_en = fields.get("fields[desc][en]")
                    if desc_en:
                        fields["fields[desc][en]"] = _replace_hours_in_title(desc_en, h, "en", allow_insert=False)

                    fields["offer_id"] = "0"
                    fields["csrf_token"] = cardinal.account.csrf_token

                    lot = FunPayAPI.types.LotFields(0, fields)
                    time.sleep(0.7)  # анти rate-limit
                    ret = cardinal.account.save_lot(lot)

                    if isinstance(ret, dict):
                        u = str(ret.get("url", ""))
                        m = re.search(r"(\d{6,})", u)
                        if m:
                            new_id = int(m.group(1))

                    new_id = None
                    if isinstance(ret, int) and ret > 0:
                        new_id = ret
                    elif isinstance(ret, str) and ret.isdigit():
                        new_id = int(ret)

                    if not new_id:
                        for attr in ("lot_id", "id", "offer_id"):
                            v = getattr(lot, attr, None)
                            if isinstance(v, int) and v > 0:
                                new_id = v; break
                            if isinstance(v, str) and v.isdigit() and int(v) > 0:
                                new_id = int(v); break

                    if not new_id:
                        try:
                            new_id = _guess_created_id(cardinal, fields)
                        except Exception:
                            new_id = None

                    created += 1
                    created_details.append((new_id, float(h), int(src_id)))
                except Exception as ex:
                    failed += 1
                    logger.error(f"[LCOT] error creating lot (src={src_id}, h={h}): {ex}")

        SESSION.pop(chat_id, None)
        bot.send_message(chat_id, f"✅ Готово. Создано: {created}. Ошибок: {failed}.")

        by_src: dict[int, dict] = {}
        for new_id, hours, src in created_details:
            d = by_src.setdefault(int(src), {"ids": [], "hours": []})
            d["ids"].append(new_id if new_id else None)
            d["hours"].append(hours)

        lines_ids_only = []
        for src in (lot_ids or []):
            d = by_src.get(int(src), {"ids": [], "hours": []})
            ids = [str(x) for x in d["ids"] if isinstance(x, int) and x > 0]
            if ids:
                lines_ids_only.append(f'(из "{int(src)}") ' + ", ".join(ids))
        if lines_ids_only:
            chunk, acc = [], 0
            for ln in lines_ids_only:
                if acc + len(ln) + 1 > 3500:
                    bot.send_message(chat_id, "🆕 Новые лоты (только ID):\n" + "\n".join(chunk))
                    chunk, acc = [], 0
                chunk.append(ln); acc += len(ln) + 1
            if chunk:
                bot.send_message(chat_id, "🆕 Новые лоты (только ID):\n" + "\n".join(chunk))

        lines_with_time = []
        for src in (lot_ids or []):
            d = by_src.get(int(src), {"ids": [], "hours": []})
            ids = [str(x) for x in d["ids"] if isinstance(x, int) and x > 0]
            ids_join = ", ".join(ids) if ids else "—"
            times = ", ".join(_ru_duration_phrase(h) for h in d["hours"])
            if d["hours"]:
                lines_with_time.append(f'(из "{int(src)}") ' + ids_join + " - " + times)

        if lines_with_time:
            chunk, acc = [], 0
            for ln in lines_with_time:
                if acc + len(ln) + 1 > 3500:
                    bot.send_message(chat_id, "🕒 Новые лоты (ID + длительности):\n" + "\n".join(chunk))
                    chunk, acc = [], 0
                chunk.append(ln); acc += len(ln) + 1
            if chunk:
                bot.send_message(chat_id, "🕒 Новые лоты (ID + длительности):\n" + "\n".join(chunk))

    cardinal.add_telegram_commands(UUID, [
        ("lcot", "создать копии лота для выбранных длительностей с перерасчётом цены", True),
    ])

    tg.msg_handler(cmd_lcot, commands=["lcot"])
    tg.msg_handler(handle_lot_id,  func=lambda m: tg.check_state(m.chat.id, m.from_user.id, STATE_WAIT_LOT))
    tg.msg_handler(handle_durations, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, STATE_WAIT_DURS))
    tg.msg_handler(handle_discount,  func=lambda m: tg.check_state(m.chat.id, m.from_user.id, STATE_WAIT_DISC))
    tg.cbq_handler(cb_create, lambda c: c.data == CBT_CREATE)
    tg.cbq_handler(cb_cancel, lambda c: c.data == CBT_CANCEL)


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = None
