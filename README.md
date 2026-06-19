# GitHub + Claude: Telegram-tjänster (användning, inte försäljning)

Скрипт создаёт **лендинг для доступа к твоим Telegram-ботам** — не для продажи самих ботов.

**Бизнес-модель:** клиент платит за **использование** (запросы, генерации, кредиты), а не покупает код.

Примеры сервисов:
- **Mat Egenskaper** — питательная информация о продуктах (пакеты 10/30/50 запросов)
- **Landingsside-bot** — генерация лендингов, заказ домена, интеграция сайта (Claude)
- **Produktbild-bot** — 4 картинки из одной фото продукта (Nano Banana)

Claude сам решает цены и пакеты кредитов на шведском языке.

---

## Быстрый старт (один раз)

### Шаг 1. Установите Python

Нужен Python 3.10 или новее.

Проверка в терминале Cursor (`Ctrl + \``):

```powershell
python --version
```

Если команда не найдена — скачайте Python с [python.org](https://www.python.org/downloads/) и при установке отметьте **"Add Python to PATH"**.

---

### Шаг 2. Создайте файл `.env` с ключами

В терминале Cursor (PowerShell):

```powershell
copy .env.example .env
```

Откройте `.env` и вставьте свои ключи:

| Переменная | Где взять |
|---|---|
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → Personal access tokens → Generate new token (classic). Права: **repo** |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) → API Keys |

Пример `.env`:

```env
GITHUB_TOKEN=ghp_ваш_токен
ANTHROPIC_API_KEY=sk-ant-ваш_ключ
REPO_FILTER=bots
```

> **Важно:** файл `.env` уже в `.gitignore` — он не попадёт в Git.

---

### Шаг 3. Установите зависимости

```powershell
python -m pip install -r requirements.txt
```

---

## Запуск из Cursor (3 способа)

### Способ A — кнопка Run (самый простой)

1. Откройте `generate_bot_descriptions.py`
2. Нажмите **▶ Run** (или `Ctrl+F5`)
3. Скрипт запустится в терминале внизу

### Способ B — через Debug (с выбором режима)

1. Откройте панель **Run and Debug** (`Ctrl+Shift+D`)
2. Выберите конфигурацию:
   - **Bot Generator (только боты)** — только Telegram-боты
   - **Bot Generator (все repos)** — все репозитории
3. Нажмите **▶ Start Debugging** (`F5`)

### Способ C — через Task (Build)

1. `Ctrl+Shift+B` — запустит задачу **«Запустить Bot Generator»**
2. Автоматически установит зависимости и запустит скрипт

### Способ D — вручную в терминале

```powershell
# Только Telegram-боты
python generate_bot_descriptions.py --bots-only

# Все репозитории
python generate_bot_descriptions.py --all
```

---

## Результат

После успешного запуска появятся два файла:

- **`bot_descriptions.json`** — данные для API или доработки
- **`index.html`** — готовая лендинг-страница (открой в браузере двойным кликом)

### Пересобрать только HTML (Claude + существующий JSON)

Если JSON уже есть, Claude создаст пакеты, цены (99–299 kr) и HTML:

```powershell
python generate_bot_descriptions.py --html-only
```

### Простая шаблон-страница (без Claude для HTML)

```powershell
python generate_bot_descriptions.py --html-only --template-html
```

### Только JSON, без HTML

```powershell
python generate_bot_descriptions.py --bots-only --no-html
```

### Обновить только цены (без HTML)

```powershell
python generate_bot_descriptions.py --html-only --fresh-pricing --no-html
```

Или после полного анализа GitHub:

```powershell
python generate_bot_descriptions.py --bots-only --no-html --fresh-pricing
```

### Как работает Claude

1. **Описания сервисов** — что пользователь может делать через бота
2. **Кредитные пакеты** — Claude сам решает: 10/30/50 запросов, цены в kr
3. **HTML-лендинг** — продающая страница «используй мои боты», не «купи бота»

Всё через один ключ `ANTHROPIC_API_KEY` из `.env`.

### Обновить цены и HTML (без повторного анализа GitHub)

```powershell
python generate_bot_descriptions.py --html-only --fresh-pricing
```

> Om `bot_descriptions.json` har gammalt prissättningsformat (`individual_bots` / `packages`)
> uppdateras priserna automatiskt vid `--html-only` utan extra flagga.

---

```json
{
  "generated_at": "2026-06-19 12:00:00",
  "username": "ianaranovitch-swe",
  "total_bots": 5,
  "bots": [
    {
      "name": "Название бота",
      "tagline": "Короткий слоган",
      "description": "Описание...",
      "features": ["Функция 1", "Функция 2"],
      "ideal_for": "Для кого подходит",
      "tech_stack": ["Python", "aiogram"],
      "category": "AI",
      "repo_name": "my-bot",
      "repo_url": "https://github.com/..."
    }
  ]
}
```

---

## Переменные окружения

| Переменная | Обязательна | Описание |
|---|---|---|
| `GITHUB_TOKEN` | Рекомендуется | Доступ к приватным репозиториям |
| `ANTHROPIC_API_KEY` | **Да** | Ключ Claude API |
| `GITHUB_USERNAME` | Нет | GitHub-логин (по умолчанию: `ianaranovitch-swe`) |
| `REPO_FILTER` | Нет | `bots` или `all` (если не передан аргумент `--bots-only` / `--all`) |
| `SITE_TITLE` | Нет | Заголовок HTML-страницы |
| `OUTPUT_HTML` | Нет | Имя HTML-файла (по умолчанию: `index.html`) |
| `CONTACT_EMAIL` | **Ja** (för publicering) | E-post på landningssidan — **ersätt** `din@email.se` med din riktiga adress |

---

## Runbook: диагностика проблем

### ❌ `ANTHROPIC_API_KEY` не найден

→ Создайте `.env` из `.env.example` и вставьте ключ.

### ❌ Claude API-fel: 401

→ Неверный API-ключ. Проверьте ключ на [console.anthropic.com](https://console.anthropic.com/).

### ❌ Claude API-fel: 429

→ Превышен лимит запросов. Подождите минуту и запустите снова.

### ⚠️ GITHUB_TOKEN не задан

→ Скрипт работает, но видит только **публичные** репозитории.

### ❌ `ModuleNotFoundError: requests`

→ Выполните: `python -m pip install -r requirements.txt`

### ❌ GitHub API 403 / rate limit

→ Проверьте токен. Убедитесь, что у него есть права **repo**.

---

## Структура проекта

```
GitHub-Claude-Integration/
├── generate_bot_descriptions.py   # Главный скрипт
├── requirements.txt               # Python-зависимости
├── .env.example                   # Шаблон ключей (скопируй как .env)
├── .env                           # Твои ключи (создай сам, не коммить!)
├── .gitignore                     # Исключает секреты из Git
├── .vscode/
│   ├── launch.json                # Запуск через F5 в Cursor
│   └── tasks.json                 # Запуск через Ctrl+Shift+B
├── bot_descriptions.json          # Результат (создаётся после запуска)
├── GitHub-TOKEN.txt               # ⚠️ Старый файл — перенеси токен в .env
└── Python_Script-to-Run.txt       # ⚠️ Старый файл — больше не нужен
```

---

## Безопасность

- **Никогда** не коммитьте `.env` или `GitHub-TOKEN.txt`
- Если токен случайно утёк — немедленно отзовите его на GitHub
- Храните ключи только в `.env`

---

## Следующие шаги

1. Запустите скрипт и проверьте `bot_descriptions.json`
2. Используйте описания для лендинга / Stripe-магазина
3. При необходимости — доработайте промпт в `generate_description_with_claude()`
