#!/usr/bin/env python3
"""Expose latest raw feed item metadata as Prometheus metrics."""

from __future__ import annotations

import json
import os
import re
import sqlite3
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
HALO_DB_PATH = os.getenv("HALO_DB_PATH", "/db/halo/halocommunitybot.db").strip()
HUDU_DB_PATH = os.getenv("HUDU_DB_PATH", "/db/hudu/huducommunitybot.db").strip()
PORT = int(os.getenv("RAW_FEED_EXPORTER_PORT", "9115"))
TIMEOUT_SECONDS = float(os.getenv("RAW_FEED_TIMEOUT_SECONDS", "20"))
CACHE_SECONDS = float(os.getenv("RAW_FEED_CACHE_SECONDS", "60"))

_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": ""}
_HANDLE_CACHE: dict[str, str] = {}
_CHANNEL_ID_RE = re.compile(r"\bUC[a-zA-Z0-9_-]{20,30}\b")


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


def first_text(element: ET.Element, names: list[str]) -> str:
    for name in names:
        node = element.find(name)
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
        title = first_text(entry, ["atom:title", "title"])

        link = ""
        for node in entry.findall("atom:link", ns) + entry.findall("link"):
            href = (node.attrib.get("href") or "").strip()
            rel = (node.attrib.get("rel") or "").strip().lower()
            if href and (not rel or rel == "alternate"):
                link = href
                break

        date_text = first_text(entry, ["atom:published", "published", "atom:updated", "updated"])
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


def query_channel_ids(db_path: str) -> list[str]:
    if not db_path or not os.path.exists(db_path):
        return []

    ids: set[str] = set()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = connection.cursor()
        table_names = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        if "YoutubeTrackedChannels" in table_names:
            # Prefer enabled channels when schema supports it; otherwise include all rows.
            columns = {row[1] for row in cursor.execute("PRAGMA table_info('YoutubeTrackedChannels')")}
            if "IsEnabled" in columns:
                rows = cursor.execute(
                    "SELECT ChannelId FROM YoutubeTrackedChannels WHERE COALESCE(IsEnabled, 1) = 1"
                )
            else:
                rows = cursor.execute("SELECT ChannelId FROM YoutubeTrackedChannels")

            for row in rows:
                value = str(row[0]).strip() if row and row[0] else ""
                if value:
                    ids.add(value)

        # FeedPostStates is a useful fallback when tracked-channel rows are missing or stale.
        if "FeedPostStates" in table_names:
            rows = cursor.execute(
                "SELECT SourceId FROM FeedPostStates WHERE FeedType = 'YouTube'"
            )
            for row in rows:
                value = str(row[0]).strip() if row and row[0] else ""
                if value:
                    ids.add(value)
    finally:
        connection.close()

    return sorted(ids)


def build_youtube_urls() -> list[str]:
    urls = set(YOUTUBE_FEED_URLS)

    channel_ids = set(discovered_youtube_channel_ids())

    for channel_id in channel_ids:
        urls.add(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")

    return sorted(urls)


def candidate_db_paths_for_source(db_path: str) -> list[str]:
    candidates: set[str] = set()

    if db_path and os.path.exists(db_path):
        candidates.add(db_path)

    folder = os.path.dirname(db_path)
    if folder and os.path.isdir(folder):
        for name in os.listdir(folder):
            if name.lower().endswith(".db"):
                candidates.add(os.path.join(folder, name))

    return sorted(candidates)


def source_db_paths() -> dict[str, str]:
    return {
        "halo": HALO_DB_PATH,
        "hudu": HUDU_DB_PATH,
    }


def discovered_youtube_references_by_source() -> dict[str, set[str]]:
    references_by_source: dict[str, set[str]] = {}

    for source_name, db_path in source_db_paths().items():
        references: set[str] = set()
        for path in candidate_db_paths_for_source(db_path):
            references.update(query_channel_ids(path))
        references_by_source[source_name] = references

    return references_by_source


def discovered_youtube_channel_ids_by_source() -> dict[str, list[str]]:
    channel_ids_by_source: dict[str, list[str]] = {}

    for source_name, references in discovered_youtube_references_by_source().items():
        channel_ids: set[str] = set()
        for reference in references:
            normalized = normalize_channel_id(reference)
            if normalized:
                channel_ids.add(normalized)
        channel_ids_by_source[source_name] = sorted(channel_ids)

    return channel_ids_by_source


def discovered_youtube_channel_ids() -> list[str]:
    channel_ids: set[str] = set()
    for source_channel_ids in discovered_youtube_channel_ids_by_source().values():
        channel_ids.update(source_channel_ids)

    return sorted(channel_ids)


def discovered_youtube_references() -> set[str]:
    references: set[str] = set()
    for source_references in discovered_youtube_references_by_source().values():
        references.update(source_references)
    return references


def candidate_db_paths() -> list[str]:
    candidates: set[str] = set()
    explicit_paths = [HALO_DB_PATH, HUDU_DB_PATH]

    for path in explicit_paths:
        if path and os.path.exists(path):
            candidates.add(path)

    # Fall back to scanning configured DB directories in case file names differ.
    for path in explicit_paths:
        folder = os.path.dirname(path)
        if not folder or not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if name.lower().endswith(".db"):
                candidates.add(os.path.join(folder, name))

    return sorted(candidates)


def normalize_channel_id(reference: str) -> Optional[str]:
    text = reference.strip()
    if not text:
        return None

    if text.startswith("http"):
        match = _CHANNEL_ID_RE.search(text)
        if match:
            return match.group(0)

        # Handle URLs like https://www.youtube.com/@handle by resolving channelId from HTML.
        if "youtube.com/@" in text:
            return resolve_channel_id_from_handle_url(text)

    if _CHANNEL_ID_RE.fullmatch(text):
        return text

    if text.startswith("@"):
        return resolve_channel_id_from_handle_url(f"https://www.youtube.com/{text}")

    return None


def resolve_channel_id_from_handle_url(url: str) -> Optional[str]:
    cached = _HANDLE_CACHE.get(url)
    if cached:
        return cached

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "bot-observability-raw-feed-exporter/1.0",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None

    match = _CHANNEL_ID_RE.search(body)
    if not match:
        return None

    channel_id = match.group(0)
    _HANDLE_CACHE[url] = channel_id
    return channel_id


def scrape_metrics() -> str:
    db_candidates = candidate_db_paths()
    db_readable = [path for path in db_candidates if os.path.exists(path)]
    youtube_references = discovered_youtube_references()
    youtube_references_by_source = discovered_youtube_references_by_source()
    youtube_channel_ids_by_source = discovered_youtube_channel_ids_by_source()
    youtube_channel_ids = sorted(
        {
            normalized
            for reference in youtube_references
            for normalized in [normalize_channel_id(reference)]
            if normalized
        }
    )
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
        "# HELP raw_feed_youtube_discovered_channels Number of unique YouTube channel IDs discovered from bot SQLite databases.",
        "# TYPE raw_feed_youtube_discovered_channels gauge",
        "# HELP raw_feed_youtube_discovered_channels_by_source Number of unique YouTube channel IDs discovered per source database.",
        "# TYPE raw_feed_youtube_discovered_channels_by_source gauge",
        "# HELP raw_feed_youtube_db_candidate_files Number of SQLite DB files considered for YouTube discovery.",
        "# TYPE raw_feed_youtube_db_candidate_files gauge",
        "# HELP raw_feed_youtube_db_readable_files Number of candidate SQLite DB files that exist and are readable by exporter.",
        "# TYPE raw_feed_youtube_db_readable_files gauge",
        "# HELP raw_feed_youtube_discovered_references Number of raw YouTube channel references found before normalization.",
        "# TYPE raw_feed_youtube_discovered_references gauge",
        "# HELP raw_feed_youtube_discovered_references_by_source Number of raw YouTube channel references found per source database before normalization.",
        "# TYPE raw_feed_youtube_discovered_references_by_source gauge",
    ]

    scrape_time = now_utc().timestamp()
    lines.append(f"raw_feed_youtube_db_candidate_files {len(db_candidates)}")
    lines.append(f"raw_feed_youtube_db_readable_files {len(db_readable)}")
    lines.append(f"raw_feed_youtube_discovered_references {len(youtube_references)}")
    lines.append(f"raw_feed_youtube_discovered_channels {len(youtube_channel_ids)}")

    for source_name, references in youtube_references_by_source.items():
        channel_ids = youtube_channel_ids_by_source.get(source_name, [])
        lines.append(
            f'raw_feed_youtube_discovered_references_by_source{{source="{source_name}"}} {len(references)}'
        )
        lines.append(
            f'raw_feed_youtube_discovered_channels_by_source{{source="{source_name}"}} {len(channel_ids)}'
        )

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
