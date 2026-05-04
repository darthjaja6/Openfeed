#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
    "x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def demo_html(title: str, kicker: str, body: str, accent: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      align-content: end;
      gap: 18px;
      padding: 28px;
      color: #f8fafc;
      background: linear-gradient(160deg, #111827, #1f2937 46%, {accent});
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    p {{ margin: 0; max-width: 30rem; font-size: 17px; line-height: 1.5; }}
    .kicker {{ color: #cbd5e1; font-size: 13px; text-transform: uppercase; letter-spacing: .08em; }}
    h1 {{ margin: 0; max-width: 11ch; font-size: 48px; line-height: .98; letter-spacing: 0; }}
  </style>
</head>
<body>
  <div class="kicker">{kicker}</div>
  <h1>{title}</h1>
  <p>{body}</p>
</body>
</html>"""


def queue_item(idx: int, title: str, kicker: str, body: str, accent: str, thumb: Path) -> dict:
    created_at = now_iso()
    content_id = f"local-demo-{idx:03d}"
    html = demo_html(title, kicker, body, accent)
    return {
        "content": {
            "content_id": content_id,
            "source_id": "local-quickstart",
            "topic": "demo",
            "platform": "web",
            "fetched_at": created_at,
            "source_of": "patrol",
            "web": {
                "title": title,
                "summary": body,
                "link": f"https://example.com/openfeed/local-quickstart/{idx}",
                "published_at": created_at,
                "full_body": body,
            },
        },
        "score": {
            "popularity": 1.0 - idx * 0.05,
            "engagement": 0.9,
            "freshness": 1.0,
            "preference": 0.95,
            "composite": 0.95 - idx * 0.03,
        },
        "rank_score": 0.95 - idx * 0.03,
        "admitted_at": created_at,
        "rendered_card": {
            "title": title,
            "content_subtype": "html",
            "html": html,
            "thumbnail_path": str(thumb),
            "render_fingerprint": f"local-quickstart-{idx}",
        },
    }


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "output").resolve()
    state = output / "state"
    ledgers = output / "ledgers"
    assets = state / "local_quickstart_assets"
    assets.mkdir(parents=True, exist_ok=True)
    ledgers.mkdir(parents=True, exist_ok=True)

    # Keep reruns deterministic: this example owns its output directory.
    shutil.rmtree(state / "local_web", ignore_errors=True)
    for path in (ledgers / "history.jsonl", ledgers / "feedback.jsonl", state / "feedback_state.json"):
        if path.exists():
            path.unlink()

    thumb = assets / "thumbnail.png"
    thumb.write_bytes(base64.b64decode(PNG_1X1))

    cards = [
        ("Local Queue", "Bootstrap", "A seeded queue item moved through the real OpenFeed push task.", "#0f766e"),
        ("Browser Events", "Feedback", "Views, dwell time, likes, saves, and shares are recorded locally.", "#7c3aed"),
        ("No Account", "Protocol", "The same HTTP consumer path can point at a custom client later.", "#be123c"),
    ]
    queue = {
        "generated_at": now_iso(),
        "topics": {
            "demo": [
                queue_item(idx, title, kicker, body, accent, thumb)
                for idx, (title, kicker, body, accent) in enumerate(cards, start=1)
            ],
        },
    }
    queue_path = state / "queue.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"seeded {len(cards)} demo cards: {queue_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
