"""Optional CARLA synthetic generator. Labels come from the simulator, not from a model:

* semantic camera -> road (CityObjectLabel.Roads) and painted lines (CityObjectLabel.RoadLines);
* map waypoints -> the four lane boundaries around the ego lane, projected into the image, with the
  boundary marked physical only where CARLA says a marking exists (LaneMarkingType != NONE) and the
  semantic camera sees paint at that pixel.

Needs a running CARLA server (0.9.14+ recommended) and the matching `carla` Python package.
Everything it writes is tagged synthetic=1 and kept apart from real images in exports.
"""
from __future__ import annotations

import json
import logging
import math
import queue
import random
from pathlib import Path

import numpy as np

from ..config import Settings
from ..db import Registry
from ..licenses import LicenseRegistry
from ..quality import write_png
from .base import register_image

log = logging.getLogger(__name__)

WEATHERS = ["ClearNoon", "CloudyNoon", "WetNoon", "WetCloudyNoon", "MidRainyNoon", "HardRainNoon", "SoftRainNoon",
            "ClearSunset", "CloudySunset", "WetSunset", "HardRainSunset", "ClearNight", "CloudyNight", "WetNight",
            "HardRainNight"]


def intrinsics(width: int, height: int, fov_deg: float) -> np.ndarray:
    f = width / (2.0 * math.tan(math.radians(fov_deg) / 2.0))
    return np.array([[f, 0, width / 2.0], [0, f, height / 2.0], [0, 0, 1.0]])


def project(points_world: np.ndarray, world_to_camera: np.ndarray, K: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """UE world points (N,3) -> pixel coords (N,2) and depth (N,). UE camera axes: x fwd, y right, z up."""
    pts = np.c_[points_world, np.ones(len(points_world))].T          # 4xN
    cam = world_to_camera @ pts                                      # 4xN in UE camera frame
    std = np.array([cam[1], -cam[2], cam[0]])                         # -> (right, down, forward)
    depth = std[2]
    uv = (K @ std)[:2] / np.where(depth > 1e-6, depth, np.nan)
    return uv.T, depth


def lane_boundaries(world_map, ego_location, carla, max_dist: float = 60.0, step: float = 2.0) -> list[dict]:
    """Left-left, left, right, right-right boundaries of the ego road as 3D polylines with marking type."""
    wp = world_map.get_waypoint(ego_location, project_to_road=True, lane_type=carla.LaneType.Driving)
    if wp is None:
        return []
    left = wp.get_left_lane()
    right = wp.get_right_lane()
    same_dir = lambda other: other is not None and other.lane_type == carla.LaneType.Driving and \
        (other.lane_id * wp.lane_id) > 0
    lanes = [("ll", left, "left") if same_dir(left) else None, ("l", wp, "left"), ("r", wp, "right"),
             ("rr", right, "right") if same_dir(right) else None]
    out = []
    for item in lanes:
        if item is None:
            continue
        slot, lane_wp, side = item
        pts, types = [], []
        d = 0.0
        cur = lane_wp
        while cur is not None and d <= max_dist:
            loc = cur.transform.location
            rv = cur.transform.get_right_vector()
            half = cur.lane_width / 2.0 * (-1 if side == "left" else 1)
            pts.append([loc.x + rv.x * half, loc.y + rv.y * half, loc.z + rv.z * half])
            marking = cur.left_lane_marking if side == "left" else cur.right_lane_marking
            types.append(str(marking.type))
            nxt = cur.next(step)
            cur = nxt[0] if nxt else None
            d += step
        out.append({"slot": slot, "points": np.array(pts), "marking_types": types})
    return out


def generate(settings: Settings, registry: Registry, licenses: LicenseRegistry, frames_per_town: int | None = None) -> dict:
    try:
        import carla  # type: ignore
    except ImportError as exc:
        raise RuntimeError("the `carla` package is not installed; install the wheel that matches your CARLA server") from exc
    cfg = settings.get("sources.carla", {})
    W, H = int(cfg.get("image_width", 1640)), int(cfg.get("image_height", 590))
    fov = float(cfg.get("fov", 90))
    K = intrinsics(W, H, fov)
    client = carla.Client(cfg.get("host", "127.0.0.1"), int(cfg.get("port", 2000)))
    client.set_timeout(60.0)
    rng = random.Random(settings.get("seed", 0))
    out_root = settings.path("raw", "carla")
    counts = {"frames": 0, "added": 0, "duplicate": 0, "rejected": 0}
    road_tag, line_tag = int(carla.CityObjectLabel.Roads), int(carla.CityObjectLabel.RoadLines)
    n_frames = frames_per_town or int(cfg.get("frames_per_town", 200))

    for town in cfg.get("towns", ["Town03"]):
        world = client.load_world(town)
        ws = world.get_settings()
        ws.synchronous_mode, ws.fixed_delta_seconds = True, 0.05
        world.apply_settings(ws)
        tm = client.get_trafficmanager()
        tm.set_synchronous_mode(True)
        bp = world.get_blueprint_library()
        spawns = world.get_map().get_spawn_points()
        actors = []
        try:
            ego = None
            while ego is None:
                ego = world.try_spawn_actor(bp.filter("vehicle.*model3*")[0], rng.choice(spawns))
            actors.append(ego)
            ego.set_autopilot(True, tm.get_port())
            for sp in rng.sample(spawns, min(40, len(spawns))):
                npc = world.try_spawn_actor(rng.choice(bp.filter("vehicle.*")), sp)
                if npc:
                    npc.set_autopilot(True, tm.get_port())
                    actors.append(npc)
            height, pitch = rng.choice(cfg.get("camera_heights_m", [1.4])), rng.choice(cfg.get("camera_pitch_deg", [0]))
            cam_tf = carla.Transform(carla.Location(x=0.8, z=height), carla.Rotation(pitch=pitch))
            sensors, queues = {}, {}
            for name, kind in (("rgb", "sensor.camera.rgb"), ("seg", "sensor.camera.semantic_segmentation")):
                b = bp.find(kind)
                b.set_attribute("image_size_x", str(W))
                b.set_attribute("image_size_y", str(H))
                b.set_attribute("fov", str(fov))
                s = world.spawn_actor(b, cam_tf, attach_to=ego)
                q: queue.Queue = queue.Queue()
                s.listen(q.put)
                sensors[name], queues[name] = s, q
                actors.append(s)
            for _ in range(40):                       # let traffic settle
                world.tick()
                for q in queues.values():
                    q.get()
            run_id = f"{town}_h{height}_p{pitch}_{rng.randrange(1 << 30):08x}"
            for i in range(n_frames):
                if i % 50 == 0:
                    world.set_weather(getattr(carla.WeatherParameters, rng.choice(WEATHERS)))
                for _ in range(10):                   # ~0.5 s between saved frames
                    world.tick()
                    rgb_img, seg_img = queues["rgb"].get(), queues["seg"].get()
                rgb = np.frombuffer(rgb_img.raw_data, np.uint8).reshape(H, W, 4)[:, :, 2::-1].copy()
                tags = np.frombuffer(seg_img.raw_data, np.uint8).reshape(H, W, 4)[:, :, 2]
                classes = np.zeros((H, W), np.uint8)
                classes[tags == road_tag] = 1
                classes[tags == line_tag] = 2
                w2c = np.array(sensors["rgb"].get_transform().get_inverse_matrix())
                lanes = []
                for b in lane_boundaries(world.get_map(), ego.get_location(), carla):
                    uv, depth = project(b["points"], w2c, K)
                    pts = []
                    for (u, v), z, mtype in zip(uv, depth, b["marking_types"]):
                        if not (z > 0.5 and 0 <= u < W and 0 <= v < H):
                            continue
                        painted = "NONE" not in mtype.upper()
                        y0, y1, x0, x1 = max(0, int(v) - 3), int(v) + 4, max(0, int(u) - 3), int(u) + 4
                        seen = bool((tags[y0:y1, x0:x1] == line_tag).any())
                        pts.append({"x": float(u), "y": float(v), "physical": painted, "visible": seen})
                    if len(pts) >= 2:
                        lanes.append({"slot": b["slot"], "points": pts})
                stem = f"{run_id}_{i:05d}"
                img_path = out_root / run_id / f"{stem}.png"
                write_png(img_path, rgb[:, :, ::-1])
                write_png(out_root / run_id / f"{stem}_classes.png", classes)
                (out_root / run_id / f"{stem}_lanes.json").write_text(json.dumps({"lanes": lanes, "width": W, "height": H}))
                res = register_image(settings, registry, licenses, img_path, source="carla", session_id=run_id,
                                     source_ref=f"carla:{town}", frame_index=i, synthetic=True,
                                     extra_tags={"synthetic": True, "town": town})
                counts["frames"] += 1
                counts[res.status] += 1
                if res.status == "added":
                    registry.update_image(res.image_id, has_source_label=1)
                    registry.add_annotation(res.image_id, "source_label", "carla", "sim_gt",
                                            str(out_root / run_id / f"{stem}_classes.png"), license_ids=["carla"],
                                            params={"format": "class_map", "lanes_json": str(out_root / run_id / f"{stem}_lanes.json"),
                                                    "human": False, "simulator": True})
        finally:
            for a in actors:
                try:
                    a.destroy()
                except Exception:
                    pass
            ws.synchronous_mode = False
            world.apply_settings(ws)
    return counts
