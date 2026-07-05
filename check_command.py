"""
Лёгкий "слушатель" команд в чате с ботом: "обновить сейчас" и управление
списком Telegram-каналов (/add_channel, /remove_channel, /list_sources).

У бота нет постоянно запущенного сервера — только GitHub Actions по расписанию.
Поэтому реакция не мгновенная: этот скрипт запускается по своему отдельному
расписанию (каждые 5 минут, см. check_command.yml) и спрашивает у Telegram:
"не написала ли Лия что-то новое?". В зависимости от того, что написано,
либо запускает основной сбор вакансий (main.py) через workflow_dispatch по API,
либо добавляет/удаляет канал в sources.json, либо присылает список источников.

Этот скрипт НЕ делает Playwright/Kwork — он всего лишь быстро проверяет
сообщения и (иногда) сам список каналов, поэтому может позволить себе
запускаться часто, не тратя много времени на каждый запуск.

Задержка от нажатия кнопки/отправки команды до реакции: обычно 1-6 минут
(до 5 минут — ждём своего запуска по расписанию, плюс время на саму проверку;
запуск основного сбора вакансий по команде "обновить" — ещё +30-60 сек сверху).
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request

import main as digest_main  # переиспользуем TELEGRAM_CHANNELS и константы, ничего не запуская

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # вида "user/repo", задаёт сам Actions
GITHUB_REF_NAME = os.environ.get("GITHUB_REF_NAME", "main")  # ветка, тоже задаёт сам Actions

# Файл-имя основного workflow, который нужно запустить по команде "обновить"
DIGEST_WORKFLOW_FILE = "vacancy_digest.yml"

OFFSET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_offset.json")
SOURCES_FILE = digest_main.SOURCES_FILE  # тот же файл, что читает main.py

# Любой из этих текстов (без учёта регистра и лишних пробелов) считается командой
# "обновить сейчас". "🔄 Обновить сейчас" — текст кнопки из main.py, остальное —
# на случай, если написать вручную текстом.
TRIGGER_TEXTS = {
    "🔄 обновить сейчас",
    "обновить сейчас",
    "обновить",
    "/update",
}

# Список команд для нативного меню Telegram (иконка "/" / "Menu" рядом со строкой
# ввода в чате с ботом) — регистрируется через Bot API (setMyCommands), плюс
# используется для текстового /help, чтобы всегда быстро вспомнить, что бот умеет.
BOT_COMMANDS = [
    {"command": "update", "description": "Обновить подборку вакансий прямо сейчас"},
    {"command": "add_channel", "description": "Добавить Telegram-канал: /add_channel <username>"},
    {"command": "remove_channel", "description": "Убрать добавленный канал: /remove_channel <username>"},
    {"command": "list_sources", "description": "Показать все каналы в мониторинге"},
    {"command": "help", "description": "Показать список всех команд"},
]


def http_request(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_offset():
    if not os.path.exists(OFFSET_FILE):
        return 0
    with open(OFFSET_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("last_update_id", 0)


def save_offset(update_id):
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_update_id": update_id}, f, ensure_ascii=False, indent=2)


def get_updates(offset):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset:
        params["offset"] = offset
    url = url + "?" + urllib.parse.urlencode(params)
    data = http_request(url)
    return data.get("result", [])


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
    http_request(url, data=data)


def register_bot_commands():
    """
    Регистрирует список команд в Telegram (Bot API setMyCommands) — после этого
    у бота появляется нативное меню (иконка рядом со строкой ввода в чате),
    где видно все команды с описанием. Вызов безопасно повторять — Telegram
    просто подтверждает тот же список заново, ничего не ломается.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    data = urllib.parse.urlencode({"commands": json.dumps(BOT_COMMANDS)}).encode("utf-8")
    http_request(url, data=data)


def handle_help():
    lines = ["🤖 Доступные команды:", ""]
    for c in BOT_COMMANDS:
        lines.append(f"/{c['command']} — {c['description']}")
    lines.append("")
    lines.append("Кнопка «🔄 Обновить сейчас» под сообщениями делает то же самое, что и /update.")
    send_message("\n".join(lines))


def trigger_digest_workflow():
    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/workflows/"
        f"{DIGEST_WORKFLOW_FILE}/dispatches"
    )
    body = json.dumps({"ref": GITHUB_REF_NAME}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


# ============ Управление списком Telegram-каналов ============

def load_sources():
    if not os.path.exists(SOURCES_FILE):
        return {"telegram_channels": []}
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"telegram_channels": []}
    data.setdefault("telegram_channels", [])
    return data


def save_sources(data):
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_channel(raw):
    """Принимает '@prodjob', 'prodjob', 'https://t.me/prodjob', 'https://t.me/s/prodjob' и т.п."""
    raw = raw.strip()
    raw = re.sub(r"^https?://t\.me/(s/)?", "", raw, flags=re.IGNORECASE)
    raw = raw.lstrip("@")
    raw = raw.split("/")[0].split("?")[0].strip()
    return raw


def channel_has_public_preview(username):
    """Проверяет, что t.me/s/<username> открывается и там реально видны ПОСТЫ
    (а не только карточка "о канале"). Важно проверять именно наличие постов
    (tgme_widget_message), а не только tgme_channel_info — эта карточка есть
    почти у любого канала/чата, даже у групп-обсуждений и у каналов, которые
    отключили показ истории сообщений в публичном превью. Без реальных постов
    бот физически не сможет взять оттуда ни одной вакансии, даже если канал
    технически "открывается"."""
    url = f"https://t.me/s/{username}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; VacancyDigestBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return False
    return "tgme_widget_message" in html


def handle_add_channel(text_raw):
    parts = text_raw.split(None, 1)
    if len(parts) < 2:
        send_message(
            "Использование: /add_channel <username или ссылка на канал>\n"
            "Например: /add_channel prodjob"
        )
        return
    channel = normalize_channel(parts[1])
    if not channel:
        send_message("Не поняла имя канала. Пример: /add_channel prodjob")
        return

    all_current = digest_main.get_all_telegram_channels()
    if channel.lower() in {c.lower() for c in all_current}:
        send_message(f"Канал @{channel} уже есть в списке мониторинга.")
        return

    if not channel_has_public_preview(channel):
        send_message(
            f"Добавление источника невозможно по причине: у канала @{channel} нет "
            f"открытой веб-версии (t.me/s/{channel}) — либо в имени опечатка, либо "
            f"владелец канала отключил публичный просмотр постов. Без этого бот "
            f"физически не может читать канал без входа в него под личным аккаунтом, "
            f"а это как раз то, чего мы избегаем ради безопасности твоего Telegram."
        )
        return

    sources = load_sources()
    sources["telegram_channels"].append(channel)
    save_sources(sources)
    send_message(
        f"Готово, добавила канал @{channel} в мониторинг. "
        f"Начнёт учитываться со следующего сбора вакансий."
    )


def handle_remove_channel(text_raw):
    parts = text_raw.split(None, 1)
    if len(parts) < 2:
        send_message("Использование: /remove_channel <username>")
        return
    channel = normalize_channel(parts[1])
    sources = load_sources()

    # Сравнение без учёта регистра — ищем реально сохранённое написание, чтобы
    # /remove_channel PRODJOB тоже сработал, даже если сохранено как "prodjob".
    match = next((c for c in sources["telegram_channels"] if c.lower() == channel.lower()), None)
    if match is None:
        if channel.lower() in {c.lower() for c in digest_main.TELEGRAM_CHANNELS}:
            send_message(
                f"Канал @{channel} входит в базовый (курируемый) список в коде — "
                f"через команду его убрать нельзя, скажи мне в чате, уберу вручную."
            )
        else:
            send_message(f"Канала @{channel} нет среди добавленных вручную.")
        return
    sources["telegram_channels"].remove(match)
    save_sources(sources)
    send_message(f"Готово, убрала канал @{match} из мониторинга.")


def handle_list_sources():
    curated = digest_main.TELEGRAM_CHANNELS
    extra = load_sources()["telegram_channels"]
    lines = ["📋 Каналы в мониторинге:", "", "Базовые:"]
    lines += [f"— @{ch}" for ch in curated]
    if extra:
        lines.append("")
        lines.append("Добавленные вручную:")
        lines += [f"— @{ch}" for ch in extra]
    else:
        lines.append("")
        lines.append("Добавленных вручную пока нет.")
    send_message("\n".join(lines))


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("[error] BOT_TOKEN или CHAT_ID не заданы.", file=sys.stderr)
        return

    try:
        register_bot_commands()
    except Exception as e:
        print(f"[warn] не удалось зарегистрировать меню команд: {e}", file=sys.stderr)

    offset = load_offset()
    try:
        updates = get_updates(offset)
    except Exception as e:
        print(f"[warn] не удалось получить обновления Telegram: {e}", file=sys.stderr)
        return

    if not updates:
        print("Новых сообщений нет.")
        return

    triggered = False
    max_update_id = offset

    for upd in updates:
        max_update_id = max(max_update_id, upd.get("update_id", 0))
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(CHAT_ID):
            continue  # сообщения не из твоего чата с ботом игнорируем

        text_raw = (msg.get("text") or "").strip()
        text_lower = text_raw.lower()

        if text_lower in TRIGGER_TEXTS:
            triggered = True
        elif text_lower.startswith("/add_channel"):
            handle_add_channel(text_raw)
        elif text_lower.startswith("/remove_channel"):
            handle_remove_channel(text_raw)
        elif text_lower.startswith("/list_sources") or text_lower.startswith("/list_channels"):
            handle_list_sources()
        elif text_lower.startswith("/help") or text_lower.startswith("/menu") or text_lower.startswith("/start"):
            handle_help()

    # offset должен указывать на update_id+1 самого свежего обработанного сообщения —
    # так Telegram больше не будет присылать эти же апдейты повторно.
    save_offset(max_update_id + 1)

    if triggered:
        print("Команда 'обновить сейчас' найдена — запускаю сбор вакансий.")
        try:
            send_message("Принято, обновляю подборку вакансий сейчас…")
        except Exception as e:
            print(f"[warn] не удалось отправить подтверждение: {e}", file=sys.stderr)
        try:
            trigger_digest_workflow()
            print("Основной workflow запущен.")
        except Exception as e:
            print(f"[error] не удалось запустить основной workflow: {e}", file=sys.stderr)
    else:
        print("Обработка завершена.")


if __name__ == "__main__":
    main()
