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
import re
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


PATCH_LINK_RE = re.compile(r"/news/game-updates/[a-z0-9-]*patch-(\d+)-(\d+)-notes/?$")


def find_latest_patch_url() -> str:
    """Scrape the patch notes tag/index page and return the article URL
    with the highest patch number (e.g. 26.8 beats 26.7).

    Riot is inconsistent about whether the link includes a
    "league-of-legends-" prefix (e.g. "league-of-legends-patch-26-8-notes"
    vs just "patch-26-3-notes") and links have no trailing slash, so the
    regex tolerates both. Picking the highest patch number is more
    reliable than assuming the first link on the page is newest.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PatchBot/1.0)"}
    resp = requests.get(PATCH_NOTES_INDEX, headers=headers, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    best_url = None
    best_version = (-1, -1)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = PATCH_LINK_RE.search(href)
        if not match:
            continue
        version = (int(match.group(1)), int(match.group(2)))
        if version > best_version:
            best_version = version
            best_url = href if href.startswith("http") else f"https://www.leagueoflegends.com{href}"

    if best_url is None:
        raise RuntimeError(
            "Couldn't find any patch notes link on the index page. Riot "
            "may have changed the page layout — open the index URL and "
            "check the href pattern PATCH_LINK_RE is matching against."
        )

    return best_url


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
        Discord channel of casual-to-mid ranked players who want the
        real numbers, not a vibes-based recap. Requirements:

        - Use Discord markdown (bold with **, bullet points with -)
        - Organize into these sections, in this order, skipping any
          section with no changes: Champions (Summoner's Rift), Items,
          System changes, Arena
        - Completely skip ARAM/ARAM: Mayhem changes — do not mention them
          even in passing
        - For every Summoner's Rift champion and item change, give the
          exact old value -> new value for every stat that changed
          (e.g. "Base AD: 60 -> 64", "Cooldown: 14/12/10/8/6 -> 12/10/8/6/4").
          Do not round, paraphrase, or drop numbers to save space.
        - After the numbers for each champion/item, add one short line
          on the practical effect (buff/nerf/rework and how it changes
          how they play)
        - Arena changes should be just as detailed and numeric as Rift —
          list augment/item/system changes with exact old -> new values
        - Bugfixes and pure text/UI changes can be summarized briefly
          without numbers unless the bug itself involved specific values
        - No preamble like "Here's a summary" — just the content
        - No arbitrary length cap — include everything qualifying above,
          even if the message ends up long. Do not compress or omit
          champions/items to hit a shorter length.

        Patch notes:
        {patch_text[:30000]}
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
            "max_tokens": 4000,
        },
        timeout=120,
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
