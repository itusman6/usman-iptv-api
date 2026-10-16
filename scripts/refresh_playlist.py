#!/usr/bin/env python3
"""
Copy a source .m3u to a destination, always stamping a fresh `# updated:`
header. Strips any pre-existing #EXTM3U / # generated: / # updated: lines
from the source so timestamps don't stack up.

Usage: python scripts/refresh_playlist.py <src.m3u> <dst.m3u>
"""
import sys
from datetime import datetime, timezone
from pathlib import Path


def strip_headers(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("#EXTM3U"):
            continue
        if s.startswith("# generated:") or s.startswith("# updated:"):
            continue
        out.append(ln.rstrip())
    # Trim trailing blank lines
    while out and out[-1] == "":
        out.pop()
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: refresh_playlist.py <src> <dst>", file=sys.stderr)
        return 2

    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    if not src.is_file():
        print(f"error: source not found: {src}", file=sys.stderr)
        return 1

    raw = src.read_text(encoding="utf-8", errors="replace").splitlines()
    body = strip_headers(raw)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = ["#EXTM3U", f"# updated: {now}", *body]

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
