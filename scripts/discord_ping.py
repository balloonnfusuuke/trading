#!/usr/bin/env python3
"""
Discord Webhook が届くかだけを検証する（main.py を経由しない）。
GitHub Actions またはローカルで:

  DISCORD_WEBHOOK_URL="https://..." python scripts/discord_ping.py
"""

from __future__ import annotations

import os
import sys

import requests


def main() -> int:
    url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        print("ERROR: 環境変数 DISCORD_WEBHOOK_URL が空です。", file=sys.stderr)
        return 1

    # ログ用（トークンは出さない）
    if "/webhooks/" in url:
        pre, _, rest = url.partition("/webhooks/")
        wh_id = rest.split("/")[0] if rest else "?"
        print(f"送信先ホスト: {pre.split('//')[-1]}  webhook_id={wh_id}  URL長={len(url)}")
    else:
        print(f"警告: 典型的な Discord Webhook URL ではありません（/webhooks/ なし） URL長={len(url)}")

    r = requests.post(
        url,
        json={"content": "🔔 **Trading 接続テスト**（`discord_ping.py` / GitHub Actions）"},
        timeout=20,
    )
    print(f"HTTP ステータス: {r.status_code}")
    if r.text:
        print(f"応答本文(先頭200文字): {r.text[:200]!r}")

    if r.status_code in (200, 204):
        print("OK: Discord 側は受け付けました。チャンネルを確認してください。")
        return 0

    print("NG: 上記ステータスは失敗です。Webhook URL の再発行や Secret の貼り直しを試してください。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
