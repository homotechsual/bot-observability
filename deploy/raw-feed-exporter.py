#!/usr/bin/env python3
"""Expose latest raw feed item metadata as Prometheus metrics."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

HALO_STATUS_FEED_URL = os.getenv(
    "HALO_STATUS_FEED_URL",
    "https://status.haloservicesolutions.com/pages/63ef45da7ee94905308a1a4a/rss",
).strip()
HUDU_RELEASE_FEED_URL = os.getenv(
    "HUDU_RELEASE_FEED_URL",
    "https://hq.hudu.com/public/releases.json",
).strip()
YOUTUBE_FEED_URLS = [
    value.strip()
    for value in os.getenv("YOUTUBE_FEED_URLS", "").split(",")
    if value.strip()
]
HALO_YOUTUBE_FEED_URLS_ENDPOINT = os.getenv(
    "HALO_YOUTUBE_FEED_URLS_ENDPOINT",
    "http://host.docker.internal:9191/observability/youtube-feed-urls",
).strip()
HUDU_YOUTUBE_FEED_URLS_ENDPOINT = os.getenv(
    "HUDU_YOUTUBE_FEED_URLS_ENDPOINT",
    "http://host.docker.internal:9192/observability/youtube-feed-urls",
).strip()
PORT = int(os.getenv("RAW_FEED_EXPORTER_PORT", "9115"))
TIMEOUT_SECONDS = float(os.getenv("RAW_FEED_TIMEOUT_SECONDS", "20"))
CACHE_SECONDS = float(os.getenv("RAW_FEED_CACHE_SECONDS", "60"))

_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": ""}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    # RSS dates typically follow RFC 2822.
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    # Fallback for ISO 8601 variants.
    candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def read_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "bot-observability-raw-feed-exporter/1.0",
            "Accept": "application/rss+xml, application/atom+xml, application/json, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
        content_type = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(content_type, errors="replace")


def read_url_with_failure(url: str) -> tuple[Optional[str], Optional[dict[str, str]]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "bot-observability-raw-feed-exporter/1.0",
            "Accept": "application/rss+xml, application/atom+xml, application/json, */*",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            content_type = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(content_type, errors="replace")
            return body, None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""

        return None, {
            "stage": "fetch",
            "reason": "http_error",
            "status": str(exc.code),
            "content_type": (exc.headers.get_content_type() if exc.headers else "").strip(),
            "body_kind": classify_body_shape(body),
        }
    except urllib.error.URLError as exc:
        return None, {
            "stage": "fetch",
            "reason": "url_error",
            "status": "",
            "content_type": "",
            "body_kind": "",
        }
    except (TimeoutError, ValueError) as exc:
        return None, {
            "stage": "fetch",
            "reason": exc.__class__.__name__.lower(),
            "status": "",
            "content_type": "",
            "body_kind": "",
        }


def classify_body_shape(body: str) -> str:
    text = body.lstrip().lower()
    if not text:
        return "empty"
    if text.startswith("<!doctype html") or text.startswith("<html"):
        return "html"
    if text.startswith("<feed") or text.startswith("<rss"):
        return "xml"
    if text.startswith("{") or text.startswith("["):
        return "json"
    return "text"


def first_text(
    element: ET.Element,
    names: list[str],
    namespaces: Optional[dict[str, str]] = None,
) -> str:
    for name in names:
        node = element.find(name, namespaces or {})
        if node is not None and node.text:
            return node.text.strip()
    return ""


def parse_rss_latest(content: str) -> Optional[dict[str, Any]]:
    root = ET.fromstring(content)
    items = root.findall("./channel/item")
    latest: Optional[dict[str, Any]] = None

    for item in items:
        title = first_text(item, ["title"])
        link = first_text(item, ["link"])
        date_text = first_text(item, ["pubDate", "date", "updated"])
        published = parse_datetime(date_text)
        if not published:
            continue

        entry = {"title": title, "link": link, "published": published}
        if latest is None or published > latest["published"]:
            latest = entry

    return latest


def parse_atom_latest(content: str) -> Optional[dict[str, Any]]:
    root = ET.fromstring(content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    latest: Optional[dict[str, Any]] = None

    for entry in entries:
        title = first_text(entry, ["atom:title", "title"], ns)

        link = ""
        for node in entry.findall("atom:link", ns) + entry.findall("link"):
            href = (node.attrib.get("href") or "").strip()
            rel = (node.attrib.get("rel") or "").strip().lower()
            if href and (not rel or rel == "alternate"):
                link = href
                break

        date_text = first_text(
            entry,
            ["atom:published", "published", "atom:updated", "updated"],
            ns,
        )
        published = parse_datetime(date_text)
        if not published:
            continue

        candidate = {"title": title, "link": link, "published": published}
        if latest is None or published > latest["published"]:
            latest = candidate

    return latest


def parse_hudu_release_latest(content: str) -> Optional[dict[str, Any]]:
    payload = json.loads(content)
    releases = payload.get("releases") if isinstance(payload, dict) else payload
    if not isinstance(releases, list):
        return None

    latest: Optional[dict[str, Any]] = None
    date_keys = [
        "published_at",
        "release_date",
        "created_at",
        "updated_at",
        "date",
    ]
    title_keys = ["name", "title", "version", "display_name"]
    link_keys = ["url", "link", "html_url"]

    for release in releases:
        if not isinstance(release, dict):
            continue

        published: Optional[datetime] = None
        for key in date_keys:
            published = parse_datetime(str(release.get(key, "")))
            if published:
                break

        if not published:
            continue

        title = ""
        for key in title_keys:
            value = release.get(key)
            if value:
                title = str(value).strip()
                break

        link = ""
        for key in link_keys:
            value = release.get(key)
            if value:
                link = str(value).strip()
                break

        candidate = {"title": title, "link": link, "published": published}
        if latest is None or published > latest["published"]:
            latest = candidate

    return latest


def parse_feed(url: str, content: str, parser_hint: str) -> Optional[dict[str, Any]]:
    hint = parser_hint.lower().strip()
    if hint == "json":
        return parse_hudu_release_latest(content)

    trimmed = content.lstrip()
    if trimmed.startswith("{") or trimmed.startswith("["):
        return parse_hudu_release_latest(content)

    if "<feed" in trimmed[:500]:
        return parse_atom_latest(content)

    return parse_rss_latest(content)


def prometheus_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def build_youtube_urls() -> list[str]:
    """Build YouTube feed URL list from bot endpoint payloads plus manual fallback URLs."""
    urls = set(YOUTUBE_FEED_URLS)

    for endpoint in [HALO_YOUTUBE_FEED_URLS_ENDPOINT, HUDU_YOUTUBE_FEED_URLS_ENDPOINT]:
        if not endpoint:
            continue
        urls.update(fetch_feed_urls_from_endpoint(endpoint))

    return sorted(urls)


def fetch_feed_urls_from_endpoint(endpoint: str) -> list[str]:
    content, _ = read_url_with_failure(endpoint)
    if content is None:
        return []

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return []

    raw_urls = payload.get("feedUrls") if isinstance(payload, dict) else None
    if not isinstance(raw_urls, list):
        return []

    urls: list[str] = []
    for value in raw_urls:
        if not isinstance(value, str):
            continue
        trimmed = value.strip()
        if not trimmed:
            continue
        urls.append(trimmed)

    return urls


def feed_targets(youtube_urls: list[str]) -> list[dict[str, Any]]:

    return [
        {
            "feed": "youtube",
            "urls": youtube_urls,
            "parser": "xml",
        },
        {
            "feed": "halo_status",
            "urls": [HALO_STATUS_FEED_URL] if HALO_STATUS_FEED_URL else [],
            "parser": "xml",
        },
        {
            "feed": "hudu_release",
            "urls": [HUDU_RELEASE_FEED_URL] if HUDU_RELEASE_FEED_URL else [],
            "parser": "json",
        },
    ]


def scrape_metrics() -> str:
    youtube_urls = build_youtube_urls()

    lines: list[str] = [
        "# HELP raw_feed_latest_item_unixtime Unix timestamp of the latest published item by feed.",
        "# TYPE raw_feed_latest_item_unixtime gauge",
        "# HELP raw_feed_scrape_success 1 when at least one source URL for a feed was read and parsed successfully.",
        "# TYPE raw_feed_scrape_success gauge",
        "# HELP raw_feed_last_scrape_unixtime Unix timestamp of the exporter scrape time.",
        "# TYPE raw_feed_last_scrape_unixtime gauge",
        "# HELP raw_feed_youtube_first_failure_info First observed failure details for the YouTube scrape in the current scrape cycle.",
        "# TYPE raw_feed_youtube_first_failure_info gauge",
    ]

    scrape_time = now_utc().timestamp()

    for target in feed_targets(youtube_urls):
        feed_name = target["feed"]
        urls = target["urls"]
        parser = target["parser"]
        latest: Optional[dict[str, Any]] = None
        ok = 0
        first_failure: Optional[dict[str, str]] = None
        first_failure_url = ""

        for url in urls:
            content, failure = read_url_with_failure(url)
            if content is None:
                if feed_name == "youtube" and first_failure is None:
                    first_failure = failure or {}
                    first_failure_url = url
                continue

            try:
                parsed = parse_feed(url, content, parser)
                if parsed and parsed.get("published"):
                    ok = 1
                    candidate = {
                        "published": parsed["published"],
                        "title": str(parsed.get("title", "")).strip(),
                        "link": str(parsed.get("link", "")).strip(),
                        "source_url": url,
                    }
                    if latest is None or candidate["published"] > latest["published"]:
                        latest = candidate
                elif feed_name == "youtube" and first_failure is None:
                    first_failure = {
                        "stage": "parse",
                        "reason": "no_published_item",
                        "status": "",
                        "content_type": "",
                        "body_kind": classify_body_shape(content),
                    }
                    first_failure_url = url
            except (json.JSONDecodeError, ET.ParseError) as exc:
                if feed_name == "youtube" and first_failure is None:
                    first_failure = {
                        "stage": "parse",
                        "reason": exc.__class__.__name__.lower(),
                        "status": "",
                        "content_type": "",
                        "body_kind": classify_body_shape(content),
                    }
                    first_failure_url = url

        labels = f'feed="{feed_name}"'
        lines.append(f"raw_feed_scrape_success{{{labels}}} {ok}")
        lines.append(f"raw_feed_last_scrape_unixtime{{{labels}}} {scrape_time:.0f}")

        if feed_name == "youtube" and first_failure:
            failure_labels = [
                'feed="youtube"',
                f'url="{prometheus_escape(first_failure_url)}"',
                f'stage="{prometheus_escape(first_failure.get("stage", ""))}"',
                f'reason="{prometheus_escape(first_failure.get("reason", ""))}"',
                f'status="{prometheus_escape(first_failure.get("status", ""))}"',
                f'content_type="{prometheus_escape(first_failure.get("content_type", ""))}"',
                f'body_kind="{prometheus_escape(first_failure.get("body_kind", ""))}"',
            ]
            lines.append(f"raw_feed_youtube_first_failure_info{{{','.join(failure_labels)}}} 1")

        if latest is None:
            continue

        metric_labels = [
            f'feed="{feed_name}"',
            f'item_title="{prometheus_escape(latest["title"])}"',
            f'item_link="{prometheus_escape(latest["link"])}"',
            f'source_url="{prometheus_escape(latest["source_url"])}"',
        ]
        lines.append(
            f"raw_feed_latest_item_unixtime{{{','.join(metric_labels)}}} {latest['published'].timestamp():.0f}"
        )

    lines.append("")
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/metrics", "/metrics/"):
            self.send_response(404)
            self.end_headers()
            return

        current = time.time()
        if current >= _CACHE["expires_at"] or not _CACHE["payload"]:
            _CACHE["payload"] = scrape_metrics()
            _CACHE["expires_at"] = current + CACHE_SECONDS

        payload = _CACHE["payload"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        # Keep exporter output quiet unless there is a runtime error.
        return


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()