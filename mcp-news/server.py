"""
mcp-news — MCP-сервер для дайджеста новостей с Хабра.

Инструменты:
  - search_habr(query, period, limit) — ищет статьи через RSS поиска Хабра.
  - fetch_article(url)                — достаёт полный текст статьи.

Транспорт: streamable HTTP на 0.0.0.0:8000, endpoint /mcp.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx
import trafilatura
from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mcp-news")

mcp = FastMCP("mcp-news")

USER_AGENT = "Mozilla/5.0 (compatible; news-digest-bot/1.0; +https://example.org)"
HABR_RSS_SEARCH = "https://habr.com/ru/rss/search/posts/"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


def _parse_period(period: str) -> tuple[datetime, datetime]:
    """Превращает строку периода в окно (начало, конец) в UTC.

    Форматы: "24h", "7d", "2w", "1m" или диапазон "2026-06-01..2026-06-08".
    При неизвестном формате возвращает последние 7 дней.
    """
    now = datetime.now(timezone.utc)
    period = (period or "7d").strip().lower()

    if ".." in period:
        left, right = period.split("..", 1)
        try:
            start = datetime.fromisoformat(left.strip()).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(right.strip()).replace(tzinfo=timezone.utc)
            return start, end
        except ValueError:
            log.warning("Не удалось разобрать диапазон '%s', беру последние 7 дней", period)
            return now - timedelta(days=7), now

    match = re.fullmatch(r"(\d+)\s*([hdwm])", period)
    if not match:
        log.warning("Неизвестный формат периода '%s', беру последние 7 дней", period)
        return now - timedelta(days=7), now

    amount, unit = int(match.group(1)), match.group(2)
    delta = {
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
        "m": timedelta(days=30 * amount),
    }[unit]
    return now - delta, now


def _parse_dt(value: str | None) -> datetime | None:
    """Парсит дату из RSS (RFC-2822) или ISO-формата в aware-datetime UTC."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _http_get(url: str, params: dict | None = None) -> str | None:
    """GET с таймаутами. При сетевой ошибке или 404 возвращает None."""
    try:
        with httpx.Client(
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
            follow_redirects=True,
        ) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.text
    except httpx.HTTPError as exc:
        log.error("Ошибка запроса к %s: %s", url, exc)
        return None


def _strip_tags(text: str) -> str:
    """Убирает HTML-теги из сниппета RSS."""
    return re.sub(r"<[^>]+>", " ", text).strip()


@mcp.tool
def search_habr(query: str, period: str = "7d", limit: int = 5) -> dict:
    """Ищет статьи на Хабре по теме за указанный период через RSS.

    Используй этот инструмент ПЕРВЫМ, когда пользователь просит дайджест/обзор
    новостей по теме. Полный текст статей берётся отдельно через fetch_article.

    Args:
        query: тема/поисковый запрос (например "LLM агенты").
        period: за какой срок брать материалы. Форматы: "24h", "7d", "2w", "1m"
            или диапазон "2026-06-01..2026-06-08". Извлекай из фразы пользователя:
            "за неделю" -> "7d", "за сутки" -> "24h", "за месяц" -> "1m".
        limit: сколько статей вернуть (1-5).

    Returns:
        Словарь с полями:
          period_start (str): начало периода ДД.ММ.ГГГГ — используй во вводке.
          period_end (str): конец периода ДД.ММ.ГГГГ — используй во вводке.
          articles (list): список {title, url, published_at, author, source, snippet}.
    """
    limit = max(1, min(int(limit), 5))
    start, end = _parse_period(period)

    xml_text = _http_get(
        HABR_RSS_SEARCH,
        params={"q": query, "order_by": "date", "target_type": "posts", "hl": "ru", "fl": "ru"},
    )
    if not xml_text:
        return {"period_start": start.strftime("%d.%m.%Y"), "period_end": end.strftime("%d.%m.%Y"), "articles": []}

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.error("Не удалось разобрать RSS: %s", exc)
        return {"period_start": start.strftime("%d.%m.%Y"), "period_end": end.strftime("%d.%m.%Y"), "articles": []}

    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    results: list[dict] = []

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        pub_str = item.findtext("pubDate") or item.findtext("dc:date", namespaces=ns)
        published = _parse_dt(pub_str)

        if published and not (start <= published <= end):
            continue

        description = item.findtext("description") or ""
        snippet = _strip_tags(description)[:300]
        author = (item.findtext("dc:creator", namespaces=ns) or "").strip()

        if not url:
            continue

        results.append({
            "title": title,
            "url": url.split("?")[0],
            "published_at": published.isoformat() if published else None,
            "author": author,
            "source": "habr.com",
            "snippet": snippet,
        })
        if len(results) >= limit:
            break

    log.info("search_habr(query=%r, period=%r) -> %d статей", query, period, len(results))
    return {
        "period_start": start.strftime("%d.%m.%Y"),
        "period_end": end.strftime("%d.%m.%Y"),
        "articles": results,
    }


@mcp.tool
def fetch_article(url: str) -> dict:
    """Достаёт полный текст статьи по URL для составления аннотации.

    Args:
        url: прямая ссылка на статью.

    Returns:
        {title, text, author, published_at} или {error, url} при ошибке.
    """
    if not url or not url.startswith("http"):
        return {"error": "некорректный URL", "url": url}

    html = _http_get(url)
    if not html:
        return {"error": "источник недоступен или вернул ошибку", "url": url}

    try:
        extracted = trafilatura.extract(
            html,
            output_format="json",
            with_metadata=True,
            favor_precision=True,
        )
    except Exception as exc:
        log.error("Ошибка извлечения текста из %s: %s", url, exc)
        return {"error": "не удалось обработать страницу", "url": url}

    if not extracted:
        return {"error": "не удалось извлечь текст статьи", "url": url}

    try:
        data = json.loads(extracted)
    except (json.JSONDecodeError, TypeError):
        return {"error": "не удалось разобрать данные статьи", "url": url}

    text = data.get("text") or ""
    if len(text) > 2000:
        text = text[:2000] + "…"

    return {
        "title": data.get("title"),
        "text": text,
        "author": data.get("author"),
        "published_at": data.get("date"),
        "url": url,
    }


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, path="/mcp")