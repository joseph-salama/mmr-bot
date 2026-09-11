#!/usr/bin/env python3
"""
Look up a Rocket League player's ranked MMR by Epic display name.

Rocket League has no public MMR API. This script uses Tracker Network's
unofficial profile endpoint (the same data shown on tracker.gg), accessed
with browser TLS impersonation so Cloudflare does not block the request.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote, urlencode

from curl_cffi import requests as crequests

log = logging.getLogger("rl_mmr")

API_URL = (
    "https://api.tracker.gg/api/v2/rocket-league/standard/profile/{platform}/{name}"
)
SEASON_PLAYLIST_URL = (
    "https://api.tracker.gg/api/v2/rocket-league/standard/profile/"
    "{platform}/{name}/segments/playlist"
)

# Official Tracker Network public API (requires TRN_API_KEY). Works much better from Railway.
PUBLIC_API_URL = (
    "https://public-api.tracker.gg/v2/rocket-league/standard/profile/{platform}/{name}"
)
PUBLIC_SEASON_PLAYLIST_URL = (
    "https://public-api.tracker.gg/v2/rocket-league/standard/profile/"
    "{platform}/{name}/segments/playlist"
)

# Current-season competitive playlist IDs used by Tracker Network / Psyonix.
PLAYLIST_DOUBLES = 11  # Ranked Doubles 2v2
PLAYLIST_STANDARD = 13  # Ranked Standard 3v3

# Modes counted toward "peak overall" — ranked 1s / 2s / 3s only.
PEAK_PLAYLIST_IDS = {
    10,  # Ranked Duel 1v1
    11,  # Ranked Doubles 2v2
    13,  # Ranked Standard 3v3
}

DIVISION_NAMES = {
    0: "Division I",
    1: "Division II",
    2: "Division III",
    3: "Division IV",
}

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://tracker.gg",
    "Referer": "https://tracker.gg/rocket-league",
}

# curl_cffi impersonation profiles to try (first available wins per request path).
_IMPERSONATE_CANDIDATES = (
    "chrome131",
    "chrome124",
    "chrome120",
    "chrome110",
    "chrome",
)


@dataclass
class PlaylistMMR:
    playlist: str
    playlist_id: int
    mmr: int | None
    peak_mmr: int | None
    tier: str | None
    division: str | None
    season: int | None


@dataclass
class PeakOverall:
    mmr: int
    playlist: str
    playlist_id: int
    season: int | None
    tier: str | None
    division: str | None


@dataclass
class PlayerMMR:
    epic_name: str
    platform: str
    current_season: int | None
    doubles_2v2: PlaylistMMR | None
    standard_3v3: PlaylistMMR | None
    peak_overall: PeakOverall | None


class PlayerNotFoundError(Exception):
    pass


class TrackerError(Exception):
    pass


class TrackerBlockedError(TrackerError):
    """Cloudflare / Tracker Network blocked the request."""


_session: crequests.Session | None = None
_session_lock = threading.Lock()
_impersonate: str | None = None


def _clean_env(name: str) -> str:
    value = os.getenv(name, "") or ""
    return value.strip().strip('"').strip("'")


def config_status() -> str:
    """Short non-secret summary of fetch-related config (for logs/errors)."""
    trn = _clean_env("TRN_API_KEY")
    scraper = _clean_env("SCRAPER_API_KEY")
    zenrows = _clean_env("ZENROWS_API_KEY")
    proxy = _clean_env("TRACKER_PROXY") or _clean_env("HTTPS_PROXY")
    return (
        f"TRN_API_KEY={'yes/' + str(len(trn)) if trn else 'no'}, "
        f"SCRAPER_API_KEY={'yes' if scraper else 'no'}, "
        f"ZENROWS_API_KEY={'yes' if zenrows else 'no'}, "
        f"TRACKER_PROXY={'yes' if proxy else 'no'}"
    )


def _proxy_dict() -> dict[str, str] | None:
    proxy = _clean_env("TRACKER_PROXY") or _clean_env("HTTPS_PROXY") or _clean_env("HTTP_PROXY")
    if not proxy:
        return None

    # Ignore leftover example values from .env.example / docs.
    lowered = proxy.lower()
    if (
        "user:pass@host" in lowered
        or "@host:port" in lowered
        or "example.com" in lowered
        or "your-proxy" in lowered
    ):
        log.warning("Ignoring placeholder proxy value in env: %s", proxy)
        return None

    # Basic sanity: must look like a URL with a numeric port if a port is present.
    try:
        from urllib.parse import urlparse

        parsed = urlparse(proxy)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h", "socks4"}:
            log.warning("Ignoring proxy with unsupported scheme: %s", proxy)
            return None
        if parsed.port is None and "://" in proxy:
            # host:port missing or non-numeric → curl error (5)
            if parsed.netloc and ":" in parsed.netloc.split("@")[-1]:
                hostport = parsed.netloc.split("@")[-1]
                port = hostport.rsplit(":", 1)[-1]
                if not port.isdigit():
                    log.warning("Ignoring proxy with invalid port: %s", proxy)
                    return None
    except Exception:
        log.warning("Ignoring unparsable proxy value: %s", proxy)
        return None

    return {"http": proxy, "https": proxy}


def _trn_headers() -> dict[str, str]:
    headers = dict(HEADERS)
    api_key = _clean_env("TRN_API_KEY")
    if api_key:
        headers["TRN-Api-Key"] = api_key
    return headers


def _has_trn_api_key() -> bool:
    return bool(_clean_env("TRN_API_KEY"))


def _wrap_fetch_url(url: str) -> str:
    """
    Optionally route Tracker requests through a scraping proxy.

    Railway/datacenter IPs are often Cloudflare-blocked. A scraper proxy
    fetches from residential IPs and returns the upstream body.
    """
    scraper = _clean_env("SCRAPER_API_KEY")
    if scraper:
        return (
            "http://api.scraperapi.com/?"
            + urlencode({"api_key": scraper, "url": url})
        )

    zenrows = _clean_env("ZENROWS_API_KEY")
    if zenrows:
        return (
            "https://api.zenrows.com/v1/?"
            + urlencode({"apikey": zenrows, "url": url})
        )

    return url


def _profile_urls(epic_name: str, platform: str) -> list[str]:
    encoded = quote(epic_name, safe="")
    # Official public-api does not reliably support Rocket League.
    # Prefer the site API (optionally via scraper proxy).
    return [API_URL.format(platform=platform, name=encoded)]


def _season_urls(epic_name: str, platform: str, season: int) -> list[str]:
    encoded = quote(epic_name, safe="")
    suffix = f"?season={int(season)}"
    return [SEASON_PLAYLIST_URL.format(platform=platform, name=encoded) + suffix]



def _pick_impersonate() -> str:
    global _impersonate
    if _impersonate:
        return _impersonate
    configured = os.getenv("TRACKER_IMPERSONATE")
    if configured:
        _impersonate = configured
        return _impersonate
    _impersonate = _IMPERSONATE_CANDIDATES[0]
    return _impersonate


def _reset_session() -> None:
    global _session
    with _session_lock:
        _session = None


def _build_session() -> crequests.Session:
    session = crequests.Session(impersonate=_pick_impersonate())
    proxies = _proxy_dict()
    if proxies:
        session.proxies.update(proxies)

    # Warm Cloudflare cookies the same way a browser would.
    try:
        session.get(
            "https://tracker.gg/rocket-league",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
        )
    except Exception as exc:
        log.warning("Tracker warmup request failed: %s", exc)
    return session


def _stat_value(stats: dict[str, Any], key: str) -> Any:
    entry = stats.get(key) or {}
    return entry.get("value")


def _stat_meta(stats: dict[str, Any], key: str, meta_key: str) -> Any:
    entry = stats.get(key) or {}
    return (entry.get("metadata") or {}).get(meta_key)


def _raise_for_status(response: Any, epic_name: str, platform: str) -> None:
    status = response.status_code
    body = response.text or ""

    if status == 404:
        raise PlayerNotFoundError(
            f"No Rocket League profile found for {platform} player '{epic_name}'."
        )

    if status == 401:
        raise TrackerError(
            "Tracker Network rejected the API key. Check that Railway `TRN_API_KEY` is valid."
        )

    if status == 403 or "you've been blocked" in body.lower() or "access denied" in body.lower():
        raise TrackerBlockedError(
            "Tracker Network blocked the request (Cloudflare on Railway). "
            "Add a free SCRAPER_API_KEY from https://www.scraperapi.com/ (or TRACKER_PROXY)."
        )

    if status == 429:
        raise TrackerError("Tracker Network rate-limited the bot. Try again in a minute.")

    if status != 200:
        raise TrackerError(f"Tracker Network returned HTTP {status}.")

    try:
        payload = response.json()
    except Exception as exc:
        raise TrackerError("Tracker Network returned an invalid response.") from exc

    if "errors" in payload:
        messages = "; ".join(
            err.get("message", str(err)) for err in payload.get("errors", [])
        )
        lowered = messages.lower()
        if "not found" in lowered or "no stats" in lowered:
            raise PlayerNotFoundError(
                f"No Rocket League profile found for {platform} player '{epic_name}'."
            )
        raise TrackerError(messages or "Unknown Tracker Network error")


def _request_json(url: str, *, epic_name: str, platform: str, retries: int = 3) -> Any:
    global _session, _impersonate
    last_error: Exception | None = None
    fetch_url = _wrap_fetch_url(url)
    using_scraper = fetch_url != url

    for attempt in range(retries):
        try:
            # Serialize session use (curl_cffi sessions are not thread-safe).
            with _session_lock:
                if _session is None:
                    _session = _build_session()
                # Scraper proxies need the target Accept headers less strictly;
                # still send TRN key for the upstream when not wrapped.
                headers = _trn_headers()
                if using_scraper:
                    headers = {"Accept": "application/json"}
                response = _session.get(fetch_url, headers=headers, timeout=45)
        except Exception as exc:
            short = str(exc)
            if len(short) > 120:
                short = short[:117] + "..."
            last_error = TrackerError(f"Request failed: {short}")
            time.sleep(0.8 * (attempt + 1))
            continue

        try:
            _raise_for_status(response, epic_name, platform)
            return response.json()
        except TrackerBlockedError:
            if using_scraper:
                raise
            _reset_session()
            idx = 0
            current = _pick_impersonate()
            if current in _IMPERSONATE_CANDIDATES:
                idx = _IMPERSONATE_CANDIDATES.index(current)
            _impersonate = _IMPERSONATE_CANDIDATES[(idx + 1) % len(_IMPERSONATE_CANDIDATES)]
            last_error = TrackerBlockedError(
                "Tracker Network blocked the request (Cloudflare on Railway). "
                "Add a free SCRAPER_API_KEY from https://www.scraperapi.com/ (or TRACKER_PROXY)."
            )
            time.sleep(1.2 * (attempt + 1))
            continue
        except TrackerError as exc:
            msg = str(exc).lower()
            if "rate-limited" in msg or "http 429" in msg:
                last_error = exc
                time.sleep(2.0 * (attempt + 1))
                continue
            raise
        except PlayerNotFoundError:
            raise

    assert last_error is not None
    raise last_error


def fetch_profile(epic_name: str, platform: str = "epic") -> dict[str, Any]:
    last_error: Exception | None = None
    for url in _profile_urls(epic_name, platform):
        try:
            payload = _request_json(url, epic_name=epic_name, platform=platform)
            return payload["data"]
        except PlayerNotFoundError:
            raise
        except TrackerError as exc:
            # Bad API key should not silently fall through to the Cloudflare-blocked endpoint.
            if "API key" in str(exc):
                raise
            last_error = exc
            log.warning("Profile fetch failed via %s: %s", url.split("/")[2], exc)
            continue
    assert last_error is not None
    raise last_error


def fetch_season_playlists(
    epic_name: str,
    season: int,
    platform: str = "epic",
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for url in _season_urls(epic_name, platform, season):
        try:
            payload = _request_json(url, epic_name=epic_name, platform=platform, retries=2)
            data = payload.get("data")
            return data if isinstance(data, list) else []
        except PlayerNotFoundError:
            raise
        except TrackerError as exc:
            last_error = exc
            continue
    assert last_error is not None
    raise last_error


def _parse_current_playlist(segment: dict[str, Any]) -> PlaylistMMR | None:
    attrs = segment.get("attributes") or {}
    stats = segment.get("stats") or {}
    if "rating" not in stats:
        return None

    tier = _stat_meta(stats, "tier", "name")
    division = _stat_meta(stats, "division", "name")
    mmr = _stat_value(stats, "rating")
    peak = _stat_value(stats, "peakRating")

    return PlaylistMMR(
        playlist=(segment.get("metadata") or {}).get("name") or "Unknown",
        playlist_id=int(attrs["playlistId"]),
        mmr=int(mmr) if mmr is not None else None,
        peak_mmr=int(peak) if peak is not None else None,
        tier=tier,
        division=division,
        season=attrs.get("season"),
    )


def _peak_from_segment(segment: dict[str, Any]) -> PeakOverall | None:
    if segment.get("type") != "playlist":
        return None

    attrs = segment.get("attributes") or {}
    playlist_id = attrs.get("playlistId")
    if playlist_id not in PEAK_PLAYLIST_IDS:
        return None

    stats = segment.get("stats") or {}
    peak = _stat_value(stats, "peakRating")
    if peak is None:
        # Older seasons sometimes only expose the end-of-season rating.
        peak = _stat_value(stats, "rating")
    if peak is None:
        return None

    meta = (stats.get("peakRating") or {}).get("metadata") or {}
    name = (segment.get("metadata") or {}).get("name") or meta.get("name") or "Unknown"
    tier = meta.get("tierName") or meta.get("name") or _stat_meta(stats, "tier", "name")
    division = meta.get("division") or _stat_meta(stats, "division", "name")
    if division is None:
        div_idx = _stat_value(stats, "peakDivision")
        if isinstance(div_idx, int):
            division = DIVISION_NAMES.get(div_idx)

    season = attrs.get("season")
    if season is None and isinstance(meta.get("season"), str):
        text = meta["season"]
        if "(" in text and text.endswith(")"):
            try:
                season = int(text.rsplit("(", 1)[1].rstrip(")"))
            except ValueError:
                season = None

    return PeakOverall(
        mmr=int(peak),
        playlist=name,
        playlist_id=int(playlist_id),
        season=season,
        tier=tier,
        division=division,
    )


def _iter_peak_candidates(segments: list[dict[str, Any]]) -> list[PeakOverall]:
    peaks: list[PeakOverall] = []
    for segment in segments:
        parsed = _peak_from_segment(segment)
        if parsed is not None:
            peaks.append(parsed)
    return peaks


def _available_seasons(profile: dict[str, Any]) -> list[int]:
    seasons: set[int] = set()
    current = (profile.get("metadata") or {}).get("currentSeason")
    if isinstance(current, int):
        seasons.add(current)

    for segment in profile.get("availableSegments") or []:
        if segment.get("type") != "playlist":
            continue
        season = (segment.get("attributes") or {}).get("season")
        if isinstance(season, int):
            seasons.add(season)

    return sorted(seasons)


def _fetch_all_time_peak(
    epic_name: str,
    platform: str,
    profile: dict[str, Any],
) -> PeakOverall | None:
    """Scan every available season for the highest ranked 1s/2s/3s peak."""
    peaks = _iter_peak_candidates(profile.get("segments") or [])
    seasons = _available_seasons(profile)
    current_season = (profile.get("metadata") or {}).get("currentSeason")
    # Newest seasons first — more likely to matter, and fail soft if rate-limited later.
    seasons_to_fetch = sorted(
        (s for s in seasons if s != current_season),
        reverse=True,
    )
    lookback = os.getenv("PEAK_SEASON_LOOKBACK", "12").strip()
    if lookback.isdigit():
        seasons_to_fetch = seasons_to_fetch[: int(lookback)]
    delay = float(os.getenv("PEAK_SEASON_DELAY_SECONDS", "0.35"))

    for season in seasons_to_fetch:
        try:
            segments = fetch_season_playlists(epic_name, season, platform=platform)
        except (PlayerNotFoundError, TrackerError) as exc:
            log.warning("Season %s peak fetch failed for %s: %s", season, epic_name, exc)
            # Back off a bit harder after failures, then keep scanning.
            time.sleep(max(delay, 1.0))
            continue
        peaks.extend(_iter_peak_candidates(segments))
        if delay > 0:
            time.sleep(delay)

    return max(peaks, key=lambda p: p.mmr) if peaks else None


def parse_player_mmr(
    profile: dict[str, Any],
    epic_name: str,
    *,
    platform: str = "epic",
    include_all_time_peak: bool = True,
) -> PlayerMMR:
    segments = profile.get("segments") or []
    current: dict[int, PlaylistMMR] = {}

    for segment in segments:
        if segment.get("type") != "playlist":
            continue
        parsed = _parse_current_playlist(segment)
        if parsed is None:
            continue
        # Prefer the segment that has live rating data for this playlist.
        current[parsed.playlist_id] = parsed

    if include_all_time_peak:
        peak_overall = _fetch_all_time_peak(epic_name, platform, profile)
    else:
        peaks = _iter_peak_candidates(segments)
        peak_overall = max(peaks, key=lambda p: p.mmr) if peaks else None

    return PlayerMMR(
        epic_name=profile.get("platformInfo", {}).get("platformUserHandle") or epic_name,
        platform=profile.get("platformInfo", {}).get("platformSlug") or platform,
        current_season=(profile.get("metadata") or {}).get("currentSeason"),
        doubles_2v2=current.get(PLAYLIST_DOUBLES),
        standard_3v3=current.get(PLAYLIST_STANDARD),
        peak_overall=peak_overall,
    )


def get_player_mmr(
    epic_name: str,
    platform: str = "epic",
    *,
    include_all_time_peak: bool | None = None,
) -> PlayerMMR:
    if include_all_time_peak is None:
        # Default off for fast/reliable cloud lookups; Sunday/updatemmr can enable.
        flag = _clean_env("PEAK_ALL_TIME") or "0"
        include_all_time_peak = flag.lower() not in {"0", "false", "no"}
    profile = fetch_profile(epic_name, platform=platform)
    return parse_player_mmr(
        profile,
        epic_name,
        platform=platform,
        include_all_time_peak=include_all_time_peak,
    )


def _fmt_rank(playlist: PlaylistMMR | None) -> str:
    if playlist is None or playlist.mmr is None:
        return "N/A"
    rank = "Unranked"
    if playlist.tier:
        rank = playlist.tier
        if playlist.division:
            rank = f"{playlist.tier} {playlist.division}"
    return f"{playlist.mmr} MMR ({rank})"


def print_report(player: PlayerMMR) -> None:
    print(f"Player: {player.epic_name} ({player.platform})")
    if player.current_season is not None:
        print(f"Season: {player.current_season}")
    print(f"Ranked 2s: {_fmt_rank(player.doubles_2v2)}")
    print(f"Ranked 3s: {_fmt_rank(player.standard_3v3)}")

    if player.peak_overall is None:
        print("Peak overall: N/A")
        return

    peak = player.peak_overall
    extras = [peak.playlist]
    if peak.tier:
        rank = peak.tier
        if peak.division:
            rank = f"{peak.tier} {peak.division}"
        extras.append(rank)
    if peak.season is not None:
        extras.append(f"season {peak.season}")
    print(f"Peak overall: {peak.mmr} MMR ({', '.join(extras)})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Rocket League ranked 2s/3s MMR and peak overall MMR by Epic name."
    )
    parser.add_argument(
        "epic_name",
        nargs="?",
        default="Kinorah",
        help="Epic Games display name (default: Kinorah)",
    )
    parser.add_argument(
        "--platform",
        default="epic",
        choices=("epic", "steam", "xbl", "psn", "switch"),
        help="Tracker Network platform slug (default: epic)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a text report",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        player = get_player_mmr(args.epic_name, platform=args.platform)
    except PlayerNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except TrackerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(player), indent=2))
    else:
        print_report(player)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
