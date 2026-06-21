import discord
from discord.ext import commands, tasks
import aiohttp
import os
import logging
import json

logger = logging.getLogger("plexwatch_bot")

# Lädt die config.json für Keywords (optional)
def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "..", "data", "config.json")
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def trim_name(name: str, keywords: list[str], max_len: int = 40) -> str:
    """
    Kürzt den Download-Namen am ersten gefundenen Keyword (wie SABnzbd-Cog).
    Punkte werden durch Leerzeichen ersetzt, danach auf max_len Zeichen begrenzt.
    """
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
    """Wandelt Bytes/s in lesbare Einheit um."""
    if bytes_per_sec >= 1_048_576:
        return f"{bytes_per_sec / 1_048_576:.1f} MB/s"
    elif bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec} B/s"


def format_size(total_bytes: int) -> str:
    """Wandelt Bytes in lesbare Einheit um."""
    if total_bytes >= 1_073_741_824:
        return f"{total_bytes / 1_073_741_824:.2f} GB"
    elif total_bytes >= 1_048_576:
        return f"{total_bytes / 1_048_576:.1f} MB"
    return f"{total_bytes / 1024:.1f} KB"


# Status-Kürzel von qBittorrent auf lesbaren Text mappen
QB_STATE_MAP = {
    "downloading":      "⬇️ Lädt",
    "stalledDL":        "⏸️ Pausiert (DL)",
    "uploading":        "⬆️ Seeden",
    "stalledUP":        "⏸️ Pausiert (UP)",
    "pausedDL":         "⏸️ Pausiert",
    "pausedUP":         "⏸️ Pausiert (UP)",
    "queuedDL":         "🕐 Warteschlange",
    "queuedUP":         "🕐 Warteschlange (UP)",
    "checkingDL":       "🔍 Prüfe",
    "checkingUP":       "🔍 Prüfe (UP)",
    "checkingResumeData": "🔍 Prüfe Resume",
    "moving":           "📦 Verschiebe",
    "error":            "❌ Fehler",
    "missingFiles":     "⚠️ Dateien fehlen",
    "unknown":          "❓ Unbekannt",
    "metaDL":           "🔍 Metadaten",
    "forcedDL":         "⬇️ Erzwungen",
    "forcedUP":         "⬆️ Erzwungen (UP)",
}


class QBittorrentCog(commands.Cog):
    """
    Cog für qBittorrent-Download-Tracking in PlexWatch.

    Umgebungsvariablen:
      QBITTORRENT_URL      – z.B. http://192.168.1.10:8080
      QBITTORRENT_USERNAME – qBittorrent WebUI-Benutzername (Standard: admin)
      QBITTORRENT_PASSWORD – qBittorrent WebUI-Passwort

    Optional in config.json unter "qbittorrent":
      "keywords": ["1080p", "2160p", "German", ...]   – zum Kürzen der Namen
      "max_torrents": 5                                 – max. angezeigte Torrents (Standard: 5)
      "only_downloading": true                          – nur aktive Downloads zeigen (Standard: false)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.qb_url = os.getenv("QBITTORRENT_URL", "").rstrip("/")
        self.qb_user = os.getenv("QBITTORRENT_USERNAME", "admin")
        self.qb_pass = os.getenv("QBITTORRENT_PASSWORD", "")
        self._session: aiohttp.ClientSession | None = None
        self._cookie = None  # SID-Cookie nach Login

        cfg = load_config()
        qb_cfg = cfg.get("qbittorrent", {})
        self.keywords = qb_cfg.get("keywords", cfg.get("sabnzbd", {}).get("keywords", []))
        self.max_torrents = int(qb_cfg.get("max_torrents", 5))
        self.only_downloading = bool(qb_cfg.get("only_downloading", False))

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        logger.info("QBittorrentCog geladen.")

    async def cog_unload(self):
        if self._session:
            await self._session.close()
        logger.info("QBittorrentCog entladen.")

    # ------------------------------------------------------------------ #
    #  Interne Hilfsmethoden                                               #
    # ------------------------------------------------------------------ #

    async def _login(self) -> bool:
        """Meldet sich an der qBittorrent WebUI an und speichert den SID-Cookie."""
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
                    logger.debug("qBittorrent Login erfolgreich.")
                    return True
                logger.warning(f"qBittorrent Login fehlgeschlagen: {resp.status} – {text!r}")
                return False
        except Exception as e:
            logger.error(f"qBittorrent Login-Fehler: {e}")
            return False

    async def _get_torrents(self) -> list[dict] | None:
        """Holt die Torrent-Liste von qBittorrent. Versucht bei 403 einen Re-Login."""
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
                            logger.info("qBittorrent Session abgelaufen, re-login…")
                            await self._login()
                            cookies = {"SID": self._cookie.value} if self._cookie else {}
                            continue
                        return None
                    if resp.status != 200:
                        logger.warning(f"qBittorrent /torrents/info: HTTP {resp.status}")
                        return None
                    return await resp.json()
            except Exception as e:
                logger.error(f"qBittorrent Torrent-Abruf fehlgeschlagen: {e}")
                return None
        return None

    # ------------------------------------------------------------------ #
    #  Öffentliche API (wird vom plex_core-Cog aufgerufen)                #
    # ------------------------------------------------------------------ #

    async def get_downloads_field(self) -> tuple[str, str] | None:
        """
        Gibt ein (name, value)-Tuple für ein Discord-Embed-Field zurück,
        oder None wenn qBittorrent nicht konfiguriert/erreichbar ist.

        Dieses Format ist identisch mit dem, das der SABnzbd-Cog zurückgibt,
        damit plex_core.py beide gleich behandeln kann.
        """
        if not self.qb_url:
            return None

        # Beim ersten Aufruf oder nach Session-Ablauf einloggen
        if not self._cookie:
            if not await self._login():
                return ("🔴 qBittorrent", "Nicht erreichbar")

        torrents = await self._get_torrents()
        if torrents is None:
            return ("🔴 qBittorrent", "Nicht erreichbar")

        # Nur aktive Downloads (downloading / forced) anzeigen wenn gewünscht
        if self.only_downloading:
            active = [t for t in torrents if t.get("state", "") in (
                "downloading", "forcedDL", "metaDL", "checkingDL"
            )]
        else:
            active = torrents

        if not active:
            return ("⬇️ qBittorrent", "Keine aktiven Downloads")

        lines = []
        for torrent in active[: self.max_torrents]:
            name = trim_name(torrent.get("name", "?"), self.keywords)
            state = QB_STATE_MAP.get(torrent.get("state", "unknown"), "❓")
            progress = torrent.get("progress", 0) * 100          # 0–100 %
            speed = format_speed(torrent.get("dlspeed", 0))
            size = format_size(torrent.get("size", 0))
            eta_sec = torrent.get("eta", -1)

            # ETA berechnen
            if eta_sec > 0 and eta_sec < 8_640_000:  # < 100 Tage
                h, rem = divmod(eta_sec, 3600)
                m = rem // 60
                eta_str = f"{h}h {m}m" if h else f"{m}m"
            else:
                eta_str = "–"

            bar = self._progress_bar(progress)
            lines.append(
                f"**{name}**\n"
                f"{state} · {bar} {progress:.0f}%\n"
                f"⚡ {speed} · 📦 {size} · ⏱️ {eta_str}"
            )

        total_speed = format_speed(sum(t.get("dlspeed", 0) for t in active))
        header = f"⬇️ qBittorrent — {len(active)} Torrent(s) · 🌐 {total_speed}"
        value = "\n\n".join(lines)

        # Discord Embed-Field-Werte dürfen max. 1024 Zeichen lang sein
        if len(value) > 1024:
            value = value[:1020] + "…"

        return (header, value)

    @staticmethod
    def _progress_bar(percent: float, length: int = 10) -> str:
        filled = int(length * percent / 100)
        return "█" * filled + "░" * (length - filled)


async def setup(bot: commands.Bot):
    await bot.add_cog(QBittorrentCog(bot))
