"""Publish a read-only visual report of registered SAM 3 predictions.

This performs no inference and never opens a review API or a writable registry.
Its only writes are beneath the ignored pipeline ``test-results`` directory.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import math
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT.parent.parent / "BrakeCheck_Dataset"
RESULTS = ROOT / "test-results"


def escaped(value):
    return html.escape(str(value), quote=True)


def data_url(raw, mime="image/png"):
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")


def json_file(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def probability_tint(path, color, threshold=.35):
    """Return a visual map of recorded probability bytes; no photo is edited."""
    if not path or not Path(path).is_file():
        return None
    from PIL import Image
    with Image.open(path) as original:
        probability = original.convert("L")
    alpha = probability.point(lambda value: round(value * .30) if value / 255 >= threshold else 0)
    tint = Image.new("RGBA", probability.size, (*color, 0))
    tint.putalpha(alpha)
    buffer = io.BytesIO()
    tint.save(buffer, format="PNG")
    return data_url(buffer.getvalue())


def latest_candidates(data_root, split, limit, version=None, teacher="sam3"):
    db_path = data_root / "metadata.sqlite"
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    filters, values = ["a.kind='teacher'", "a.producer=?"], [teacher]
    if split != "all":
        filters.append("i.split=?")
        values.append(split)
    if version:
        filters.append("a.model_version=?")
        values.append(version)
    # URI read-only mode avoids Registry's schema/PRAGMA initialization.
    connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT * FROM (
              SELECT i.image_id,i.path AS image_path,i.width,i.height,i.split,i.review_status,i.locked,
                     a.path AS metadata_path,a.model_version,a.checkpoint_sha,a.license_ids,a.created_at,
                     ROW_NUMBER() OVER (PARTITION BY i.image_id ORDER BY a.created_at DESC,a.id DESC) AS ordinal
              FROM images i JOIN annotations a ON i.image_id=a.image_id
              WHERE """ + " AND ".join(filters) + ") WHERE ordinal=1 ORDER BY created_at DESC,image_id", values).fetchall()
    finally:
        connection.close()
    samples, skipped = [], []
    for row in rows:
        if len(samples) == limit:
            break
        row = dict(row)
        image_id = row["image_id"]
        geometry_path = data_root / "lane_geometry" / "teachers" / teacher / (image_id + ".json")
        try:
            metadata, metadata_sha = json_file(row["metadata_path"])
            geometry, geometry_sha = json_file(geometry_path)
            if geometry.get("teacher_version") != row["model_version"]:
                raise ValueError("Registered prediction and current geometry have different versions; an update may be in progress")
            if not Path(row["image_path"]).is_file():
                raise FileNotFoundError("Original photo is missing")
        except (OSError, ValueError, TypeError) as exc:
            skipped.append({"image_id": image_id, "reason": str(exc)})
            continue
        samples.append({
            "image_id": image_id, "split": row["split"], "width": row["width"], "height": row["height"],
            "version": row["model_version"], "checkpoint_sha": row["checkpoint_sha"],
            "license_ids": json.loads(row["license_ids"] or "[]"), "registered_at": row["created_at"],
            "review_status": row["review_status"], "human_locked": bool(row["locked"]),
            "image_path": row["image_path"], "metadata_path": row["metadata_path"], "geometry_path": str(geometry_path),
            "metadata_sha256": metadata_sha, "geometry_sha256": geometry_sha,
            "model_meta": metadata.get("meta", {}), "maps": metadata.get("maps", {}), "geometry": geometry,
        })
    return samples, skipped


def polyline(points, color, label, width, height, dashed=False):
    clean = []
    for point in points or []:
        if len(point) < 2:
            continue
        x, y = point[:2]
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or not math.isfinite(x) or not math.isfinite(y):
            continue
        if 0 <= x < width and 0 <= y < height:
            clean.append((float(x), float(y)))
    if len(clean) < 2:
        return ""
    coordinates = " ".join(f"{x:.4f},{y:.4f}" for x, y in clean)
    x, y = max(clean, key=lambda point: point[1])
    stroke = max(width / 390, 2.5)
    dash = f' stroke-dasharray="{stroke * 4:.2f} {stroke * 2:.2f}"' if dashed else ""
    return (f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="{stroke:.2f}"'
            f' stroke-linejoin="round" stroke-linecap="round"{dash}/>'
            f'<text x="{min(x + stroke * 2, width - 90):.2f}" y="{max(y - stroke * 3, 22):.2f}"'
            f' fill="{color}" stroke="#07100c" stroke-width="{stroke:.2f}" paint-order="stroke"'
            f' font-size="{max(width / 55, 16):.2f}" font-family="system-ui" font-weight="700">{escaped(label)}</text>')


def sample_html(sample, include_road_mask):
    from PIL import Image
    photo_path = Path(sample["image_path"])
    with Image.open(photo_path) as photo:
        width, height = photo.size
        mime = Image.MIME.get(photo.format, "image/png")
    photo = data_url(photo_path.read_bytes(), mime)
    geometry, meta, maps = sample["geometry"], sample["model_meta"], sample["maps"]
    overlay = [f'<image href="{photo}" width="{width}" height="{height}"/>']
    if include_road_mask:
        tint = probability_tint(maps.get("road"), (62, 210, 170), meta.get("road_geometry_threshold", .35))
        if tint:
            overlay.append(f'<image class="road-mask" href="{tint}" width="{width}" height="{height}"/>')
    occlusion = probability_tint(maps.get("occlusion"), (255, 82, 100), meta.get("occlusion_geometry_threshold", .5))
    if occlusion:
        overlay.append(f'<image class="vehicle-mask" href="{occlusion}" width="{width}" height="{height}"/>')
    colors = {1: "#ffc55d", 2: "#ff7777", 3: "#75d8ff", 4: "#dcadff"}
    painted = 0
    for lane in geometry.get("lanes", []):
        if lane.get("slot"):
            line = polyline(lane.get("polyline_points") or lane.get("points"), colors.get(lane["slot"], "#fff"),
                            "Paint " + str(lane["slot"]), width, height)
            if line:
                painted += 1
                overlay.append(line)
    edge_states = []
    for side, color in (("left", "#ffe6a0"), ("right", "#aaffce")):
        edge = geometry.get("road_edges", {}).get(side, {})
        status = edge.get("status", "not_requested")
        label = side.capitalize() + " edge: " + status
        if status == "visible":
            label += " / " + edge.get("kind", "unspecified")
            overlay.append(polyline(edge.get("points"), color, "Edge " + side[0].upper(), width, height, True))
        elif edge.get("reason"):
            label += " — " + edge["reason"]
        edge_states.append(label)
    counters = " · ".join(f"{name}: {meta.get(key, 'not recorded')}" for name, key in (
        ("Image encoder forwards", "image_encoder_forwards"), ("Prompt forwards", "prompt_forwards"), ("Model forwards", "model_forwards")))
    independence = f"source_guided={json.dumps(meta.get('source_guided', 'not recorded'))}; reference_inputs={json.dumps(meta.get('reference_inputs', 'not recorded'))}"
    information = {key: sample[key] for key in ("version", "checkpoint_sha", "metadata_path", "geometry_path", "metadata_sha256", "geometry_sha256", "license_ids")}
    information["model_meta"] = meta
    return (f'<section><h2>{escaped(sample["image_id"])} <span>{escaped(sample["split"])}</span></h2>'
            '<div class="comparison"><figure><figcaption>Original photo</figcaption>'
            f'<img src="{photo}" alt="Original {escaped(sample["image_id"])}" width="{width}" height="{height}"></figure>'
            '<figure><figcaption>Automatic model proposals</figcaption>'
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="SAM 3 paint and road edge candidates">{"".join(overlay)}</svg></figure></div>'
            f'<p><b>{painted} drawable painted lines.</b> {escaped(" · ".join(edge_states))}</p>'
            f'<p class="mono">{escaped(independence)}</p><p>{escaped(counters)}</p>'
            f'<p class="version">Version: {escaped(sample["version"])}</p>'
            '<details><summary>Exact registered prediction metadata</summary>'
            f'<pre>{escaped(json.dumps(information, indent=2))}</pre></details></section>')


def render_report(samples, skipped, generated, include_road_mask):
    content = "".join(sample_html(sample, include_road_mask) for sample in samples)
    if not samples:
        content = '<section><p>No matching complete registered predictions were found.</p></section>'
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>BrakeCheck SAM 3 visual inspection</title><style>'
            '*{box-sizing:border-box}body{margin:0;background:#0d1512;color:#e4f2e9;font:15px/1.5 system-ui}main{max-width:1780px;margin:auto;padding:28px}'
            'h1{margin:0 0 10px}header p{max-width:1000px;color:#b0c5b9}section{margin:28px 0;background:#14231b;border:1px solid #34503f;border-radius:14px;padding:18px}'
            'h2{font-size:18px;margin-top:0}h2 span{font-weight:400;color:#9bc9b0}figure{margin:0;min-width:0}figcaption{margin-bottom:8px;color:#b8cfc0}'
            '.comparison{display:grid;grid-template-columns:1fr 1fr;gap:16px}.comparison img,.comparison svg{display:block;width:100%;height:auto;border-radius:8px}'
            '.mono,.version,pre{font-family:ui-monospace,monospace;overflow-wrap:anywhere}.mono,.version{font-size:12px;color:#badbc7}pre{white-space:pre-wrap;font-size:12px}'
            'label{display:inline-block;margin:0 20px 8px 0}body.hide-road .road-mask{display:none}.vehicle-mask{display:none}body.show-vehicles .vehicle-mask{display:block}'
            'summary{cursor:pointer;color:#a6e2c0}details{margin:12px 0}.note{color:#d9c798}@media(max-width:760px){main{padding:14px}.comparison{grid-template-columns:1fr}}'
            '</style><body><main><header><h1>SAM 3 visual inspection</h1>'
            f'<p>{len(samples)} registered predictions · generated {escaped(generated)}. Originals and saved model outputs were read without changing reviews.</p>'
            '<p class="note">Automatic candidates require human review. A predicted road contour does not establish a physical curb boundary. Unknown edges remain uncertain. This report does not measure accuracy.</p>'
            '<label><input id="road" type="checkbox" checked> Tint predicted road probability</label>'
            '<label><input id="vehicles" type="checkbox"> Tint predicted vehicle exclusions</label>'
            '<p>Solid colored lines: painted slots 1–4. Dashed pale lines: model road-edge candidates. Green tint: model road map. Red tint: model vehicle map.</p></header>'
            + content + (f'<details><summary>{len(skipped)} incomplete candidates skipped</summary><pre>{escaped(json.dumps(skipped, indent=2))}</pre></details>' if skipped else '')
            + '</main><script>document.getElementById("road").onchange=e=>document.body.classList.toggle("hide-road",!e.target.checked);'
            'document.getElementById("vehicles").onchange=e=>document.body.classList.toggle("show-vehicles",e.target.checked);</script></body></html>')


def screenshot_report(index, output):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            executable = next((Path(path) for path in (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe") if Path(path).is_file()), None)
            browser = playwright.chromium.launch(executable_path=str(executable) if executable else None, headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1100}, device_scale_factor=1)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(index.as_uri(), wait_until="load")
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(output / "inspection.png"), full_page=True)
            page.locator("#vehicles").check()
            page.screenshot(path=str(output / "inspection-vehicle-exclusions.png"), full_page=True)
            browser.close()
        return {"state": "rendered", "javascript_errors": errors}
    except Exception as exc:
        return {"state": "unavailable", "reason": type(exc).__name__ + ": " + str(exc)[:500]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--split", choices=("val", "train", "test", "all"), default="val")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--teacher", choices=("sam3", "comma_segnet_fresh", "dinov3_seg"), default="sam3")
    parser.add_argument("--version", help="Only inspect this exact registered teacher version")
    parser.add_argument("--no-road-mask", action="store_true")
    parser.add_argument("--no-screenshot", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 25:
        parser.error("--limit must be between 1 and 25")
    samples, skipped = latest_candidates(args.data_root.resolve(), args.split, args.limit, args.version, args.teacher)
    now = datetime.now(timezone.utc)
    generated = now.isoformat(timespec="seconds")
    output = RESULTS / ("sam3-real-" + now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    output.mkdir(parents=True)
    index = output / "index.html"
    html = render_report(samples, skipped, generated, not args.no_road_mask)
    if args.teacher != "sam3":
        html = html.replace("SAM 3", "DINOv3 7B trained decoder" if args.teacher == "dinov3_seg" else "Fresh road-marking model")
    index.write_text(html, encoding="utf-8")
    report = {"generated_at_utc": generated, "read_only": True, "inference_run": False, "split": args.split,
              "samples": samples, "skipped": skipped, "html": str(index),
              "browser": {"state": "not_requested"} if args.no_screenshot else screenshot_report(index, output)}
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"html": str(index), "png": str(output / "inspection.png") if (output / "inspection.png").is_file() else None,
                      "samples": len(samples), "browser": report["browser"]}))


if __name__ == "__main__":
    main()
