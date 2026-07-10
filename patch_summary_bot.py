"""
LoL Patch Notes -> OpenRouter summary -> Discord webhook

Usage:
    python patch_summary_bot.py --url https://www.leagueoflegends.com/en-us/news/game-updates/patch-14-x-notes/

Config via environment variables (recommended, so you never hardcode secrets):
    OPENROUTER_API_KEY   your OpenRouter key
    DISCORD_WEBHOOK_URL  your Discord channel webhook URL
    OPENROUTER_MODEL     e.g. "anthropic/claude-3.5-haiku" or "openai/gpt-4o-mini" (default below)

Install deps:
    pip install requests beautifulsoup4 --break-system-packages
"""

import os
import sys
import argparse
import textwrap
import requests
from bs4 import BeautifulSoup

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-haiku")

DISCORD_MSG_LIMIT = 2000  # Discord hard limit per message
PATCH_NOTES_INDEX = "https://www.leagueoflegends.com/en-us/news/tags/patch-notes/"
SEEN_FILE = "last_patch_url.txt"  # tracks the last patch we already posted


def find_latest_patch_url() -> str:
    """Scrape the patch notes tag/index page and return the newest
    patch notes article URL."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PatchBot/1.0)"}
    resp = requests.get(PATCH_NOTES_INDEX, headers=headers, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news/game-updates/patch-" in href and href.endswith("-notes/"):
            if href.startswith("http"):
                return href
            return f"https://www.leagueoflegends.com{href}"

    raise RuntimeError(
        "Couldn't find a patch notes link on the index page. Riot may "
        "have changed the page layout — open the index URL and check "
        "the href pattern this function is matching against."
    )


def already_posted(url: str) -> bool:
    if not os.path.exists(SEEN_FILE):
        return False
    with open(SEEN_FILE) as f:
        return f.read().strip() == url.strip()


def mark_posted(url: str):
    with open(SEEN_FILE, "w") as f:
        f.write(url.strip())


def fetch_patch_notes_text(url: str) -> str:
    """Download the patch notes page and pull out the readable text.

    Riot's site is a bit different page-to-page, so this grabs every
    <p>, <li>, and <h2>/<h3> heading inside the main article body and
    joins them. If it comes back empty or looks wrong, open the page's
    HTML (view-source) and adjust the `article` selector below to match
    whatever container wraps the actual notes.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PatchBot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try to scope to the main article content if such a container exists.
    article = soup.find("article") or soup.find("main") or soup

    parts = []
    for tag in article.find_all(["h2", "h3", "p", "li"]):
        text = tag.get_text(strip=True)
        if text:
            parts.append(text)

    full_text = "\n".join(parts)

    if len(full_text) < 200:
        raise RuntimeError(
            "Scraped content looks too short — the page is probably "
            "rendered with JavaScript and requests/BeautifulSoup can't "
            "see it. See the note at the bottom of this file for a "
            "fallback (paste the notes in manually, or use a headless "
            "browser like playwright)."
        )

    return full_text


def summarize_with_openrouter(patch_text: str) -> str:
    """Send the patch notes text to an OpenRouter model and get back a
    Discord-friendly summary."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY environment variable is not set.")

    prompt = textwrap.dedent(f"""
        Summarize the following League of Legends patch notes for a
        Discord channel of casual-to-mid ranked players. Requirements:

        - Use Discord markdown (bold with **, bullet points with -)
        - Organize by section: Champions, Items, System changes, ARAM/other
          (skip any section that has no changes)
        - For champion changes, briefly say whether it's a buff, nerf, or
          rework, and the practical effect on how they play
        - Keep it under 1500 characters total
        - Skip pure numeric bookkeeping unless it changes how a champion
          or item feels to play
        - No preamble like "Here's a summary" — just the content

        Patch notes:
        {patch_text[:12000]}
    """).strip()

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def send_to_discord(content: str, source_url: str = None):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL environment variable is not set.")

    if source_url:
        content = f"{content}\n\n_Source: {source_url}_"

    # Split into <=2000 char chunks on line breaks so we don't cut mid-sentence
    chunks = []
    current = ""
    for line in content.split("\n"):
        if len(current) + len(line) + 1 > DISCORD_MSG_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=20)
        resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description="Summarize LoL patch notes to Discord")
    parser.add_argument(
        "--url",
        help="URL of a specific patch notes page. If omitted, auto-detects "
             "the latest one from Riot's patch notes index.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Post even if this URL was already posted before (skips the dedup check).",
    )
    args = parser.parse_args()

    url = args.url or find_latest_patch_url()
    print(f"Using patch notes URL: {url}")

    if not args.force and already_posted(url):
        print("Already posted this patch — nothing to do.")
        return

    patch_text = fetch_patch_notes_text(url)

    print(f"Got {len(patch_text)} characters. Summarizing with {OPENROUTER_MODEL} ...")
    summary = summarize_with_openrouter(patch_text)

    print("Posting to Discord ...")
    send_to_discord(summary, source_url=url)

    mark_posted(url)
    print("Done.")


if __name__ == "__main__":
    main()
