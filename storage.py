"""Persistent JSON storage linking Discord users to Rocket League MMR."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rl_mmr import PlayerMMR


def default_data_path() -> Path:
    """Prefer Railway volume mount, otherwise local ./data."""
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR")
    base = Path(mount) if mount else Path("data")
    return base / "mmr_players.json"


class PlayerStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"players": {}})

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"players": {}}
        if "players" not in data or not isinstance(data["players"], dict):
            data = {"players": {}}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(data, indent=2, ensure_ascii=False)
        # Atomic replace so a crash mid-write does not corrupt the file.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=".mmr_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(raw)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def get(self, discord_id: int | str) -> dict[str, Any] | None:
        return self._read()["players"].get(str(discord_id))

    def all_players(self) -> dict[str, dict[str, Any]]:
        return self._read()["players"]

    def upsert(self, discord_id: int | str, epic_name: str, player: PlayerMMR) -> dict[str, Any]:
        data = self._read()
        entry = {
            "discord_id": str(discord_id),
            "epic_name": player.epic_name or epic_name,
            "platform": player.platform,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "mmr": asdict(player),
        }
        data["players"][str(discord_id)] = entry
        self._write(data)
        return entry

    def delete(self, discord_id: int | str) -> bool:
        data = self._read()
        removed = data["players"].pop(str(discord_id), None) is not None
        if removed:
            self._write(data)
        return removed
