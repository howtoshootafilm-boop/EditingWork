"""
Бот-подборщик вакансий на монтаж вертикального видео (Reels/Shorts).

Что делает:
1. Читает ПУБЛИЧНЫЕ веб-версии Telegram-каналов (t.me/s/...) и публичные страницы бирж
   (FL.ru, Weblancer, Kwork). Никакой авторизации, никакого личного аккаунта — только открытые страницы.
   Список каналов = курируемый TELEGRAM_CHANNELS ниже + добавленные вручную через
   команду /add_channel в чате с ботом (хранятся в sources.json, см. check_command.py).
2. Отбирает новые посты, которые (а) похожи на монтаж/видео по ключевым словам и
   (б) по деньгам: либо цена (цифрами/словами/пакетом с пересчётом на один ролик)
   не ниже порога, либо оплата не названа числом, но описана обобщённо ("по рынку",
   "достойная оплата") — в этом случае пост не отбрасывается, а помечается как
   вакансия без точной цифры. Если про оплату вообще ничего не сказано — пропускается.
3. Не повторяет уже показанные вакансии — по двум механизмам:
   а) по uid (та же ссылка/пост уже была) — хранится в seen.json;
   б) по содержанию (одна и та же вакансия репостнута в разных каналах/агрегаторах,
      например @montage_search дублирует часть постов с FL.ru) — сравнение текста
      через нечёткое совпадение, отпечатки хранятся там же.
4. Для каждой вакансии формирует карточку: ссылка, краткое описание, цена, контакт
   (если есть в тексте), особое условие отклика (если работодатель просит что-то
   конкретное написать/сделать), и отдельным блоком — готовый шаблон отклика для
   копирования (разный для Telegram и для бирж, см. REPLY_TEMPLATE_*).
5. Отправляет всё в Telegram через официального бота (Bot API), созданного через
   @BotFather, с кнопкой «Обновить сейчас» под каждым сообщением.

Этот файл — только сбор и отправка вакансий по расписанию (см. vacancy_digest.yml).
Команды в чате (/update, /add_channel, /remove_channel, /list_sources, /help) и кнопка
«Обновить сейчас» обрабатываются отдельным скриптом check_command.py (своё расписание,
каждые 5 минут, см. check_command.yml) — у бота нет постоянно работающего сервера,
поэтому реакция на команды не мгновенная (обычно 1-6 минут).

Настройка — в блоке CONFIG. Как добавить новый источник — см. комментарии
в конце файла, раздел "КАК ДОБАВИТЬ НОВЫЙ ИСТОЧНИК".
"""

import difflib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# ============ CONFIG ============

# Telegram-каналы для мониторинга (публичные, без логина)
TELEGRAM_CHANNELS = [
    "prodjob",
    "SearchEditorr",
    "distantsiya2",
    "mediajobs_ru",
    "videojobs",        # VIDEO & ANIMATION JOBS — моушн/видеомонтаж, релевантный
    "rueventjob",       # вакансии ивент-индустрии, регулярно есть монтаж афтермувиков
    "motionhunter",     # моушн-дизайн/видеопродакшн вакансии
    "montage_search",   # узкотематический канал по поиску монтажёров (частично дублирует FL.ru — на это и рассчитана дедупликация)
]

# Статические страницы (обычный HTML, без JS) — регистр (сайт, url, паттерн ссылки на объявление)
STATIC_SOURCES = [
    {
        "name": "fl.ru",
        "url": "https://www.fl.ru/projects/category/audio-video-photo/videomontazher/",
        "link_pattern": r'https://www\.fl\.ru/projects/(\d+)/[^"\s]+',
    },
    {
        "name": "weblancer",
        "url": "https://www.weblancer.net/freelance/videomontazh-41/",
        "link_pattern": r'https://www\.weblancer\.net/freelance/videomontazh-41/[a-z0-9\-]+-(\d+)/',
    },
]

# Страницы, требующие рендеринга JavaScript (Kwork отдаёт пустой HTML без браузера)
JS_SOURCES = [
    {
        "name": "kwork",
        "url": "https://kwork.ru/projects?c=audio-video",
        "link_pattern": r'https://kwork\.ru/projects/(\d+)[^"\s]*',
    },
]

# Ключевые слова релевантности — пост должен содержать хотя бы одно (без регистра),
# иначе пропускается. Это защищает от шума, если источник не строго профильный.
KEYWORDS = [
    "монтаж", "видеомонтаж", "монтажер", "монтажёр", "editor", "video edit",
    "reels", "reel", "рилс", "shorts", "шортс", "tiktok", "тикток",
    "motion", "моушн", "видеоролик", "видеограф",
    "вертикальный ролик", "вертикальное видео", "вертикальный видеоролик",
    "вертикальный формат", "вертикальный контент", "вертикалка",
]

# Минимальная цена за ролик, ниже которой вакансию пропускаем.
# Указаны отдельно для рублёвых и долларовых вакансий (курс сильно меняется,
# поэтому не пересчитываем автоматически — для проектов в $ порог задан отдельно).
MIN_PRICE_RUB = 1000
MIN_PRICE_USD = 15

# Портфолио — подставляется в шаблон отклика.
# Прайс-лист в текст отклика больше не зашиваем: на вакансии из Telegram он
# прикладывается отдельным фото (плюс есть в портфолио), на биржах (FL.ru/Kwork/
# Weblancer) стоимость называется вручную под конкретное ТЗ.
PORTFOLIO_LINK = "https://t.me/kamonvsezanato"

# Шаблон для вакансий из Telegram-каналов: напоминание про прайс-фото + защита
# от недобросовестных заказчиков через тестовое с водяным знаком.
REPLY_TEMPLATE_TELEGRAM = (
    "Здравствуйте! Готова взяться за монтаж вашего ролика.\n"
    f"Портфолио: {PORTFOLIO_LINK}\n"
    "Прайс на услуги приложу отдельным фото.\n"
    "Если нужно тестовое задание — сделаю, но пришлю результат с водяным знаком "
    "(уберу после того как договоримся о сотрудничестве).\n"
    "Пришлите, пожалуйста, исходники и ТЗ — отвечу с деталями по срокам."
)

# Шаблон для вакансий с бирж фриланса: там стоимость каждый раз называется
# вручную под конкретное ТЗ, поэтому прайс и водяной знак не упоминаем.
REPLY_TEMPLATE_EXCHANGE = (
    "Здравствуйте! Готова взяться за монтаж вашего ролика.\n"
    f"Портфолио: {PORTFOLIO_LINK}\n"
    "Пришлите, пожалуйста, исходники и ТЗ — рассчитаю сроки и стоимость."
)

# Telegram Bot API — токен и chat_id берутся из переменных окружения
# (задаются как GitHub Secrets, см. DEPLOY.md)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

# Каналы, добавленные вручную через команду /add_channel в чате с ботом (см. check_command.py).
# Хранятся отдельно от куратируемого списка TELEGRAM_CHANNELS выше, чтобы не путать
# "проверенные вместе с Лией" источники и "добавленные ей самой на лету".
SOURCES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.json")

# Если новых вакансий нет — присылать короткое уведомление, что бот всё же отработал
NOTIFY_WHEN_EMPTY = True

# Если Kwork/другой JS-источник не смог обработаться (нет Playwright, сайт изменился и т.п.) —
# не роняем весь запуск, просто пропускаем этот источник и пишем предупреждение в лог.
FAIL_SILENTLY_ON_JS_SOURCES = True

# Дедупликация по содержанию: если два поста (из разных каналов/бирж) совпадают
# по тексту на столько-то (0..1) — считаем их одной и той же вакансией и шлём только
# первую встреченную копию. 0.85 — довольно строго (мелкие правки/эмодзи не собьют),
# но разные вакансии не склеятся.
DUPLICATE_SIMILARITY_THRESHOLD = 0.85

# Сколько последних отпечатков хранить между запусками (ограничивает размер seen.json
# и время сравнения — сравнивать с вакансиями недельной давности смысла нет).
MAX_STORED_FINGERPRINTS = 500


# ============ HTTP helper (без внешних библиотек) ============

def http_get(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; VacancyDigestBot/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ============ Парсинг цены и релевантности ============

# Рубли цифрами — валюта после числа: "2000 ₽", "500 руб", "500р"
RUB_RE = re.compile(r"(\d[\d\s]{1,7})\s*(?:₽|руб\.?|р\.?\b)", re.IGNORECASE)
# Доллары — валюта до или после числа: "$500", "от $800", "500 usd", "500$"
USD_RE = re.compile(
    r"(?:\$\s?(\d[\d\s]{1,7}))|(?:(\d[\d\s]{1,7})\s?(?:\$|usd\b|usdt\b))",
    re.IGNORECASE,
)
# Цифра + слово "тысяч(а/и)" без явной валюты рядом — "135 тысяч", "20 тысяч рублей".
# В контексте вакансии это всегда рубли, отдельная валюта не нужна.
DIGIT_THOUSAND_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*тысяч[а-яё]*", re.IGNORECASE)
# Сокращения "тысяч" — "50т.р.", "50 т.р.", "50тыс", "50 тыс."
ABBREV_THOUSAND_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:т\.?\s?р\.?|тыс\.?)\b", re.IGNORECASE)

# Числа, записанные словами (без цифр вообще) — "пять тысяч", "полторы тысячи",
# "двадцать тысяч рублей". Разбираем только диапазон, реалистичный для цены за
# один ролик/пакет роликов (единицы — сотни тысяч), не претендуя на полный
# грамматический разбор всех числительных русского языка.
RU_UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
}
RU_HALF = {"полтора": 1.5, "полторы": 1.5}


def _parse_word_number_run(tokens):
    """Разбирает подряд идущие слова-числительные ('двадцать', 'пять', 'тысяч')
    в одно число. Возвращает (число, сколько токенов реально использовано)."""
    total = 0.0
    current = 0.0
    used = 0
    for w in tokens:
        if w in RU_HALF:
            current += RU_HALF[w]
        elif w in RU_UNITS:
            current += RU_UNITS[w]
        elif w.startswith("тысяч"):
            total += (current if current else 1) * 1000
            current = 0.0
        else:
            break
        used += 1
    total += current
    if used == 0:
        return None, 0
    return total, used


def find_word_prices_rub(text):
    """Ищет цены, написанные словами ('пять тысяч рублей', 'полторы тысячи за
    ролик'), а не цифрами. Число считается ценой, если сразу за ним идёт явное
    указание валюты (руб/₽/р), либо в самом числе есть 'тысяч' — этого слова
    в вакансиях почти всегда достаточно, чтобы понять, что речь о деньгах."""
    words = list(re.finditer(r"[а-яёА-ЯЁ]+", text))
    results = []
    i = 0
    while i < len(words):
        tok = words[i].group(0).lower()
        if tok in RU_UNITS or tok in RU_HALF or tok.startswith("тысяч"):
            run = []
            j = i
            while j < len(words):
                t = words[j].group(0).lower()
                if t in RU_UNITS or t in RU_HALF or t.startswith("тысяч"):
                    run.append(t)
                    j += 1
                else:
                    break
            value, used = _parse_word_number_run(run)
            if value:
                has_thousand = any(t.startswith("тысяч") for t in run)
                end_pos = words[j - 1].end()
                tail = text[end_pos:end_pos + 20].lower()
                has_currency_after = bool(re.match(r"\s*(₽|руб[а-яё]*\.?|р\.?\b)", tail))
                if has_thousand or has_currency_after:
                    results.append(int(value))
            i = j
        else:
            i += 1
    return results


def _find_usd_amounts(text):
    values = []
    for m in USD_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        v = _to_int(raw)
        if v is not None:
            values.append(v)
    return values


def _find_all_rub_amounts(text):
    values = []
    for m in RUB_RE.finditer(text):
        v = _to_int(m.group(1))
        if v is not None:
            values.append(v)
    for m in DIGIT_THOUSAND_RE.finditer(text):
        v = _to_float(m.group(1))
        if v is not None:
            values.append(int(v * 1000))
    for m in ABBREV_THOUSAND_RE.finditer(text):
        v = _to_float(m.group(1))
        if v is not None:
            values.append(int(v * 1000))
    values.extend(find_word_prices_rub(text))
    return values


def _to_int(raw):
    raw = raw.replace(" ", "").replace("\xa0", "")
    try:
        return int(raw)
    except ValueError:
        return None


def _to_float(raw):
    raw = raw.replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


# Пакетная/абонентская оплата: "за 10 роликов", "40-50 видео в месяц", "около 75 рилсов".
# Эти паттерны специально "заякорены" словами за/в месяц/около/примерно, а не просто
# "любое число рядом со словом видео" — иначе случайное упоминание количества где-то
# в другом контексте (например, "3 видео уже отсняты") ошибочно превратилось бы
# в пересчёт цены.
_UNIT_WORD = r"(?:роликов|ролика|рилсов|рилса|видео|видосов|шортсов|тиктоков|клипов)"
PACKAGE_COUNT_PATTERNS = [
    re.compile(rf"за\s+(\d{{1,4}})(?:\s*[-–—]\s*(\d{{1,4}}))?\s*{_UNIT_WORD}", re.IGNORECASE),
    re.compile(rf"(\d{{1,4}})(?:\s*[-–—]\s*(\d{{1,4}}))?\s*{_UNIT_WORD}\s+в\s+месяц", re.IGNORECASE),
    re.compile(rf"около\s+(\d{{1,4}})(?:\s*[-–—]\s*(\d{{1,4}}))?\s*{_UNIT_WORD}", re.IGNORECASE),
    re.compile(rf"примерно\s+(\d{{1,4}})(?:\s*[-–—]\s*(\d{{1,4}}))?\s*{_UNIT_WORD}", re.IGNORECASE),
]

# Если сумма явно помечена как цена ЗА ОДИН ролик/видео ("2000 руб/ролик",
# "$50 за видео") — доверяем этому и не пытаемся её ещё раз "распаковать"
# как будто это общая сумма пакета, даже если где-то рядом есть число+"видео".
EXPLICIT_PER_UNIT_RE = re.compile(
    r"(?:₽|руб[а-яё]*\.?|р\.?|\$)\s*(?:/|за)\s*(?:ролик|видео|рилс|шортс|тикток|клип)",
    re.IGNORECASE,
)

# Цену не назвали конкретно, но и не обошли молчанием — общие обещания вида
# "оплата по рынку", "достойная оплата" и т.п. Такие вакансии не отбрасываем
# (в отличие от полного отсутствия упоминания оплаты), а показываем как есть,
# честно указывая, что цифры нет, и просим уточнить её в шаблоне отклика.
VAGUE_PRICE_RE = re.compile(
    r"оплата\s+по\s+рынку|по\s+рынку|рыночн(?:ая|ые)\s+(?:оплата|расценк[а-яё]*)"
    r"|достойная\s+оплата|хорошая\s+оплата|адекватная\s+оплата"
    r"|оплата\s+(?:обсуждается|индивидуальн[а-яё]*|по\s+договор[её]нности)"
    r"|индивидуальные\s+условия\s+оплаты",
    re.IGNORECASE,
)


def _extract_package_price(text):
    """Если в тексте есть пакетная/абонентская привязка количества роликов —
    считает цену за один ролик = общая сумма / количество (при диапазоне
    количества, например "40-50", берёт большее число — самый осторожный
    вариант: чем больше роликов за те же деньги, тем меньше цена за штуку).
    Возвращает (цена_за_ролик, валюта, пояснение) либо None, если пакетного
    признака нет."""
    if EXPLICIT_PER_UNIT_RE.search(text):
        return None  # цена уже явно указана как "за один", пересчитывать не нужно

    max_count = 0
    for pattern in PACKAGE_COUNT_PATTERNS:
        for m in pattern.finditer(text):
            vals = [int(v) for v in m.groups() if v]
            if vals:
                max_count = max(max_count, max(vals))
    if max_count <= 0:
        return None

    usd_values = _find_usd_amounts(text)
    if usd_values:
        total = max(usd_values)
        currency = "USD"
        symbol = "$"
    else:
        rub_values = _find_all_rub_amounts(text)
        if not rub_values:
            return None
        total = max(rub_values)
        currency = "RUB"
        symbol = "₽"

    per_unit = total / max_count
    per_unit = round(per_unit, 2) if currency == "USD" else int(round(per_unit))
    note = f"пересчитано из {int(total)} {symbol} за {max_count} роликов"
    return per_unit, currency, note


def extract_price(text):
    """
    Возвращает (цена_за_ролик, валюта, пояснение) либо (None, None, None), если
    об оплате вообще ничего не сказано.

    Порядок проверки:
    1. Пакетная/абонентская оплата ("10000 за 10 роликов" и т.п.) — пересчитываем
       в цену за штуку.
    2. Обычная разовая цена (цифрами, "тысячами" или словами) — берём наименьшую
       упомянутую сумму. Доллары приоритетнее рублей, если встречаются оба варианта.
    3. Ничего конкретного, но есть общая фраза про оплату ("по рынку", "достойная
       оплата") — валюта помечается как 'VAGUE', сумма None, пояснение — сама фраза.
    """
    package = _extract_package_price(text)
    if package is not None:
        return package

    usd_values = _find_usd_amounts(text)
    if usd_values:
        return min(usd_values), "USD", None

    rub_values = _find_all_rub_amounts(text)
    if rub_values:
        return min(rub_values), "RUB", None

    m = VAGUE_PRICE_RE.search(text)
    if m:
        return None, "VAGUE", m.group(0).strip()

    return None, None, None


def price_passes_threshold(amount, currency):
    if currency == "VAGUE":
        return True  # конкретной цифры нет, но и явного "мало платим" тоже — пропускаем
    if currency == "USD":
        return amount >= MIN_PRICE_USD
    if currency == "RUB":
        return amount >= MIN_PRICE_RUB
    return False


def is_relevant(text):
    low = text.lower()
    return any(kw in low for kw in KEYWORDS)


def clean_text(text, limit=500):
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


# ============ Краткое описание и контакт для карточки вакансии ============

# Источники-биржи, где отклик всегда идёт через сайт (регистрация/форма), а не напрямую —
# даже если в тексте объявления случайно мелькнёт какой-то @ или номер, это не реальный
# контакт для связи по этой конкретной вакансии.
SOURCES_WITHOUT_DIRECT_CONTACT = {"fl.ru", "weblancer", "kwork"}

CONTACT_HINT_RE = re.compile(
    r"(?:контакт|связь|пиш[а-я]*|отклик[а-я]*|закидывайте|присылайте|резюме)"
    r"[^\n@]{0,40}?(@[a-zA-Z][a-zA-Z0-9_]{3,31}|https?://t\.me/[a-zA-Z0-9_]+)",
    re.IGNORECASE,
)
TG_MENTION_RE = re.compile(r"(@[a-zA-Z][a-zA-Z0-9_]{3,31}|https?://t\.me/[a-zA-Z0-9_]+)")
PHONE_RE = re.compile(r"(\+?\d[\d\-\s()]{8,14}\d)")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def extract_contact(text, source_name):
    """
    Возвращает контакт для связи (строку) или None, если в объявлении его нет
    или отклик по этому источнику всегда идёт через сайт биржи.
    """
    if source_name in SOURCES_WITHOUT_DIRECT_CONTACT:
        return None

    m = CONTACT_HINT_RE.search(text)
    if m:
        return m.group(1)

    mentions = TG_MENTION_RE.findall(text)
    if mentions:
        return mentions[-1]  # обычно контакт указывают ближе к концу поста

    m = EMAIL_RE.search(text)
    if m:
        return m.group(0)

    m = PHONE_RE.search(text)
    if m:
        return m.group(1).strip()

    return None


# Особые условия отклика — работодатель просит что-то конкретное сделать/написать
# в отклике, либо ставит жёсткое условие, при невыполнении которого отклик не
# рассматривается вовсе. Ловим и то, и другое одним механизмом: находим "триггерную"
# фразу и забираем текст, идущий сразу за ней — это и есть суть условия.
SPECIAL_REQUIREMENTS_RE = re.compile(
    r"(?:"
    r"отклик[а-яё]*\s+без\s+ответ[а-яё]*\s+на\s+(?:эти\s+)?вопрос[а-яё]*[^\n:]*:"
    r"|без\s+ответ[а-яё]*\s+на\s+(?:эти\s+)?вопрос[а-яё]*[^\n]*?не\s+буд[а-яё]*\s+рассматрива[а-яё]*:?"
    r"|при\s+отклике\s+(?:укажите|напишите|обязательно)\s*:?"
    r"|в\s+отклике\s+(?:укажите|напишите|обязательно\s+(?:укажите|напишите))\s*:?"
    r"|укажите\s+в\s+отклике\s*:?"
    r"|напишите\s+в\s+отклике\s*:?"
    r"|просьба\s+(?:указать|написать|уточнить)[^\n]*"
    r"|ответьте\s+на\s+(?:следующие\s+)?вопрос[а-яё]*\s*:?"
    r"|обязательно\s+(?:укажите|напишите|ответьте)\s*:?"
    r"|важно\s*:"
    r"|если\s+(?:у\s+вас\s+нет|нет)[^\n]{0,80}?не\s+отклика[а-яё]*"
    r"|не\s+отклика[а-яё]*,?\s*если[^\n]{0,100}"
    r")",
    re.IGNORECASE,
)


def extract_special_requirements(text, limit=350):
    """
    Возвращает текст особого условия/просьбы к отклику (например, список вопросов,
    на которые нужно ответить, или предупреждение вида «если нет опыта — не
    откликайтесь»), либо None, если в объявлении такого не найдено.
    """
    m = SPECIAL_REQUIREMENTS_RE.search(text)
    if not m:
        return None
    body = text[m.end():]

    # Если следом идёт контактная информация — обрезаем на ней: контакт уже
    # выводится отдельным полем, дублировать его внутри условия не нужно.
    cut_at = None
    cm = CONTACT_HINT_RE.search(body)
    if cm:
        cut_at = cm.start()
    else:
        tm = TG_MENTION_RE.search(body)
        if tm:
            cut_at = tm.start()
    if cut_at is not None:
        body = body[:cut_at]

    body = body.strip(" :\n-–—")
    if not body:
        return None
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + "…"
    return body


def make_short_description(text, max_sentences=2, limit=220):
    """Первые 1-2 предложения текста вакансии — без цены/контактов/лишних деталей."""
    body = TG_MENTION_RE.sub(" ", text)
    body = re.sub(r"https?://\S+", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    sentences = re.split(r"(?<=[.!?])\s+", body)
    sentences = [s.strip() for s in sentences if s.strip()]
    desc = " ".join(sentences[:max_sentences]).strip()
    if not desc:
        desc = clean_text(text, limit)
    if len(desc) > limit:
        desc = desc[:limit].rsplit(" ", 1)[0] + "…"
    return desc


# ============ Дедупликация одинаковых вакансий из разных источников ============

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+"
)


def make_fingerprint(text):
    """
    Приводит текст вакансии к "ядру" для сравнения: без эмодзи/ссылок/лишних пробелов,
    в нижнем регистре. Разные каналы могут по-разному оформлять один и тот же пост
    (добавлять свою шапку, эмодзи, обрезать конец) — отпечаток должен это игнорировать.
    """
    t = text.lower()
    t = _EMOJI_RE.sub(" ", t)
    t = re.sub(r"https?://\S+", " ", t)          # ссылки могут отличаться (реф-метки и т.п.)
    t = re.sub(r"[^a-zа-яё0-9\s]", " ", t)         # знаки препинания долой
    t = re.sub(r"\s+", " ", t).strip()
    return t[:400]  # ядро текста; достаточно для сравнения, не раздувает объём хранения


def is_duplicate_content(fingerprint, known_fingerprints):
    """True, если fingerprint достаточно похож на что-то уже отправленное/увиденное."""
    if not fingerprint:
        return False
    for known in known_fingerprints:
        ratio = difflib.SequenceMatcher(None, fingerprint, known).ratio()
        if ratio >= DUPLICATE_SIMILARITY_THRESHOLD:
            return True
    return False


# ============ Источник: публичные Telegram-каналы ============

def fetch_telegram_channel(channel):
    """Возвращает список (uid, link, text) из публичной веб-версии канала."""
    url = f"https://t.me/s/{channel}"
    try:
        html = http_get(url)
    except Exception as e:
        print(f"[warn] не удалось открыть {url}: {e}", file=sys.stderr)
        return []

    items = []
    for block in re.split(r'(?=data-post="' + re.escape(channel) + r'/)', html):
        m_id = re.search(r'data-post="' + re.escape(channel) + r'/(\d+)"', block)
        if not m_id:
            continue
        post_id = m_id.group(1)
        m_text = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block,
            re.DOTALL,
        )
        if not m_text:
            continue
        raw_html = m_text.group(1)
        text = re.sub(r"<br\s*/?>", "\n", raw_html)
        text = re.sub(r"<[^>]+>", "", text)
        text = (
            text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
        )
        # limit=4000: практически без обрезки для обычного поста. Раньше здесь стоял
        # limit=500, из-за чего контакт/особые условия отклика, если они были ближе
        # к концу длинного поста, обрезались ДО того, как extract_contact() и
        # extract_special_requirements() успевали их увидеть. Итоговое сообщение
        # всё равно остаётся коротким — его длину контролируют make_short_description()
        # (описание, 220 символов) и extract_special_requirements() (350 символов)
        # уже на выходе, а не здесь, на входе.
        text = clean_text(text, limit=4000)
        link = f"https://t.me/{channel}/{post_id}"
        items.append((f"tg:{channel}:{post_id}", link, text))
    return items


# ============ Источник: обычные (не-JS) страницы бирж ============

def extract_listing_items(html, source_name, link_pattern):
    """Общая функция: ищет ссылки по паттерну, вытаскивает текст вокруг них.

    Окно 900/900 символов сырого HTML вокруг ссылки (а не 500/500, как раньше) и
    итоговый лимит 1500 (а не 400) — страницы-агрегаторы (FL.ru, Weblancer) склеивают
    много объявлений подряд, и при маленьком окне текст конкретного объявления мог
    обрезаться раньше контакта или условий отклика. Итоговое сообщение в Telegram
    от этого не удлиняется — его размер ограничивают make_short_description() и
    extract_special_requirements() уже при формировании карточки."""
    items = []
    seen_ids = set()
    for m in re.finditer(link_pattern, html):
        item_id = m.group(1) if m.groups() else m.group(0)
        uid = f"{source_name}:{item_id}"
        if uid in seen_ids:
            continue
        seen_ids.add(uid)
        start = max(0, m.start() - 900)
        end = min(len(html), m.end() + 900)
        context = re.sub(r"<[^>]+>", " ", html[start:end])
        context = re.sub(r"&nbsp;|&amp;|&quot;", " ", context)
        items.append((uid, m.group(0), clean_text(context, 1500)))
    return items


def fetch_static_source(source):
    try:
        html = http_get(source["url"])
    except Exception as e:
        print(f"[warn] не удалось открыть {source['url']}: {e}", file=sys.stderr)
        return []
    return extract_listing_items(html, source["name"], source["link_pattern"])


# ============ Источник: страницы с JS-рендерингом (Kwork и подобные) ============

def fetch_js_source(source):
    """Рендерит страницу через headless-браузер (Playwright) и парсит так же, как статику."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        msg = (
            f"[warn] Playwright не установлен — источник '{source['name']}' пропущен. "
            f"Добавьте playwright в requirements.txt и шаг установки браузера в workflow."
        )
        print(msg, file=sys.stderr)
        if FAIL_SILENTLY_ON_JS_SOURCES:
            return []
        raise

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent="Mozilla/5.0 (compatible; VacancyDigestBot/1.0)")
            page.goto(source["url"], timeout=30000)
            page.wait_for_timeout(4000)  # дать JS время отрисовать карточки
            html = page.content()
            browser.close()
    except Exception as e:
        print(f"[warn] не удалось отрендерить {source['url']}: {e}", file=sys.stderr)
        if FAIL_SILENTLY_ON_JS_SOURCES:
            return []
        raise

    return extract_listing_items(html, source["name"], source["link_pattern"])


# ============ Состояние (что уже показывали) ============
#
# Формат seen.json: {"uids": [...], "fingerprints": [...]}
# uids — точные идентификаторы постов (канал+id), fingerprints — "ядра" текста
# для отлова дублей из разных источников. Старый формат (просто список uid) тоже
# поддерживается — при первом запуске после обновления он просто конвертируется.

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"uids": set(), "fingerprints": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):  # старый формат — просто список uid
        return {"uids": set(data), "fingerprints": []}
    return {"uids": set(data.get("uids", [])), "fingerprints": list(data.get("fingerprints", []))}


def save_state(state):
    fingerprints = state["fingerprints"][-MAX_STORED_FINGERPRINTS:]  # не даём файлу расти бесконечно
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"uids": sorted(state["uids"]), "fingerprints": fingerprints},
            f,
            ensure_ascii=False,
            indent=2,
        )


# ============ Источники, добавленные вручную через /add_channel ============
#
# Формат sources.json: {"telegram_channels": ["username1", "username2", ...]}
# Пополняется командой /add_channel в чате с ботом (см. check_command.py) —
# сам main.py этот файл только читает, не изменяет.

def load_extra_channels():
    if not os.path.exists(SOURCES_FILE):
        return []
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return list(data.get("telegram_channels", []))


def get_all_telegram_channels():
    """Курируемый список + добавленные вручную, без повторов, с сохранением порядка.

    Сравнение без учёта регистра: Telegram не различает регистр в username канала,
    а "SearchEditorr" и "searcheditorr" — один и тот же канал. Без этой проверки
    один канал мог бы попасть в список дважды под разным регистром и обрабатываться
    вдвое (не ломает результат — дедуп по контенту всё равно отфильтрует дубль —
    но тратит время впустую и путает список в /list_sources)."""
    combined = list(TELEGRAM_CHANNELS)
    combined_lower = {ch.lower() for ch in combined}
    for ch in load_extra_channels():
        if ch.lower() not in combined_lower:
            combined.append(ch)
            combined_lower.add(ch.lower())
    return combined


# ============ Отправка в Telegram ============

# Текст кнопки "обновить сейчас" — должен совпадать с тем, что распознаёт check_command.py
UPDATE_BUTTON_TEXT = "🔄 Обновить сейчас"

# Постоянная клавиатура с одной кнопкой — прикрепляем к каждому сообщению, чтобы
# кнопка всегда была под рукой в чате с ботом. Нажатие отправляет этот же текст
# обычным сообщением, его и ловит отдельный workflow-опрос (см. check_command.py).
_UPDATE_KEYBOARD = json.dumps({
    "keyboard": [[{"text": UPDATE_BUTTON_TEXT}]],
    "resize_keyboard": True,
    "is_persistent": True,
})


def send_telegram_message(text, with_button=True):
    if not BOT_TOKEN or not CHAT_ID:
        print("[error] BOT_TOKEN или CHAT_ID не заданы — сообщение не отправлено.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    if with_button:
        payload["reply_markup"] = _UPDATE_KEYBOARD
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as e:
        print(f"[error] не удалось отправить сообщение в Telegram: {e}", file=sys.stderr)


# ============ Основная логика ============

def source_name_from_uid(uid):
    """tg:канал:id -> 'telegram'; fl.ru:id / weblancer:id / kwork:id -> имя источника как есть."""
    if uid.startswith("tg:"):
        return "telegram"
    return uid.split(":", 1)[0]


def build_message(link, description, amount, currency, contact, source_name,
                   special_requirements=None, price_note=None):
    vague_price = currency == "VAGUE"
    if vague_price:
        phrase = price_note or "оплата по рынку"
        price_str = f"конкретная сумма не указана — в объявлении: «{phrase}»"
    elif amount is None:
        price_str = "не указана"
    elif currency == "USD":
        price_str = f"${amount}"
        if price_note:
            price_str += f" ({price_note})"
    else:
        price_str = f"{amount} ₽"
        if price_note:
            price_str += f" ({price_note})"

    if contact:
        contact_str = contact
    elif source_name in SOURCES_WITHOUT_DIRECT_CONTACT:
        contact_str = f"не указан — отклик через сайт по ссылке выше ({source_name})"
    else:
        contact_str = "не указан в объявлении"

    # На биржах (FL.ru/Kwork/Weblancer) стоимость называется вручную под ТЗ — там
    # используем шаблон без прайса и без пункта про тестовое с водяным знаком.
    reply_template = (
        REPLY_TEMPLATE_EXCHANGE if source_name in SOURCES_WITHOUT_DIRECT_CONTACT
        else REPLY_TEMPLATE_TELEGRAM
    )

    # Особые условия отклика — отдельным пунктом в карточке (чтобы сразу было видно,
    # что тут не обычная вакансия), и ЕЩЁ РАЗ встроены прямо в копируемый блок шаблона —
    # чтобы при копировании отклика сообщение сразу собиралось с местом под ответ,
    # а не приходилось возвращаться к исходному объявлению.
    special_line = ""
    reply_block = reply_template
    if special_requirements:
        special_line = f"❗ Особое условие отклика: {special_requirements}\n"
        reply_block = (
            f"{reply_block}\n\n"
            f"❗ Отдельно по вашему условию:\n{special_requirements}\n"
            f"(впишите сюда свой ответ/подтверждение перед отправкой)"
        )

    # Если конкретная цена не указана — прямо в шаблоне отклика спрашиваем её у
    # заказчика, вместо того чтобы откликаться вслепую.
    if vague_price:
        reply_block = (
            f"{reply_block}\n\n"
            f"Мой прайс приложен фотографией — подскажите, пожалуйста, "
            f"какая оплата за ролик у вас предусмотрена?"
        )

    # Порядок карточки: 1) ссылка 2) краткое описание 3) стоимость 4) контакт
    # 5) особое условие (если есть), затем ОТДЕЛЬНЫМ блоком — шаблон отклика,
    # чтобы его было удобно скопировать одним куском.
    return (
        f"🎬 Новая вакансия\n\n"
        f"🔗 Ссылка: {link}\n"
        f"📝 Описание: {description}\n"
        f"💰 Оплата: {price_str}\n"
        f"📩 Контакт: {contact_str}\n"
        f"{special_line}\n"
        f"— — — шаблон отклика (скопировать) — — —\n"
        f"{reply_block}"
    )


def main():
    state = load_state()
    seen_uids = state["uids"]
    fingerprints = state["fingerprints"]  # используем и дополняем по ходу запуска
    new_uids = set(seen_uids)
    sent_count = 0
    duplicate_count = 0

    all_items = []
    for channel in get_all_telegram_channels():
        all_items.extend(fetch_telegram_channel(channel))
        time.sleep(1)
    for source in STATIC_SOURCES:
        all_items.extend(fetch_static_source(source))
        time.sleep(1)
    for source in JS_SOURCES:
        all_items.extend(fetch_js_source(source))
        time.sleep(1)

    for uid, link, text in all_items:
        if uid in seen_uids:
            continue
        new_uids.add(uid)

        if not is_relevant(text):
            continue  # не про монтаж/видео — пропускаем как шум

        amount, currency, price_note = extract_price(text)
        if amount is None and currency != "VAGUE":
            continue  # об оплате вообще ничего не сказано — пропускаем, как договорились
        if not price_passes_threshold(amount, currency):
            continue  # ниже рыночного порога (для VAGUE порог не проверяется — пропускаем)

        # Проверка на дубль по содержанию (та же вакансия из другого канала/биржи)
        fp = make_fingerprint(text)
        if is_duplicate_content(fp, fingerprints):
            duplicate_count += 1
            continue
        fingerprints.append(fp)

        source_name = source_name_from_uid(uid)
        description = make_short_description(text)
        contact = extract_contact(text, source_name)
        special_requirements = extract_special_requirements(text)

        send_telegram_message(
            build_message(
                link, description, amount, currency, contact, source_name,
                special_requirements=special_requirements, price_note=price_note,
            )
        )
        sent_count += 1
        time.sleep(1)

    if sent_count == 0 and NOTIFY_WHEN_EMPTY:
        send_telegram_message("Проверка прошла — новых подходящих вакансий не нашлось.")

    save_state({"uids": new_uids, "fingerprints": fingerprints})
    print(
        f"Готово. Новых отправлено: {sent_count}. Дублей отфильтровано: {duplicate_count}. "
        f"Всего просмотрено: {len(all_items)}."
    )


if __name__ == "__main__":
    main()


# ============ КАК ДОБАВИТЬ НОВЫЙ ИСТОЧНИК ============
#
# 1. Telegram-канал (публичный, с открытым просмотром t.me/s/<username>):
#    Быстрее всего — команда /add_channel <username> прямо в чате с ботом (бот сам
#    проверит, что канал существует и открыт, см. check_command.py). Если нужно
#    внести канал в куратируемый список в коде — добавьте его username строкой
#    в TELEGRAM_CHANNELS выше.
#
# 2. Обычный сайт-биржа (открывается и без JS показывает объявления, как FL.ru):
#    Добавьте словарь в STATIC_SOURCES:
#      {"name": "мой_сайт", "url": "...", "link_pattern": r'https://.../(\d+)...'}
#    link_pattern — регулярное выражение, которое находит ссылки на конкретные объявления
#    на странице (число в скобках — просто для уникального ID, не обязательно значимое).
#
# 3. Сайт с JS-рендерингом (как Kwork — данные подгружаются скриптом, обычный запрос
#    видит пустую страницу):
#    Добавьте так же, как STATIC_SOURCES, но в список JS_SOURCES.
#    Проверить, JS-рендеринг или нет: откройте страницу в браузере с отключённым
#    JavaScript (или просто curl её) — если объявлений не видно, это JS_SOURCES.
#
# 4. Зарубежные фриланс-биржи (Upwork, Fiverr и т.д.):
#    Технически подключаются так же (обычно как JS_SOURCES, у большинства бирж SPA-интерфейс).
#    Цены там чаще в $ — extract_price() их уже понимает и сравнивает с MIN_PRICE_USD
#    отдельно от рублёвого порога (см. блок CONFIG), пересчёт курса не нужен.
