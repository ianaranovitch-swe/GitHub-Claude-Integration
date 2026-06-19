#!/usr/bin/env python3
"""
Генератор описаний Telegram-ботов.
Берёт репозитории с GitHub, анализирует код через Claude AI
и создаёт продающие описания на шведском языке.
"""

import argparse
import base64
import html
import json
import os
import sys
import time
from pathlib import Path

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
SITE_TITLE = os.environ.get("SITE_TITLE", "Telegram-botar till salu")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
# ──────────────────────────────────────────────────────────────────────────────


def get_github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def fetch_all_repos():
    """Забирает все репозитории (публичные + приватные, если есть токен)."""
    url = "https://api.github.com/user/repos"
    params = {"per_page": 100, "sort": "updated", "type": "all"}

    if not GITHUB_TOKEN:
        url = f"https://api.github.com/users/{GITHUB_USERNAME}/repos"
        print("⚠️  GITHUB_TOKEN не задан — берём только публичные repos.")

    resp = requests.get(url, headers=get_github_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_file_content(repo_name, filepath):
    """Читает содержимое одного файла из репозитория."""
    url = (
        f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}"
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
    url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/git/trees/HEAD"
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
    """Отправляет контекст в Claude и получает JSON-описание бота."""
    prompt = f"""Du är en copywriter som specialiserar dig på att sälja mjukvarulösningar och Telegram-botar.

Analysera koden och metadata nedan från ett GitHub-repo och skriv en säljande produktbeskrivning på SVENSKA för denna Telegram-bot eller mjukvarulösning.

VIKTIGT:
- Skriv på svenska
- Var specifik om vad boten faktiskt gör (baserat på koden)
- Lyft fram nyttan för köparen
- Skriv i ett professionellt men tillgängligt sätt
- Inkludera: Rubrik, kort pitch (1-2 meningar), funktioner (3-5 punkter), vem det passar för

Svara med ett JSON-objekt i exakt detta format (utan markdown-kodblock):
{{
  "name": "Botens namn",
  "tagline": "En kort säljande mening",
  "description": "2-3 meningar om vad boten gör och varför det är värdefullt",
  "features": ["Funktion 1", "Funktion 2", "Funktion 3"],
  "ideal_for": "Vem passar denna bot för?",
  "tech_stack": ["Python", "etc"],
  "category": "En kategori t.ex. E-handel, AI, Produktivitet, etc"
}}

REPO-INFORMATION:
{repo_context}
"""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )

    if resp.status_code != 200:
        print(f"  ❌ Claude API-fel: {resp.status_code} – {resp.text[:200]}")
        return None

    raw = resp.json()["content"][0]["text"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        print(f"  ⚠️  Kunde inte parsa JSON: {err}")
        print(f"  Råsvar: {raw[:300]}")
        return {"name": repo_name, "raw_description": raw}


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
    repo_url = escape_text(bot.get("repo_url", "#"))
    language = escape_text(bot.get("language", ""))

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
      <div class="tech-stack">{tech_tags}</div>
      <div class="bot-card-actions">
        <a class="btn btn-secondary" href="{repo_url}" target="_blank" rel="noopener">Visa på GitHub</a>
        <a class="btn btn-primary" href="#kontakt">Köp / fråga</a>
      </div>
      {f'<p class="meta">Språk: {language}</p>' if language else ""}
    </article>
    """


def build_landing_html(output_data):
    """Создаёт полную HTML-страницу из JSON-данных."""
    bots = output_data.get("bots", [])
    username = escape_text(output_data.get("username", GITHUB_USERNAME))
    generated_at = escape_text(output_data.get("generated_at", ""))
    total = output_data.get("total_bots", len(bots))
    site_title = escape_text(SITE_TITLE)
    contact_block = (
        f'<a href="mailto:{escape_text(CONTACT_EMAIL)}">{escape_text(CONTACT_EMAIL)}</a>'
        if CONTACT_EMAIL
        else "Kontakta oss för pris och demo."
    )

    cards = "".join(build_bot_card(bot) for bot in bots)

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{site_title}</title>
  <meta name="description" content="Färdiga Telegram-botar byggda av {username}. Köp, anpassa och deploya direkt.">
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
      <p>Färdiga Telegram-botar från GitHub — analyserade, beskrivna och redo att köpas eller anpassas efter dina behov.</p>
      <div class="stats">
        <span class="stat"><strong>{total}</strong> produkter</span>
        <span class="stat">av <strong>@{username}</strong></span>
        <span class="stat">Uppdaterad <strong>{generated_at}</strong></span>
      </div>
    </div>
  </header>

  <main class="container">
    <div class="grid">
      {cards}
    </div>
  </main>

  <section id="kontakt">
    <div class="container">
      <h2>Intresserad?</h2>
      <p>{contact_block}</p>
      <p style="margin-top: 1rem;">Stripe-betalning kan kopplas in senare — byt ut knappen «Köp / fråga» mot din betalningslänk.</p>
    </div>
  </section>

  <footer>
    <div class="container">Genererad automatiskt av Bot Description Generator</div>
  </footer>
</body>
</html>
"""


def save_landing_page(output_data):
    """Сохраняет HTML-лендинг на диск."""
    html_content = build_landing_html(output_data)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as file:
        file.write(html_content)


def load_json_output():
    """Читает ранее сохранённый JSON."""
    json_path = Path(OUTPUT_FILE)
    if not json_path.exists():
        print(f"❌ Filen {OUTPUT_FILE} hittades inte. Kör skriptet utan --html-only först.")
        return None
    with open(json_path, encoding="utf-8") as file:
        return json.load(file)


def parse_args():
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Generera säljande beskrivningar för GitHub Telegram-botar."
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
        help="Skapa bara HTML från befintlig bot_descriptions.json (ingen API-anrop).",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skippa HTML-generering, spara bara JSON.",
    )
    return parser.parse_args()


def choose_target_repos(repos, args):
    """Выбирает, какие репозитории обрабатывать."""
    bot_repos = [r for r in repos if is_telegram_bot(r)]
    other_repos = [r for r in repos if not is_telegram_bot(r)]

    print(f"   → {len(bot_repos)} Telegram-botar identifierade")
    print(f"   → {len(other_repos)} övriga repos")

    if args.all:
        return repos
    if args.bots_only:
        return bot_repos

    # Из .env или интерактивный выбор в терминале
    if REPO_FILTER == "all":
        return repos
    if REPO_FILTER == "bots":
        return bot_repos

    print("\nVilka repos vill du generera beskrivningar för?")
    print("  1 = Bara Telegram-botar")
    print("  2 = Alla repos")
    choice = input("Välj (1/2): ").strip()
    return repos if choice == "2" else bot_repos


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
        output = load_json_output()
        if not output:
            sys.exit(1)
        save_landing_page(output)
        print(f"✅ HTML sparad i {OUTPUT_HTML}")
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

    if not args.no_html:
        save_landing_page(output)
        print(f"✅ HTML-landningssida sparad i {OUTPUT_HTML}")

    print("\nNästa steg:")
    print("  → Öppna bot_descriptions.json och granska")
    print(f"  → Öppna {OUTPUT_HTML} i webbläsaren")
    print("  → Byt ut «Köp / fråga» mot Stripe-länkar när du är redo")


if __name__ == "__main__":
    main()
