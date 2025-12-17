# Kanzi Bot - Comprehensive Discord Bot
# Features: Music streaming, AI assistance, fun commands, profiles
# Author: Shariar Mahmud Saif
# License: MIT

import os
import json
import asyncio
import aiohttp
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
import logging
import structlog
from diskcache import Cache
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import wavelink
import requests
import openai
from prometheus_client import Counter, Histogram, start_http_server

import nextcord
from nextcord.ext import commands, tasks
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.join(PROJECT_ROOT, "data")
PROFILES_DIR = os.path.join(DATA_ROOT, "profiles")
BANNERS_DIR = os.path.join(DATA_ROOT, "banners")
MUSIC_DIR = os.path.join(DATA_ROOT, "music")
MUSIC_LOCAL_DIR = os.path.join(MUSIC_DIR, "local")
SNIPPETS_DIR = os.path.join(DATA_ROOT, "snippets")
GAMES_DIR = os.path.join(DATA_ROOT, "games")
STUDY_DIR = os.path.join(DATA_ROOT, "study")
QUIZZES_DIR = os.path.join(DATA_ROOT, "quizzes")
CANVAS_DIR = os.path.join(DATA_ROOT, "canvas")
CANVAS_COLLAB_DIR = os.path.join(CANVAS_DIR, "collab")

PLAYLIST_FILE = os.path.join(MUSIC_DIR, "playlist.json")
LISTENING_FILE = os.path.join(MUSIC_DIR, "listening.json")
GAMES_SCORES_FILE = os.path.join(GAMES_DIR, "scores.json")
QUIZZES_RESULTS_FILE = os.path.join(QUIZZES_DIR, "results.json")

ADMIN_FILE = os.path.join(PROFILES_DIR, "admin.json")
OWNER_FILE = os.path.join(PROFILES_DIR, "owner.json")

PREMIUM_PREVIEW_DAYS = 2
REWARD_LISTEN_SECONDS_REQUIRED = 3 * 60 * 60

ANIME_THEME = "anime"
NEUTRAL_THEME = "neutral"

FILENAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]")


# Setup logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Setup caching
cache = Cache(os.path.join(PROJECT_ROOT, "cache"))

# Setup metrics
REQUEST_COUNT = Counter('api_requests_total', 'Total API requests', ['api', 'status'])
REQUEST_LATENCY = Histogram('api_request_duration_seconds', 'API request latency', ['api'])

# API clients
spotify_client = None
audiodb_client = None

def safe_filename(name: str) -> str:
    return FILENAME_SAFE_RE.sub("_", name)[:128]


def ensure_dirs() -> None:
    for path in [
        DATA_ROOT,
        PROFILES_DIR,
        BANNERS_DIR,
        MUSIC_DIR,
        MUSIC_LOCAL_DIR,
        SNIPPETS_DIR,
        GAMES_DIR,
        STUDY_DIR,
        QUIZZES_DIR,
        CANVAS_DIR,
        CANVAS_COLLAB_DIR,
    ]:
        os.makedirs(path, exist_ok=True)
    for fpath, default in [
        (PLAYLIST_FILE, []),
        (LISTENING_FILE, {}),
        (GAMES_SCORES_FILE, {}),
        (QUIZZES_RESULTS_FILE, {}),
        (ADMIN_FILE, {"admins": []}),
        (OWNER_FILE, {"owner_id": None}),
    ]:
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=2)


class MusicControls(nextcord.ui.View):
    def __init__(self, vc: nextcord.VoiceClient):
        super().__init__(timeout=None)
        self.vc = vc

    @nextcord.ui.button(label="⏸️ Pause", style=nextcord.ButtonStyle.secondary)
    async def pause_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        if self.vc and self.vc.is_playing():
            self.vc.pause()
            await interaction.response.send_message("⏸️ Paused!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @nextcord.ui.button(label="▶️ Resume", style=nextcord.ButtonStyle.secondary)
    async def resume_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        if self.vc and self.vc.is_paused():
            self.vc.resume()
            await interaction.response.send_message("▶️ Resumed!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Not paused.", ephemeral=True)

    @nextcord.ui.button(label="⏹️ Stop", style=nextcord.ButtonStyle.danger)
    async def stop_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
            await interaction.response.send_message("⏹️ Stopped!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @nextcord.ui.button(label="🔊 Vol +", style=nextcord.ButtonStyle.primary)
    async def vol_up_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        await interaction.response.send_message("🔊 Volume control not available with current setup.", ephemeral=True)

    @nextcord.ui.button(label="🔉 Vol -", style=nextcord.ButtonStyle.primary)
    async def vol_down_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        await interaction.response.send_message("🔉 Volume control not available with current setup.", ephemeral=True)

    @nextcord.ui.button(label="❓ Help", style=nextcord.ButtonStyle.secondary)
    async def help_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        embed = build_help_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)


def auto_solve_playback(link):
    import yt_dlp
    if not link.startswith(('http://', 'https://')):
        link = f'scsearch:{link}'
    steps = [
        {"format": "bestaudio/best", "quiet": True, "nocheckcertificate": True, "noplaylist": True, "compat_opts": ["js-runtimes=deno"]},
        {"format": "best", "quiet": True, "nocheckcertificate": True, "noplaylist": True, "compat_opts": ["js-runtimes=deno"]},
        {"format": "bestaudio", "quiet": True, "nocheckcertificate": True, "noplaylist": True, "compat_opts": ["js-runtimes=deno"]},
        {"format": "worst", "quiet": True, "nocheckcertificate": True, "noplaylist": True, "compat_opts": ["js-runtimes=deno"]},
        {"format": "bestaudio/best", "quiet": True, "nocheckcertificate": True, "noplaylist": True, "extract_flat": False, "compat_opts": ["js-runtimes=deno"]},
    ]
    for i, opts in enumerate(steps, 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(link, download=False)
                if 'entries' in info:
                    if info['entries']:
                        info = info['entries'][0]
                    else:
                        raise Exception("No search results found")
                return info
        except Exception as e:
            print(f"Auto-solve step {i} failed: {e}")
            continue
    raise Exception("All 5 auto-solve steps failed. Unable to play this track.")


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def profile_path(user_id: int) -> str:
    return os.path.join(PROFILES_DIR, f"{user_id}.json")


def load_profile(user_id: int) -> Dict[str, Any]:
    path = profile_path(user_id)
    prof = read_json(
        path,
        {
            "user_id": user_id,
            "premium": False,
            "premium_preview_until": None,
            "premium_unlocked_by_reward": False,
            "theme": NEUTRAL_THEME,
            "badges": [],
            "nickname": None,
            "status_text": None,
            "emoji_flair": None,
            "accent_color": None,
            "quote": None,
            "frame": None,
            "banner_file": None,
        },
    )
    return prof


def save_profile(user_id: int, prof: Dict[str, Any]) -> None:
    write_json(profile_path(user_id), prof)


def is_owner(user_id: int) -> bool:
    data = read_json(OWNER_FILE, {"owner_id": None})
    return data.get("owner_id") == user_id


def is_admin(user_id: int) -> bool:
    data = read_json(ADMIN_FILE, {"admins": []})
    return user_id in set(data.get("admins") or [])


def has_premium(user_id: int) -> bool:
    prof = load_profile(user_id)
    if prof.get("premium"):
        return True
    if prof.get("premium_unlocked_by_reward"):
        return True
    until = prof.get("premium_preview_until")
    if until:
        try:
            dt = datetime.fromisoformat(until)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc):
                return True
        except Exception:
            pass
    return False


intents = nextcord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def load_env():
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            os.environ.setdefault(k, v)
            except Exception:
                pass

    # Initialize API clients
    global spotify_client
    spotify_id = os.getenv("SPOTIFY_CLIENT_ID")
    spotify_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if spotify_id and spotify_secret:
        spotify_client = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=spotify_id, client_secret=spotify_secret))

    openai.api_key = os.getenv('OPENAI_API_KEY')

@bot.event
async def on_ready():
    ensure_dirs()
    start_listening_tracker.start()
    
    # Start metrics server
    start_http_server(8000)
    
    try:
        await bot.sync_application_commands()
        for guild in bot.guilds:
            try:
                await guild.sync_application_commands()
            except Exception:
                pass
        print("Slash commands synced.")
    except Exception as e:
        print(f"Slash sync failed: {e}")
    print(f"Kanzi Bot is online as {bot.user}")


# @wavelink.listener()
# async def on_wavelink_track_start(payload: wavelink.TrackStartPayload):
#     logger.info("Track started", track=payload.track.title, guild=payload.player.guild.id)


# @wavelink.listener()
# async def on_wavelink_track_end(payload: wavelink.TrackEndPayload):
#     logger.info("Track ended", track=payload.track.title, guild=payload.player.guild.id)


def grant_free_preview_if_needed(user_id: int) -> None:
    prof = load_profile(user_id)
    if not prof.get("premium_preview_until"):
        until = datetime.now(timezone.utc) + timedelta(days=PREMIUM_PREVIEW_DAYS)
        prof["premium_preview_until"] = until.isoformat()
        save_profile(user_id, prof)


def listening_stats() -> Dict[str, Any]:
    return read_json(LISTENING_FILE, {})


def update_listening(user_id: int, seconds: int) -> None:
    stats = listening_stats()
    u = str(user_id)
    cur = int(stats.get(u, 0))
    cur += max(0, seconds)
    stats[u] = cur
    write_json(LISTENING_FILE, stats)
    if cur >= REWARD_LISTEN_SECONDS_REQUIRED:
        prof = load_profile(user_id)
        if not prof.get("premium_unlocked_by_reward"):
            prof["premium_unlocked_by_reward"] = True
            save_profile(user_id, prof)


def human_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return "/".join(parts)

def theme_color(premium: bool, theme: str) -> int:
    if premium:
        return 0xFFD700  # Gold for premium
    if theme == ANIME_THEME:
        return 0xFF69B4  # Hot pink for anime
    else:
        return 0x00BCD4  # Cyan for neutral

def make_embed(title: str, description: Optional[str], user: Optional[nextcord.abc.User], premium: bool, theme: str) -> nextcord.Embed:
    embed = nextcord.Embed(title=title, description=description or "", color=theme_color(premium, theme))
    try:
        if user and hasattr(user, "display_avatar"):
            embed.set_thumbnail(url=user.display_avatar.url)
    except Exception:
        pass
    try:
        if bot.user and hasattr(bot.user, "display_avatar"):
            embed.set_author(name="Kanzi Bot", icon_url=bot.user.display_avatar.url)
    except Exception:
        pass
    embed.timestamp = datetime.now(timezone.utc)
    return embed

@tasks.loop(seconds=60)
async def start_listening_tracker():
    for guild in bot.guilds:
        vc: Optional[nextcord.VoiceClient] = guild.voice_client
        if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
            channel: Optional[nextcord.VoiceChannel] = vc.channel
            if not channel:
                continue
            for member in channel.members:
                if member.bot:
                    continue
                update_listening(member.id, 60)


async def require_premium(ctx: commands.Context) -> bool:
    user_id = ctx.author.id
    grant_free_preview_if_needed(user_id)
    if has_premium(user_id) or is_admin(user_id) or is_owner(user_id):
        return True
    await ctx.send("This feature is premium. Earn it by listening 3h or get admin grant.")
    return False


@bot.command(name="profile")
async def cmd_profile(ctx: commands.Context):
    grant_free_preview_if_needed(ctx.author.id)
    prof = load_profile(ctx.author.id)
    premium = has_premium(ctx.author.id)
    theme = prof.get("theme") or NEUTRAL_THEME
    total_seconds = int(listening_stats().get(str(ctx.author.id), 0))
    embed = nextcord.Embed(
        title=f"Kanzi Profile • {ctx.author.display_name}",
        description=prof.get("quote") or "Welcome to Kanzi Bot",
        color=0xFFC107 if premium else (0xFF4081 if theme == ANIME_THEME else 0x607D8B),
    )
    embed.add_field(
        name="Premium",
        value=("⭐ Active" if premium else "⛔ Locked"),
        inline=True,
    )
    embed.add_field(name="Theme", value=theme, inline=True)
    embed.add_field(
        name="Music Reward",
        value=f"{human_time(total_seconds)}/{human_time(REWARD_LISTEN_SECONDS_REQUIRED)}",
        inline=False,
    )
    if prof.get("banner_file"):
        try:
            file_path = prof["banner_file"]
            if os.path.exists(file_path):
                file = nextcord.File(file_path, filename=os.path.basename(file_path))
                embed.set_image(url=f"attachment://{os.path.basename(file_path)}")
                await ctx.send(file=file, embed=embed, view=KanziView(ctx.author))
                return
        except Exception:
            pass
    await ctx.send(embed=embed, view=KanziView(ctx.author))

@bot.slash_command(name="profile", description="Show your Kanzi profile")
async def slash_profile(interaction: nextcord.Interaction):
    uid = interaction.user.id
    grant_free_preview_if_needed(uid)
    prof = load_profile(uid)
    premium = has_premium(uid)
    theme = prof.get("theme") or NEUTRAL_THEME
    total_seconds = int(listening_stats().get(str(uid), 0))
    title = f"Kanzi Profile • {interaction.user.display_name if hasattr(interaction.user, 'display_name') else interaction.user.name}"
    embed = make_embed(title, prof.get("quote") or "Welcome to Kanzi Bot", interaction.user, premium, theme)
    embed.add_field(name="Premium", value=("⭐ Active" if premium else "⛔ Locked"), inline=True)
    embed.add_field(name="Theme", value=theme, inline=True)
    embed.add_field(name="Listening Time", value=human_time(total_seconds), inline=True)
    progress = min(total_seconds / REWARD_LISTEN_SECONDS_REQUIRED, 1.0)
    bar_length = 10
    filled = int(progress * bar_length)
    bar = "▰" * filled + "▱" * (bar_length - filled)
    embed.add_field(name="Music Reward Progress", value=f"{bar} {int(progress * 100)}%", inline=False)
    embed.set_footer(text="🎵 'Music is the universal language of mankind.' - Henry Wadsworth Longfellow 🎶")
    if prof.get("banner_file") and os.path.exists(prof["banner_file"]):
        file = nextcord.File(prof["banner_file"], filename=os.path.basename(prof["banner_file"]))
        embed.set_image(url=f"attachment://{os.path.basename(prof['banner_file'])}")
        await interaction.response.send_message(file=file, embed=embed, view=KanziView(interaction.user), ephemeral=True)
        return
    # Default CDN image based on theme
    color = "00BCD4" if theme == NEUTRAL_THEME else "FF69B4"
    embed.set_image(url=f"https://via.placeholder.com/800x200/{color}/FFFFFF?text=Kanzi+Bot+Profile")
    await interaction.response.send_message(embed=embed, view=KanziView(interaction.user), ephemeral=True)


@bot.command(name="theme")
@commands.cooldown(1, 3, commands.BucketType.user)
async def cmd_theme(ctx: commands.Context, subcmd: Optional[str] = None, mode: Optional[str] = None):
    user_id = ctx.author.id
    grant_free_preview_if_needed(user_id)
    if subcmd is None:
        await ctx.send("Usage: !theme set [anime|neutral] • !theme toggle • !theme status")
        return
    subcmd = subcmd.lower()
    if subcmd == "status":
        prof = load_profile(user_id)
        await ctx.send(f"Current theme: {prof.get('theme')}")
        return
    if subcmd == "toggle":
        if not await require_premium(ctx):
            return
        prof = load_profile(user_id)
        prof["theme"] = ANIME_THEME if (prof.get("theme") != ANIME_THEME) else NEUTRAL_THEME
        save_profile(user_id, prof)
        await ctx.send(f"Toggled theme to {prof['theme']}")
        return
    if subcmd == "set":
        if not await require_premium(ctx):
            return
        if mode not in (ANIME_THEME, NEUTRAL_THEME):
            await ctx.send("Allowed modes: anime, neutral")
            return
        prof = load_profile(user_id)
        prof["theme"] = mode
        save_profile(user_id, prof)
        await ctx.send(f"Theme set to {mode}")
        return
    await ctx.send("Unknown theme subcommand")

@bot.slash_command(name="theme_toggle", description="Toggle theme (premium)")
async def slash_theme_toggle(interaction: nextcord.Interaction):
    user = interaction.user
    grant_free_preview_if_needed(user.id)
    if not (has_premium(user.id) or is_admin_member(user) or is_owner_member(user)):
        await interaction.response.send_message("Premium required.", ephemeral=True)
        return
    prof = load_profile(user.id)
    prof["theme"] = ANIME_THEME if (prof.get("theme") != ANIME_THEME) else NEUTRAL_THEME
    save_profile(user.id, prof)
    await interaction.response.send_message(f"Toggled theme to {prof['theme']}", ephemeral=True)

@bot.slash_command(name="theme_set", description="Set theme (premium)")
async def slash_theme_set(interaction: nextcord.Interaction, mode: str = nextcord.SlashOption(name="mode", description="anime or neutral", choices=[ANIME_THEME, NEUTRAL_THEME])):
    user = interaction.user
    grant_free_preview_if_needed(user.id)
    if not (has_premium(user.id) or is_admin_member(user) or is_owner_member(user)):
        await interaction.response.send_message("Premium required.", ephemeral=True)
        return
    prof = load_profile(user.id)
    prof["theme"] = mode
    save_profile(user.id, prof)
    await interaction.response.send_message(f"Theme set to {mode}", ephemeral=True)

@bot.slash_command(name="theme_status", description="Show current theme")
async def slash_theme_status(interaction: nextcord.Interaction):
    prof = load_profile(interaction.user.id)
    await interaction.response.send_message(f"Current theme: {prof.get('theme')}", ephemeral=True)


@bot.command(name="premium")
async def cmd_premium(ctx: commands.Context, action: Optional[str] = None, user: Optional[nextcord.Member] = None):
    actor_id = ctx.author.id
    grant_free_preview_if_needed(actor_id)
    if action is None:
        await ctx.send("Usage: !premium grant @user • !premium revoke @user • !premium status @user")
        return
    action = action.lower()
    if action == "status":
        tgt = user or ctx.author
        status = "Active" if has_premium(tgt.id) else "Locked"
        await ctx.send(f"Premium status for {tgt.mention}: {status}")
        return
    if action in ("grant", "revoke"):
        if not (is_admin_member(ctx.author) or is_owner_member(ctx.author)):
            await ctx.send("Only admins/owner may grant or revoke premium.")
            return
        if not user:
            await ctx.send("Please mention a target user.")
            return
        prof = load_profile(user.id)
        prof["premium"] = (action == "grant")
        save_profile(user.id, prof)
        await ctx.send(f"Premium {'granted' if action=='grant' else 'revoked'} for {user.mention}")
        return
    await ctx.send("Unknown premium action")

@bot.slash_command(name="premium_status", description="Show premium status for a user")
async def slash_premium_status(interaction: nextcord.Interaction, user: Optional[nextcord.Member] = None):
    tgt = user or interaction.user
    status = "Active" if has_premium(tgt.id) else "Locked"
    await interaction.response.send_message(f"Premium status for {tgt.mention}: {status}", ephemeral=True)

@bot.slash_command(name="premium_grant", description="Grant premium to a user (admin/owner)")
async def slash_premium_grant(interaction: nextcord.Interaction, user: nextcord.Member):
    if not (is_admin_member(interaction.user) or is_owner_member(interaction.user)):
        await interaction.response.send_message("Only admins/owner may grant premium.", ephemeral=True)
        return
    prof = load_profile(user.id)
    prof["premium"] = True
    save_profile(user.id, prof)
    await interaction.response.send_message(f"Premium granted for {user.mention}", ephemeral=True)

@bot.slash_command(name="premium_revoke", description="Revoke premium from a user (admin/owner)")
async def slash_premium_revoke(interaction: nextcord.Interaction, user: nextcord.Member):
    if not (is_admin_member(interaction.user) or is_owner_member(interaction.user)):
        await interaction.response.send_message("Only admins/owner may revoke premium.", ephemeral=True)
        return
    prof = load_profile(user.id)
    prof["premium"] = False
    save_profile(user.id, prof)
    await interaction.response.send_message(f"Premium revoked for {user.mention}", ephemeral=True)


@bot.command(name="owner")
async def cmd_owner(ctx: commands.Context, action: Optional[str] = None):
    actor_id = ctx.author.id
    if action == "override":
        if not is_owner_member(ctx.author):
            await ctx.send("Only the owner can override.")
            return
        prof = load_profile(actor_id)
        prof["premium"] = True
        prof["premium_unlocked_by_reward"] = True
        prof["premium_preview_until"] = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
        save_profile(actor_id, prof)
        await ctx.send("Owner override applied. All premium features unlocked.")
        return
    await ctx.send("Usage: !owner override")

@bot.slash_command(name="owner_override", description="Owner unlocks all premium features")
async def slash_owner_override(interaction: nextcord.Interaction):
    actor = interaction.user
    if not is_owner_member(actor):
        await interaction.response.send_message("Only the owner can override.", ephemeral=True)
        return
    prof = load_profile(actor.id)
    prof["premium"] = True
    prof["premium_unlocked_by_reward"] = True
    prof["premium_preview_until"] = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
    save_profile(actor.id, prof)
    await interaction.response.send_message("Owner override applied. All premium features unlocked.", ephemeral=True)


@bot.command(name="listen")
async def cmd_listen(ctx: commands.Context, subcmd: Optional[str] = None):
    if subcmd == "status":
        total_seconds = int(listening_stats().get(str(ctx.author.id), 0))
        await ctx.send(f"Listening progress: {human_time(total_seconds)}/{human_time(REWARD_LISTEN_SECONDS_REQUIRED)}")
        return
    await ctx.send("Usage: !listen status")

@bot.slash_command(name="listen_status", description="Show listening progress")
async def slash_listen_status(interaction: nextcord.Interaction):
    total_seconds = int(listening_stats().get(str(interaction.user.id), 0))
    await interaction.response.send_message(f"Listening progress: {human_time(total_seconds)}/{human_time(REWARD_LISTEN_SECONDS_REQUIRED)}", ephemeral=True)

def local_track_path(filename: str) -> Optional[str]:
    if not filename:
        return None
    fname = safe_filename(filename)
    path = os.path.join(MUSIC_LOCAL_DIR, fname)
    if os.path.exists(path):
        return path
    return None


ALLOWED_MUSIC_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "soundcloud.com",
    "freemusicarchive.org",
    "jamendo.com",
    "ccmixter.org",
)


def is_allowed_music_link(link: str) -> bool:
    if not link.lower().startswith(("http://", "https://")):
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(link).netloc.lower()
        for dom in ALLOWED_MUSIC_DOMAINS:
            if host.endswith(dom):
                return True
        return False
    except Exception:
        return False


# API Integration Functions
async def fetch_anime_info(query: str) -> Dict[str, Any]:
    """Fetch anime info from Jikan API"""
    cache_key = f"anime_{query}"
    if cache_key in cache:
        return cache[cache_key]
    
    url = f"https://api.jikan.moe/v4/anime?q={query}&limit=1"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            REQUEST_COUNT.labels(api='jikan', status=resp.status).inc()
            with REQUEST_LATENCY.labels(api='jikan').time():
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get('data', [{}])[0] if data.get('data') else {}
                    cache.set(cache_key, result, expire=3600)
                    return result
    return {}


async def fetch_game_info(query: str) -> Dict[str, Any]:
    """Fetch game info from IGDB API"""
    cache_key = f"game_{query}"
    if cache_key in cache:
        return cache[cache_key]
    
    client_id = os.getenv('TWITCH_CLIENT_ID')
    access_token = os.getenv('TWITCH_ACCESS_TOKEN')
    if not client_id or not access_token:
        return {}
    
    url = "https://api.igdb.com/v4/games"
    headers = {
        'Client-ID': client_id,
        'Authorization': f"Bearer {access_token}"
    }
    body = f'search "{query}"; fields name,summary,cover.url,genres.name,platforms.name; limit 1;'
    
    try:
        response = requests.post(url, headers=headers, data=body)
        REQUEST_COUNT.labels(api='igdb', status=response.status_code).inc()
        with REQUEST_LATENCY.labels(api='igdb').time():
            if response.status_code == 200:
                data = response.json()
                result = data[0] if data else {}
                cache.set(cache_key, result, expire=3600)
                return result
    except Exception as e:
        logger.error("IGDB search failed", error=str(e))
    return {}


async def fetch_joke() -> str:
    """Fetch a random joke"""
    cache_key = "joke"
    if cache_key in cache:
        return cache[cache_key]
    
    url = "https://official-joke-api.appspot.com/random_joke"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            REQUEST_COUNT.labels(api='joke', status=resp.status).inc()
            with REQUEST_LATENCY.labels(api='joke').time():
                if resp.status == 200:
                    data = await resp.json()
                    joke = f"{data['setup']} - {data['punchline']}"
                    cache.set(cache_key, joke, expire=300)
                    return joke
    return "Why did the scarecrow win an award? Because he was outstanding in his field!"


async def fetch_meme() -> Dict[str, Any]:
    """Fetch a random meme"""
    cache_key = "meme"
    if cache_key in cache:
        return cache[cache_key]
    
    url = "https://meme-api.com/gimme"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            REQUEST_COUNT.labels(api='meme', status=resp.status).inc()
            with REQUEST_LATENCY.labels(api='meme').time():
                if resp.status == 200:
                    data = await resp.json()
                    cache.set(cache_key, data, expire=300)
                    return data
    return {"title": "Meme unavailable", "url": ""}


async def fetch_nature_fact() -> str:
    """Fetch a random nature fact"""
    cache_key = "nature_fact"
    if cache_key in cache:
        return cache[cache_key]
    
    url = "https://uselessfacts.jsph.pl/random.json?language=en"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            REQUEST_COUNT.labels(api='uselessfacts', status=resp.status).inc()
            with REQUEST_LATENCY.labels(api='uselessfacts').time():
                if resp.status == 200:
                    data = await resp.json()
                    fact = data.get('text', 'Nature is amazing!')
                    cache.set(cache_key, fact, expire=3600)
                    return fact
    return "Did you know? The Earth's core is as hot as the surface of the Sun."


async def roll_dice(sides: int = 6) -> int:
    """Roll a die"""
    import random
    return random.randint(1, sides)


async def search_spotify(query: str) -> Dict[str, Any]:
    """Search Spotify for tracks"""
    if not spotify_client:
        return {}
    
    cache_key = f"spotify_{query}"
    if cache_key in cache:
        return cache[cache_key]
    
    try:
        results = spotify_client.search(q=query, type='track', limit=1)
        track = results['tracks']['items'][0] if results['tracks']['items'] else {}
        cache.set(cache_key, track, expire=3600)
        return track
    except Exception as e:
        logger.error("Spotify search failed", error=str(e))
        return {}


async def get_artist_info(artist: str) -> Dict[str, Any]:
    """Get artist info from TheAudioDB"""
    cache_key = f"audiodb_artist_{artist}"
    if cache_key in cache:
        return cache[cache_key]
    
    api_key = os.getenv("THEAUDIODB_API_KEY")
    if not api_key:
        return {}
    
    try:
        url = f"https://www.theaudiodb.com/api/v1/json/{api_key}/search.php?s={artist}"
        response = requests.get(url)
        REQUEST_COUNT.labels(api='theaudiodb', status=response.status_code).inc()
        with REQUEST_LATENCY.labels(api='theaudiodb').time():
            if response.status_code == 200:
                data = response.json()
                artists = data.get('artists', [])
                if artists:
                    info = artists[0]
                    cache.set(cache_key, info, expire=3600)
                    return info
    except Exception as e:
        logger.error("AudioDB artist search failed", error=str(e))
    return {}


@bot.command(name="playlocal")
@commands.cooldown(1, 5, commands.BucketType.user)
async def cmd_playlocal(ctx: commands.Context, filename: Optional[str] = None):
    if not filename:
        await ctx.send("Usage: !playlocal [filename]")
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Join a voice channel first.")
        return
    path = local_track_path(filename)
    if not path:
        await ctx.send("File not found in data/music/local/")
        return
    try:
        channel: nextcord.VoiceChannel = ctx.author.voice.channel
        vc: Optional[nextcord.VoiceClient] = ctx.guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            vc = await channel.connect()
        if vc.is_playing():
            vc.stop()
        source = nextcord.FFmpegPCMAudio(path)
        vc.play(source)
        await ctx.send(f"Playing local track: {os.path.basename(path)}")
    except Exception as e:
        await ctx.send(f"Playback error: {e}")

@bot.slash_command(name="playlocal", description="Play a local track from data/music/local/")
async def slash_playlocal(interaction: nextcord.Interaction, filename: str):
    member = interaction.user
    if not isinstance(member, nextcord.Member):
        guild = interaction.guild
        member = guild.get_member(member.id) if guild else None
    if not member or not member.voice or not member.voice.channel:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return
    path = local_track_path(filename)
    if not path:
        await interaction.response.send_message("File not found in data/music/local/", ephemeral=True)
        return
    try:
        channel: nextcord.VoiceChannel = member.voice.channel
        vc: Optional[nextcord.VoiceClient] = interaction.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
        else:
            vc = await channel.connect()
        if vc.is_playing():
            vc.stop()
        source = nextcord.FFmpegPCMAudio(path)
        vc.play(source)
        await interaction.response.send_message(f"Playing local track: {os.path.basename(path)}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Playback error: {e}", ephemeral=True)

@bot.command(name="play")
@commands.cooldown(1, 5, commands.BucketType.user)
async def cmd_play(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        await ctx.send("Usage: !play [link]")
        return
    if not is_allowed_music_link(link):
        await ctx.send("Link must be from allowed free sources (YouTube, SoundCloud, FMA, Jamendo, ccMixter).")
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Join a voice channel first.")
        return
    try:
        import yt_dlp
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "nocheckcertificate": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=False)
            url = info["url"]
        channel: nextcord.VoiceChannel = ctx.author.voice.channel
        vc: Optional[nextcord.VoiceClient] = ctx.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
        else:
            vc = await channel.connect()
        if vc.is_playing():
            vc.stop()
        source = nextcord.FFmpegPCMAudio(url)
        vc.play(source)
        await ctx.send("Playing track from free source.")
    except Exception as e:
        await ctx.send(f"Playback error: {e}")

@bot.slash_command(name="play", description="Play from YouTube, SoundCloud, or search by name")
async def slash_play(interaction: nextcord.Interaction, link: str):
    if link.startswith(('http://', 'https://')) and not is_allowed_music_link(link):
        await interaction.response.send_message("🚫 Oops! That link isn't from our approved sources. Stick to YouTube or SoundCloud for the best vibes! 'Music is the universal language.' 🎶", ephemeral=True)
        return
    member = interaction.user
    if not isinstance(member, nextcord.Member):
        guild = interaction.guild
        member = guild.get_member(member.id) if guild else None
    if not member or not member.voice or not member.voice.channel:
        await interaction.response.send_message("🎤 Hey there! You need to be in a voice channel to jam with me. Let's get this party started! 'Where words fail, music speaks.' 🎉", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        info = auto_solve_playback(link)
        url = info["url"]
        channel: nextcord.VoiceChannel = member.voice.channel
        vc: Optional[nextcord.VoiceClient] = interaction.guild.voice_client
        if vc and vc.channel == channel:
            pass
        elif vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            vc = await channel.connect()
        if vc.is_playing():
            vc.stop()
        source = nextcord.FFmpegPCMAudio(url)
        vc.play(source)
        embed = nextcord.Embed(
            title="🎵 Now Playing",
            description=f"[{info.get('title', 'Unknown')}]({link})\n\n💬 'Music is the strongest form of magic.' - Marilyn Manson 🎸",
            color=0x00FF00
        )
        embed.add_field(name="Author", value=info.get('uploader', 'Unknown'), inline=True)
        embed.add_field(name="Duration", value=f"{info.get('duration', 0)}s", inline=True)
        if info.get('thumbnail'):
            embed.set_thumbnail(url=info.get('thumbnail'))
        view = MusicControls(vc)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"🎼 Oops! Something went wrong with playback: {e}. 'Music is my religion.' - Jimi Hendrix 🎶", ephemeral=True)

@bot.command(name="playlist")
async def cmd_playlist(ctx: commands.Context):
    data = read_json(PLAYLIST_FILE, [])
    if not data:
        await ctx.send("Community playlist is empty.")
        return
    lines = []
    for i, item in enumerate(data[:10], start=1):
        lines.append(f"{i}. {item.get('title') or item.get('link')}")
    embed = nextcord.Embed(
        title="Community Playlist",
        description="\n".join(lines),
        color=0x03A9F4,
    )
    await ctx.send(embed=embed)

@bot.command(name="sources")
async def cmd_sources(ctx: commands.Context):
    await ctx.send("Allowed sources: " + ", ".join(ALLOWED_MUSIC_DOMAINS))

@bot.slash_command(name="sources", description="List allowed streaming domains")
async def slash_sources(interaction: nextcord.Interaction):
    await interaction.response.send_message("Allowed sources: " + ", ".join(ALLOWED_MUSIC_DOMAINS), ephemeral=True)
    await ctx.send(embed=embed)

@bot.command(name="addsong")
@commands.cooldown(1, 5, commands.BucketType.user)
async def cmd_addsong(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        await ctx.send("Usage: !addsong [link]")
        return
    if not is_allowed_music_link(link):
        await ctx.send("Link must be from allowed free sources (YouTube, SoundCloud, FMA, Jamendo, ccMixter).")
        return
    title = None
    try:
        import yt_dlp
        ydl_opts = {"quiet": True, "nocheckcertificate": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=False)
            title = info.get("title")
    except Exception:
        pass
    data = read_json(PLAYLIST_FILE, [])
    data.append({"link": link, "title": title, "added_by": ctx.author.id, "ts": datetime.now(timezone.utc).isoformat()})
    write_json(PLAYLIST_FILE, data)
    await ctx.send("Added to community playlist.")

@bot.slash_command(name="playlist", description="Show community playlist")
async def slash_playlist(interaction: nextcord.Interaction):
    data = read_json(PLAYLIST_FILE, [])
    if not data:
        await interaction.response.send_message("Community playlist is empty.", ephemeral=True)
        return
    lines = []
    for i, item in enumerate(data[:10], start=1):
        lines.append(f"{i}. {item.get('title') or item.get('link')}")
    embed = nextcord.Embed(title="Community Playlist", description="\n".join(lines), color=0x03A9F4)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.slash_command(name="addsong", description="Add song link to community playlist")
async def slash_addsong(interaction: nextcord.Interaction, link: str):
    if not is_allowed_music_link(link):
        await interaction.response.send_message("Link must be from allowed free sources.", ephemeral=True)
        return
    title = None
    try:
        import yt_dlp
        ydl_opts = {"quiet": True, "nocheckcertificate": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=False)
            title = info.get("title")
    except Exception:
        pass
    data = read_json(PLAYLIST_FILE, [])
    data.append({"link": link, "title": title, "added_by": interaction.user.id, "ts": datetime.now(timezone.utc).isoformat()})
    write_json(PLAYLIST_FILE, data)
    await interaction.response.send_message("Added to community playlist.", ephemeral=True)
@bot.command(name="stop")
async def cmd_stop(ctx: commands.Context):
    vc: Optional[nextcord.VoiceClient] = ctx.guild.voice_client
    if vc and vc.is_connected():
        vc.stop()
        await ctx.send("Playback stopped.")
        return
    await ctx.send("Bot is not connected.")

@bot.command(name="skip")
async def cmd_skip(ctx: commands.Context):
    vc: Optional[nextcord.VoiceClient] = ctx.guild.voice_client
    if vc and vc.is_connected() and vc.is_playing():
        vc.stop()
        await ctx.send("Skipped current track.")
        return
    await ctx.send("Nothing is playing.")

@bot.slash_command(name="stop", description="Stop playback")
async def slash_stop(interaction: nextcord.Interaction):
    vc: Optional[nextcord.VoiceClient] = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_connected():
        vc.stop()
        await interaction.response.send_message("🛑 Playback stopped. 'Silence is golden.' - Thomas Carlyle 🤫", ephemeral=True)
        return
    await interaction.response.send_message("❓ I'm not connected to any voice channel right now. 'The music is not in the notes, but in the silence between.' - Wolfgang Amadeus Mozart 🎼", ephemeral=True)

@bot.slash_command(name="skip", description="Skip current track")
async def slash_skip(interaction: nextcord.Interaction):
    vc: Optional[nextcord.VoiceClient] = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_connected() and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped! 'Change is the law of life.' - John F. Kennedy 🔄", ephemeral=True)
        return
    await interaction.response.send_message("🎵 Nothing is playing at the moment. 'Music is the wine that fills the cup of silence.' - Robert Fripp 🍷", ephemeral=True)

@bot.command(name="banner")
@commands.cooldown(1, 10, commands.BucketType.user)
async def cmd_banner(ctx: commands.Context, action: Optional[str] = None, link: Optional[str] = None):
    if action != "set" or not link:
        await ctx.send("Usage: !banner set [link]")
        return
    try:
        import requests
        if not link.lower().startswith(("http://", "https://")):
            await ctx.send("Invalid link. Use http/https URLs only.")
            return
        resp = requests.get(link, timeout=10)
        if resp.status_code != 200 or not resp.content:
            await ctx.send("Failed to download banner.")
            return
        filename = f"{ctx.author.id}_banner.png"
        fpath = os.path.join(BANNERS_DIR, filename)
        with open(fpath, "wb") as f:
            f.write(resp.content)
        prof = load_profile(ctx.author.id)
        prof["banner_file"] = fpath
        save_profile(ctx.author.id, prof)
        await ctx.send("Banner updated.")
    except Exception as e:
        await ctx.send(f"Error: {e}")

@bot.slash_command(name="banner_set", description="Set banner image from URL")
async def slash_banner_set(interaction: nextcord.Interaction, link: str):
    try:
        import requests
        if not link.lower().startswith(("http://", "https://")):
            await interaction.response.send_message("Invalid link. Use http/https URLs only.", ephemeral=True)
            return
        resp = requests.get(link, timeout=10)
        if resp.status_code != 200 or not resp.content:
            await interaction.response.send_message("Failed to download banner.", ephemeral=True)
            return
        filename = f"{interaction.user.id}_banner.png"
        fpath = os.path.join(BANNERS_DIR, filename)
        with open(fpath, "wb") as f:
            f.write(resp.content)
        prof = load_profile(interaction.user.id)
        prof["banner_file"] = fpath
        save_profile(interaction.user.id, prof)
        await interaction.response.send_message("Banner updated.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)

@bot.command(name="status")
async def cmd_status(ctx: commands.Context, *, text: Optional[str] = None):
    if not text:
        await ctx.send("Usage: !status [text]")
        return
    prof = load_profile(ctx.author.id)
    prof["status_text"] = text.strip()[:200]
    save_profile(ctx.author.id, prof)
    await ctx.send("Bot-only status updated.")

@bot.slash_command(name="status", description="Set bot-only status text")
async def slash_status(interaction: nextcord.Interaction, text: str):
    prof = load_profile(interaction.user.id)
    prof["status_text"] = text.strip()[:200]
    save_profile(interaction.user.id, prof)
    await interaction.response.send_message("Bot-only status updated.", ephemeral=True)
@bot.command(name="quote")
@commands.cooldown(1, 5, commands.BucketType.user)
async def cmd_quote(ctx: commands.Context, *, text: Optional[str] = None):
    if not text:
        await ctx.send("Usage: !quote [text]")
        return
    prof = load_profile(ctx.author.id)
    prof["quote"] = text.strip()[:200]
    save_profile(ctx.author.id, prof)
    await ctx.send("Quote updated.")

@bot.slash_command(name="quote", description="Set personal/anime quote")
async def slash_quote(interaction: nextcord.Interaction, text: str):
    prof = load_profile(interaction.user.id)
    prof["quote"] = text.strip()[:200]
    save_profile(interaction.user.id, prof)
    await interaction.response.send_message("Quote updated.", ephemeral=True)
@bot.command(name="ping")
async def cmd_ping(ctx: commands.Context):
    ms = round(bot.latency * 1000)
    await ctx.send(f"Pong {ms}ms")

@bot.slash_command(name="ping", description="Check bot latency")
async def slash_ping(interaction: nextcord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong {ms}ms", ephemeral=True)

def owner_username() -> Optional[str]:
    return os.getenv("KANZI_OWNER_USERNAME") or ".mr.hyper"

def admin_usernames() -> List[str]:
    raw = os.getenv("KANZI_ADMIN_USERNAMES")
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [".mr.hyper"]

def is_owner_member(member: nextcord.abc.User) -> bool:
    if is_owner(member.id):
        return True
    try:
        return member.name == owner_username()
    except Exception:
        return False

def is_admin_member(member: nextcord.abc.User) -> bool:
    if is_admin(member.id):
        return True
    try:
        return member.name in set(admin_usernames())
    except Exception:
        return False

class KanziView(nextcord.ui.View):
    def __init__(self, requester: nextcord.Member):
        super().__init__(timeout=120)
        self.requester = requester

    @nextcord.ui.button(label="Theme Toggle", style=nextcord.ButtonStyle.primary)
    async def toggle_theme(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("This panel is bound to another user.", ephemeral=True)
            return
        ctx_author = interaction.user
        if not (has_premium(ctx_author.id) or is_admin_member(ctx_author) or is_owner_member(ctx_author)):
            await interaction.response.send_message("Premium required. Earn by listening 3h or get admin grant.", ephemeral=True)
            return
        prof = load_profile(ctx_author.id)
        prof["theme"] = ANIME_THEME if (prof.get("theme") != ANIME_THEME) else NEUTRAL_THEME
        save_profile(ctx_author.id, prof)
        await interaction.response.send_message(f"Toggled theme to {prof['theme']}", ephemeral=True)

    @nextcord.ui.button(label="Anime Rec", style=nextcord.ButtonStyle.success)
    async def anime_rec_btn(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("This panel is bound to another user.", ephemeral=True)
            return
        ctx_author = interaction.user
        if not (has_premium(ctx_author.id) or is_admin_member(ctx_author) or is_owner_member(ctx_author)):
            await interaction.response.send_message("Premium required.", ephemeral=True)
            return
        prof = load_profile(ctx_author.id)
        theme = prof.get("theme") or ANIME_THEME
        recs = [
            "Fullmetal Alchemist: Brotherhood",
            "Attack on Titan",
            "Cyberpunk: Edgerunners",
            "Your Name",
            "Jujutsu Kaisen",
        ] if theme == ANIME_THEME else [
            "Violet Evergarden",
            "Mushishi",
            "Barakamon",
            "A Silent Voice",
            "Made in Abyss",
        ]
        embed = nextcord.Embed(title="Anime Recommendations", description="\n".join(f"• {r}" for r in recs), color=0xE91E63)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @nextcord.ui.button(label="Playlist", style=nextcord.ButtonStyle.secondary)
    async def playlist_btn(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        data = read_json(PLAYLIST_FILE, [])
        if not data:
            await interaction.response.send_message("Playlist is empty.", ephemeral=True)
            return
        lines = []
        for i, item in enumerate(data[:10], start=1):
            lines.append(f"{i}. {item.get('title') or item.get('link')}")
        embed = nextcord.Embed(title="Community Playlist", description="\n".join(lines), color=0x03A9F4)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @nextcord.ui.button(label="Listening Status", style=nextcord.ButtonStyle.blurple)
    async def listen_btn(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        total_seconds = int(listening_stats().get(str(interaction.user.id), 0))
        await interaction.response.send_message(f"{human_time(total_seconds)}/{human_time(REWARD_LISTEN_SECONDS_REQUIRED)}", ephemeral=True)

def build_help_embed() -> nextcord.Embed:
    lines = []
    lines.append("• Identity: /profile, /banner_set, /status, /quote")
    lines.append("• Theme: /theme_status, /theme_set, /theme_toggle")
    lines.append("• Premium: /premium_status, /premium_grant, /premium_revoke, /owner_override")
    lines.append("• Admin: /admin_add, /admin_remove, /ownerset")
    lines.append("• Music: /playlocal, /play, /addsong, /playlist, /skip, /stop, /listen_status, /sources")
    lines.append("• Fun: /anime_search, /game_search, /joke, /meme, /nature_fact, /roll_dice, /spotify_search, /artist_info")
    lines.append("• Anime: /anime_rec")
    lines.append("• Utility: /ping, /help, /leaderboard")
    lines.append("• Sources: youtube.com, youtu.be, soundcloud.com, freemusicarchive.org, jamendo.com, ccmixter.org")
    embed = make_embed("Kanzi Bot • Help", "\n".join(lines), bot.user, False, ANIME_THEME)
    embed.set_image(url="https://via.placeholder.com/800x200/FF69B4/FFFFFF?text=Kanzi+Bot+Help")
    embed.add_field(name="About", value="Comprehensive Discord bot with music streaming, fun APIs, and community features. 'The best way to predict the future is to create it.' - Peter Drucker 🤖", inline=False)
    embed.add_field(name="Bot Owner", value="Admin ID: 808937599452446770 - The mastermind behind the magic! 👑", inline=False)
    embed.add_field(name="Admin Info", value="Contact admin for support or feature requests. 'Alone we can do so little; together we can do so much.' - Helen Keller 👥", inline=False)
    embed.add_field(name="Bot Info", value="Running with love and code. Version: 1.0 | Uptime: Since last restart. 'Code is poetry.' - Unknown 💻", inline=False)
    embed.add_field(name="Storage", value="Local project folder: data/ - All your data is safe with us! 🔒", inline=False)
    return embed

@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    embed = build_help_embed()
    view = KanziView(ctx.author)
    await ctx.send(embed=embed, view=view)

@bot.slash_command(name="help", description="Show help panel")
async def slash_help(interaction: nextcord.Interaction):
    embed = build_help_embed()
    view = KanziView(interaction.user)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.command(name="leaderboard")
async def cmd_leaderboard(ctx: commands.Context):
    stats = listening_stats()
    items = [(int(uid), int(sec)) for uid, sec in stats.items()]
    items.sort(key=lambda x: x[1], reverse=True)
    top = items[:10]
    lines = []
    for i, (uid, sec) in enumerate(top, start=1):
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else str(uid)
        lines.append(f"{i}. {name} — {human_time(sec)}")
    embed = nextcord.Embed(title="Top Listeners", description="\n".join(lines) or "No data", color=0x8BC34A)
    await ctx.send(embed=embed)

@bot.slash_command(name="leaderboard", description="Show top listeners")
async def slash_leaderboard(interaction: nextcord.Interaction):
    stats = listening_stats()
    items = [(int(uid), int(sec)) for uid, sec in stats.items()]
    items.sort(key=lambda x: x[1], reverse=True)
    top = items[:10]
    lines = []
    for i, (uid, sec) in enumerate(top, start=1):
        member = interaction.guild.get_member(uid) if interaction.guild else None
        name = member.display_name if member else str(uid)
        lines.append(f"{i}. {name} — {human_time(sec)}")
    embed = nextcord.Embed(title="Top Listeners", description="\n".join(lines) or "No data", color=0x8BC34A)
    await interaction.response.send_message(embed=embed, ephemeral=True)
@bot.command(name="anime")
@commands.cooldown(1, 5, commands.BucketType.user)
async def cmd_anime(ctx: commands.Context, subcmd: Optional[str] = None):
    if subcmd != "rec":
        await ctx.send("Usage: !anime rec")
        return
    if not await require_premium(ctx):
        return
    prof = load_profile(ctx.author.id)
    theme = prof.get("theme") or ANIME_THEME
    recs = [
        "Fullmetal Alchemist: Brotherhood",
        "Attack on Titan",
        "Cyberpunk: Edgerunners",
        "Your Name",
        "Jujutsu Kaisen",
    ] if theme == ANIME_THEME else [
        "Violet Evergarden",
        "Mushishi",
        "Barakamon",
        "A Silent Voice",
        "Made in Abyss",
    ]
    embed = nextcord.Embed(title="Anime Recommendations", description="\n".join(f"• {r}" for r in recs), color=0xE91E63)
    await ctx.send(embed=embed)

@bot.slash_command(name="anime_rec", description="Anime recommendations (premium)")
async def slash_anime_rec(interaction: nextcord.Interaction):
    user = interaction.user
    if not (has_premium(user.id) or is_admin_member(user) or is_owner_member(user)):
        await interaction.response.send_message("Premium required.", ephemeral=True)
        return
    prof = load_profile(user.id)
    theme = prof.get("theme") or ANIME_THEME
    recs = [
        "Fullmetal Alchemist: Brotherhood",
        "Attack on Titan",
        "Cyberpunk: Edgerunners",
        "Your Name",
        "Jujutsu Kaisen",
    ] if theme == ANIME_THEME else [
        "Violet Evergarden",
        "Mushishi",
        "Barakamon",
        "A Silent Voice",
        "Made in Abyss",
    ]
    embed = nextcord.Embed(title="Anime Recommendations", description="\n".join(f"• {r}" for r in recs), color=0xE91E63)
    await interaction.response.send_message(embed=embed, ephemeral=True)
@bot.command(name="admin")
async def cmd_admin(ctx: commands.Context, action: Optional[str] = None, user: Optional[nextcord.Member] = None):
    if not is_owner_member(ctx.author):
        await ctx.send("Only the owner can manage admins.")
        return
    if action not in ("add", "remove"):
        await ctx.send("Usage: !admin add @user • !admin remove @user")
        return
    if not user:
        await ctx.send("Please mention a user.")
        return
    cfg = read_json(ADMIN_FILE, {"admins": []})
    admins = set(cfg.get("admins") or [])
    if action == "add":
        admins.add(user.id)
        await ctx.send(f"Admin added: {user.mention}")
    else:
        admins.discard(user.id)
        await ctx.send(f"Admin removed: {user.mention}")
    write_json(ADMIN_FILE, {"admins": list(admins)})

@bot.command(name="ownerset")
async def cmd_owner_set(ctx: commands.Context, user: Optional[nextcord.Member] = None):
    if not is_owner(ctx.author.id):
        await ctx.send("Only current owner can set a new owner.")
        return
    if not user:
        await ctx.send("Usage: !ownerset @user")
        return
    write_json(OWNER_FILE, {"owner_id": user.id})
    await ctx.send(f"New owner set: {user.mention}")

@bot.slash_command(name="admin_add", description="Owner adds admin by mention")
async def slash_admin_add(interaction: nextcord.Interaction, user: nextcord.Member):
    if not is_owner_member(interaction.user):
        await interaction.response.send_message("Only owner can add admins.", ephemeral=True)
        return
    cfg = read_json(ADMIN_FILE, {"admins": []})
    admins = set(cfg.get("admins") or [])
    admins.add(user.id)
    write_json(ADMIN_FILE, {"admins": list(admins)})
    await interaction.response.send_message(f"Admin added: {user.mention}", ephemeral=True)

@bot.slash_command(name="admin_remove", description="Owner removes admin by mention")
async def slash_admin_remove(interaction: nextcord.Interaction, user: nextcord.Member):
    if not is_owner_member(interaction.user):
        await interaction.response.send_message("Only owner can remove admins.", ephemeral=True)
        return
    cfg = read_json(ADMIN_FILE, {"admins": []})
    admins = set(cfg.get("admins") or [])
    admins.discard(user.id)
    write_json(ADMIN_FILE, {"admins": list(admins)})
    await interaction.response.send_message(f"Admin removed: {user.mention}", ephemeral=True)

@bot.slash_command(name="ownerset", description="Owner sets a new owner by mention")
async def slash_ownerset(interaction: nextcord.Interaction, user: nextcord.Member):
    if not is_owner_member(interaction.user):
        await interaction.response.send_message("Only current owner can set a new owner.", ephemeral=True)
        return
    write_json(OWNER_FILE, {"owner_id": user.id})
    await interaction.response.send_message(f"New owner set: {user.mention}", ephemeral=True)

# Fun Feature Slash Commands
@bot.slash_command(name="anime_search", description="Search for anime information")
async def slash_anime_search(interaction: nextcord.Interaction, query: str):
    await interaction.response.defer()
    data = await fetch_anime_info(query)
    if not data:
        embed = nextcord.Embed(title="Anime Not Found", description="Sorry, I couldn't find that anime!", color=0xFF5722)
    else:
        embed = nextcord.Embed(
            title=data.get('title', 'Unknown'),
            description=data.get('synopsis', 'No description available.')[:500] + "...",
            color=0x9C27B0
        )
        embed.add_field(name="Score", value=data.get('score', 'N/A'), inline=True)
        embed.add_field(name="Episodes", value=data.get('episodes', 'N/A'), inline=True)
        embed.add_field(name="Status", value=data.get('status', 'N/A'), inline=True)
        if data.get('images', {}).get('jpg', {}).get('image_url'):
            embed.set_thumbnail(url=data['images']['jpg']['image_url'])
    await interaction.followup.send(embed=embed)

@bot.slash_command(name="game_search", description="Search for game information")
async def slash_game_search(interaction: nextcord.Interaction, query: str):
    await interaction.response.defer()
    data = await fetch_game_info(query)
    if not data:
        embed = nextcord.Embed(title="Game Not Found", description="Sorry, I couldn't find that game!", color=0xFF5722)
    else:
        embed = nextcord.Embed(
            title=data.get('name', 'Unknown'),
            description=data.get('summary', 'No description available.')[:500] + "...",
            color=0x4CAF50
        )
        if data.get('genres'):
            genres = [g['name'] for g in data['genres']]
            embed.add_field(name="Genres", value=", ".join(genres), inline=True)
        if data.get('platforms'):
            platforms = [p['name'] for p in data['platforms']]
            embed.add_field(name="Platforms", value=", ".join(platforms), inline=True)
        if data.get('cover', {}).get('url'):
            embed.set_thumbnail(url=f"https:{data['cover']['url']}")
    await interaction.followup.send(embed=embed)

@bot.slash_command(name="joke", description="Get a random joke")
async def slash_joke(interaction: nextcord.Interaction):
    joke = await fetch_joke()
    embed = nextcord.Embed(title="😂 Random Joke", description=joke, color=0xFFC107)
    await interaction.response.send_message(embed=embed)

@bot.slash_command(name="meme", description="Get a random meme")
async def slash_meme(interaction: nextcord.Interaction):
    meme = await fetch_meme()
    embed = nextcord.Embed(title=meme.get('title', 'Random Meme'), color=0xFF9800)
    if meme.get('url'):
        embed.set_image(url=meme['url'])
    await interaction.response.send_message(embed=embed)

@bot.slash_command(name="nature_fact", description="Get a random nature fact")
async def slash_nature_fact(interaction: nextcord.Interaction):
    fact = await fetch_nature_fact()
    embed = nextcord.Embed(title="🌿 Nature Fact", description=fact, color=0x4CAF50)
    await interaction.response.send_message(embed=embed)

@bot.slash_command(name="roll_dice", description="Roll a dice")
async def slash_roll_dice(interaction: nextcord.Interaction, sides: int = 6):
    result = await roll_dice(sides)
    embed = nextcord.Embed(title="🎲 Dice Roll", description=f"You rolled a {result} on a {sides}-sided die!", color=0x2196F3)
    await interaction.response.send_message(embed=embed)

@bot.slash_command(name="spotify_search", description="Search Spotify for a track")
async def slash_spotify_search(interaction: nextcord.Interaction, query: str):
    await interaction.response.defer()
    track = await search_spotify(query)
    if not track:
        embed = nextcord.Embed(title="Track Not Found", description="Sorry, I couldn't find that track on Spotify!", color=0xFF5722)
    else:
        embed = nextcord.Embed(
            title=track.get('name', 'Unknown'),
            description=f"By {', '.join([a['name'] for a in track.get('artists', [])])}",
            color=0x1DB954
        )
        embed.add_field(name="Album", value=track.get('album', {}).get('name', 'N/A'), inline=True)
        embed.add_field(name="Duration", value=f"{track.get('duration_ms', 0) // 1000}s", inline=True)
        if track.get('album', {}).get('images'):
            embed.set_thumbnail(url=track['album']['images'][0]['url'])
        if track.get('external_urls', {}).get('spotify'):
            embed.add_field(name="Listen on Spotify", value=f"[Click here]({track['external_urls']['spotify']})", inline=False)
    await interaction.followup.send(embed=embed)

@bot.slash_command(name="artist_info", description="Get artist information from TheAudioDB")
async def slash_artist_info(interaction: nextcord.Interaction, artist: str):
    await interaction.response.defer()
    info = await get_artist_info(artist)
    if not info:
        embed = nextcord.Embed(title="Artist Not Found", description="Sorry, I couldn't find information for that artist!", color=0xFF5722)
    else:
        embed = nextcord.Embed(
            title=info.get('strArtist', 'Unknown'),
            description=info.get('strBiographyEN', 'No biography available.')[:500] + "...",
            color=0xE91E63
        )
        embed.add_field(name="Genre", value=info.get('strGenre', 'N/A'), inline=True)
        embed.add_field(name="Country", value=info.get('strCountry', 'N/A'), inline=True)
        if info.get('strArtistThumb'):
            embed.set_thumbnail(url=info['strArtistThumb'])
    await interaction.followup.send(embed=embed)

def run():
    ensure_dirs()
    load_env()
    global ALLOWED_MUSIC_DOMAINS
    doms = os.getenv("KANZI_ALLOWED_DOMAINS")
    if doms:
        ALLOWED_MUSIC_DOMAINS = tuple([d.strip() for d in doms.split(",") if d.strip()])
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Please set DISCORD_TOKEN environment variable with your bot token.")
        return
    bot.run(token)


@bot.slash_command(name="admin_send", description="Admin: Send a message through the bot")
async def slash_admin_send(interaction: nextcord.Interaction, channel: nextcord.TextChannel, message: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin permissions.", ephemeral=True)
        return
    try:
        await channel.send(message)
        await interaction.response.send_message("✅ Message sent.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to send message: {e}", ephemeral=True)


@bot.slash_command(name="admin_join", description="Admin: Join a voice channel")
async def slash_admin_join(interaction: nextcord.Interaction, channel: nextcord.VoiceChannel):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin permissions.", ephemeral=True)
        return
    try:
        vc: Optional[nextcord.VoiceClient] = interaction.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
        else:
            vc = await channel.connect()
        await interaction.response.send_message(f"✅ Joined {channel.name}.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to join: {e}", ephemeral=True)


@bot.slash_command(name="admin_give_role", description="Admin: Give a role to a member")
async def slash_admin_give_role(interaction: nextcord.Interaction, member: nextcord.Member, role: nextcord.Role):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin permissions.", ephemeral=True)
        return
    try:
        await member.add_roles(role)
        await interaction.response.send_message(f"✅ Gave {role.name} to {member.display_name}.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to give role: {e}", ephemeral=True)


@bot.slash_command(name="ai_help", description="Ask AI for help or information")
async def slash_ai_help(interaction: nextcord.Interaction, query: str):
    if not openai.api_key:
        await interaction.response.send_message("❌ OpenAI API key not set.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": query}],
            max_tokens=500
        )
        answer = response.choices[0].message.content.strip()
        await interaction.followup.send(f"🤖 AI Help: {answer}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ AI error: {e}", ephemeral=True)


@bot.slash_command(name="ai_fix", description="Ask AI to fix a bot error")
async def slash_ai_fix(interaction: nextcord.Interaction, error: str):
    if not openai.api_key:
        await interaction.response.send_message("❌ OpenAI API key not set.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        prompt = f"Fix this Discord bot error in Python/nextcord: {error}. Provide code fix and explanation."
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000
        )
        fix = response.choices[0].message.content.strip()
        await interaction.followup.send(f"🔧 AI Fix: {fix}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ AI error: {e}", ephemeral=True)


@bot.slash_command(name="ai_play", description="Ask AI to suggest a song for a mood")
async def slash_ai_play(interaction: nextcord.Interaction, mood: str):
    if not openai.api_key:
        await interaction.response.send_message("❌ OpenAI API key not set.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        prompt = f"Suggest one popular song for {mood} mood. Just the song name and artist."
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100
        )
        suggestion = response.choices[0].message.content.strip()
        await interaction.followup.send(f"🎵 AI Suggestion for {mood}: {suggestion}. Use /play {suggestion} to listen!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ AI error: {e}", ephemeral=True)


if __name__ == "__main__":
    run()

