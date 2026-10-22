"""Telegram-бот для запуска в GitHub Actions (без своего сервера).

Каждый запуск: забирает новые сообщения из Telegram, отвечает на команды
владельца, при необходимости публикует в канал и (по желанию) сохраняет
сообщения из групп в data/group_log.jsonl. Потом завершается.

Команды владельца:
    клод, подскажи <вопрос>      -> ответ ИИ
    клод, напиши пост <тема>     -> ИИ пишет черновик (в канал НЕ публикуется)
    /post <текст>                -> публикует текст в канал как есть
    /post (в ответ на черновик)  -> публикует текст того сообщения в канал
    /myid                        -> показывает Telegram ID (работает у всех)

Все секреты берутся из переменных окружения (GitHub: Settings -> Secrets).
Не вписывайте токены и ключи прямо в этот файл.
"""
import datetime
import json
import os
import pathlib

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID") or "0")
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()
PROVIDER = (os.getenv("AI_PROVIDER") or "gemini").strip().lower()
GEMINI_KEY = os.getenv("GEMINI_API_KEY") or ""
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY") or ""
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5"
LOG_GROUPS = (os.getenv("LOG_GROUPS") or "false").strip().lower() == "true"

ASK_TRIGGER = "клод, подскажи"
POST_TRIGGER = "клод, напиши пост"
LOG_FILE = pathlib.Path("data/group_log.jsonl")
CONTEXT_LINES = 40

API = f"https://api.telegram.org/bot{TOKEN}"

ASSISTANT_PROMPT = (
    "Ты помощник автора русскоязычных нарезок по Minecraft (YouTube Shorts, TikTok). "
    "Отвечай по-русски, коротко и по делу. Если дан список недавних сообщений из "
    "рабочих групп, используй его только когда он относится к вопросу, и не выдумывай "
    "того, чего в нём нет."
)
POST_PROMPT = (
    "Напиши готовый пост для Telegram-канала автора русскоязычных нарезок по Minecraft. "
    "Живо, коротко, без воды. Верни только текст поста, без пояснений."
)


def log(message):
    print(message, flush=True)


def tg(method, **params):
    """Вызов Telegram Bot API. Ошибки печатаем без адреса запроса (в нём токен)."""
    try:
        response = requests.post(f"{API}/{method}", json=params, timeout=40)
        data = response.json()
    except Exception as error:
        log(f"Telegram {method}: сетевая ошибка ({type(error).__name__})")
        return None
    if not data.get("ok"):
        log(f"Telegram {method}: {data.get('description')}")
        return None
    return data["result"]


def send(chat_id, text, reply_to=None):
    """Отправляет текст (режет на куски по 4000 символов). Возвращает True/False."""
    text = text or "(пусто)"
    all_ok = True
    for start in range(0, len(text), 4000):
        params = {"chat_id": chat_id, "text": text[start:start + 4000]}
        if reply_to and start == 0:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        if tg("sendMessage", **params) is None:
            all_ok = False
    return all_ok


def ask_ai(system_prompt, user_text):
    """Обращение к ИИ. По умолчанию Gemini, можно переключить на Claude."""
    if PROVIDER == "anthropic":
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_text}],
            },
            timeout=90,
        )
        response.raise_for_status()
        blocks = response.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"},
        json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {"maxOutputTokens": 2048},
        },
        timeout=90,
    )
    response.raise_for_status()
    parts = response.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def safe_ai(system_prompt, user_text):
    """Возвращает (ответ, None) или (None, текст_ошибки_для_владельца)."""
    try:
        return ask_ai(system_prompt, user_text), None
    except requests.HTTPError as error:
        code = error.response.status_code if error.response is not None else "?"
        log(f"ИИ вернул ошибку, код {code}")
        return None, f"ИИ вернул ошибку (код {code}). Проверьте ключ и название модели."
    except Exception as error:
        log(f"Ошибка при обращении к ИИ ({type(error).__name__})")
        return None, "Не получилось обратиться к ИИ, смотрите журнал запуска в GitHub."


def log_group_message(msg):
    """Дописывает сообщение группы в data/group_log.jsonl."""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    files = []
    for kind in ("video", "document", "audio", "voice", "animation"):
        if kind in msg:
            name = msg[kind].get("file_name") or ""
            files.append(f"{kind} {name}".strip())
    if "photo" in msg:
        files.append("photo")
    if not text and not files:
        return
    when = datetime.datetime.fromtimestamp(msg["date"], datetime.timezone.utc)
    entry = {
        "time": when.strftime("%Y-%m-%d %H:%M"),
        "chat": msg["chat"].get("title", ""),
        "from": (msg.get("from") or {}).get("first_name", ""),
        "text": text,
        "files": files,
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_context():
    """Последние сообщения из журнала групп в виде текста для ИИ."""
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-CONTEXT_LINES:]
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        files = f" [{', '.join(entry['files'])}]" if entry.get("files") else ""
        out.append(f"{entry['time']} {entry['chat']} / {entry['from']}: {entry['text']}{files}")
    return "\n".join(out)[-6000:]


def handle_message(msg):
    chat = msg["chat"]
    chat_id = chat["id"]
    message_id = msg["message_id"]
    user_id = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    low = text.lower()
    command = low.split()[0].split("@")[0] if low.startswith("/") else ""
    is_trigger = low.startswith(ASK_TRIGGER) or low.startswith(POST_TRIGGER)
    is_owner = OWNER_ID != 0 and user_id == OWNER_ID

    if command == "/myid":
        send(chat_id, f"Ваш Telegram ID: {user_id}", message_id)
        return

    if LOG_GROUPS and chat.get("type") in ("group", "supergroup") and not command and not is_trigger:
        log_group_message(msg)

    if not is_owner:
        return

    if low.startswith(ASK_TRIGGER):
        question = text[len(ASK_TRIGGER):].lstrip(" ,:—-")
        if not question:
            send(chat_id, "Что подсказать?", message_id)
            return
        context = load_context()
        if context:
            question = f"Недавние сообщения из рабочих групп:\n{context}\n\nВопрос: {question}"
        answer, error = safe_ai(ASSISTANT_PROMPT, question)
        send(chat_id, answer or error, message_id)

    elif low.startswith(POST_TRIGGER):
        topic = text[len(POST_TRIGGER):].lstrip(" ,:—-")
        if not topic:
            send(chat_id, "О чём написать пост?", message_id)
            return
        draft, error = safe_ai(POST_PROMPT, topic)
        if draft:
            send(chat_id, draft, message_id)
            send(chat_id, "Чтобы опубликовать черновик в канале, ответьте на него командой /post")
        else:
            send(chat_id, error, message_id)

    elif command == "/post":
        parts = text.split(None, 1)
        body = parts[1].strip() if len(parts) > 1 else ""
        if not body:
            body = ((msg.get("reply_to_message") or {}).get("text") or "").strip()
        if not body:
            send(chat_id, "Нечего публиковать. Напишите /post и текст или ответьте командой на черновик.", message_id)
        elif not CHANNEL_ID:
            send(chat_id, "Не задан CHANNEL_ID (секрет в GitHub).", message_id)
        elif tg("sendMessage", chat_id=CHANNEL_ID, text=body[:4000]) is not None:
            send(chat_id, "Опубликовано в канале.", message_id)
        else:
            send(chat_id, "Не получилось опубликовать. Бот должен быть администратором канала с правом публикации.", message_id)


def main():
    offset = None
    for _ in range(10):
        params = {"timeout": 0, "limit": 100, "allowed_updates": ["message", "channel_post"]}
        if offset:
            params["offset"] = offset
        updates = tg("getUpdates", **params)
        if not updates:
            break
        for update in updates:
            try:
                if "message" in update:
                    handle_message(update["message"])
                elif "channel_post" in update:
                    post_chat = update["channel_post"]["chat"]
                    log(f"Сообщение в канале: chat_id={post_chat['id']}, название={post_chat.get('title', '')}")
            except Exception as error:
                log(f"Ошибка при обработке сообщения ({type(error).__name__})")
        offset = updates[-1]["update_id"] + 1

    # Подтверждаем Telegram, что всё до offset обработано
    if offset:
        tg("getUpdates", offset=offset, timeout=0, limit=1, allowed_updates=["message", "channel_post"])
    log("Готово")


if __name__ == "__main__":
    main()
