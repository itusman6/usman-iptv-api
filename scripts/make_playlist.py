
#!/usr/bin/env python3
"""
Generate converge.m3u from Supabase.

Features:
- Concurrent channel retrieval with thread-local HTTP sessions
- Retries for temporary HTTP failures
- CryptoJS/OpenSSL Salted__ AES-256-CBC decryption
- Kodi HLS/MPD metadata and required stream headers
- Ordered playlist output and generation timestamp
- Validation before replacing the output file
"""

import os
import sys
import time
import base64
import hashlib
import logging
import threading

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


# ============================================================
# CONFIGURATION
# ============================================================

PROJECT_REF = "tkqgxdfxortnpstxedpy"
BASE = f"https://{PROJECT_REF}.supabase.co"

API_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
PASSPHRASE = os.environ.get("STREAM_PASSPHRASE", "")

OUT_FILE = Path("converge.m3u")
MAX_WORKERS = 8
MIN_CHANNELS = 10
HTTP_TIMEOUT = (10, 30)
MAX_RETRIES = 3

STREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gproject.netlify.app/",
    "Origin": "https://gproject.netlify.app",
}

thread_local = threading.local()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


# ============================================================
# HTTP SESSION
# ============================================================

def get_session() -> requests.Session:
    """Create one HTTP session per worker thread."""
    session = getattr(thread_local, "session", None)

    if session is None:
        session = requests.Session()
        session.headers.update({
            "apikey": API_KEY,
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Origin": "https://gproject.netlify.app",
            "Referer": "https://gproject.netlify.app/",
            "User-Agent": STREAM_HEADERS["User-Agent"],
        })

        adapter = HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
            max_retries=0,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        thread_local.session = session

    return session


def request_with_retries(method, url, **kwargs):
    """Retry temporary network failures and HTTP 5xx/429 responses."""
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = get_session().request(
                method,
                url,
                timeout=HTTP_TIMEOUT,
                **kwargs,
            )

            if response.status_code == 429 or response.status_code >= 500:
                status = response.status_code
                response.close()
                raise requests.HTTPError(
                    f"Temporary HTTP status {status}"
                )

            response.raise_for_status()
            return response

        except (requests.RequestException, ValueError) as exc:
            last_error = exc

            if attempt == MAX_RETRIES:
                break

            delay = min(2 ** (attempt - 1), 8)
            logging.warning(
                "Request attempt %d/%d failed: %s; retrying in %ds",
                attempt, MAX_RETRIES, exc, delay,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Request failed after {MAX_RETRIES} attempts: {last_error}"
    ) from last_error


# ============================================================
# DECRYPTION
# ============================================================

def decrypt_cryptojs(encrypted_b64: str, password: str) -> str:
    """
    Decrypt CryptoJS/OpenSSL salted AES-256-CBC data.

    Expected format:
        Base64("Salted__" + 8-byte salt + ciphertext)
    """
    if not encrypted_b64 or not password:
        return ""

    try:
        data = base64.b64decode(encrypted_b64, validate=True)

        if len(data) < 32 or data[:8] != b"Salted__":
            return ""

        salt = data[8:16]
        ciphertext = data[16:]

        if len(ciphertext) == 0 or len(ciphertext) % AES.block_size:
            return ""

        # OpenSSL EVP_BytesToKey using MD5.
        material = b""
        previous = b""

        while len(material) < 48:
            previous = hashlib.md5(
                previous + password.encode("utf-8") + salt
            ).digest()
            material += previous

        key = material[:32]
        iv = material[32:48]

        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = unpad(
            cipher.decrypt(ciphertext),
            AES.block_size,
        )

        return plaintext.decode("utf-8")

    except (
        ValueError,
        UnicodeDecodeError,
        base64.binascii.Error,
    ):
        return ""


# ============================================================
# SUPABASE API
# ============================================================

def get_all_channels() -> list:
    url = f"{BASE}/rest/v1/channels"

    params = {
        "select": (
            "id,name,logo_url,category_id,stream_type,"
            "sort_order,is_active"
        ),
        "order": "id.asc",
    }

    response = request_with_retries("GET", url, params=params)

    try:
        data = response.json()
    finally:
        response.close()

    if not isinstance(data, list):
        raise RuntimeError(
            "Supabase channel-list response was not a JSON array"
        )

    return data


def get_channel_details(channel_id: int) -> dict:
    url = f"{BASE}/functions/v1/get-channel"

    response = request_with_retries(
        "POST",
        url,
        json={"id": channel_id},
    )

    try:
        data = response.json()
    finally:
        response.close()

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Unexpected response for channel {channel_id}"
        )

    # Support either a direct object or a wrapped response.
    if isinstance(data.get("data"), dict):
        data = data["data"]

    return data


# ============================================================
# PLAYLIST HELPERS
# ============================================================

def classify_stream(
    stream_url: str,
    stream_type: str,
    drm_key_id: str,
    drm_key: str,
) -> str:
    url = (stream_url or "").lower()
    stream_type = (stream_type or "").lower()

    if ".mpd" in url:
        return "mpd"

    if ".m3u8" in url:
        return "hls"

    if stream_type in ("dash", "mpd"):
        return "mpd"

    if stream_type in ("hls", "m3u8", "live"):
        return "hls"

    return stream_type or "hls"


def kodi_header_props(headers: dict) -> str:
    return "&".join(
        f"{key}={quote(str(value), safe='')}"
        for key, value in headers.items()
    )


def format_entry(
    name: str,
    logo: str,
    group: str,
    url: str,
    kind: str,
    drm_key_id: str,
    drm_key: str,
) -> list:
    lines = [
        "#KODIPROP:inputstream.adaptive.stream_headers="
        + kodi_header_props(STREAM_HEADERS)
    ]

    if kind == "mpd":
        lines.append(
            "#KODIPROP:inputstream.adaptive.manifest_type=mpd"
        )

        # Add DRM metadata only when configured for authorized playback.
        if drm_key_id and drm_key:
            lines.append(
                "#KODIPROP:inputstream.adaptive.license_type=clearkey"
            )
            lines.append(
                "#KODIPROP:inputstream.adaptive.license_key="
                f"{drm_key_id}:{drm_key}"
            )
    else:
        lines.append(
            "#KODIPROP:inputstream.adaptive.manifest_type=hls"
        )

    # Escape double quotes in metadata values.
    def safe(value):
        return str(value or "").replace('"', "'").replace("\n", " ")

    meta = (
        f'tvg-id="{safe(name)}" '
        f'tvg-name="{safe(name)}" '
        f'tvg-logo="{safe(logo)}" '
        f'group-title="{safe(group)}"'
    )

    lines.append(f"#EXTINF:-1 {meta},{safe(name)}")
    lines.append(url)

    return lines


# ============================================================
# CHANNEL WORKER
# ============================================================

def process_channel(index: int, channel: dict) -> dict:
    channel_id = channel.get("id")
    name = channel.get("name") or f"Channel {channel_id}"

    if channel.get("is_active", True) is False:
        return {
            "idx": index,
            "name": name,
            "status": "inactive",
            "lines": None,
        }

    try:
        details = get_channel_details(channel_id)

        encrypted_url = details.get("stream_url", "")
        encrypted_kid = details.get("drm_key_id", "")
        encrypted_key = details.get("drm_key", "")

        stream_url = decrypt_cryptojs(encrypted_url, PASSPHRASE)

        if not stream_url:
            return {
                "idx": index,
                "name": name,
                "status": "decrypt_failed_or_missing_url",
                "lines": None,
            }

        # Preserve the API's DRM metadata for configured playback.
        drm_key_id = (
            decrypt_cryptojs(encrypted_kid, PASSPHRASE)
            if encrypted_kid else ""
        )
        drm_key = (
            decrypt_cryptojs(encrypted_key, PASSPHRASE)
            if encrypted_key else ""
        )

        stream_type = channel.get("stream_type") or "hls"

        kind = classify_stream(
            stream_url, stream_type, drm_key_id, drm_key
        )

        lines = format_entry(
            name=name,
            logo=channel.get("logo_url") or "",
            group=str(channel.get("category_id") or "General"),
            url=stream_url,
            kind=kind,
            drm_key_id=drm_key_id,
            drm_key=drm_key,
        )

        return {
            "idx": index,
            "name": name,
            "status": kind,
            "lines": lines,
        }

    except Exception as exc:
        return {
            "idx": index,
            "name": name,
            "status": f"error: {exc}",
            "lines": None,
        }


# ============================================================
# MAIN
# ============================================================

def main():
    if not API_KEY:
        raise RuntimeError(
            "SUPABASE_ANON_KEY environment variable is missing"
        )

    if not PASSPHRASE:
        raise RuntimeError(
            "STREAM_PASSPHRASE environment variable is missing"
        )

    started = time.time()

    logging.info("Fetching Supabase channel list")
    channels = get_all_channels()

    if not channels:
        raise RuntimeError(
            "Supabase returned zero channels; refusing to overwrite output"
        )

    logging.info(
        "Received %d channels; using %d workers",
        len(channels), MAX_WORKERS,
    )

    results = {}
    failures = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(process_channel, index, channel): index
            for index, channel in enumerate(channels, 1)
        }

        for future in as_completed(futures):
            index = futures[future]

            try:
                result = future.result()
            except Exception as exc:
                failures += 1
                logging.error("Worker %d failed: %s", index, exc)
                continue

            results[result["idx"]] = result

            if result["lines"]:
                logging.info(
                    "[%d/%d] %s: %s",
                    len(results), len(channels),
                    result["name"], result["status"],
                )
            else:
                failures += 1
                logging.warning(
                    "[%d] %s: %s",
                    index, result["name"], result["status"],
                )

    output = [
        "#EXTM3U",
        "# generated: "
        + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ]

    written = 0

    for index in range(1, len(channels) + 1):
        result = results.get(index)

        if result and result["lines"]:
            output.extend(result["lines"])
            written += 1

    logging.info(
        "Successful channels: %d; failed/inactive: %d",
        written, failures,
    )

    if written < MIN_CHANNELS:
        raise RuntimeError(
            f"Only {written} usable channels; minimum is {MIN_CHANNELS}. "
            "Existing output was not overwritten."
        )

    # Write a temporary file first so a failed write won't destroy
    # an existing valid playlist.
    temporary = OUT_FILE.with_suffix(".m3u.tmp")
    temporary.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUT_FILE)

    logging.info(
        "Wrote %s with %d entries in %.1f seconds",
        OUT_FILE, written, time.time() - started,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.error("Interrupted")
        sys.exit(130)
    except Exception:
        logging.exception("Playlist generation failed")
        sys.exit(1)
