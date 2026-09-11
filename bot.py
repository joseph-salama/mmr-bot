"""
Discord bot: look up Rocket League MMR by Epic name, persist per Discord user.

Commands (allowed channel only; silent everywhere else):
  /check                         → list all stored players
  /check @user                   → refresh & show that player's MMR
  /check @user epic_username     → fetch, link, save, and show
  /updatemmr
  /delete @user

Every Sunday (configurable timezone/hour), refreshes all stored MMR values
and posts an update notice in the allowed channel.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from rl_mmr import PlayerNotFoundError, TrackerError, get_player_mmr
from storage import PlayerStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mmr-bot")

ALLOWED_CHANNEL_ID = int(os.environ.get("ALLOWED_CHANNEL_ID", "0") or "0")
GUILD_ID = os.getenv("GUILD_ID")
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
SUNDAY_UPDATE_HOUR = int(os.getenv("SUNDAY_UPDATE_HOUR", "12"))
UPDATE_DELAY_SECONDS = float(os.getenv("UPDATE_DELAY_SECONDS", "1.5"))

store = PlayerStore()


def _rank_line(label: str, playlist: dict | None) -> str:
    if not playlist or playlist.get("mmr") is None:
        return f"**{label}:** N/A"
    mmr = playlist["mmr"]
    tier = playlist.get("tier")
    division = playlist.get("division")
    if tier:
        rank = f"{tier} {division}".strip() if division else tier
        return f"**{label}:** {mmr} MMR ({rank})"
    return f"**{label}:** {mmr} MMR"


def _peak_line(peak: dict | None) -> str:
    if not peak or peak.get("mmr") is None:
        return "**Peak overall:** N/A"
    parts = [peak.get("playlist") or "Unknown"]
    if peak.get("tier"):
        rank = peak["tier"]
        if peak.get("division"):
            rank = f"{peak['tier']} {peak['division']}"
        parts.append(rank)
    if peak.get("season") is not None:
        parts.append(f"season {peak['season']}")
    return f"**Peak overall:** {peak['mmr']} MMR ({', '.join(parts)})"


def _short_mmr(playlist: dict | None) -> str:
    if not playlist or playlist.get("mmr") is None:
        return "N/A"
    return str(playlist["mmr"])


def format_mmr_embed(
    *,
    discord_user: discord.abc.User,
    epic_name: str,
    entry: dict,
    title: str | None = None,
) -> discord.Embed:
    mmr = entry.get("mmr") or {}
    season = mmr.get("current_season")
    embed = discord.Embed(
        title=title or "Rocket League MMR",
        color=discord.Color.blurple(),
        timestamp=datetime.now(ZoneInfo("UTC")),
    )
    embed.set_author(
        name=str(discord_user),
        icon_url=discord_user.display_avatar.url,
    )
    description = [
        f"**Discord:** {discord_user.mention}",
        f"**Epic:** {epic_name}",
    ]
    if season is not None:
        description.append(f"**Season:** {season}")
    description.append("")
    description.append(_rank_line("Ranked 2s", mmr.get("doubles_2v2")))
    description.append(_rank_line("Ranked 3s", mmr.get("standard_3v3")))
    description.append(_peak_line(mmr.get("peak_overall")))
    embed.description = "\n".join(description)
    embed.set_footer(text="Data from Tracker Network · stored locally")
    return embed


def format_player_list_embeds(players: dict[str, dict]) -> list[discord.Embed]:
    """Build one or more embeds listing every stored player."""
    lines: list[str] = []
    for discord_id, entry in sorted(
        players.items(),
        key=lambda item: (item[1].get("epic_name") or "").lower(),
    ):
        mmr = entry.get("mmr") or {}
        epic = entry.get("epic_name") or "Unknown"
        twos = _short_mmr(mmr.get("doubles_2v2"))
        threes = _short_mmr(mmr.get("standard_3v3"))
        peak = _short_mmr(mmr.get("peak_overall"))
        lines.append(
            f"<@{discord_id}> · **{epic}**\n"
            f"2s: `{twos}` · 3s: `{threes}` · Peak: `{peak}`"
        )

    embeds: list[discord.Embed] = []
    chunk: list[str] = []
    size = 0
    for line in lines:
        # +2 for the blank line separator between entries
        extra = len(line) + (2 if chunk else 0)
        if chunk and size + extra > 3800:
            embed = discord.Embed(
                title="Stored players" if not embeds else "Stored players (cont.)",
                description="\n\n".join(chunk),
                color=discord.Color.blurple(),
                timestamp=datetime.now(ZoneInfo("UTC")),
            )
            embeds.append(embed)
            chunk = [line]
            size = len(line)
        else:
            chunk.append(line)
            size += extra

    if chunk:
        embed = discord.Embed(
            title="Stored players" if not embeds else "Stored players (cont.)",
            description="\n\n".join(chunk),
            color=discord.Color.blurple(),
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        embeds.append(embed)

    if embeds:
        embeds[-1].set_footer(text=f"{len(players)} player(s) stored")
    return embeds


class MMRBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.synced = False
        self._last_sunday_key: str | None = None

    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced slash commands to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced slash commands globally")
        self.synced = True
        self.sunday_update_loop.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user and self.user.id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Wrong channel: do not reply at all (no public or ephemeral message).
        return interaction.channel_id == ALLOWED_CHANNEL_ID

    @tasks.loop(minutes=15)
    async def sunday_update_loop(self) -> None:
        try:
            tz = ZoneInfo(TIMEZONE)
        except Exception:
            log.exception("Invalid TIMEZONE=%s", TIMEZONE)
            return

        now = datetime.now(tz)
        if now.weekday() != 6:  # Sunday
            return
        if now.hour < SUNDAY_UPDATE_HOUR:
            return

        sunday_key = now.strftime("%Y-%m-%d")
        if self._last_sunday_key == sunday_key:
            return

        channel = self.get_channel(ALLOWED_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(ALLOWED_CHANNEL_ID)
            except discord.HTTPException:
                log.exception("Could not fetch allowed channel %s", ALLOWED_CHANNEL_ID)
                return

        if not isinstance(channel, discord.TextChannel):
            log.error("ALLOWED_CHANNEL_ID is not a text channel")
            return

        self._last_sunday_key = sunday_key
        log.info("Starting Sunday MMR refresh for %s", sunday_key)
        updated, failed = await refresh_all_players()

        lines = [
            f"Sunday MMR update complete for **{sunday_key}**.",
            f"Updated: **{updated}** · Failed: **{failed}**",
        ]
        await channel.send("\n".join(lines))

    @sunday_update_loop.before_loop
    async def before_sunday_loop(self) -> None:
        await self.wait_until_ready()


bot = MMRBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    # Wrong-channel CheckFailure: stay completely silent (no reply at all).
    if isinstance(error, app_commands.CheckFailure):
        return
    log.exception("App command error: %s", error)
    message = "Something went wrong running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message)
        else:
            await interaction.response.send_message(message)
    except discord.HTTPException:
        pass


async def fetch_and_store(discord_id: int, epic_name: str) -> dict:
    player = await asyncio.to_thread(get_player_mmr, epic_name)
    return store.upsert(discord_id, epic_name, player)


async def refresh_all_players() -> tuple[int, int]:
    players = store.all_players()
    updated = 0
    failed = 0
    for discord_id, entry in list(players.items()):
        epic_name = entry.get("epic_name")
        if not epic_name:
            failed += 1
            continue
        try:
            await fetch_and_store(int(discord_id), epic_name)
            updated += 1
        except (PlayerNotFoundError, TrackerError) as exc:
            failed += 1
            log.warning("Failed updating %s (%s): %s", discord_id, epic_name, exc)
        except Exception:
            failed += 1
            log.exception("Unexpected error updating %s (%s)", discord_id, epic_name)
        await asyncio.sleep(UPDATE_DELAY_SECONDS)
    return updated, failed


@bot.tree.command(
    name="check",
    description="List all players, refresh one linked player, or link a new Epic username.",
)
@app_commands.describe(
    member="Discord user (optional). Alone = refresh their MMR. With username = link them.",
    username="Epic Games display name (optional). Required when linking a new player.",
)
async def check_command(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
    username: str | None = None,
) -> None:
    await interaction.response.defer(thinking=True)
    epic = (username or "").strip() or None

    # /check  → list everyone
    if member is None and epic is None:
        players = store.all_players()
        if not players:
            await interaction.followup.send("No players stored yet.")
            return
        embeds = format_player_list_embeds(players)
        # Discord allows up to 10 embeds per message.
        for i in range(0, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i : i + 10])
        return

    # username without @user
    if member is None and epic is not None:
        await interaction.followup.send(
            "Mention a Discord user when linking an Epic username.\n"
            "Usage: `/check @user epic_username`"
        )
        return

    assert member is not None

    # /check @user  → refresh stored epic name
    if epic is None:
        existing = store.get(member.id)
        if existing is None:
            await interaction.followup.send(
                f"{member.mention} is not linked yet. "
                f"Use `/check @user epic_username` first."
            )
            return
        epic = existing.get("epic_name")
        if not epic:
            await interaction.followup.send(
                f"{member.mention} has no Epic username stored. "
                f"Use `/check @user epic_username` to link one."
            )
            return

    # /check @user epic_username  (or refresh path above)
    try:
        entry = await fetch_and_store(member.id, epic)
    except PlayerNotFoundError as exc:
        await interaction.followup.send(f"Could not find that player: {exc}")
        return
    except TrackerError as exc:
        await interaction.followup.send(f"Tracker Network error: {exc}")
        return
    except Exception:
        log.exception("check failed for %s / %s", member.id, epic)
        await interaction.followup.send("Something went wrong while fetching MMR.")
        return

    embed = format_mmr_embed(
        discord_user=member,
        epic_name=entry["epic_name"],
        entry=entry,
        title="MMR checked & saved",
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="updatemmr",
    description="Re-fetch MMR for every linked player and overwrite stored values.",
)
async def updatemmr_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    players = store.all_players()
    if not players:
        await interaction.followup.send("No players are stored yet. Use `/check` first.")
        return

    updated, failed = await refresh_all_players()
    await interaction.followup.send(
        f"MMR refresh finished.\nUpdated: **{updated}** · Failed: **{failed}**"
    )


@bot.tree.command(
    name="delete",
    description="Remove a Discord user and their stored MMR from memory.",
)
@app_commands.describe(member="Discord user to unlink and remove")
async def delete_command(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    removed = store.delete(member.id)
    if removed:
        await interaction.response.send_message(
            f"Removed {member.mention} and their MMR from storage."
        )
    else:
        await interaction.response.send_message(
            f"{member.mention} was not linked in storage."
        )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    channel = os.getenv("ALLOWED_CHANNEL_ID")
    missing = [name for name, value in (("DISCORD_TOKEN", token), ("ALLOWED_CHANNEL_ID", channel)) if not value]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    global ALLOWED_CHANNEL_ID
    ALLOWED_CHANNEL_ID = int(channel)  # type: ignore[arg-type]
    bot.run(token)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
