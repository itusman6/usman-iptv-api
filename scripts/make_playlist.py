#!/usr/bin/env python3
"""
Build converge.m3u — Kodi-compatible playlist with:
  - channel name
  - channel logo (tvg-logo)
  - HLS stream URL
  - DRM info (Kodi property) if MPD/DRM
  - required HTTP headers (Kodi property)

Fast threaded version. Also stamps a generation timestamp into the
playlist header so every run produces a git diff.
"""

import base64
import hashlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


# ============================================================
# CONFIG
# ============================================================
PROJECT_REF = "tkqgxdfxortnpstxedpy"
BASE = f"https://{PROJECT_REF}.supabase.co"

API_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
           "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRrcWd4ZGZ4b3J0bnBzdHhlZHB5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzcxMjAwMjEsImV4cCI6MjA5MjY5NjAyMX0."
           "N6pkXrRdMH_hzp5FNf8yEVSpk6-AWZMG-7XF5V5genY")

PASSPHRASE = "eeboot41k2a.p.t"
OUT_FILE = "converge.m3u"

# How many channels to fetch in parallel.
MAX_WORKERS = 16

STREAM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/154.0.0.0 Safari/537.36"),
    "Referer": "https://gproject.netlify.app/",
    "Origin":  "https://gproject.netlify.app",
}

API_HEADERS = {
    "apikey": API_KEY,
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "Origin": "https://gproject.netlify.app",
    "Referer": "https://gproject.netlify.app/",
}

SESSION = requests.Session()
SESSION.headers.update(API_HEADERS)


# ============================================================
# DECRYPT (CryptoJS / OpenSSL Salted__)
# ============================================================
def decrypt_cryptojs(encrypted_b64: str, password: str) -> str:
    if not encrypted_b64:
        return ""
    try:
        data = base64.b64decode(encrypted_b64)
    except Exception:
        return ""
    if data[:8] != b"Salted__":
        return ""

    salt = data[8:16]
    ciphertext = data[16:]

    key = b""
    prev = b""
    while len(key) < 32:
        prev = hashlib.md5(prev + password.encode("utf-8") + salt).digest()
        key += prev
    key = key[:32]
    iv = hashlib.md5(prev + password.encode("utf-8") + salt).digest()[:16]

    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        pt = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return pt.decode("utf-8")
    except Exception:
        return ""


# ============================================================
# API
# ============================================================
def get_all_channels() -> list:
    url = f"{BASE}/rest/v1/channels"
    params = {
        "select": "id,name,logo_url,category_id,stream_type,sort_order,is_active",
        "order":  "id.asc",
    }
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_channel_details(channel_id: int) -> dict:
    url = f"{BASE}/functions/v1/get-channel"
    last_exc = None
    for attempt in range(3):
        try:
            r = SESSION.post(url, json={"id": channel_id}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            time.sleep(0.5 * (attempt + 1))
    raise last_exc


# ============================================================
# HELPERS
# ============================================================
def classify_stream(stream_url: str, stream_type: str,
                    drm_key_id: str, drm_key: str) -> str:
    u = (stream_url or "").lower()
    if u.endswith(".mpd") or ".mpd?" in u or "manifest.mpd" in u:
        return "mpd"
    if u.endswith(".m3u8") or ".m3u8?" in u:
        return "hls"
    if stream_type in ("dash", "mpd"):
        return "mpd"
    if stream_type in ("hls", "m3u8", "live"):
        return "hls"
    return stream_type or "hls"


def has_drm(drm_key_id: str, drm_key: str) -> bool:
    return bool(drm_key_id and drm_key)


def kodi_header_props(headers: dict) -> str:
    return "&".join(f"{k}={v}" for k, v in headers.items())


def format_entry(cid: int, name: str, logo: str, group: str, url: str,
                 kind: str, drm_key_id: str, drm_key: str) -> list:
    lines = []

    meta = [
        f'tvg-id="{cid}"',
        f'tvg-name="{name}"',
        f'tvg-logo="{logo}"',
        f'group-title="{group}"',
    ]

    lines.append(
        "#KODIPROP:inputstream.adaptive.stream_headers="
        + kodi_header_props(STREAM_HEADERS)
    )

    if kind == "mpd" and has_drm(drm_key_id, drm_key):
        lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
        lines.append(
            f"#KODIPROP:inputstream.adaptive.license_key={drm_key_id}:{drm_key}"
        )
        lines.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
    elif kind == "mpd":
        lines.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
    else:
        lines.append("#KODIPROP:inputstream.adaptive.manifest_type=hls")

    lines.append("#EXTINF:-1 " + " ".join(meta) + f",{name}")
    lines.append(url)
    return lines


# ============================================================
# WORKER
# ============================================================
def process_channel(idx: int, ch: dict) -> dict:
    cid    = ch.get("id")
    name   = ch.get("name") or f"Channel {cid}"
    logo   = ch.get("logo_url") or ""
    group  = ch.get("category_id") or "General"
    stype  = ch.get("stream_type") or "hls"
    active = ch.get("is_active", True)

    if not active:
        return {"idx": idx, "name": name, "status": "inactive", "lines": None}

    try:
        details = get_channel_details(cid)
    except Exception as e:
        return {"idx": idx, "name": name, "status": f"api_error: {e}", "lines": None}

    enc_url = details.get("stream_url", "")
    enc_kid = details.get("drm_key_id", "")
    enc_key = details.get("drm_key", "")

    stream_url = decrypt_cryptojs(enc_url, PASSPHRASE)
    drm_key_id = decrypt_cryptojs(enc_kid, PASSPHRASE) if enc_kid else ""
    drm_key    = decrypt_cryptojs(enc_key, PASSPHRASE) if enc_key else ""

    if not stream_url:
        return {"idx": idx, "name": name, "status": "decrypt_failed", "lines": None}

    kind = classify_stream(stream_url, stype, drm_key_id, drm_key)
    drm  = has_drm(drm_key_id, drm_key)

    lines = format_entry(
        cid=cid, name=name, logo=logo, group=group,
        url=stream_url, kind=kind,
        drm_key_id=drm_key_id, drm_key=drm_key,
    )

    return {"idx": idx, "name": name,
            "status": f"{kind}{' DRM' if drm else ''}",
            "lines": lines}


# ============================================================
# MAIN
# ============================================================
def main():
    t0 = time.time()
    print("[*] Fetching channel list...")
    channels = get_all_channels()
    total = len(channels)
    print(f"[+] Got {total} channel(s) — {MAX_WORKERS} workers\n")

    results: dict[int, dict] = {}
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(process_channel, i, ch)
                   for i, ch in enumerate(channels, 1)]
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                print(f"[!] worker error: {e}")
                continue
            results[res["idx"]] = res
            done += 1
            print(f"[{done:4d}/{total}] {res['name']:<45} {res['status']}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_lines = [
        "#EXTM3U",
        f"# generated: {now}",
    ]
    written = 0
    for i in range(1, total + 1):
        res = results.get(i)
        if not res or not res["lines"]:
            continue
        out_lines.extend(res["lines"])
        written += 1

    Path(OUT_FILE).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"\n[✓] Wrote {OUT_FILE}  ({written} entries)  in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Aborted by user")
        sys.exit(1)
