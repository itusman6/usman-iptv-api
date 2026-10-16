#!/usr/bin/env python3
"""
Rebuild playlists/index.m3u from every other .m3u in playlists/.
Always stamps a fresh `# generated:` header.

Run from repo root: python scripts/build_index.py
"""
from datetime import datetime, timezone
from pathlib import Path


def count_channels(path: Path) -> int:
    return sum(
        1
        for l in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if l.startswith("#EXTINF")
    )


def main() -> int:
    d = Path("playlists")
    d.mkdir(exist_ok=True)

    files = sorted(p for p in d.glob("*.m3u") if p.name != "index.m3u")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = ["#EXTM3U", f"# generated: {now}"]
    for f in files:
        n = count_channels(f)
        lines.append(
            f'#EXTINF:-1 tvg-id="{f.stem}" tvg-name="{f.stem}" '
            f'tvg-logo="https://i.imgur.com/c7CDrvS.png" '
            f'group-title="Playlists",{f.stem}  [{n} ch]'
        )
        lines.append(f"./{f.name}")

    (d / "index.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"index.m3u -> {len(files)} entries @ {now}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
