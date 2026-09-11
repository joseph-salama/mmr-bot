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
import sys
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

from curl_cffi import requests as crequests

API_URL = (
    "https://api.tracker.gg/api/v2/rocket-league/standard/profile/{platform}/{name}"
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
    "Accept": "application/json",
    "Origin": "https://tracker.gg",
    "Referer": "https://tracker.gg/",
}


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


def _stat_value(stats: dict[str, Any], key: str) -> Any:
    entry = stats.get(key) or {}
    return entry.get("value")


def _stat_meta(stats: dict[str, Any], key: str, meta_key: str) -> Any:
    entry = stats.get(key) or {}
    return (entry.get("metadata") or {}).get(meta_key)


def _peak_meta(stats: dict[str, Any], meta_key: str) -> Any:
    return _stat_meta(stats, "peakRating", meta_key)


def fetch_profile(epic_name: str, platform: str = "epic") -> dict[str, Any]:
    encoded = quote(epic_name, safe="")
    url = API_URL.format(platform=platform, name=encoded)
    try:
        response = crequests.get(
            url,
            impersonate="chrome",
            headers=HEADERS,
            timeout=30,
        )
    except Exception as exc:  # network / TLS failures
        raise TrackerError(f"Request failed: {exc}") from exc

    if response.status_code == 404:
        raise PlayerNotFoundError(
            f"No Rocket League profile found for {platform} player '{epic_name}'."
        )
    if response.status_code != 200:
        raise TrackerError(
            f"Tracker Network returned HTTP {response.status_code}: {response.text[:300]}"
        )

    payload = response.json()
    if "errors" in payload:
        messages = "; ".join(
            err.get("message", str(err)) for err in payload.get("errors", [])
        )
        raise TrackerError(messages or "Unknown Tracker Network error")

    return payload["data"]


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


def _iter_peak_candidates(segments: list[dict[str, Any]]) -> list[PeakOverall]:
    """Collect peak MMR entries from current + historical playlist segments."""
    peaks: list[PeakOverall] = []

    for segment in segments:
        if segment.get("type") != "playlist":
            continue

        attrs = segment.get("attributes") or {}
        playlist_id = attrs.get("playlistId")
        if playlist_id not in PEAK_PLAYLIST_IDS:
            continue

        stats = segment.get("stats") or {}
        peak = _stat_value(stats, "peakRating")
        if peak is None:
            continue

        meta = (stats.get("peakRating") or {}).get("metadata") or {}
        name = (segment.get("metadata") or {}).get("name") or meta.get("name") or "Unknown"
        tier = meta.get("tierName") or meta.get("name")
        division = meta.get("division")
        if division is None:
            div_idx = _stat_value(stats, "peakDivision")
            if isinstance(div_idx, int):
                division = DIVISION_NAMES.get(div_idx)

        season = attrs.get("season")
        if season is None and isinstance(meta.get("season"), str):
            # Historical peaks look like: "Season 23 (37)"
            text = meta["season"]
            if "(" in text and text.endswith(")"):
                try:
                    season = int(text.rsplit("(", 1)[1].rstrip(")"))
                except ValueError:
                    season = None

        peaks.append(
            PeakOverall(
                mmr=int(peak),
                playlist=name,
                playlist_id=int(playlist_id),
                season=season,
                tier=tier,
                division=division,
            )
        )

    return peaks


def parse_player_mmr(profile: dict[str, Any], epic_name: str) -> PlayerMMR:
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

    peaks = _iter_peak_candidates(segments)
    peak_overall = max(peaks, key=lambda p: p.mmr) if peaks else None

    return PlayerMMR(
        epic_name=profile.get("platformInfo", {}).get("platformUserHandle") or epic_name,
        platform=profile.get("platformInfo", {}).get("platformSlug") or "epic",
        current_season=(profile.get("metadata") or {}).get("currentSeason"),
        doubles_2v2=current.get(PLAYLIST_DOUBLES),
        standard_3v3=current.get(PLAYLIST_STANDARD),
        peak_overall=peak_overall,
    )


def get_player_mmr(epic_name: str, platform: str = "epic") -> PlayerMMR:
    profile = fetch_profile(epic_name, platform=platform)
    return parse_player_mmr(profile, epic_name)


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
