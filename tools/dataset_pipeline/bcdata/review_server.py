"""LAN phone review app. The PC owns the data; phones only submit decisions."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
import hashlib
import io
import json
import secrets
import re
import socket
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .bootstrap import atomic_json
from .fusion import load_fused
from .label_policy import LABELING_POLICY, ROAD_EDGE_KINDS, ROAD_EDGE_STATUSES
from .review_export import ExportWorker
from .review_store import ReviewError, ReviewStore

UI = Path(__file__).resolve().parent.parent / "review_ui"


def lan_addresses():
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))  # route lookup; sends no packet
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in addresses if not ip.startswith(("127.", "169.254.")))


class PairRequest(BaseModel):
    code: str = Field(pattern=r'^[0-9]{6}$', min_length=6, max_length=6)
    reviewer: str = Field(default="Reviewer", min_length=1, max_length=80)


class DecisionRequest(BaseModel):
    image_id: str = Field(min_length=1, max_length=100)
    action: str
    revision: int = Field(ge=0)
    event_id: str
    reviewer: str = Field(default="Nick", min_length=1, max_length=80)
    lanes: list[dict] | None = None
    notes: str = Field(default="", max_length=4000)
    seconds: float | None = Field(default=None, ge=0, le=86400)
    candidate: str | None = Field(default=None, max_length=40)
    road_edges: dict | None = None
    road_edge_candidate: str | None = Field(default=None, max_length=40)
    road_edge_candidate_version: str | None = Field(default=None, max_length=256)
    candidate_version: str | None = Field(default=None, max_length=256)
    assist_ids: list[str] | None = Field(default=None, max_length=50)
    fingerprint: str | None = None
    complete_annotation: bool = False
    no_visible_lane_paint: bool = False


class RemotePairRequest(BaseModel):
    access_key: str = Field(min_length=32, max_length=128)
    reviewer: str = Field(default="Nick", min_length=1, max_length=80)


class DeferRequest(BaseModel):
    revision: int = Field(ge=0)
    deferred: bool = True
    fingerprint: str | None = None


class ClaimRequest(BaseModel):
    revision: int = Field(ge=0)
    fingerprint: str | None = None
    release: bool = False
    activate: bool = False


class TraceRequest(BaseModel):
    revision: int = Field(ge=0)
    points: list[list[float]] = Field(min_length=2, max_length=2)
    layer: str = "lanes"
    side: str = "right"


class UndoRequest(BaseModel):
    event_id: str
    reviewer: str = "Nick"
    image_id: str | None = None
    source: str | None = None


def create_app(settings, port=8765, start_worker=True):
    from .remote_access import load_remote, valid_key, rotate_remote
    store = ReviewStore(settings)
    from .zod_mirror_review import ZodMirrorReview
    from . import zod_main_review
    zod = ZodMirrorReview(settings.root)
    worker = ExportWorker(settings)
    addresses = lan_addresses()
    state = {"pin": f"{secrets.randbelow(1000000):06d}", "expires": time.time() + 24 * 3600}

    def pairing_info():
        if time.time() > state["expires"]:
            state.update(pin=f"{secrets.randbelow(1000000):06d}", expires=time.time() + 24 * 3600)
        urls = [f"http://{ip}:{port}" for ip in addresses]
        return {"urls": urls, "code": state["pin"], "expires": state["expires"],
                "qr_url": "/api/host/qr", "desktop_url": f"http://127.0.0.1:{port}",
                "remote_url": load_remote(settings).get("review_url"),
                "instructions": "Connect phone and PC to the same Wi-Fi, scan this QR, and enter your reviewer name."}

    @asynccontextmanager
    async def lifespan(app):
        atomic_json(settings.path("reports", "phone_pairing.json"), pairing_info())
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                worker.close()

    app = FastAPI(title="BrakeCheck human review", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pairing = state
    app.state.store = store
    app.state.worker = worker
    allowed = ["127.0.0.1", "localhost", socket.gethostname(), *addresses]
    remote = load_remote(settings)
    if remote: allowed.append(remote["host"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    def is_local(request):
        # A tunnel originates on loopback too. Its public Host/Cloudflare
        # headers must never grant desktop authentication or host controls.
        host = (urlsplit("//" + request.headers.get("host", "")).hostname or "").lower()
        return (request.client is not None and request.client.host in ("127.0.0.1", "::1")
                and host in ("127.0.0.1", "localhost", "::1", socket.gethostname().lower())
                and not request.headers.get("cf-ray") and not request.headers.get("cf-connecting-ip"))

    def session(request):
        if is_local(request):
            return {"reviewer": "Nick", "owner": "desktop", "local": True}
        token = request.cookies.get("bc_review")
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with store.registry() as reg:
            row = reg.conn.execute("SELECT reviewer FROM phone_sessions WHERE token_hash=? AND expires>?", (digest, time.time())).fetchone()
        return {"reviewer": row[0], "owner": digest, "local": False} if row else None

    @app.middleware("http")
    async def access(request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin:
                try:
                    same_host = urlsplit(origin).netloc == request.headers.get("host")
                except ValueError:
                    same_host = False
                if not same_host:
                    return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
            request.state.session = session(request)
            client_id = request.headers.get('x-review-client', '')
            if request.state.session and re.fullmatch(r'[A-Za-z0-9-]{16,80}', client_id):
                request.state.session['owner'] += ':' + client_id
            if request.url.path not in ("/api/session", "/api/pair", "/api/remote-pair", "/api/health") and not request.state.session:
                return JSONResponse({"detail": "Pair this phone with the code shown on the PC"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: blob:; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ReviewError)
    async def review_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/api/health")
    def health():
        from .report_reader import optional_report
        training = settings.path("reports", "ufld_training.json")
        phase = optional_report(training, {"phase":"status_unavailable"}).get("phase", "deferred") if training.exists() else "deferred"
        return {"status": "ok", "app": "brakecheck-human-review", "training": phase}

    @app.get("/api/session")
    def get_session(request: Request):
        s = request.state.session
        return {"paired": bool(s), "local": bool(s and s["local"]), "reviewer": s["reviewer"] if s else ""}

    @app.post("/api/remote-pair")
    def remote_pair(body: RemotePairRequest):
        if not valid_key(settings, body.access_key):
            raise HTTPException(401, "Open the current private remote link from the PC.")
        token = secrets.token_urlsafe(32)
        reviewer = body.reviewer.strip()
        if not reviewer: raise HTTPException(400, "Enter your reviewer name")
        with store.registry() as reg:
            with reg.tx() as con:
                con.execute("INSERT INTO phone_sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), reviewer, time.time() + 7 * 86400))
        response = JSONResponse({"paired": True, "reviewer": reviewer})
        response.set_cookie("bc_review", token, max_age=7 * 86400, httponly=True, secure=True, samesite="strict", path="/")
        return response

    @app.post("/api/pair")
    def pair(body: PairRequest, request: Request):
        remote_host = load_remote(settings).get("host")
        if request.url.hostname == remote_host or request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip"):
            raise HTTPException(401, "Use the private remote review link from the PC.")
        now = time.time()
        if now > state["expires"] or not secrets.compare_digest(body.code, state["pin"]):
            raise HTTPException(401, "Incorrect or expired pairing code. Check the PC screen.")
        token = secrets.token_urlsafe(32)
        reviewer = body.reviewer.strip()
        if not reviewer:
            raise HTTPException(400, "Enter your reviewer name")
        with store.registry() as reg:
            with reg.tx() as con:
                con.execute("DELETE FROM phone_sessions WHERE expires<?", (now,))
                con.execute("INSERT INTO phone_sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), reviewer, now + 7 * 86400))
        response = JSONResponse({"paired": True, "reviewer": reviewer})
        response.set_cookie("bc_review", token, max_age=7 * 86400, httponly=True, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        token = request.cookies.get("bc_review")
        if token:
            with store.registry() as reg:
                with reg.tx() as con:
                    con.execute("DELETE FROM phone_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response = JSONResponse({"paired": False})
        response.delete_cookie("bc_review")
        return response

    def local_only(request):
        if not is_local(request):
            raise HTTPException(403, "Pairing codes are visible only on the host PC")

    @app.get("/api/host")
    def host_info(request: Request):
        local_only(request)
        return pairing_info()

    @app.post("/api/host/revoke")
    def revoke(request: Request):
        local_only(request)
        with store.registry() as reg:
            with reg.tx() as con:
                con.execute("DELETE FROM phone_sessions")
        state.update(pin=f"{secrets.randbelow(1000000):06d}", expires=time.time() + 24 * 3600)
        rotate_remote(settings)
        atomic_json(settings.path("reports", "phone_pairing.json"), pairing_info())
        return pairing_info()

    @app.get("/api/host/qr")
    def qr(request: Request):
        local_only(request)
        import qrcode
        url = pairing_info()["urls"]
        if not url:
            raise HTTPException(503, "No Wi-Fi/LAN address detected")
        picture = qrcode.make(url[0] + "/#pair=" + state["pin"])
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        return Response(buffer.getvalue(), media_type="image/png")

    @app.get("/api/host/remote-qr")
    def remote_qr(request: Request):
        local_only(request)
        import qrcode
        link = load_remote(settings).get("review_url")
        if not link: raise HTTPException(404, "Remote access is not running")
        buffer = io.BytesIO()
        qrcode.make(link).save(buffer, format="PNG")
        return Response(buffer.getvalue(), media_type="image/png")

    @app.get("/api/leaderboard")
    def leaderboard():
        return store.leaderboard()

    @app.get("/api/stats")
    def stats():
        result = store.stats()
        if (zod.root / "audits.sqlite").exists():
            result["zod_audits"] = zod.status()["counts"]
        return result

    @app.get("/api/labeling-policy")
    def labeling_policy():
        return {**LABELING_POLICY, "model_candidate_api": 2, "road_edge_statuses": list(ROAD_EDGE_STATUSES),
                "road_edge_kinds": list(ROAD_EDGE_KINDS)}

    @app.get("/api/next")
    def next_image(request: Request, split="all", status="pending", exclude=""):
        excluded = exclude.split(",") if exclude else []
        if status in ("zod_audit", "zod_repair", "zod_later"):
            return zod_main_review.next_sample(zod, split, {"zod_audit": "pending", "zod_repair": "needs_repair", "zod_later": "skip"}[status], excluded, request.state.session['owner'])
        result = store.next_image(request.state.session["owner"], split, status, excluded)
        if result is None and status == "audit_sample" and (zod.root / "audits.sqlite").exists():
            return zod_main_review.next_sample(zod, split, "pending", excluded, request.state.session['owner'])
        return result

    @app.get("/api/images/{image_id}")
    def detail(image_id: str, request: Request):
        if image_id.startswith("zod-"):
            return zod_main_review.detail(zod, image_id)
        return store.detail(image_id)

    @app.post('/api/images/{image_id}/claim')
    def claim_image(image_id: str, body: ClaimRequest, request: Request):
        owner = request.state.session['owner']
        if image_id.startswith('zod-'):
            if body.release:
                zod.release(image_id, owner)
                return {'image_id': image_id, 'released': True}
            return zod.claim(image_id, owner, revision=body.revision, fingerprint=body.fingerprint, replace_previous=body.activate)
        return store.renew_claim(image_id, body.revision, owner, release=body.release, replace_previous=body.activate)

    @app.get("/api/batch")
    def batch_images(request: Request, split="all", limit: int = 8, exclude="", status="pending"):
        return store.batch(request.state.session["owner"], split, limit, exclude.split(",") if exclude else [], status)

    @app.post("/api/images/{image_id}/defer")
    def defer_image(image_id: str, body: DeferRequest, request: Request):
        session = request.state.session
        if image_id.startswith("zod-"):
            from .simulation_review import SimulationDecision
            item = zod.detail(image_id)
            if not body.deferred:
                raise HTTPException(422, "Reopen this skipped sample from the ZOD review-later queue")
            if body.fingerprint is None:
                raise HTTPException(422, "Refresh this ZOD sample before skipping")
            event_id = secrets.token_hex(16)
            saved = zod.decide(SimulationDecision(image_id=image_id, action="skip", event_id=event_id,
                       revision=body.revision, fingerprint=body.fingerprint), session["reviewer"], session['owner'])
            return {"image_id": image_id, "review_later": True, 'event_id': event_id,
                    'revision': saved['revision'], 'source': 'zod_mirror'}
        result = store.defer(image_id, body.revision, session["reviewer"], session["owner"], body.deferred)
        worker.request()
        return result

    @app.post("/api/images/{image_id}/trace")
    def trace_image(image_id: str, body: TraceRequest, request: Request):
        from .trace_assist import create_assist
        return create_assist(store, image_id, body.revision, request.state.session["owner"],
                             body.points, body.layer, body.side)

    @app.get("/api/images/{image_id}/photo")
    def photo(image_id: str):
        with store.registry() as reg:
            row = store._check_image(reg, image_id)
            path = Path(row["path"])
            if not path.resolve().is_relative_to(settings.root.resolve()):
                raise HTTPException(403, "Image path is outside the dataset")
        return FileResponse(path)

    @app.get("/api/images/{image_id}/mask")
    def mask(image_id: str):
        import numpy as np
        from PIL import Image
        with store.registry() as reg:
            store._check_image(reg, image_id)
            fused = load_fused(reg, image_id)
        if fused is None:
            raise HTTPException(404, "Source mask unavailable")
        lane = fused[1]["lane"] >= 0.5
        rgba = np.zeros((*lane.shape, 4), dtype=np.uint8)
        rgba[lane] = (250, 215, 40, 170)
        buffer = io.BytesIO()
        Image.fromarray(rgba).save(buffer, format="PNG")
        return Response(buffer.getvalue(), media_type="image/png")

    @app.post("/api/reviews")
    def decide(body: DecisionRequest, request: Request):
        s = request.state.session
        reviewer = body.reviewer if s["local"] else s["reviewer"]
        if body.image_id.startswith("zod-"):
            return zod_main_review.decide(zod, body, reviewer, s['owner'])
        result = store.decide(body.image_id, body.action, reviewer, body.revision, body.event_id, body.lanes, body.notes, body.seconds, s["owner"], body.candidate, body.road_edges,
                              road_edge_candidate=body.road_edge_candidate, road_edge_candidate_version=body.road_edge_candidate_version,
                              candidate_version=body.candidate_version, assist_ids=body.assist_ids)
        from .source_validation import refresh_groups
        refresh_groups(settings)
        worker.request()
        return result

    @app.post("/api/undo")
    def undo(body: UndoRequest, request: Request):
        s = request.state.session
        if body.source == 'zod_mirror' or (body.image_id or '').startswith('zod-'):
            return zod.undo(body.event_id, body.reviewer if s['local'] else s['reviewer'], s['owner'], body.image_id)
        result = store.undo(body.event_id, body.reviewer if s["local"] else s["reviewer"])
        from .source_validation import refresh_groups
        refresh_groups(settings)
        worker.request()
        return result

    @app.get("/api/history")
    def history():
        with store.registry() as reg:
            return [dict(r) for r in reg.conn.execute("SELECT event_id,image_id,action,reviewer,created_at FROM review_events ORDER BY created_at DESC LIMIT 30")]

    @app.post("/api/export")
    def export():
        worker.request()
        return {"state": "queued"}

    @app.post("/api/backup")
    def backup():
        return {"folder": store.backup()}

    @app.get("/")
    def index():
        return FileResponse(UI / "index.html")

    from .experiment_review import create_experiment_router

    def require_experiment_session(request: Request):
        current = getattr(request.state, "session", None)
        if not current:
            raise HTTPException(401, "Pair this phone with the PC first")
        return current

    app.include_router(create_experiment_router(settings.root, require_experiment_session))

    from .lead_review import create_lead_router
    app.include_router(create_lead_router(settings.root, require_experiment_session))

    @app.get("/lead-cars")
    def lead_cars():
        return FileResponse(UI / "lead_cars.html")

    from .studio_datasets import create_studio_datasets_router
    app.include_router(create_studio_datasets_router(settings.root, require_experiment_session))

    from .experiment_dashboard import create_experiment_dashboard_router
    app.include_router(create_experiment_dashboard_router(settings.root, require_experiment_session))

    from .lane_error_review import create_lane_error_router
    app.include_router(create_lane_error_router(settings.root, require_experiment_session))

    @app.get("/model-errors")
    def model_error_review():
        return FileResponse(UI / "model_errors.html")

    @app.get("/experiments")
    def experiments_dashboard():
        return FileResponse(UI / "experiments.html")

    from .simulation_review import create_simulation_router
    app.include_router(create_simulation_router(settings.root, require_experiment_session))

    from .real_source_review import create_real_source_router
    app.include_router(create_real_source_router(settings.root, require_experiment_session))

    from .zod_mirror_review import create_zod_mirror_router
    app.include_router(create_zod_mirror_router(settings.root, require_experiment_session))

    from .college_repair_review import create_college_repair_router
    app.include_router(create_college_repair_router(settings.root, require_experiment_session))

    @app.get("/simulation")
    def simulation_audits():
        return FileResponse(UI / "simulation.html")

    @app.get("/source-audits")
    def real_source_audits():
        return FileResponse(UI / "real_source_audits.html")

    @app.get("/datasets")
    def dataset_collections():
        return FileResponse(UI / "datasets.html")

    @app.get("/experiment")
    def flagged_experiment():
        return FileResponse(UI / "experiment.html")

    app.mount("/static", StaticFiles(directory=UI), name="static")
    return app
