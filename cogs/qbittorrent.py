import discord
from discord.ext import commands
import aiohttp
import os
import logging
import json

logger = logging.getLogger("plexwatch_bot")

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "..", "data", "config.json")
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def trim_name(name: str, keywords: list, max_len: int = 40) -> str:
    name = name.replace(".", " ")
    lower = name.lower()
    cut = len(name)
    for kw in keywords:
        idx = lower.find(kw.lower())
        if idx != -1 and idx < cut:
            cut = idx
    name = name[:cut].strip()
    if len(name) > max_len:
        name = name[:max_len - 3] + "..."
    return name if name else "Unbekannt"


def format_speed(bytes_per_sec: int) -> str:
    if bytes_per_sec >= 1_048_576:
        return f"{bytes_per_sec / 1_048_576:.1f} MB/s"
    elif bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec} B/s"


def format_size(total_bytes: int) -> str:
    if total_bytes >= 1_073_741_824:
        return f"{total_bytes / 1_073_741_824:.2f} GB"
    elif total_bytes >= 1_048_576:
        return f"{total_bytes / 1_048_576:.1f} MB"
    return f"{total_bytes / 1024:.1f} KB"


QB_STATE_MAP = {
    "downloading":        "⬇️",
    "stalledDL":          "⏸️",
    "uploading":          "⬆️",
    "stalledUP":          "⬆️",
    "pausedDL":           "⏸️",
    "pausedUP":           "⏸️",
    "queuedDL":           "🕐",
    "queuedUP":           "🕐",
    "checkingDL":         "🔍",
    "checkingUP":         "🔍",
    "checkingResumeData": "🔍",
    "moving":             "📦",
    "error":              "❌",
    "missingFiles":       "⚠️",
    "unknown":            "❓",
    "metaDL":             "🔍",
    "forcedDL":           "⬇️",
    "forcedUP":           "⬆️",
}


class QBittorrentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.qb_url = os.getenv("QBITTORRENT_URL", "").rstrip("/")
        self.qb_user = os.getenv("QBITTORRENT_USERNAME", "admin")
        self.qb_pass = os.getenv("QBITTORRENT_PASSWORD", "")
        self._session = None
        self._cookie = None

        cfg = load_config()
        qb_cfg = cfg.get("qbittorrent", {})
        self.keywords = qb_cfg.get("keywords", cfg.get("sabnzbd", {}).get("keywords", []))
        self.max_torrents = int(qb_cfg.get("max_torrents", 5))
        self.only_downloading = bool(qb_cfg.get("only_downloading", True))

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        logger.info("QBittorrentCog geladen.")

    async def cog_unload(self):
        if self._session:
            await self._session.close()
        logger.info("QBittorrentCog entladen.")

    async def _login(self) -> bool:
        if not self.qb_url:
            return False
        try:
            async with self._session.post(
                f"{self.qb_url}/api/v2/auth/login",
                data={"username": self.qb_user, "password": self.qb_pass},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()
                if resp.status == 200 and text.strip() == "Ok.":
                    self._cookie = resp.cookies.get("SID")
                    return True
                return False
        except Exception as e:
            logger.error(f"qBittorrent Login-Fehler: {e}")
            return False

    async def _get_torrents(self):
        if not self.qb_url:
            return None

        cookies = {"SID": self._cookie.value} if self._cookie else {}
        filter_val = "downloading" if self.only_downloading else "all"

        for attempt in range(2):
            try:
                async with self._session.get(
                    f"{self.qb_url}/api/v2/torrents/info",
                    params={"filter": filter_val, "sort": "added_on", "reverse": "true"},
                    cookies=cookies,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 403:
                        if attempt == 0:
                            await self._login()
                            cookies = {"SID": self._cookie.value} if self._cookie else {}
                            continue
                        return None
                    if resp.status != 200:
                        return None
                    return await resp.json()
            except Exception as e:
                logger.error(f"qBittorrent Abruf fehlgeschlagen: {e}")
                return None
        return None

    async def get_downloads_field(self):
        if not self.qb_url:
            return None

        if not self._cookie:
            if not await self._login():
                return ("qBit:", "🔴 *Not reachable*")

        torrents = await self._get_torrents()
        if torrents is None:
            return ("qBit:", "🔴 *Not reachable*")

        # Nur wirklich aktive Downloads
        active = [t for t in torrents if t.get("state", "") in (
            "downloading", "forcedDL", "metaDL", "checkingDL"
        )]

        if not active:
            return ("qBit:", "💤 *No active downloads currently*")

        total_speed = format_speed(sum(t.get("dlspeed", 0) for t in active))
        lines = []

        for torrent in active[: self.max_torrents]:
            name = trim_name(torrent.get("name", "?"), self.keywords)
            progress = torrent.get("progress", 0) * 100
            bar = f"{'▓' * int(progress / 10)}{'░' * (10 - int(progress / 10))}"
            speed = format_speed(torrent.get("dlspeed", 0))
            size_total = format_size(torrent.get("size", 0))
            size_done = format_size(int(torrent.get("size", 0) * torrent.get("progress", 0)))
            eta_sec = torrent.get("eta", -1)

            if eta_sec > 0 and eta_sec < 8_640_000:
                h, rem = divmod(eta_sec, 3600)
                m = rem // 60
                eta_str = f"{h}h {m}m" if h else f"{m}m"
            else:
                eta_str = "–"

            lines.append(
                f"**```⬇️ {name}\n"
                f"└─ {bar} {progress:.1f}% · {size_done} / {size_total}\n"
                f" └─ ⚡ {speed} · ⏱️ {eta_str}```**"
            )

        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1020] + "…"

        return (f"qBit: {total_speed}", value)


async def setup(bot: commands.Bot):
    await bot.add_cog(QBittorrentCog(bot))
