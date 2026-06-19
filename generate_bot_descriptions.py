#!/usr/bin/env python3
"""
Генератор описаний и лендинга для Telegram-сервисов.
Берёт репозитории с GitHub, анализирует код через Claude AI
и создаёт продающие описания доступа к ботам (не продажу самих ботов).
"""

import argparse
import base64
import html
import json
import os
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

# Загружаем ключи из файла .env (если он есть)
load_dotenv(Path(__file__).resolve().parent / ".env")

# ── Конфигурация ──────────────────────────────────────────────────────────────
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "ianaranovitch-swe")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REPO_FILTER = os.environ.get("REPO_FILTER", "bots").lower()
OUTPUT_FILE = "bot_descriptions.json"
OUTPUT_HTML = os.environ.get("OUTPUT_HTML", "index.html")
SITE_TITLE = os.environ.get("SITE_TITLE", "Telegram-tjänster — använd mina botar")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
MIN_PRICE_SEK = int(os.environ.get("MIN_PRICE_SEK", "29"))
MAX_PRICE_SEK = int(os.environ.get("MAX_PRICE_SEK", "499"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Заглушки e-post — не показывать на сайте
PLACEHOLDER_CONTACT_EMAILS = frozenset({
    "din@email.se",
    "din@email.com",
    "kontakt@example.com",
    "your@email.com",
    "example@example.com",
})
# ──────────────────────────────────────────────────────────────────────────────

# Контекст текущей задачи (безопасно для нескольких пользователей Telegram)
_job_ctx: ContextVar[Optional["JobConfig"]] = ContextVar("job", default=None)


@dataclass
class JobConfig:
    """Параметры одного запуска генерации (CLI или Telegram-пользователь)."""
    github_username: str
    github_token: str = ""
    repo_filter: str = "all"
    output_dir: Path = field(default_factory=Path.cwd)
    site_title: str = ""
    use_claude_html: bool = True
    use_template_html: bool = False
    fresh_pricing: bool = False

    @property
    def output_json_path(self) -> Path:
        return self.output_dir / "bot_descriptions.json"

    @property
    def output_html_path(self) -> Path:
        return self.output_dir / "index.html"

    def resolved_site_title(self) -> str:
        if self.site_title:
            return self.site_title
        return f"Telegram-tjänster — @{self.github_username}"


def _active_job() -> Optional[JobConfig]:
    return _job_ctx.get()


def _github_username() -> str:
    job = _active_job()
    return job.github_username if job else GITHUB_USERNAME


def _github_token() -> str:
    job = _active_job()
    return job.github_token if job else GITHUB_TOKEN


def _output_json_path() -> Path:
    job = _active_job()
    return job.output_json_path if job else Path(OUTPUT_FILE)


def _output_html_path() -> Path:
    job = _active_job()
    return job.output_html_path if job else Path(OUTPUT_HTML)


def _site_title() -> str:
    job = _active_job()
    return job.resolved_site_title() if job else SITE_TITLE


def _repo_filter() -> str:
    job = _active_job()
    return job.repo_filter if job else REPO_FILTER


ProgressCallback = Callable[[str], None]


def is_placeholder_contact_email(email):
    """Проверяет, что email — заглушка из .env.example."""
    if not email or not str(email).strip():
        return True
    normalized = str(email).strip().lower()
    if normalized in PLACEHOLDER_CONTACT_EMAILS:
        return True
    return normalized.endswith("@example.com")


def resolve_contact_email(pricing_data=None):
    """Возвращает реальный email: .env → pricing JSON, без заглушек."""
    candidates = [CONTACT_EMAIL]
    if pricing_data and isinstance(pricing_data, dict):
        candidates.append(pricing_data.get("contact_email", ""))
    for email in candidates:
        if email and not is_placeholder_contact_email(email):
            return str(email).strip()
    return ""


def warn_if_placeholder_contact_email():
    """Предупреждает, если в .env осталась заглушка."""
    if is_placeholder_contact_email(CONTACT_EMAIL):
        print("  ⚠️  CONTACT_EMAIL är en platshållare (t.ex. din@email.se).")
        print("     Uppdatera .env med din riktiga e-post innan publicering.")


def build_contact_block(pricing_data=None):
    """HTML-блок контакта с реальным email или понятным сообщением."""
    email = resolve_contact_email(pricing_data)
    if email:
        safe_email = escape_text(email)
        return f'<a href="mailto:{safe_email}">{safe_email}</a>'
    return (
        '<span class="contact-missing">Kontakt-e-post saknas — '
        "sätt <code>CONTACT_EMAIL</code> i <code>.env</code> "
        "(ersätt <code>din@email.se</code> med din riktiga adress).</span>"
    )

BUSINESS_MODEL_CONTEXT = """
AFFÄRESMODELL (VIKTIGT — följ detta strikt):
- Du säljer INTE botarna, koden eller GitHub-repona
- Kunder betalar för att ANVÄNDA tjänsterna via Telegram (pay-per-use / kreditpaket)
- Ägaren tjänar pengar på att erbjuda tillgång till sina botar som tjänster
- Prissättning baseras på antal förfrågningar, generationer eller liknande enheter

EXEMPEL PÅ TJÄNSTER (anpassa efter faktisk kod i repot):
1. Mat Egenskaper-bot: användaren skriver en matprodukt → får näringsinformation.
   Prispaket: t.ex. 10, 30 eller 50 förfrågningar.
2. Landingsside-bot: genererar professionella landningssidor med Claude, tar emot
   beställningar på domännamn och vidare sajtintegrering på ny domän.
3. Produktbild-bot: genererar 4 produktbilder baserat på användarens produktbild
   (använder Nano Banana / bildgenerering).
4. Andra botar: analysera koden och föreslå rimlig användningsenhet och paket.

SPRÅK: Svenska i all kundtext.
"""
# ──────────────────────────────────────────────────────────────────────────────


def call_claude(prompt, max_tokens=1000):
    """Общий вызов Claude API через ключ из .env."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )

    if resp.status_code != 200:
        print(f"  ❌ Claude API-fel: {resp.status_code} – {resp.text[:300]}")
        return None

    return resp.json()["content"][0]["text"].strip()


def clean_json_response(raw):
    """Убирает markdown-обёртку вокруг JSON от Claude."""
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


def clean_html_response(raw):
    """Убирает markdown-обёртку вокруг HTML от Claude."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text.lower().startswith("<!doctype") and not text.lower().startswith("<html"):
        print("  ⚠️  Claude returnerade ogiltig HTML — använder reservmall.")
        return None
    return text


def get_github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def validate_github_username(username: str) -> bool:
    """Проверяет, что GitHub-пользователь существует."""
    clean = username.strip().lstrip("@")
    if not clean:
        return False
    resp = requests.get(
        f"https://api.github.com/users/{clean}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=20,
    )
    return resp.status_code == 200


def validate_github_token(username: str, token: str) -> tuple[bool, str]:
    """Проверяет токен и что он принадлежит указанному пользователю."""
    clean_user = username.strip().lstrip("@")
    resp = requests.get(
        "https://api.github.com/user",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token.strip()}",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        return False, "Ogiltig GitHub-token. Kontrollera att token har rättigheten «repo»."
    login = (resp.json().get("login") or "").lower()
    if login != clean_user.lower():
        return False, f"Token tillhör @{login}, inte @{clean_user}. Använd token för rätt konto."
    return True, ""


def count_public_repos(username: str) -> int:
    """Считает публичные репозитории пользователя."""
    clean = username.strip().lstrip("@")
    resp = requests.get(
        f"https://api.github.com/users/{clean}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=20,
    )
    if resp.status_code != 200:
        return 0
    return int(resp.json().get("public_repos", 0))


def fetch_all_repos():
    """Забирает репозитории (публичные или все, если есть токен)."""
    username = _github_username()
    token = _github_token()

    if token:
        url = "https://api.github.com/user/repos"
        params = {"per_page": 100, "sort": "updated", "type": "all"}
    else:
        url = f"https://api.github.com/users/{username}/repos"
        params = {"per_page": 100, "sort": "updated", "type": "public"}
        print("⚠️  Ingen GitHub-token — hämtar bara publika repos.")

    resp = requests.get(url, headers=get_github_headers(), params=params, timeout=30)
    resp.raise_for_status()
    repos = resp.json()

    if token:
        return repos
    return [repo for repo in repos if not repo.get("private", False)]


def fetch_file_content(repo_name, filepath):
    """Читает содержимое одного файла из репозитория."""
    url = (
        f"https://api.github.com/repos/{_github_username()}/{repo_name}"
        f"/contents/{filepath}"
    )
    resp = requests.get(url, headers=get_github_headers(), timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
    return None


def fetch_repo_tree(repo_name):
    """Возвращает список всех файлов в репозитории."""
    url = f"https://api.github.com/repos/{_github_username()}/{repo_name}/git/trees/HEAD"
    resp = requests.get(
        url, headers=get_github_headers(), params={"recursive": "1"}, timeout=30
    )
    if resp.status_code == 200:
        return [
            item["path"]
            for item in resp.json().get("tree", [])
            if item["type"] == "blob"
        ]
    return []


def collect_repo_context(repo):
    """Собирает код и метаданные репозитория для отправки в Claude."""
    name = repo["name"]
    description = repo.get("description") or ""
    language = repo.get("language") or "okänt"
    topics = repo.get("topics", [])

    context_parts = [
        f"Repo-namn: {name}",
        f"Beskrivning: {description}",
        f"Primärt språk: {language}",
        f"Ämnen: {', '.join(topics) if topics else 'inga'}",
    ]

    priority_files = [
        "README.md",
        "README.txt",
        "bot.py",
        "main.py",
        "app.py",
        "requirements.txt",
        "config.py",
        "handlers.py",
        "run_bot.py",
    ]

    files = fetch_repo_tree(name)

    for pf in priority_files:
        if pf in files:
            content = fetch_file_content(name, pf)
            if content:
                preview = content[:2000] + ("..." if len(content) > 2000 else "")
                context_parts.append(f"\n--- {pf} ---\n{preview}")

    if not any(pf in files for pf in ["bot.py", "main.py", "app.py"]):
        py_files = [f for f in files if f.endswith(".py") and "/" not in f]
        if py_files:
            content = fetch_file_content(name, py_files[0])
            if content:
                preview = content[:2000] + ("..." if len(content) > 2000 else "")
                context_parts.append(f"\n--- {py_files[0]} ---\n{preview}")

    return "\n".join(context_parts)


def generate_description_with_claude(repo_context, repo_name):
    """Отправляет контекст в Claude и получает JSON-описание сервиса."""
    prompt = f"""Du är en copywriter som marknadsför Telegram-baserade TJÄNSTER (SaaS / pay-per-use).

{BUSINESS_MODEL_CONTEXT}

Analysera koden och metadata nedan från ett GitHub-repo och beskriv TJÄNSTEN som kunden kan använda — INTE att de köper boten.

VIKTIGT:
- Skriv på svenska
- Beskriv vad användaren får GÖRA via boten (t.ex. slå upp näringsvärden, generera landningssida)
- Lyft fram nyttan per användning/förfrågan
- Ange rimlig användningsenhet (förfrågningar, generationer, bilder, landningssidor, domänordrar)
- Nämn INTE att kunden köper källkod eller äger boten

Svara med JSON (utan markdown):
{{
  "name": "Tjänstens namn",
  "tagline": "En kort säljande mening om vad användaren kan göra",
  "description": "2-3 meningar om tjänsten och varför den är värdefull att använda",
  "features": ["Funktion 1", "Funktion 2", "Funktion 3"],
  "ideal_for": "Vem passar tjänsten för?",
  "tech_stack": ["Python", "Claude", "Nano Banana", "etc"],
  "category": "Kategori t.ex. Mat & hälsa, Webb & domän, E-handel, AI",
  "usage_unit": "förfrågningar",
  "usage_unit_singular": "förfrågan",
  "example_usage": "Konkret exempel: Användaren skriver 'havregryn' och får näringsvärden",
  "access_via": "Telegram"
}}

REPO-INFORMATION:
{repo_context}
"""

    raw = call_claude(prompt, max_tokens=1000)
    if not raw:
        return None

    try:
        return clean_json_response(raw)
    except json.JSONDecodeError as err:
        print(f"  ⚠️  Kunde inte parsa JSON: {err}")
        print(f"  Råsvar: {raw[:300]}")
        return {"name": repo_name, "raw_description": raw}


def generate_pricing_with_claude(bots):
    """Claude создаёт пакеты доступа (кредиты/запросы) и цены."""
    if not bots:
        return None

    bots_json = json.dumps(bots, ensure_ascii=False, indent=2)
    contact_hint = resolve_contact_email() or "ange riktig e-post i CONTACT_EMAIL"

    prompt = f"""Du är en prissättningsstrateg för Telegram-baserade tjänster i Sverige.

{BUSINESS_MODEL_CONTEXT}

Baserat på tjänstelistorna nedan ska du skapa en komplett prissättning för ANVÄNDNING — inte försäljning av botar.

REGLER:
- Valuta: SEK (kr)
- Varje tjänst ska ha 2–4 kreditpaket (t.ex. 10/30/50 förfrågningar, eller 1/3/5 generationer)
- Pris per paket: rimligt mellan {MIN_PRICE_SEK} och {MAX_PRICE_SEK} kr totalt
- Dyrare/more komplexa tjänster (landningssidor + domän) kan ha högre pris
- Enklare tjänster (matuppslag) kan ha lägre pris
- Claude bestämmer själv paketstorlekar och priser baserat på tjänstens värde
- Skapa 1–2 kombopaket som blandar flera tjänster (valfritt)
- Markera recommended: true på bästa paketet per tjänst
- Sälj ALDRIG boten/koden — bara tillgång till användning

Svara ENDAST med JSON:
{{
  "business_model": "usage_access",
  "currency": "SEK",
  "pricing_strategy_summary": "Kort förklaring på svenska",
  "services": [
    {{
      "repo_name": "exakt repo_name",
      "display_name": "Tjänstens namn",
      "usage_unit": "förfrågningar",
      "usage_unit_singular": "förfrågan",
      "example": "Användaren skriver en matprodukt och får näringsvärden",
      "credit_packages": [
        {{
          "id": "mat-10",
          "credits": 10,
          "label": "10 förfrågningar",
          "price_sek": 49,
          "price_per_credit_sek": 4.9,
          "recommended": false
        }}
      ]
    }}
  ],
  "combo_packages": [
    {{
      "id": "prova",
      "name": "Prova-paket",
      "tagline": "Testa flera tjänster",
      "description": "Beskrivning",
      "price_sek": 99,
      "includes": [
        {{"repo_name": "repo1", "credits": 10, "usage_unit": "förfrågningar"}}
      ],
      "recommended": true
    }}
  ],
  "contact_email": "{contact_hint}",
  "how_it_works": [
    "Steg 1: Välj paket och betala",
    "Steg 2: Öppna boten i Telegram",
    "Steg 3: Använd dina krediter"
  ]
}}

TJÄNSTELISTA:
{bots_json}
"""

    print("  💰 Claude planerar kreditpaket och priser...")
    raw = call_claude(prompt, max_tokens=4000)
    if not raw:
        return None

    try:
        return clean_json_response(raw)
    except json.JSONDecodeError as err:
        print(f"  ⚠️  Kunde inte parsa prissättning: {err}")
        print(f"  Råsvar: {raw[:400]}")
        return None


def generate_landing_html_with_claude(output_data, pricing_data):
    """Claude создаёт продающую HTML-страницу для доступа к сервисам."""
    bots_json = json.dumps(output_data.get("bots", []), ensure_ascii=False, indent=2)
    pricing_json = json.dumps(pricing_data, ensure_ascii=False, indent=2)
    site_title = _site_title()
    username = output_data.get("username", _github_username())
    generated_at = output_data.get("generated_at", "")

    prompt = f"""Du är en senior webbdesigner och copywriter som bygger SaaS-landningssidor.

{BUSINESS_MODEL_CONTEXT}

Skapa en komplett, professionell HTML-landningssida på SVENSKA för att sälja TILLGÅNG till Telegram-tjänster.

DATA:
- Sajttitel: {site_title}
- Ägare: @{username}
- Genererad: {generated_at}
- Tjänstebeskrivningar (JSON): {bots_json}
- Kreditpaket och priser (JSON): {pricing_json}

KRAV:
1. EN komplett HTML-fil med inbäddad CSS (ingen extern CSS/JS, inga CDN)
2. Modern, professionell SaaS-design — mörkt tema, tydliga CTA
3. Sektioner: Hero, Så fungerar det (3 steg), Våra tjänster, Prispaket per tjänst, Kombopaket, FAQ (5 frågor), Kontakt/footer
4. Hero ska förklara: "Använd mina Telegram-botar — betala per förfrågan/generation"
5. Visa priser i SEK för kreditpaket (10/30/50 förfrågningar etc.) — INTE "köp boten"
6. Knappar: "Köp krediter", "Välj paket", "Kom igång" — href="#kontakt"
7. Förklara tydligt att kunden får ANVÄNDA tjänsten, inte äga koden
8. Nämn Telegram som plattform för att använda tjänsterna
9. Responsiv design, svenska språket
10. Använd riktiga tjänstenamn från JSON — hitta inte på tjänster
11. Svara ENDAST med rå HTML

Kontakt: {resolve_contact_email(pricing_data) or "sätt CONTACT_EMAIL i .env — använd INTE din@email.se"}
"""

    print("  🎨 Claude skapar tjänstelandningssida (HTML)...")
    raw = call_claude(prompt, max_tokens=16000)
    if not raw:
        return None
    return clean_html_response(raw)


def is_telegram_bot(repo):
    """Простая проверка: похоже ли репо на Telegram-бота."""
    name = repo["name"].lower()
    desc = (repo.get("description") or "").lower()
    topics = [t.lower() for t in repo.get("topics", [])]
    bot_keywords = ["bot", "telegram", "botfather"]
    return any(kw in name or kw in desc or kw in topics for kw in bot_keywords)


def escape_text(value):
    """Безопасно вставляет текст в HTML."""
    if value is None:
        return ""
    return html.escape(str(value))


def build_bot_card(bot):
    """Собирает HTML-карточку одного бота."""
    name = escape_text(bot.get("name") or bot.get("repo_name", "Bot"))
    tagline = escape_text(bot.get("tagline", ""))
    description = escape_text(bot.get("description", ""))
    category = escape_text(bot.get("category", "Telegram-bot"))
    ideal_for = escape_text(bot.get("ideal_for", ""))
    usage_unit = escape_text(bot.get("usage_unit", "förfrågningar"))
    example_usage = escape_text(bot.get("example_usage", ""))

    features = bot.get("features") or []
    feature_items = "".join(
        f"<li>{escape_text(feature)}</li>" for feature in features
    )

    tech_stack = bot.get("tech_stack") or []
    tech_tags = "".join(
        f'<span class="tag">{escape_text(tech)}</span>' for tech in tech_stack
    )

    return f"""
    <article class="bot-card">
      <div class="bot-card-header">
        <span class="category">{category}</span>
        <h2>{name}</h2>
        <p class="tagline">{tagline}</p>
      </div>
      <p class="description">{description}</p>
      <ul class="features">{feature_items}</ul>
      <p class="ideal-for"><strong>Passar för:</strong> {ideal_for}</p>
      {f'<p class="meta">Enhet: {usage_unit}</p>' if usage_unit else ""}
      {f'<p class="meta">Exempel: {example_usage}</p>' if example_usage else ""}
      <div class="tech-stack">{tech_tags}</div>
      <div class="bot-card-actions">
        <a class="btn btn-primary" href="#priser">Köp krediter</a>
      </div>
    </article>
    """


def build_credit_package_card(package):
    """HTML-карточка одного кредитного пакета."""
    recommended = package.get("recommended", False)
    badge = '<span class="recommended-badge">Rekommenderad</span>' if recommended else ""
    label = escape_text(package.get("label", f"{package.get('credits', '')} krediter"))
    price = escape_text(package.get("price_sek", ""))
    price_per = package.get("price_per_credit_sek")
    per_credit = (
        f'<span class="per-credit">{escape_text(price_per)} kr/st</span>'
        if price_per is not None
        else ""
    )
    card_class = "price-card recommended" if recommended else "price-card"
    return f"""
    <div class="{card_class}">
      {badge}
      <h4>{label}</h4>
      <p class="price-amount">{price} kr</p>
      {per_credit}
      <a class="btn btn-primary" href="#kontakt">Välj paket</a>
    </div>
    """


def build_service_pricing_block(service):
    """Блок цен для одного сервиса."""
    name = escape_text(service.get("display_name", service.get("repo_name", "Tjänst")))
    usage_unit = escape_text(service.get("usage_unit", "förfrågningar"))
    example = escape_text(service.get("example", ""))
    packages = service.get("credit_packages") or []
    package_cards = "".join(build_credit_package_card(pkg) for pkg in packages)
    if not package_cards:
        return ""
    return f"""
    <div class="service-pricing">
      <h3>{name}</h3>
      <p class="usage-unit">Enhet: {usage_unit}</p>
      {f'<p class="pricing-example">{example}</p>' if example else ""}
      <div class="price-grid">{package_cards}</div>
    </div>
    """


def build_combo_packages_block(pricing_data):
    """HTML для комбо-пакетов."""
    combos = pricing_data.get("combo_packages") or []
    if not combos:
        return ""

    cards = []
    for combo in combos:
        recommended = combo.get("recommended", False)
        badge = '<span class="recommended-badge">Rekommenderad</span>' if recommended else ""
        name = escape_text(combo.get("name", "Kombopaket"))
        tagline = escape_text(combo.get("tagline", ""))
        description = escape_text(combo.get("description", ""))
        price = escape_text(combo.get("price_sek", ""))
        includes = combo.get("includes") or []
        include_items = "".join(
            f"<li>{escape_text(item.get('credits', ''))} "
            f"{escape_text(item.get('usage_unit', 'krediter'))} "
            f"({escape_text(item.get('repo_name', ''))})</li>"
            for item in includes
        )
        card_class = "combo-card recommended" if recommended else "combo-card"
        cards.append(f"""
        <div class="{card_class}">
          {badge}
          <h4>{name}</h4>
          <p class="tagline">{tagline}</p>
          <p>{description}</p>
          <ul class="combo-includes">{include_items}</ul>
          <p class="price-amount">{price} kr</p>
          <a class="btn btn-primary" href="#kontakt">Välj paket</a>
        </div>
        """)

    return f"""
    <div class="combo-section">
      <h3>Kombopaket</h3>
      <div class="combo-grid">{"".join(cards)}</div>
    </div>
    """


def build_how_it_works_block(pricing_data):
    """Секция «Как это работает»."""
    steps = pricing_data.get("how_it_works") if pricing_data else None
    if not steps:
        steps = [
            "Välj kreditpaket för den tjänst du vill använda",
            "Betala via Stripe (kopplas in snart)",
            "Öppna boten i Telegram och använd dina krediter",
        ]
    step_items = "".join(f"<li>{escape_text(step)}</li>" for step in steps)
    return f"""
    <section id="sa-fungerar-det" class="section-block">
      <div class="container">
        <h2>Så fungerar det</h2>
        <ol class="steps-list">{step_items}</ol>
      </div>
    </section>
    """


def build_pricing_section(pricing_data):
    """Полная секция с кредитными пакетами."""
    if not is_current_pricing_format(pricing_data):
        return """
    <section id="priser" class="section-block pricing-missing">
      <div class="container">
        <h2>Kreditpaket</h2>
        <p class="pricing-missing-text">
          Priser saknas ännu. Kör:
          <code>python generate_bot_descriptions.py --html-only --fresh-pricing</code>
        </p>
      </div>
    </section>
    """

    services = pricing_data.get("services") or []
    service_blocks = "".join(build_service_pricing_block(svc) for svc in services)
    combo_block = build_combo_packages_block(pricing_data)
    summary = escape_text(pricing_data.get("pricing_strategy_summary", ""))

    return f"""
    <section id="priser" class="section-block">
      <div class="container">
        <h2>Kreditpaket &amp; priser</h2>
        {f'<p class="pricing-summary">{summary}</p>' if summary else ""}
        {service_blocks}
        {combo_block}
      </div>
    </section>
    """


def build_fallback_landing_html(output_data):
    """Простая резервная страница, если Claude не смог создать HTML."""
    bots = output_data.get("bots", [])
    pricing_data = output_data.get("pricing")
    username = escape_text(output_data.get("username", _github_username()))
    generated_at = escape_text(output_data.get("generated_at", ""))
    total = output_data.get("total_bots", len(bots))
    site_title = escape_text(_site_title())
    contact_block = build_contact_block(pricing_data)
    has_pricing = is_current_pricing_format(pricing_data)
    contact_cta = (
        "Välj ett kreditpaket ovan och betala via Stripe — "
        "sedan använder du tjänsten direkt i Telegram."
        if has_pricing
        else "När kreditpaketen är genererade kan du betala via Stripe och använda tjänsten i Telegram."
    )

    cards = "".join(build_bot_card(bot) for bot in bots)
    how_it_works = build_how_it_works_block(pricing_data)
    pricing_section = build_pricing_section(pricing_data)

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{site_title}</title>
  <meta name="description" content="Använd Telegram-tjänster — betala per förfrågan eller generation. Mat, landningssidor, produktbilder och mer.">
  <style>
    :root {{
      --bg: #0f1419;
      --surface: #1a2332;
      --surface-hover: #243044;
      --text: #e7ecf3;
      --muted: #9aa8bc;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --border: #2d3a4f;
      --success: #22c55e;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.6;
    }}
    .container {{ max-width: 1100px; margin: 0 auto; padding: 0 1.25rem; }}
    header {{
      padding: 4rem 0 3rem;
      text-align: center;
      background: linear-gradient(180deg, #162032 0%, var(--bg) 100%);
      border-bottom: 1px solid var(--border);
    }}
    header h1 {{
      font-size: clamp(2rem, 5vw, 3rem);
      margin-bottom: 0.75rem;
    }}
    header p {{ color: var(--muted); max-width: 620px; margin: 0 auto 1.5rem; }}
    .stats {{
      display: inline-flex;
      gap: 1rem;
      flex-wrap: wrap;
      justify-content: center;
    }}
    .stat {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.4rem 1rem;
      font-size: 0.9rem;
      color: var(--muted);
    }}
    .stat strong {{ color: var(--text); }}
    main {{ padding: 3rem 0 4rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 1.5rem;
    }}
    .bot-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.5rem;
      display: flex;
      flex-direction: column;
      gap: 0.85rem;
      transition: border-color 0.2s, transform 0.2s;
    }}
    .bot-card:hover {{
      border-color: var(--accent);
      transform: translateY(-2px);
    }}
    .category {{
      display: inline-block;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--success);
      margin-bottom: 0.25rem;
    }}
    .bot-card h2 {{ font-size: 1.35rem; }}
    .tagline {{ color: var(--muted); font-size: 0.95rem; }}
    .description {{ font-size: 0.95rem; }}
    .features {{
      list-style: none;
      display: grid;
      gap: 0.35rem;
    }}
    .features li::before {{
      content: "✓ ";
      color: var(--success);
      font-weight: bold;
    }}
    .ideal-for {{ font-size: 0.9rem; color: var(--muted); }}
    .tech-stack {{ display: flex; flex-wrap: wrap; gap: 0.4rem; }}
    .tag {{
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.2rem 0.65rem;
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .bot-card-actions {{
      display: flex;
      gap: 0.75rem;
      flex-wrap: wrap;
      margin-top: auto;
      padding-top: 0.5rem;
    }}
    .btn {{
      display: inline-block;
      text-decoration: none;
      border-radius: 10px;
      padding: 0.6rem 1rem;
      font-size: 0.9rem;
      font-weight: 600;
      transition: background 0.2s;
    }}
    .btn-primary {{
      background: var(--accent);
      color: white;
    }}
    .btn-primary:hover {{ background: var(--accent-hover); }}
    .btn-secondary {{
      background: transparent;
      color: var(--text);
      border: 1px solid var(--border);
    }}
    .btn-secondary:hover {{ background: var(--surface-hover); }}
    .meta {{ font-size: 0.8rem; color: var(--muted); }}
    .section-block {{ padding: 3rem 0; border-top: 1px solid var(--border); }}
    .section-block h2 {{ text-align: center; margin-bottom: 1.5rem; }}
    .steps-list {{
      max-width: 640px;
      margin: 0 auto;
      padding-left: 1.25rem;
      color: var(--muted);
    }}
    .steps-list li {{ margin-bottom: 0.5rem; }}
    .pricing-summary {{
      text-align: center;
      color: var(--muted);
      max-width: 720px;
      margin: 0 auto 2rem;
    }}
    .service-pricing {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.5rem;
      margin-bottom: 1.5rem;
    }}
    .service-pricing h3 {{ margin-bottom: 0.35rem; }}
    .usage-unit, .pricing-example {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1rem; }}
    .price-grid, .combo-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: 1rem;
    }}
    .price-card, .combo-card {{
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }}
    .price-card.recommended, .combo-card.recommended {{
      border-color: var(--success);
      box-shadow: 0 0 0 1px var(--success);
    }}
    .recommended-badge {{
      display: inline-block;
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--success);
      font-weight: 700;
    }}
    .price-amount {{ font-size: 1.5rem; font-weight: 700; }}
    .per-credit {{ font-size: 0.85rem; color: var(--muted); }}
    .combo-section {{ margin-top: 2rem; }}
    .combo-section h3 {{ margin-bottom: 1rem; }}
    .combo-includes {{ list-style: none; color: var(--muted); font-size: 0.9rem; }}
    .combo-includes li::before {{ content: "✓ "; color: var(--success); }}
    .pricing-missing-text, .contact-missing {{ color: var(--muted); }}
    .pricing-missing-text code, .contact-missing code {{
      background: var(--surface);
      padding: 0.15rem 0.4rem;
      border-radius: 4px;
      font-size: 0.85rem;
    }}
    #kontakt {{
      background: var(--surface);
      border-top: 1px solid var(--border);
      padding: 3rem 0;
      text-align: center;
    }}
    #kontakt h2 {{ margin-bottom: 0.75rem; }}
    #kontakt p {{ color: var(--muted); }}
    footer {{
      text-align: center;
      padding: 1.5rem;
      color: var(--muted);
      font-size: 0.85rem;
      border-top: 1px solid var(--border);
    }}
    @media (max-width: 640px) {{
      .bot-card-actions {{ flex-direction: column; }}
      .btn {{ text-align: center; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="container">
      <h1>{site_title}</h1>
      <p>Använd mina Telegram-tjänster — betala bara för det du behöver. Matuppslag, landningssidor, produktbilder och mer.</p>
      <div class="stats">
        <span class="stat"><strong>{total}</strong> tjänster</span>
        <span class="stat">av <strong>@{username}</strong></span>
        <span class="stat">Uppdaterad <strong>{generated_at}</strong></span>
      </div>
    </div>
  </header>

  {how_it_works}

  {pricing_section}

  <main class="container section-block">
    <h2>Våra tjänster</h2>
    <div class="grid">
      {cards}
    </div>
  </main>

  <section id="kontakt">
    <div class="container">
      <h2>Kom igång</h2>
      <p>{contact_block}</p>
      <p style="margin-top: 1rem;">{contact_cta}</p>
    </div>
  </section>

  <footer>
    <div class="container">Genererad automatiskt av Bot Description Generator</div>
  </footer>
</body>
</html>
"""


def is_current_pricing_format(pricing_data):
    """Проверяет, что pricing в новом формате (services + credit_packages)."""
    if not pricing_data or not isinstance(pricing_data, dict):
        return False

    # Старый формат: продажа ботов, не кредиты
    if "individual_bots" in pricing_data or "packages" in pricing_data:
        return False

    services = pricing_data.get("services")
    if not isinstance(services, list) or not services:
        return False

    for service in services:
        if not isinstance(service, dict):
            return False
        credit_packages = service.get("credit_packages")
        if not isinstance(credit_packages, list) or not credit_packages:
            return False

    return True


def update_pricing_if_needed(output_data, fresh_pricing=False, require_for_claude=False, require_for_html=False):
    """Генерирует pricing при --fresh-pricing, отсутствии или устаревшем формате."""
    existing_pricing = output_data.get("pricing")
    has_valid_pricing = is_current_pricing_format(existing_pricing)
    needs_pricing = require_for_claude or require_for_html
    should_generate = fresh_pricing or (needs_pricing and not has_valid_pricing)

    if not should_generate:
        return existing_pricing

    if existing_pricing and not has_valid_pricing:
        print("  ⚠️  Gammalt prissättningsformat — genererar om kreditpaket...")

    pricing_data = generate_pricing_with_claude(output_data.get("bots", []))
    if pricing_data:
        output_data["pricing"] = pricing_data
        with open(_output_json_path(), "w", encoding="utf-8") as file:
            json.dump(output_data, file, ensure_ascii=False, indent=2)
    return pricing_data


def save_landing_page(output_data, use_claude=True, use_template=False, fresh_pricing=False):
    """Сохраняет HTML-лендинг: через Claude или резервный шаблон."""
    warn_if_placeholder_contact_email()
    html_content = None
    require_for_claude = use_claude and not use_template
    pricing_data = update_pricing_if_needed(
        output_data,
        fresh_pricing=fresh_pricing,
        require_for_claude=require_for_claude,
        require_for_html=True,
    )

    if require_for_claude and pricing_data:
        html_content = generate_landing_html_with_claude(output_data, pricing_data)

    if not html_content:
        print("  ⚠️  Använder enkel reservmall för HTML.")
        html_content = build_fallback_landing_html(output_data)

    with open(_output_html_path(), "w", encoding="utf-8") as file:
        file.write(html_content)


def load_json_output():
    """Читает ранее сохранённый JSON."""
    json_path = _output_json_path()
    if not json_path.exists():
        print(f"❌ Filen {json_path} hittades inte. Kör skriptet utan --html-only först.")
        return None
    with open(json_path, encoding="utf-8") as file:
        return json.load(file)


def parse_args():
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Generera tjänstebeskrivningar och landningssida för Telegram-botar (användning, inte försäljning)."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--bots-only",
        action="store_true",
        help="Bearbeta bara Telegram-botar (standard).",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Bearbeta alla repos.",
    )
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="Skapa HTML från befintlig bot_descriptions.json via Claude.",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skippa HTML-generering (pricing kan fortfarande uppdateras med --fresh-pricing).",
    )
    parser.add_argument(
        "--template-html",
        action="store_true",
        help="Använd enkel mall istället för Claude för HTML.",
    )
    parser.add_argument(
        "--fresh-pricing",
        action="store_true",
        help="Generera om kreditpaket och priser (fungerar även med --no-html).",
    )
    return parser.parse_args()


def filter_target_repos(repos, repo_filter=None, bots_only=False, all_repos=False):
    """Фильтрует список репозиториев по типу."""
    repo_filter = repo_filter or _repo_filter()
    bot_repos = [repo for repo in repos if is_telegram_bot(repo)]
    other_repos = [repo for repo in repos if not is_telegram_bot(repo)]

    if all_repos or repo_filter == "all":
        return repos
    if bots_only or repo_filter == "bots":
        return bot_repos
    return repos


def choose_target_repos(repos, args):
    """Выбирает, какие репозитории обрабатывать (CLI)."""
    bot_repos = [repo for repo in repos if is_telegram_bot(repo)]
    other_repos = [repo for repo in repos if not is_telegram_bot(repo)]

    print(f"   → {len(bot_repos)} Telegram-botar identifierade")
    print(f"   → {len(other_repos)} övriga repos")

    if args.all:
        return repos
    if args.bots_only:
        return bot_repos

    if REPO_FILTER == "all":
        return repos
    if REPO_FILTER == "bots":
        return bot_repos

    print("\nVilka repos vill du generera beskrivningar för?")
    print("  1 = Bara Telegram-botar")
    print("  2 = Alla repos")
    choice = input("Välj (1/2): ").strip()
    return repos if choice == "2" else bot_repos


def run_generation_job(job: JobConfig, on_progress: Optional[ProgressCallback] = None):
    """Полный цикл генерации JSON + HTML для одного пользователя."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY saknas i serverns .env")

    def progress(message: str):
        if on_progress:
            on_progress(message)
        else:
            print(message)

    job.output_dir.mkdir(parents=True, exist_ok=True)
    token = _job_ctx.set(job)
    try:
        progress(f"📦 Hämtar repos för @{job.github_username}...")
        repos = fetch_all_repos()
        progress(f"   Hittade {len(repos)} repos")

        target_repos = filter_target_repos(
            repos,
            repo_filter=job.repo_filter,
            all_repos=job.repo_filter == "all",
        )
        if not target_repos:
            raise RuntimeError("Inga repos hittades att analysera.")

        progress(f"✍️ Genererar beskrivningar för {len(target_repos)} repos...")

        results = []
        for index, repo in enumerate(target_repos, 1):
            name = repo["name"]
            progress(f"[{index}/{len(target_repos)}] {name}")
            context = collect_repo_context(repo)
            description = generate_description_with_claude(context, name)
            if description:
                description["repo_name"] = name
                description["repo_url"] = repo["html_url"]
                description["language"] = repo.get("language")
                description["updated_at"] = repo.get("updated_at")
                results.append(description)
            if index < len(target_repos):
                time.sleep(1)

        output = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "username": job.github_username,
            "total_bots": len(results),
            "bots": results,
        }

        with open(job.output_json_path, "w", encoding="utf-8") as file:
            json.dump(output, file, ensure_ascii=False, indent=2)

        progress("🎨 Skapar HTML-landningssida...")
        save_landing_page(
            output,
            use_claude=job.use_claude_html,
            use_template=job.use_template_html,
            fresh_pricing=job.fresh_pricing,
        )
        progress("✅ Klart!")
        return output, job.output_json_path, job.output_html_path
    finally:
        _job_ctx.reset(token)


def check_prerequisites():
    """Проверяет, что все ключи на месте."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not GITHUB_TOKEN:
        print("⚠️  GITHUB_TOKEN saknas — endast publika repos kommer att hämtas.")

    if missing:
        print("❌ Saknade miljövariabler:")
        for key in missing:
            print(f"   - {key}")
        print("\nSkapa filen .env från .env.example och fyll i dina nycklar.")
        return False
    return True


def main():
    args = parse_args()

    print("🤖 Bot Description Generator")
    print("=" * 50)

    if args.html_only:
        if not check_prerequisites():
            sys.exit(1)
        output = load_json_output()
        if not output:
            sys.exit(1)

        if args.no_html:
            if not args.fresh_pricing:
                print("ℹ️  --no-html: hoppar över HTML. Lägg till --fresh-pricing för att uppdatera priser.")
                return
            update_pricing_if_needed(
                output,
                fresh_pricing=True,
                require_for_claude=False,
            )
            if output.get("pricing"):
                print("✅ Kreditpaket och priser uppdaterade i bot_descriptions.json")
            else:
                print("⚠️  Ingen pricing genererades — kontrollera att bots finns i JSON.")
            return

        save_landing_page(
            output,
            use_claude=not args.template_html,
            use_template=args.template_html,
            fresh_pricing=args.fresh_pricing,
        )
        print(f"✅ HTML sparad i {OUTPUT_HTML}")
        if output.get("pricing"):
            print("  → Kreditpaket och priser sparade i bot_descriptions.json under 'pricing'")
        print("  → Öppna filen i webbläsaren för att förhandsgranska")
        return

    if not check_prerequisites():
        sys.exit(1)

    print(f"📦 Hämtar repos för {GITHUB_USERNAME}...")
    repos = fetch_all_repos()
    print(f"   Hittade {len(repos)} repos totalt")

    target_repos = choose_target_repos(repos, args)
    print(f"\n✍️  Genererar beskrivningar för {len(target_repos)} repos...\n")

    results = []

    for i, repo in enumerate(target_repos, 1):
        name = repo["name"]
        print(f"[{i}/{len(target_repos)}] {name}")

        print("  📖 Hämtar kod...")
        context = collect_repo_context(repo)

        print("  🧠 Analyserar med Claude...")
        description = generate_description_with_claude(context, name)

        if description:
            description["repo_name"] = name
            description["repo_url"] = repo["html_url"]
            description["language"] = repo.get("language")
            description["updated_at"] = repo.get("updated_at")
            results.append(description)
            print(f"  ✅ Klar: {description.get('name', name)}")
        else:
            print(f"  ❌ Misslyckades för {name}")

        if i < len(target_repos):
            time.sleep(1)

    output = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "username": GITHUB_USERNAME,
        "total_bots": len(results),
        "bots": results,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print(f"\n✅ Klart! {len(results)} beskrivningar sparade i {OUTPUT_FILE}")

    if args.fresh_pricing and args.no_html:
        update_pricing_if_needed(
            output,
            fresh_pricing=True,
            require_for_claude=False,
        )
        if output.get("pricing"):
            print("✅ Kreditpaket och priser uppdaterade i bot_descriptions.json")
    elif not args.no_html:
        save_landing_page(
            output,
            use_claude=True,
            use_template=args.template_html,
            fresh_pricing=args.fresh_pricing,
        )
        print(f"✅ Tjänstelandningssida sparad i {OUTPUT_HTML}")
        if output.get("pricing"):
            print("  → Kreditpaket och priser finns i bot_descriptions.json")

    print("\nNästa steg:")
    print("  → Öppna bot_descriptions.json och granska")
    if not args.no_html:
        print(f"  → Öppna {OUTPUT_HTML} i webbläsaren")
    print("  → Koppla Stripe för kreditköp och Telegram för leverans av tjänsten")


if __name__ == "__main__":
    main()
