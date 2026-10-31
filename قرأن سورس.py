#!/usr/bin/env python3
"""Repeat a local audio file in a Telegram group call using PyTgCalls."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from pytgcalls import PyTgCalls


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("telegram-audio-loop")
BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_string: str
    channel_id: int
    audio_file: Path
    replay_gap_seconds: float
    reconnect_delay_seconds: int


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.upper().startswith(("PUT_", "CHANGE_ME", "YOUR_")):
        raise ValueError(f"Environment variable {name} is missing or still a placeholder.")
    return value


def load_config() -> Config:
    try:
        api_id = int(required_env("33659580"))
        channel_id = int(required_env("-1003949140800"))
    except ValueError as exc:
        raise ValueError("API_ID and CHANNEL_ID must be valid integers.") from exc

    api_hash = required_env("5a7ec2310794e9cd41fa6e8b8f6d28d5")
    session_string = required_env("1BVtsOKoBuytqaeUbY0HPDXT3bO_sDN9WRJejvJy-4xP1QDJM10l2M5vXn4Pa-4k2v5U4oudy7DnikuWpSlIQ_NnydeKhAAY9nVdpUI-b7K8XXjfo6GFuyd3trvtzfcOPPB2A4LURnk8p4iNrW9W4GVphlNse_Dc7tWtQu70Le18YWyQPsQTDmzR2JjTlbfN6MOc5j2bhHfIOsZNgcXkn9tYMYTwdITAltapKiJuj_XWODAURtXMuYJFXCiidylyxuF3hqtB7MeiURq2Spd4CLK-_fdQ-I3owaeVAz6xHtYZEAPuhOwxr0VRwT9LKPjNH9LEtgBN9w_dm5H_0J3cw0qOHUolWWZE=")
    audio_value = os.getenv("AUDIO_FILE", "assets/quran_audio.m4a").strip()
    audio_file = Path(audio_value).expanduser()
    if not audio_file.is_absolute():
        audio_file = BASE_DIR / audio_file
    audio_file = audio_file.resolve()
    if not audio_file.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_file}")

    try:
        replay_gap = float(os.getenv("REPLAY_GAP_SECONDS", "0"))
        reconnect_delay = int(os.getenv("RECONNECT_DELAY_SECONDS", "15"))
    except ValueError as exc:
        raise ValueError("REPLAY_GAP_SECONDS and RECONNECT_DELAY_SECONDS must be numeric.") from exc
    if replay_gap < 0 or reconnect_delay < 1:
        raise ValueError("Replay gap must be >= 0 and reconnect delay must be >= 1 second.")

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        channel_id=channel_id,
        audio_file=audio_file,
        replay_gap_seconds=replay_gap,
        reconnect_delay_seconds=reconnect_delay,
    )


def get_audio_duration(audio_file: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required. Install FFmpeg and make sure ffprobe is on PATH.")
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_file),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Could not determine the audio file duration.") from exc
    if duration <= 0:
        raise RuntimeError("The audio file has an invalid duration.")
    return duration


async def wait_until_repeat(seconds: float, stop_event: threading.Event) -> None:
    """Keep Telethon's event loop active while waiting, but honor shutdown quickly."""
    end_time = asyncio.get_running_loop().time() + max(0.0, seconds)
    while not stop_event.is_set():
        remaining = end_time - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(1.0, remaining))


def cleanup(player: PyTgCalls | None, client: TelegramClient | None, channel_id: int, loop: asyncio.AbstractEventLoop) -> None:
    if player is not None:
        try:
            player.leave_call(channel_id)
        except Exception as exc:
            log.info("Call cleanup skipped: %s", exc)
    if client is not None:
        try:
            if client.is_connected():
                client.disconnect()
        except Exception as exc:
            log.warning("Telegram disconnect warning: %s", exc)
    try:
        if not loop.is_closed():
            loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        pass
    try:
        if not loop.is_closed():
            loop.close()
    except Exception:
        pass
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass


def run() -> None:
    config = load_config()
    duration = get_audio_duration(config.audio_file)
    stop_event = threading.Event()

    def request_shutdown(signum: int, _frame: object) -> None:
        log.info("Received shutdown signal %s; stopping cleanly.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    log.info("Audio file: %s (%.2f seconds)", config.audio_file.name, duration)

    while not stop_event.is_set():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client: TelegramClient | None = None
        player: PyTgCalls | None = None
        try:
            client = TelegramClient(
                StringSession(config.session_string),
                config.api_id,
                config.api_hash,
                loop=loop,
                auto_reconnect=True,
                connection_retries=5,
                retry_delay=2,
            )
            loop.run_until_complete(client.connect())
            if not loop.run_until_complete(client.is_user_authorized()):
                raise RuntimeError(
                    "Telegram session is not authorized. Generate a fresh SESSION_STRING and update Render."
                )
            account = loop.run_until_complete(client.get_me())
            log.info("Connected to Telegram account.")

            player = PyTgCalls(client)
            player.start()
            log.info("PyTgCalls is ready; starting continuous audio playback.")

            while not stop_event.is_set():
                play_started = time.monotonic()
                player.play(config.channel_id, str(config.audio_file))
                play_setup_seconds = time.monotonic() - play_started
                log.info("Audio playback started; the file will repeat automatically.")

                wait_seconds = max(0.0, duration - play_setup_seconds) + config.replay_gap_seconds
                loop.run_until_complete(wait_until_repeat(wait_seconds, stop_event))
                if not stop_event.is_set():
                    log.info("Audio ended; replaying from the beginning.")

        except Exception:
            if not stop_event.is_set():
                log.exception("Playback or Telegram connection failed; will retry.")
        finally:
            cleanup(player, client, config.channel_id, loop)

        if not stop_event.is_set():
            log.info("Retrying in %s seconds.", config.reconnect_delay_seconds)
            stop_event.wait(config.reconnect_delay_seconds)

    log.info("Player stopped.")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        log.error("Startup error: %s", exc)
        raise SystemExit(1) from exc
