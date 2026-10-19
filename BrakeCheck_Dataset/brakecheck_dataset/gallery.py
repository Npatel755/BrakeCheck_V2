"""Build a color-first local review gallery; preserve binary masks as data."""

from __future__ import annotations

from collections import Counter
from html import escape
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def human_overlay(original: Image.Image, mask: Image.Image) -> Image.Image:
    photo = np.asarray(original.convert("RGB")).copy()
    labels = np.asarray(mask.convert("RGB"))
    lane = np.all(labels == (255, 0, 0), axis=2)
    road = np.all(labels == (64, 32, 32), axis=2)
    photo[road] = (photo[road] * .78 + np.array((0, 200, 255)) * .22).astype(np.uint8)
    photo[lane] = (photo[lane] * .25 + np.array((255, 0, 255)) * .75).astype(np.uint8)
    return Image.fromarray(photo)


def make_card(root: Path, item: dict, destination: Path) -> None:
    width, height = 320, 240
    with Image.open(root / item["image"]) as image:
        original = image.convert("RGB")
    with Image.open(root / item["original_mask"]) as image:
        source_mask = image.convert("RGB")
    predicted_overlay = root / "annotations/teacher_predictions/comma10k-segnet" / f"{item['image_id']}-overlay.jpg"
    with Image.open(predicted_overlay) as image:
        model_view = image.convert("RGB")
    panels = [original, model_view, human_overlay(original, source_mask)]
    card = Image.new("RGB", (width * 3, height + 34), (17, 23, 31))
    draw = ImageDraw.Draw(card)
    for index, (panel, label) in enumerate(zip(panels, ("Original camera image", "AI candidate overlay", "Existing human labels"))):
        panel = panel.resize((width, height), Image.Resampling.LANCZOS)
        card.paste(panel, (index * width, 34))
        draw.text((index * width + 10, 10), label, fill=(238, 245, 250))
    destination.parent.mkdir(parents=True, exist_ok=True)
    card.save(destination, quality=88, optimize=True)


def build_gallery(root: Path) -> dict:
    queue = root / "review_queue/comma-source-disagreements.jsonl"
    if not queue.is_file():
        raise FileNotFoundError("Run 'review-queue' before building the gallery")
    cases = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines() if line.strip()]
    destination = root / "review_gallery"
    destination.mkdir(parents=True, exist_ok=True)
    cards = []
    for item in cases:
        card_file = destination / "cards" / f"{item['image_id']}.jpg"
        if not card_file.exists():
            make_card(root, item, card_file)
        image_id = escape(item["image_id"], quote=True)
        priority = escape(item["priority"], quote=True)
        original = escape("../" + item["image"], quote=True)
        overlay = escape("../annotations/teacher_predictions/comma10k-segnet/" + item["image_id"] + "-overlay.jpg", quote=True)
        cards.append(f'''<article class="card" data-priority="{priority}">
          <div class="card-head"><strong>{image_id}</strong><span class="badge {priority}">{priority.replace('_', ' ')}</span></div>
          <a href="{overlay}" target="_blank" aria-label="Open full-size overlay for {image_id}">
            <img loading="lazy" src="cards/{image_id}.jpg" alt="Original photo, AI overlay, and human labels for {image_id}"></a>
          <div class="card-foot">Lane agreement: {item['lane']['iou']:.1%}
            <span><a href="{original}" target="_blank">Original</a> · <a href="{overlay}" target="_blank">Full overlay</a></span></div>
        </article>''')
    counts = Counter(item["priority"] for item in cases)
    html = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>BrakeCheck Lane Review</title><style>
    :root {color-scheme: dark;font-family: system-ui, sans-serif;background:#0d141d;color:#f3f7fa}
    body{max-width:1440px;margin:auto;padding:24px}h1{margin-bottom:8px}
    .intro{color:#b4c6d4;max-width:900px;line-height:1.5}
    .toolbar{position:sticky;top:0;background:#0d141df2;padding:14px 0;z-index:2;display:flex;gap:10px;flex-wrap:wrap}
    button{background:#213449;color:#e7f5ff;border:1px solid #405973;border-radius:8px;padding:9px 12px;cursor:pointer}
    button.active{background:#1d78c1;border-color:#76c5ff}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:16px}
    .card{border:1px solid #2a4155;border-radius:12px;overflow:hidden;background:#152230}
    .card[hidden]{display:none}.card img{width:100%;display:block}.card-head,.card-foot{padding:10px 13px;display:flex;justify-content:space-between;gap:10px;align-items:center}
    .card-head strong{font-size:13px;overflow-wrap:anywhere}.card-foot{color:#b7c8d5;font-size:13px}
    a{color:#82c8ff}.badge{border-radius:20px;padding:3px 9px;font-size:12px;background:#304254}
    .badge.high{background:#79383d}.badge.medium{background:#715525}
    .badge.random_qc{background:#315965}
    </style></head><body><h1>BrakeCheck Lane Review</h1>
    <p class="intro">Each card shows the <b>original color frame</b>, the provisional AI overlay,
    and the existing human-label overlay. Cyan marks road; magenta marks lane paint.
    White-on-black PNGs are machine-readable masks, not photos. Open a card for a full-size overlay.
    Agreement ranks review priority; it is not an independent accuracy score or an approval decision.</p>
    <div class="toolbar" id="filters">'''
    for value, label in (("all", "All"), ("high", "High"), ("medium", "Medium"),
                         ("random_qc", "Random QC"), ("low", "Low")):
        count = len(cases) if value == "all" else counts[value]
        html += f'<button type="button" data-filter="{value}" class="{"active" if value == "all" else ""}">{label} ({count})</button>'
    html += '</div><main class="grid">' + "\n".join(cards) + '''</main>
    <script>document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{
      const choice=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));
      document.querySelectorAll('.card').forEach(card=>card.hidden=choice!=='all'&&card.dataset.priority!==choice);
    }));</script></body></html>'''
    (destination / "index.html").write_text(html, encoding="utf-8")
    return {"cards": len(cases), "index": str(destination / "index.html"), "priority_counts": dict(counts)}
