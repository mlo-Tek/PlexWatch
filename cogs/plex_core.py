import discord
from discord.ext import commands, tasks
from plexapi.server import PlexServer
import time
import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv

RUNNING_IN_DOCKER = os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true"

if not RUNNING_IN_DOCKER:
    load_dotenv()

class PlexCore(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger("plexwatch_bot.plex")

        self.PLEX_URL = os.getenv("PLEX_URL")
        self.PLEX_TOKEN = os.getenv("PLEX_TOKEN")
        channel_id = os.getenv("CHANNEL_ID")
        if channel_id is None:
            self.logger.error("CHANNEL_ID not set in .env file")
            raise ValueError("CHANNEL_ID must be set in .env")
        self.CHANNEL_ID = int(channel_id)

        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        self.MESSAGE_ID_FILE = os.path.join(self.current_dir, "..", "data", "dashboard_message_id.json")
        self.USER_MAPPING_FILE = os.path.join(self.current_dir, "..", "data", "user_mapping.json")
        self.CONFIG_FILE = os.path.join(self.current_dir, "..", "data", "config.json")

        self.config = self._load_config()
        self.plex: Optional[PlexServer] = None
        self.plex_start_time: Optional[float] = None
        self.dashboard_message_id = self._load_message_id()
        self.last_scan = datetime.now()
        self.offline_since: Optional[datetime] = None
        self.stream_debug = False

        self.library_cache: Dict[str, Dict[str, Any]] = {}
        self.last_library_update: Optional[datetime] = None
        self.library_update_interval = self.config.get("cache", {}).get("library_update_interval", 900)

        self.user_mapping = self._load_user_mapping()
        self.update_status.start()
        self.update_dashboard.start()

    def _load_config(self) -> Dict[str, Any]:
        default_config = {
            "dashboard": {"name": "Plex Dashboard", "icon_url": "", "footer_icon_url": ""},
            "plex_sections": {"show_all": True, "sections": {}},
            "presence": {
                "sections": [],
                "offline_text": "🔴 Server Offline!",
                "stream_text": "{count} active Stream{s} 🟢",
            },
            "cache": {"library_update_interval": 900},
        }
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                return {**default_config, **config}
        except (FileNotFoundError, json.JSONDecodeError) as e:
            self.logger.error(f"Failed to load config: {e}. Using defaults.")
            return default_config

    def _load_message_id(self) -> Optional[int]:
        if not os.path.exists(self.MESSAGE_ID_FILE):
            return None
        try:
            with open(self.MESSAGE_ID_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return int(data.get("message_id"))
        except (json.JSONDecodeError, ValueError) as e:
            self.logger.error(f"Failed to load message ID: {e}")
            return None

    def _save_message_id(self, message_id: int) -> None:
        try:
            with open(self.MESSAGE_ID_FILE, "w", encoding="utf-8") as f:
                json.dump({"message_id": message_id}, f)
        except OSError as e:
            self.logger.error(f"Failed to save message ID: {e}")

    def _load_user_mapping(self) -> Dict[str, str]:
        try:
            with open(self.USER_MAPPING_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            self.logger.error(f"Failed to load user mapping: {e}")
            return {}

    def connect_to_plex(self) -> Optional[PlexServer]:
        try:
            plex = PlexServer(self.PLEX_URL, self.PLEX_TOKEN)
            if self.plex_start_time is None:
                self.plex_start_time = time.time()
            return plex
        except Exception as e:
            self.logger.error(f"Failed to connect to Plex server: {e}")
            self.plex_start_time = None
            return None

    def get_server_info(self) -> Dict[str, Any]:
        self.plex = self.connect_to_plex()
        if not self.plex:
            return self.get_offline_info()
        try:
            self.offline_since = None
            return {
                "status": "🟢 Online",
                "uptime": self.calculate_uptime(),
                "library_stats": self.get_library_stats(),
                "active_users": self.get_active_streams(),
                "current_streams": self.plex.sessions(),
            }
        except Exception as e:
            self.logger.error(f"Error retrieving server info: {e}")
            return self.get_offline_info()

    def calculate_uptime(self) -> str:
        if not self.plex_start_time:
            return "Offline"
        total_minutes = int((time.time() - self.plex_start_time) / 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return "99+ Hours" if hours > 99 else f"{hours:02d}:{minutes:02d}"

    def get_library_stats(self) -> Dict[str, Dict[str, Any]]:
        current_time = datetime.now()
        if (
            self.last_library_update
            and (current_time - self.last_library_update).total_seconds() <= self.library_update_interval
        ):
            return self.library_cache

        if not self.plex:
            self.plex = self.connect_to_plex()
        if not self.plex:
            return self.library_cache

        try:
            sections = {section.title: section for section in self.plex.library.sections()}
            stats: Dict[str, Dict[str, Any]] = {}
            plex_config = self.config["plex_sections"]
            configured_sections = plex_config["sections"]

            if not plex_config["show_all"]:
                for title in configured_sections:
                    if title in sections:
                        config = configured_sections[title]
                        section = sections[title]
                        stats[title] = self._build_section_stats(section, config)
            else:
                for title in configured_sections:
                    if title in sections:
                        config = configured_sections[title]
                        section = sections[title]
                        stats[title] = self._build_section_stats(section, config)
                for title, section in sections.items():
                    if title not in configured_sections:
                        stats[title] = {
                            "count": len(section.all()),
                            "episodes": 0,
                            "display_name": title,
                            "emoji": "🎬",
                            "show_episodes": False,
                        }

            self.library_cache = stats
            self.last_library_update = current_time
            self.logger.info(f"Library stats updated and cached (interval: {self.library_update_interval}s)")
            return stats
        except Exception as e:
            self.logger.error(f"Error updating library stats: {e}")
            return self.library_cache

    def _build_section_stats(self, section, config: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "count": len(section.all()),
            "episodes": sum(show.leafCount for show in section.all()) if config["show_episodes"] and hasattr(section, "all") else 0,
            "display_name": config["display_name"],
            "emoji": config["emoji"],
            "show_episodes": config["show_episodes"],
        }

    def get_active_streams(self) -> List[str]:
        if not self.plex:
            return []
        sessions = self.plex.sessions()
        return [
            stream_info
            for idx, session in enumerate(sessions, start=1)
            if (stream_info := self.format_stream_info(session, idx))
        ]

    def format_stream_info(self, session, idx: int) -> str:
        try:
            user = session.usernames[0] if session.usernames else "Unbekannt"
            displayed_user = self.user_mapping.get(user, user)
            section_title = getattr(session, "librarySectionTitle", "Unknown")
            stats = self.get_library_stats()
            content_emoji = stats.get(section_title, {}).get("emoji") or (
                "🎵" if getattr(session, "type", "") == "track" else
                "🎥" if getattr(session, "type", "") in ["movie", None] else "📺"
            )

            title = self._get_formatted_title(session)
            progress_percent = (
                (session.viewOffset / session.duration * 100)
                if hasattr(session, "viewOffset") and hasattr(session, "duration") and session.duration
                else 0
            )
            is_paused = getattr(session.players[0], "state", "") == "paused" if hasattr(session, "players") and session.players else False
            bar = f"{'▓' * int(progress_percent / 10)}{'░' * (10 - int(progress_percent / 10))}"
            progress_display = f"⏸️ {bar} {progress_percent:.1f}%" if is_paused else f"{bar} {progress_percent:.1f}%"

            current_time = self._format_time(str(timedelta(milliseconds=session.viewOffset or 0)).split(".")[0], session.duration or 0)
            total_time = self._format_time(str(timedelta(milliseconds=session.duration or 0)).split(".")[0], session.duration or 0)

            media = session.media[0] if hasattr(session, "media") and session.media else None

            if getattr(session, "type", "") == "track":
                audio_stream = next((stream for part in media.parts for stream in part.streams if stream.streamType == 2), None) if media else None
                quality = f"{getattr(audio_stream, 'bitDepth', '')}bit" if audio_stream and getattr(audio_stream, 'bitDepth', None) else ""
                if audio_stream and getattr(audio_stream, 'samplingRate', None):
                    quality += f" {int(audio_stream.samplingRate/1000)}kHz" if quality else f"{int(audio_stream.samplingRate/1000)}kHz"
                quality = quality if quality else "Audio"
            else:
                quality = f"{getattr(media, 'videoResolution', '1080')}p" if media else "1080p"
                quality = quality[:-1] if quality.endswith("pp") else "4K" if quality in ["4kp", "4Kp"] else quality

            transcode_session = getattr(session, "transcodeSession", None)
            transcode_emoji = "🔄" if transcode_session else "⏯️"
            bitrate = (
                f"⚡ {transcode_session.bitrate / 1000:.1f} Mbps" if transcode_session and getattr(transcode_session, "bitrate", None)
                else f"⚡ {media.bitrate / 1000:.1f} Mbps" if media and getattr(media, "bitrate", None)
                else ""
            )

            product_name = (
                session.players[0].product.replace("Plex for ", "").replace("Infuse-Library", "Infuse")
                if hasattr(session, "players") and session.players
                else "Unknown"
            )

            return (
                f"**```{content_emoji} {title} · {displayed_user}\n"
                f"└─ {progress_display} · {current_time}/{total_time}\n"
                f" └─ {transcode_emoji} 🖥️ {product_name} · 📡 {quality} · {bitrate}```**"
            )
        except Exception as e:
            self.logger.error(f"Error formatting stream info: {e}")
            return f"```❓ Stream could not be loaded (# {idx})```"

    def _format_time(self, time_str: str, duration: int) -> str:
        parts = time_str.split(":")
        less_than_hour = (duration // 1000) < 3600
        return f"{int(parts[-2]):02d}:{int(parts[-1]):02d}" if less_than_hour else f"{int(parts[0]):01d}:{int(parts[1]):02d}:{int(parts[2]):02d}"

    def _get_formatted_title(self, session) -> str:
        if hasattr(session, "type") and session.type == "track":
            artist = getattr(session, "grandparentTitle", "Unknown Artist")
            track = getattr(session, "title", "Unknown Track")
            return f"{artist} - {track}"
        elif hasattr(session, "grandparentTitle"):
            series_title = session.grandparentTitle.split(":")[0].split("-")[0].strip()
            episode_info = (
                f"S{session.parentIndex:02d}E{session.index:02d}"
                if hasattr(session, "parentIndex") and hasattr(session, "index")
                else ""
            )
            return f"{series_title} - {episode_info}"
        year = f" ({session.year})" if hasattr(session, "year") and session.year else ""
        return f"{session.title}{year}"

    def get_offline_info(self) -> Dict[str, Any]:
        current_time = discord.utils.utcnow()
        if not self.offline_since:
            self.offline_since = current_time
        stats: Dict[str, Dict[str, Any]] = {}
        plex_config = self.config["plex_sections"]
        configured_sections = plex_config["sections"]
        for title in configured_sections:
            config = configured_sections[title]
            stats[title] = {
                "count": 0,
                "episodes": 0,
                "display_name": config["display_name"],
                "emoji": config["emoji"],
                "show_episodes": config["show_episodes"],
            }
        return {
            "status": "🔴 Offline",
            "offline_since": self.offline_since,
            "library_stats": stats,
            "active_users": [],
            "current_streams": [],
        }

    @tasks.loop(minutes=5)
    async def update_status(self) -> None:
        try:
            info = self.get_server_info()
            active_streams = len(info["active_users"])
            presence_config = self.config["presence"]
            stats = info["library_stats"]

            if info["status"] != "🟢 Online":
                activity_text = presence_config["offline_text"]
                status = discord.Status.dnd
            elif active_streams > 0:
                activity_text = presence_config["stream_text"].format(
                    count=active_streams, s="s" if active_streams != 1 else ""
                )
                status = discord.Status.online
            else:
                presence_parts = [
                    f"{'{:,.0f}'.format(stats[section['section_title']]['count']).replace(',', '.')} {section['display_name']} {section['emoji']}"
                    for section in presence_config["sections"]
                    if section["section_title"] in stats
                ]
                activity_text = " | ".join(presence_parts) if presence_parts else "No streams or sections configured"
                status = discord.Status.online

            await self.bot.change_presence(activity=discord.CustomActivity(name=activity_text), status=status)
            self.logger.info(f"Status updated: {activity_text} ({status})")
        except Exception as e:
            self.logger.error(f"Error updating status: {e}")

    @tasks.loop(minutes=1)
    async def update_dashboard(self) -> None:
        channel = self.bot.get_channel(self.CHANNEL_ID)
        if not channel:
            return
        try:
            info = self.get_server_info()

            sabnzbd_cog = self.bot.get_cog("SABnzbd")
            if sabnzbd_cog:
                info["downloads"] = await sabnzbd_cog.get_sabnzbd_info()

            qb_cog = self.bot.get_cog("QBittorrentCog")
            if qb_cog:
                info["qb_downloads"] = await qb_cog.get_downloads_field()

            uptime_cog = self.bot.get_cog("Uptime")
            if uptime_cog:
                uptime_data = uptime_cog.get_uptime_data()
                info["uptime_24h"] = (
                    f"{uptime_data[0]:.1f}% ({uptime_cog.format_online_time(uptime_data[1])})"
                    if uptime_data[0] is not None else "No data"
                )
                info["uptime_7d"] = (
                    f"{uptime_data[2]:.1f}% ({uptime_cog.format_online_time(uptime_data[3])})"
                    if uptime_data[2] is not None else "No data"
                )
                info["uptime_30d"] = (
                    f"{uptime_data[4]:.1f}% ({uptime_cog.format_online_time(uptime_data[5])})"
                    if uptime_data[4] is not None else "No data"
                )
                info["last_offline"] = uptime_data[6] if uptime_data[6] else "Not available"

            embed = await self.create_dashboard_embed(info)
            await self._update_dashboard_message(channel, embed)
        except Exception as e:
            self.logger.error(f"Error updating dashboard: {e}")

    async def create_dashboard_embed(self, info: Dict[str, Any]) -> discord.Embed:
        dashboard_config = self.config["dashboard"]
        embed = discord.Embed(
            title="Server is currently Offline! :warning:" if info["status"] != "🟢 Online" else "Server is currently Online! :white_check_mark:",
            color=discord.Color.red() if info["status"] != "🟢 Online" else discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )

        if info["status"] != "🟢 Online":
            offline_since_str, time_diff_str = "Unknown", "Unknown duration"
            if info["offline_since"] and isinstance(info["offline_since"], datetime):
                offline_since_dt = info["offline_since"]
                offline_since_str = (offline_since_dt + timedelta(hours=1)).strftime("%d.%m.%Y %H:%M")
                time_diff = discord.utils.utcnow() - offline_since_dt
                days, hours, minutes = time_diff.days, time_diff.seconds // 3600, (time_diff.seconds % 3600) // 60
                time_diff_str = (
                    f"{days} day{'s' if days != 1 else ''}, {hours} hour{'s' if hours != 1 else ''}, {minutes} minute{'s' if minutes != 1 else ''} ago"
                    if days > 0 else
                    f"{hours} hour{'s' if hours != 1 else ''}, {minutes} minute{'s' if minutes != 1 else ''} ago"
                    if hours > 0 else
                    f"{minutes} minute{'s' if minutes != 1 else ''} ago"
                    if minutes > 0 else "Just now"
                )
            embed.add_field(name="Offline since:", value=f"```{offline_since_str}\n{time_diff_str}```", inline=False)

            uptime_cog = self.bot.get_cog("Uptime")
            if uptime_cog and "uptime_24h" in info and info["uptime_24h"] != "No data":
                embed.add_field(name="Uptime (24h)", value=f"```{info['uptime_24h']}```", inline=True)
                embed.add_field(name="Uptime (7 days)", value=f"```{info['uptime_7d']}```", inline=True)
                embed.add_field(name="Uptime (30 days)", value=f"```{info['uptime_30d']}```", inline=True)
        else:
            await self._add_embed_fields(embed, info)

        embed.set_author(name=dashboard_config["name"], icon_url=dashboard_config["icon_url"])
        embed.set_thumbnail(url=dashboard_config["icon_url"])
        embed.set_footer(text="Last updated", icon_url=dashboard_config["footer_icon_url"])
        return embed

    async def _add_embed_fields(self, embed: discord.Embed, info: Dict[str, Any]) -> None:
        embed.add_field(name="Server Uptime 🖥️", value=f"```{info['uptime']}```", inline=True)
        embed.add_field(name="", value="", inline=True)
        embed.add_field(name="", value="", inline=True)

        stats = info["library_stats"]
        plex_config = self.config["plex_sections"]
        configured_sections = plex_config["sections"]

        sections_to_display = configured_sections if not plex_config["show_all"] else {**configured_sections, **{k: None for k in stats if k not in configured_sections}}
        for title in sections_to_display:
            if title in stats:
                section_data = stats[title]
                display_name = f"{section_data['display_name']} {section_data['emoji']}"
                value = f"```{'{:,.0f}'.format(section_data['count']).replace(',', '.')}```"
                embed.add_field(name=display_name, value=value, inline=True)
                if section_data["show_episodes"]:
                    embed.add_field(
                        name=f"{section_data['display_name']} Episodes 📺",
                        value=f"```{'{:,.0f}'.format(section_data['episodes']).replace(',', '.')}```",
                        inline=True,
                    )

        # Streams
        if info["active_users"]:
            stream_count = len(info["active_users"])
            streams_text = " ".join(info["active_users"][:8])
            embed.add_field(
                name=f"{stream_count} current Stream{'s' if stream_count != 1 else ''}:" + (f" (showing 8 of {stream_count})" if stream_count > 8 else ""),
                value=streams_text,
                inline=False,
            )
        else:
            embed.add_field(name="Current Streams:", value="💤 *No active streams currently*", inline=False)

        # SABnzbd
        sabnzbd_cog = self.bot.get_cog("SABnzbd")
        if sabnzbd_cog:
            if info.get("downloads", {}).get("downloads"):
                downloads = info["downloads"]["downloads"][:4]
                download_count = len(info["downloads"]["downloads"])
                downloads_text = "\n".join(
                    self._format_sab_download(download, i)
                    for i, download in enumerate(downloads)
                )
                embed.add_field(
                    name=f"Sab: {download_count} current Download{'s' if download_count != 1 else ''}:",
                    value=downloads_text,
                    inline=False,
                )
                embed.add_field(name="Downloads 📥", value=f"```{self._calculate_total_size(downloads)}```", inline=True)
                embed.add_field(name="Free Space 💾", value=f"```{info['downloads']['diskspace1']}```", inline=True)
                embed.add_field(name="Total Space 🗄️", value=f"```{info['downloads']['diskspacetotal1']}```", inline=True)
            else:
                embed.add_field(name="Sab:", value="💤 *No active downloads currently*", inline=False)

        # qBittorrent
        qb_field = info.get("qb_downloads")
        if qb_field:
            value = qb_field[1]
            if value in ("Keine aktiven Downloads", "Nicht erreichbar"):
                embed.add_field(name="qBit:", value="💤 *No active downloads currently*", inline=False)
            else:
                speed_part = qb_field[0].split("🌐")[-1].strip() if "🌐" in qb_field[0] else ""
                name = f"qBit: {speed_part}" if speed_part else "qBit:"
                embed.add_field(name=name, value=value, inline=False)

    def _format_sab_download(self, download: Dict[str, Any], idx: int) -> str:
        """Formatiert einen SABnzbd-Download im gleichen Style wie qBittorrent."""
        name = download.get("filename", "Unknown")[:40]
        progress = float(download.get("percentage", 0))
        bar = f"{'▓' * int(progress / 10)}{'░' * (10 - int(progress / 10))}"
        size = download.get("size", "?")
        downloaded = download.get("downloaded", "?")
        speed = download.get("speed", "?")
        eta = download.get("timeleft", "?")

        return (
            f"**```⬇️ {name}\n"
            f"└─ {bar} {progress:.1f}% · {downloaded} / {size}\n"
            f" └─ ⚡ {speed} · ⏱️ {eta}```**"
        )

    def _calculate_total_size(self, downloads: List[Dict[str, Any]]) -> str:
        total_size_mb = 0
        for download in downloads:
            if download["size"] == "Unknown":
                continue
            value, unit = download["size"].split()
            value = float(value)
            total_size_mb += value / 1024 if unit == "KB" else value if unit == "MB" else value * 1024 if unit == "GB" else 0
        return f"{total_size_mb / 1024:.2f} GB" if total_size_mb >= 1024 else f"{total_size_mb:.2f} MB"

    async def _update_dashboard_message(self, channel: discord.TextChannel, embed: discord.Embed) -> None:
        if self.dashboard_message_id:
            try:
                message = await channel.fetch_message(self.dashboard_message_id)
                await message.edit(embed=embed)
                self.logger.debug("Dashboard message updated successfully")
            except discord.NotFound:
                self.logger.warning("Dashboard message not found, creating new one")
                self.dashboard_message_id = None

        if not self.dashboard_message_id:
            message = await channel.send(embed=embed)
            self.dashboard_message_id = message.id
            self._save_message_id(self.dashboard_message_id)
            self.logger.info(f"New dashboard message created with ID: {self.dashboard_message_id}")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlexCore(bot))
