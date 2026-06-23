#!/usr/bin/env python3
"""Social and web-search news sources via Agent-Reach upstream CLIs.

Each fetcher shells out to an upstream tool (twitter-cli, rdt-cli / OpenCLI,
mcporter + Exa) and returns a list of article-shaped dicts identical to the RSS
articles in ``agent.py`` so they flow through the same analysis pipeline.

Every fetcher degrades gracefully: a missing or unauthenticated CLI yields an
empty list plus a status note, never an exception. The agent stays fully
functional on RSS alone and lights up each social source as you configure it.

Setup (enable only what you want):
  Twitter/X : pipx install twitter-cli
              export TWITTER_AUTH_TOKEN="..." TWITTER_CT0="..."   (browser cookies)
  Reddit    : pipx install 'git+https://github.com/public-clis/rdt-cli.git'
              rdt login            (or OpenCLI desktop — reuses Chrome login)
  Exa       : npm install -g mcporter
              mcporter config add exa https://mcp.exa.ai/mcp      (free, no key)

NOTE: the upstream CLIs are evolving and their JSON shapes vary by version, so
the extractors below are deliberately forgiving (they search common key names).
If a source returns items but fields look wrong, adjust ``_FIELD_*`` below.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable

# Field-name candidates for forgiving extraction across CLI versions.
_FIELD_TEXT = ("text", "full_text", "content", "body", "selftext", "snippet", "description")
_FIELD_TITLE = ("title", "headline", "name")
_FIELD_URL = ("url", "link", "permalink", "tweet_url", "post_url", "href")
_FIELD_AUTHOR = ("author", "username", "screen_name", "user", "handle", "subreddit")
_FIELD_TIME = ("created_at", "createdAt", "created_utc", "publishedDate", "published", "date", "time")
_LIST_KEYS = ("results", "data", "items", "tweets", "posts", "entries", "hits")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _article(source: str, title: str, summary: str, link: str, published: str) -> dict[str, Any]:
    title = (title or "").strip()
    raw_id = link or title or summary
    return {
        "id": hashlib.sha256(raw_id.encode("utf-8")).hexdigest(),
        "source": source,
        "title": title[:300],
        "summary": (summary or "").strip()[:400],
        "link": (link or "").strip(),
        "published": published or "recent",
    }


def _field(item: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = item.get(name)
        if isinstance(value, dict):
            # e.g. {"user": {"screen_name": "x"}}
            value = value.get("screen_name") or value.get("name") or value.get("title")
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _coerce_items(data: Any) -> list[dict[str, Any]]:
    """Pull a list of record dicts out of an arbitrary CLI JSON payload."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # Some tools nest one level deeper, e.g. {"data": {"results": [...]}}.
        for value in data.values():
            if isinstance(value, dict):
                nested = _coerce_items(value)
                if nested:
                    return nested
    return []


def _parse_json(stdout: str) -> Any:
    stdout = (stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    # mcporter and some CLIs wrap JSON in surrounding log text; grab the
    # outermost JSON object/array if present.
    match = re.search(r"(\{.*\}|\[.*\])", stdout, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def _run(cmd: list[str], timeout: int) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    if result.returncode != 0:
        return False, (result.stdout or "") + (result.stderr or "")
    return True, result.stdout or ""


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #

def availability(env: dict[str, str]) -> dict[str, bool]:
    """Best-effort check of which social sources are usable right now."""
    twitter = bool(shutil.which("twitter")) and bool(
        env.get("TWITTER_AUTH_TOKEN") or env.get("TWITTER_CT0")
    )
    reddit = bool(shutil.which("rdt") or shutil.which("opencli"))
    exa = bool(shutil.which("mcporter"))
    return {"twitter": twitter, "reddit": reddit, "exa": exa}


# --------------------------------------------------------------------------- #
# Per-source fetchers (return article dicts; never raise)
# --------------------------------------------------------------------------- #

def fetch_twitter(query: str, limit: int, timeout: int) -> list[dict[str, Any]]:
    if not shutil.which("twitter"):
        return []
    ok, out = _run(["twitter", "search", query, "-n", str(limit), "--json"], timeout)
    if not ok:
        # search endpoint is the flaky one; fall back to OpenCLI if present.
        if shutil.which("opencli"):
            ok, out = _run(["opencli", "twitter", "search", query, "-f", "json"], timeout)
        if not ok:
            return []
    items = _coerce_items(_parse_json(out))
    articles: list[dict[str, Any]] = []
    for item in items[:limit]:
        text = _field(item, _FIELD_TEXT)
        if not text:
            continue
        author = _field(item, _FIELD_AUTHOR)
        title = f"@{author}: {text[:120]}" if author else text[:120]
        articles.append(
            _article("X/Twitter", title, text, _field(item, _FIELD_URL), _field(item, _FIELD_TIME))
        )
    return articles


def _find_children(data: Any) -> list[dict[str, Any]]:
    """Locate the Reddit listing ``children`` array anywhere in the payload.

    rdt returns the native Reddit API shape
    ``{"data": {"data": {"children": [{"kind": "t3", "data": {<post>}}]}}}``;
    OpenCLI may wrap it differently, so search recursively for ``children``.
    """
    if isinstance(data, dict):
        children = data.get("children")
        if isinstance(children, list):
            return [c for c in children if isinstance(c, dict)]
        for value in data.values():
            found = _find_children(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_children(value)
            if found:
                return found
    return []


def fetch_reddit(query: str, limit: int, timeout: int) -> list[dict[str, Any]]:
    out = ""
    ok = False
    if shutil.which("rdt"):
        ok, out = _run(["rdt", "search", query, "--limit", str(limit), "--json"], timeout)
        if not ok:
            ok, out = _run(["rdt", "search", query, "--limit", str(limit)], timeout)
    if not ok and shutil.which("opencli"):
        ok, out = _run(["opencli", "reddit", "search", query, "-f", "json"], timeout)
    if not ok:
        return []

    return _reddit_articles(out, limit)


def fetch_reddit_sub(sub: str, limit: int, timeout: int) -> list[dict[str, Any]]:
    """Pull hot posts from a specific subreddit (e.g. stocks, wallstreetbets)."""
    out = ""
    ok = False
    if shutil.which("rdt"):
        ok, out = _run(["rdt", "sub", sub, "--limit", str(limit), "--json"], timeout)
        if not ok:
            ok, out = _run(["rdt", "sub", sub, "--limit", str(limit)], timeout)
    if not ok and shutil.which("opencli"):
        ok, out = _run(["opencli", "reddit", "subreddit", sub, "-f", "json"], timeout)
    if not ok:
        return []
    return _reddit_articles(out, limit)


def _reddit_articles(out: str, limit: int) -> list[dict[str, Any]]:
    parsed = _parse_json(out)
    children = _find_children(parsed)
    # Each child is {"kind": "t3", "data": {<post>}}; fall back to generic
    # coercion if the payload isn't in Reddit listing shape (e.g. OpenCLI).
    posts = [c.get("data", c) for c in children] if children else _coerce_items(parsed)

    articles: list[dict[str, Any]] = []
    for post in posts[:limit]:
        if not isinstance(post, dict):
            continue
        title = _field(post, _FIELD_TITLE) or _field(post, _FIELD_TEXT)
        if not title:
            continue
        sub = post.get("subreddit") or ""
        permalink = post.get("permalink") or ""
        link = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else _field(post, _FIELD_URL)
        body = post.get("selftext") or ""
        score = post.get("score")
        comments = post.get("num_comments")
        meta = " ".join(
            part for part in (
                f"r/{sub}" if sub else "",
                f"{score}↑" if isinstance(score, int) else "",
                f"{comments} comments" if isinstance(comments, int) else "",
            ) if part
        )
        summary = (f"[{meta}] " if meta else "") + (body or title)
        published = ""
        created = post.get("created_utc") or post.get("created")
        if isinstance(created, (int, float)):
            published = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
        articles.append(_article("Reddit", title, summary, link, published))
    return articles


def _exa_text(out: str) -> str:
    """Pull the human-readable result text out of mcporter's JSON envelope.

    `mcporter call ... --output json` returns the MCP content envelope
    ``{"content": [{"type": "text", "text": "Title: ...\\nURL: ..."}]}`` where the
    actual search hits live as one formatted text blob (verified live 2026-06).
    """
    data = _parse_json(out)
    if isinstance(data, dict):
        blocks = data.get("content")
        if isinstance(blocks, list):
            texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("text")]
            if texts:
                return "\n".join(texts)
    return out  # plain-text output fallback


def _parse_exa_blocks(text: str, limit: int) -> list[dict[str, Any]]:
    """Split Exa's ``Title:/URL:/Highlights:`` blocks (separated by ---)."""
    articles: list[dict[str, Any]] = []
    for block in re.split(r"\n-{3,}\n", text):
        block = block.strip()
        if not block:
            continue
        title = _line_value(block, "Title")
        url = _line_value(block, "URL")
        published = _line_value(block, "Published")
        if published.upper() in ("", "N/A"):
            published = "recent"
        highlights = ""
        match = re.search(r"Highlights:\s*(.+)", block, re.DOTALL)
        if match:
            # Exa joins snippet fragments with literal "..." separators.
            highlights = re.sub(r"\s*\.\.\.\s*", " ", match.group(1)).strip()
        if not (title or highlights):
            continue
        articles.append(_article("Exa Search", title or highlights[:120], highlights, url, published))
        if len(articles) >= limit:
            break
    return articles


def _line_value(block: str, key: str) -> str:
    match = re.search(rf"^{key}:\s*(.+)$", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def fetch_exa(query: str, limit: int, timeout: int) -> list[dict[str, Any]]:
    if not shutil.which("mcporter"):
        return []
    call = f'exa.web_search_exa(query: "{query}", numResults: {limit})'
    ok, out = _run(["mcporter", "call", call, "--output", "json"], timeout)
    if not ok:
        return []
    return _parse_exa_blocks(_exa_text(out), limit)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

_FETCHERS: dict[str, Callable[[str, int, int], list[dict[str, Any]]]] = {
    "twitter": fetch_twitter,
    "reddit": fetch_reddit,
    "exa": fetch_exa,
}


def gather(
    queries: list[str],
    *,
    env: dict[str, str],
    reddit_subs: list[str] | None = None,
    per_query_limit: int = 8,
    timeout: int = 30,
    log: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch articles from every available social source.

    Each source is searched with ``queries``; Reddit additionally pulls hot
    posts from ``reddit_subs`` (e.g. stocks, wallstreetbets). Returns
    ``(articles, notes)`` where ``notes`` is a short per-source status list for
    the /sources command.
    """
    ready = availability(env)
    notes: list[str] = []
    articles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _collect(items: list[dict[str, Any]]) -> int:
        added = 0
        for art in items:
            if art["id"] in seen_ids:
                continue
            seen_ids.add(art["id"])
            articles.append(art)
            added += 1
        return added

    for name, fetcher in _FETCHERS.items():
        if not ready.get(name):
            notes.append(f"{name}: not configured (skipped)")
            continue
        count = 0
        for query in queries:
            try:
                count += _collect(fetcher(query, per_query_limit, timeout))
            except Exception as exc:  # never let one source break the cycle
                if log:
                    log.warning("Social fetch failed (%s, %r): %s", name, query, exc)
        if name == "reddit":
            for sub in reddit_subs or []:
                try:
                    count += _collect(fetch_reddit_sub(sub, per_query_limit, timeout))
                except Exception as exc:
                    if log:
                        log.warning("Reddit sub fetch failed (%r): %s", sub, exc)
        notes.append(f"{name}: {count} items")
    return articles, notes
