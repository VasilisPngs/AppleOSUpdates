#!/usr/bin/env python3
# Copyright (C) 2026 VasilisPngs
# Licensed under the GNU Affero General Public License v3 or later.
# See LICENSE, or <https://www.gnu.org/licenses/>.
"""Tell @appleosupdates the moment Apple ships an OS release.

Reads three of Apple's own endpoints; first one to report a release wins.
Each release is announced once, and only if it shipped on or after the day
the channel opened. Run once a minute by the workflow.

SKIP_SECURITY_PAGE=true drops the heavy source; DRY_RUN=true prints.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent

# What has already been announced. Lives in the repo; the workflow rewrites
# the branch as one parentless commit, so history never accumulates.
STATE = ROOT / "state" / "seen.json"

GDMF_URL = "https://gdmf.apple.com/v2/pmv"
SECURITY_URL = "https://support.apple.com/en-us/100100"
DEV_RSS_URL = "https://developer.apple.com/news/releases/rss/releases.rss"

# gdmf runs on Apple's private PKI, which no public trust store carries.
APPLE_ROOT_CA = ROOT / "certs" / "AppleRootCA.pem"

USER_AGENT = "AppleOSUpdates-bot/1.0"

# The day the channel opened. Nothing published earlier is ever announced.
EPOCH = date(2026, 8, 15)

# A moving floor, so a lost state file can never replay more than a month.
RECENT = timedelta(days=30)

# Apple's busiest day on record is 14 releases, its busiest month 22. Far
# more than that means something broke (usually dates that stopped parsing,
# which drops the floor). Refuse rather than post the back catalogue.
FLOOD = 30

LABELS = {
    "ios": "iOS",
    "ipados": "iPadOS",
    "macos": "macOS",
    "tvos": "tvOS",
    "watchos": "watchOS",
    "visionos": "visionOS",
}
# Unicode has no tablet, so iPadOS shares the phone glyph; its name on the
# title line is what tells the two apart.
EMOJI = {
    "ios": "📱",
    "ipados": "📱",
    "macos": "💻",
    "tvos": "📺",
    "watchos": "⌚",
    "visionos": "🥽",
}

# gdmf files watchOS, tvOS and audioOS under "iOS", so the real platform
# comes from the devices. One asset can cover two (iPhone + iPad).
DEVICE_PLATFORMS = [
    ("Watch", "watchos"),
    ("AppleTV", "tvos"),
    ("AudioAccessory", "tvos"),
    ("RealityDevice", "visionos"),
    ("iPhone", "ios"),
    ("iPod", "ios"),
    ("iPad", "ipados"),
]

# Longest first, so "iPadOS" is never truncated to "iOS". Anything matching
# nothing here — Safari, Xcode, TestFlight — is not an OS and is discarded.
NAME_PREFIXES = [
    ("visionOS", "visionos"),
    ("watchOS", "watchos"),
    ("iPadOS", "ipados"),
    ("macOS", "macos"),
    ("tvOS", "tvos"),
    ("iOS", "ios"),
]

VERSION_RE = re.compile(r"\b(\d+(?:\.\d+)*)\b")
BUILD_RE = re.compile(r"\(([^()]+)\)\s*$")
BUILD_PARTS = re.compile(r"^(\d+[A-Z])(\d+)([a-z]?)$")


def same_build(a: str, b: str) -> bool:
    """Apple ships every release under two builds at once: 23G71 and
    23G6071, the second being the first plus 6000. Not a reissue."""
    if a == b:
        return True
    first, second = BUILD_PARTS.match(a), BUILD_PARTS.match(b)
    if not (first and second):
        return False
    if first.group(1) != second.group(1) or first.group(3) != second.group(3):
        return False
    return abs(int(first.group(2)) - int(second.group(2))) == 6000


# --------------------------------------------------------------------------
# Release
# --------------------------------------------------------------------------


@dataclass
class Release:
    family: str
    version: str
    channel: str  # stable, rsr, beta5, rc, ...
    title: str = ""
    build: str = ""
    released: date | None = None
    source_rank: int = 5  # lower wins when two sources disagree on the title

    @property
    def key(self) -> str:
        """Identity across sources. The build is deliberately excluded —
        it comes in two flavours at once — but tracked separately, because
        a build that changes later is a reissue and must be announced."""
        return f"{self.family}|{self.version}|{self.channel}"

    def merge(self, other: "Release") -> None:
        """Fill blanks from another source's view of the same release."""
        if other.title and (not self.title or other.source_rank < self.source_rank):
            self.title = other.title
        # Apple lists an alternate build (23G6071) beside the main one
        # (23G71); the shorter string is the one users actually get.
        if other.build and (not self.build or len(other.build) < len(self.build)):
            self.build = other.build
        if other.released and (not self.released or other.released < self.released):
            self.released = other.released


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def fetch(url: str, cafile: Path | None = None) -> bytes:
    """Read a source, never a cached copy of one.

    Apple's CDN answers "hit-stale" and holds a copy for minutes. That made
    iOS 27 beta 6, out at 17:00, an announcement at 17:57. A parameter the
    cache has not seen forces it to go and ask.
    """
    stamp = f"{'&' if '?' in url else '?'}t={int(time.time())}"
    context = ssl.create_default_context(cafile=str(cafile)) if cafile else None
    last: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url + stamp,
                headers={
                    "User-Agent": USER_AGENT,
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(request, timeout=30, context=context) as reply:
                return reply.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = error
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"GET {url} failed: {last}")


# --------------------------------------------------------------------------
# Name parsing, shared by the security page and the developer feed
# --------------------------------------------------------------------------


def split_family(text: str) -> tuple[str, str] | None:
    """Strip a leading product name: 'iOS 26.6' -> ('ios', '26.6')."""
    for prefix, family in NAME_PREFIXES:
        if text.lower().startswith(prefix.lower()):
            return family, text[len(prefix) :].strip()
    return None


def parse_channel(tail: str) -> str:
    """'beta 5' -> 'beta5', 'Release Candidate 2' -> 'rc2', '' -> 'stable'."""
    cleaned = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", tail.lower())).strip()
    if not cleaned:
        return "stable"
    if "release candidate" in cleaned or re.search(r"\brc\b", cleaned):
        number = re.search(r"(?:release candidate|rc)\s*(\d+)", cleaned)
        return f"rc{number.group(1)}" if number else "rc"
    if "beta" in cleaned:
        number = re.search(r"beta\s*(\d+)", cleaned)
        return f"beta{number.group(1)}" if number else "beta"
    return "other"


def parse_rss_title(title: str) -> tuple[str, str, str, str] | None:
    """'iOS 27.0 beta 5 (24A5408d)' -> ('ios', '27.0', 'beta5', '24A5408d')."""
    text = title.strip()
    build = ""
    match = BUILD_RE.search(text)
    if match:
        build = match.group(1).strip()
        text = text[: match.start()].strip()

    split = split_family(text)
    if not split:
        return None
    family, rest = split

    version = VERSION_RE.search(rest)
    if not version:
        return None

    # Text before the version is a marketing codename ("Tahoe"); ignore it.
    channel = parse_channel(rest[version.end() :])
    if channel == "other":
        return None
    return family, version.group(1), channel, build


def parse_security_name(name: str) -> tuple[str, str, str] | None:
    """Parse one product out of a row on Apple's security releases page."""
    text = name.strip()
    channel = "stable"
    if text.lower().startswith("rapid security response"):
        channel = "rsr"
        text = text[len("rapid security response") :].strip()

    split = split_family(text)
    if not split:
        return None
    family, rest = split

    version = VERSION_RE.search(rest)
    if not version:
        return None

    # Rapid Security Responses are versioned "13.4.1 (a)".
    extra = re.match(r"\s*\(([a-z])\)", rest[version.end() :])
    full = f"{version.group(1)} ({extra.group(1)})" if extra else version.group(1)
    return family, full, channel


def parse_security_names(name: str) -> list[tuple[str, str, str]]:
    """One row can name two products: 'iOS 26.6 and iPadOS 26.6'.

    Apple announces those separately on the developer feed, and their
    versions drift apart over time — iPadOS 17.7.11 shipped while iOS was on
    18.7.10 — so each half becomes its own release.
    """
    parsed = []
    for part in re.split(r"\s+and\s+", name.strip()):
        one = parse_security_name(part)
        if one and one not in parsed:
            parsed.append(one)
    return parsed


def strip_tags(html: str) -> str:
    return unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))).strip()


def cell_name(cell: str) -> str:
    """Pull the release name out of the first column.

    Some rows append a note in their own div ("This update has no published
    CVE entries."). Letting it run into the name breaks the version regex:
    "11.7.11This update…" matches as 11.7, because a digit followed by a
    letter is not a word boundary.
    """
    anchor = re.search(r"<a\b[^>]*>(.*?)</a>", cell, re.S)
    if anchor:
        return strip_tags(anchor.group(1))
    paragraph = re.search(r"<p\b[^>]*>(.*?)</p>", cell, re.S)
    if paragraph:
        return strip_tags(paragraph.group(1))
    return strip_tags(cell)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def platforms_of(devices: list[str], fallback: str) -> list[str]:
    """Every platform an asset covers, not just the first."""
    found = [
        f for prefix, f in DEVICE_PLATFORMS if any(d.startswith(prefix) for d in devices)
    ]
    return list(dict.fromkeys(found)) or [fallback]


def from_gdmf() -> list[Release]:
    """The feed behind Software Update: when it flips, the update is
    already downloadable. Carries no betas."""
    data = json.loads(fetch(GDMF_URL, cafile=APPLE_ROOT_CA))
    releases: list[Release] = []

    groups = [
        ((data.get("PublicAssetSets") or {}), "stable", ""),
        ((data.get("PublicBackgroundSecurityImprovements") or {}), "rsr", "extra"),
    ]
    for entries_by_group, channel, needs_extra in groups:
        for group, entries in entries_by_group.items():
            for entry in entries or []:
                version = str(entry.get("ProductVersion") or "").strip()
                extra = str(entry.get("ProductVersionExtra") or "").strip()
                if not version or (needs_extra and not extra):
                    continue
                full = f"{version} {extra}" if needs_extra else version
                for family in platforms_of(
                    entry.get("SupportedDevices") or [], group.lower()
                ):
                    if family not in LABELS:
                        continue
                    releases.append(
                        Release(
                            family=family,
                            version=full,
                            channel=channel,
                            title=f"{LABELS[family]} {full}",
                            build=str(entry.get("Build") or ""),
                            released=parse_date(entry.get("PostingDate")),
                            source_rank=2,
                        )
                    )
    return releases


def from_security_page() -> list[Release]:
    """Apple's security releases index: the proper names and macOS codenames."""
    html = fetch(SECURITY_URL).decode("utf-8", errors="replace")
    releases: list[Release] = []

    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 3:
            continue

        name = cell_name(cells[0])
        parsed = parse_security_names(name)
        if not parsed:
            continue

        released = None
        when = strip_tags(cells[2])
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                released = datetime.strptime(when, fmt).date()
                break
            except ValueError:
                continue

        for family, version, channel in parsed:
            releases.append(
                Release(
                    family=family,
                    version=version,
                    channel=channel,
                    # Name each half on its own rather than repeating the
                    # combined row, so the iPadOS post reads as iPadOS.
                    title=f"{LABELS[family]} {version}" if len(parsed) > 1 else name,
                    released=released,
                    source_rank=1,
                )
            )
    return releases


def from_developer_feed() -> list[Release]:
    """Apple's developer releases feed: betas, RCs, and often the first
    place a public release appears at all."""
    root = ElementTree.fromstring(fetch(DEV_RSS_URL))
    releases: list[Release] = []

    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        parsed = parse_rss_title(title)
        if not parsed:
            continue
        family, version, channel, build = parsed

        released = None
        raw = item.findtext("pubDate")
        if raw:
            try:
                released = parsedate_to_datetime(raw).date()
            except (TypeError, ValueError):
                released = None

        releases.append(
            Release(
                family=family,
                version=version,
                channel=channel,
                # The build renders on the title line; drop Apple's "(23G82)".
                title=BUILD_RE.sub("", title).strip(),
                build=build,
                released=released,
                source_rank=3,
            )
        )
    return releases


def collect(skip_security_page: bool = False) -> tuple[list[Release], list[str]]:
    """Merge every source; return the releases and any that failed.

    gdmf and the feed are 52 KB and 27 KB, cheap to read every minute. The
    security index is 1.29 MB and never reports anything first, so most
    rounds skip it.
    """
    sources = (
        (from_gdmf, from_developer_feed)
        if skip_security_page
        else (from_gdmf, from_security_page, from_developer_feed)
    )

    merged: dict[str, Release] = {}
    failed: list[str] = []

    for source in sources:
        try:
            found = source()
        except Exception as error:  # a dead source must not block the news
            print(f"ERROR: {source.__name__} failed: {error}", file=sys.stderr)
            failed.append(source.__name__)
            continue
        for release in found:
            if release.key in merged:
                merged[release.key].merge(release)
            else:
                merged[release.key] = release

    return list(merged.values()), failed


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


def render(release: Release) -> str:
    """The whole post: what shipped, and when."""
    headline = release.title
    if release.build:
        headline += f" ({release.build})"

    lines = [f"{EMOJI[release.family]} <b>{escape(headline)}</b>"]
    if release.released:
        lines.append(release.released.strftime("%-d %B %Y"))
    return "\n".join(lines)


def send(token: str, chat_id: str, text: str) -> None:
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode()

    for attempt in range(5):
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:
                body = json.loads(reply.read())
        except urllib.error.HTTPError as error:
            # Telegram explains itself in the body even when refusing.
            try:
                body = json.loads(error.read() or b"{}")
            except ValueError:
                body = {}
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # A dropped connection is not a refusal.
            print(f"send failed ({error}); retrying", file=sys.stderr)
            time.sleep(2**attempt)
            continue
        except ValueError:
            # A reply that is not JSON at all — a proxy error page, say.
            print("Telegram replied with something that is not JSON; retrying",
                  file=sys.stderr)
            time.sleep(2**attempt)
            continue

        if body.get("ok"):
            time.sleep(3.5)  # a channel accepts about 20 messages a minute
            return

        # On 429 Telegram says exactly how long to wait. Obey it rather than
        # dropping the announcement.
        wait = (body.get("parameters") or {}).get("retry_after")
        if wait is None:
            raise RuntimeError(f"Telegram refused the message: {body.get('description')}")
        print(f"rate limited, waiting {wait}s", file=sys.stderr)
        time.sleep(float(wait) + 1)

    raise RuntimeError("Telegram kept rate limiting; giving up for this run.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    dry_run = flag("DRY_RUN")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    path = Path(os.environ.get("STATE_PATH", "").strip() or STATE)

    if not dry_run and not (token and chat_id):
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")

    try:
        stored = json.loads(path.read_text(encoding="utf-8")).get("entries") or {}
    except FileNotFoundError:
        stored = {}  # a genuine first run, and only ever that
    except (OSError, ValueError) as error:
        # Unreadable is not empty. Reading it as empty replays a month.
        raise SystemExit(f"Cannot read what has already been announced: {error}")
    # Each announced release is remembered with the build it went out as.
    seen: dict[str, str] = dict(stored)
    before = dict(seen)

    releases, failed = collect(skip_security_page=flag("SKIP_SECURITY_PAGE"))
    if not releases:
        print("No releases returned by any source.", file=sys.stderr)
        return 1

    # Learn the build of anything announced before we recorded one, so a
    # later genuine re-release is still recognised as a change.
    for release in releases:
        if release.key in seen and not seen[release.key] and release.build:
            seen[release.key] = release.build

    def unannounced(release: Release) -> bool:
        if release.key not in seen:
            return True
        # A reissue under a new build is a different thing to install, so
        # it posts again. An empty build is silence, not a change.
        was = seen[release.key]
        return bool(was) and bool(release.build) and not same_build(was, release.build)

    # Nothing from before the channel existed, and nothing older than a few
    # weeks, so a lost state file can never replay the archive.
    floor = max(EPOCH, datetime.now(timezone.utc).date() - RECENT)
    new = sorted(
        [
            r
            for r in releases
            if not (r.released and r.released < floor) and unannounced(r)
        ],
        key=lambda r: (r.released or date.min, r.title),
    )
    print(f"{len(releases)} releases known, {len(new)} to announce.")

    # Nothing is recorded, so the backlog still goes out once fixed.
    if len(new) > FLOOD:
        print(
            f"ERROR: refusing to announce {len(new)} releases at once. Apple's "
            f"busiest month on record is 22, so this is a fault, not news.",
            file=sys.stderr,
        )
        return 1

    posted = 0
    try:
        for release in new:
            if dry_run:
                print(f"--- would send ---\n{render(release)}\n")
            else:
                send(token, chat_id, render(release))
            seen[release.key] = release.build
            posted += 1
    finally:
        # Record only what went out, so a failure mid-run is retried rather
        # than lost. Releases the floor rejects are never recorded, which is
        # why the state holds only what the channel actually said.
        if not dry_run and seen != before:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write beside it and move into place: a run cut short mid-write
            # must leave the old file whole, not half of one.
            spare = path.with_name(path.name + ".new")
            spare.write_text(
                json.dumps(
                    {
                        "updated": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "entries": dict(sorted(seen.items())),
                    },
                    indent=1,
                )
                + "\n",
                encoding="utf-8",
            )
            spare.replace(path)

    print(f"Announced {posted}.")

    # A degraded run still posts, but must not look healthy: a source that
    # quietly died would cost a whole platform unnoticed.
    if failed:
        print(f"Run degraded: {', '.join(failed)} unavailable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
