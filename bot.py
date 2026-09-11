"""
Discord bot: look up Rocket League MMR by Epic name, persist per Discord user.

Commands (allowed channel only):
  /check @user epic_username
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
        if interaction.channel_id != ALLOWED_CHANNEL_ID:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This bot only works in the designated MMR channel.",
                    ephemeral=True,
                )
            return False
        return True

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
    description="Look up a player's Rocket League MMR and link it to their Discord account.",
)
@app_commands.describe(
    member="Discord user to link",
    username="Epic Games display name",
)
async def check_command(
    interaction: discord.Interaction,
    member: discord.Member,
    username: str,
) -> None:
    await interaction.response.defer(thinking=True)
    username = username.strip()
    if not username:
        await interaction.followup.send("Please provide an Epic username.", ephemeral=True)
        return

    try:
        entry = await fetch_and_store(member.id, username)
    except PlayerNotFoundError as exc:
        await interaction.followup.send(f"Could not find that player: {exc}")
        return
    except TrackerError as exc:
        await interaction.followup.send(f"Tracker Network error: {exc}")
        return
    except Exception:
        log.exception("check failed for %s / %s", member.id, username)
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
            f"{member.mention} was not linked in storage.",
            ephemeral=True,
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
