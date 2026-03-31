"""
PawWatch — WebRTC Signaling Server
FastAPI + WebSocket-based signaling for WebRTC peer connections.
Deploy to Render (free tier) using render.yaml.
"""

import asyncio
import json
import logging
import os
import secrets
import string
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("pawwatch")

# ── Config ─────────────────────────────────────────────────
API_KEY        = os.environ.get("PAWWATCH_API_KEY", "change-me-in-production")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")          # set your Vercel URL in prod
TURN_URL       = os.environ.get("TURN_URL", "")
TURN_USERNAME  = os.environ.get("TURN_USERNAME", "")
TURN_CREDENTIAL= os.environ.get("TURN_CREDENTIAL", "")

app = FastAPI(title="PawWatch Signaling", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory session store ────────────────────────────────
# rooms[room_id] = {"streamer": WebSocket | None, "viewers": [WebSocket], "password": str | None}
rooms: dict[str, dict] = defaultdict(lambda: {"streamer": None, "viewers": [], "password": None})


# ── Helpers ────────────────────────────────────────────────
def _room_id_valid(room_id: str) -> bool:
    allowed = set(string.ascii_uppercase + string.digits)
    return len(room_id) == 6 and all(c in allowed for c in room_id)


def _ice_config() -> list[dict]:
    servers = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]
    if TURN_URL:
        servers.append({
            "urls": TURN_URL,
            "username": TURN_USERNAME,
            "credential": TURN_CREDENTIAL,
        })
    return servers


async def _send(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


# ── HTTP routes ────────────────────────────────────────────
@app.get("/")
async def root():
    return {"service": "PawWatch Signaling", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "rooms": len(rooms)}


@app.get("/ice-config")
async def ice_config(x_api_key: Optional[str] = Query(default=None, alias="api_key")):
    """Return ICE server config to authenticated clients."""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return {"iceServers": _ice_config()}


@app.post("/rooms/create")
async def create_room(password: Optional[str] = None, api_key: Optional[str] = Query(default=None)):
    """Create a room with an optional password and return its ID."""
    if api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    room_id = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    rooms[room_id]["password"] = password
    log.info("Room created: %s", room_id)
    return {"room_id": room_id}


# ── WebSocket — Streamer (Android tablet) ─────────────────
@app.websocket("/ws/streamer/{room_id}")
async def streamer_endpoint(
    websocket: WebSocket,
    room_id: str,
    password: Optional[str] = Query(default=None),
    api_key: Optional[str] = Query(default=None),
):
    if not _room_id_valid(room_id):
        await websocket.close(code=4000, reason="Invalid room ID")
        return

    # Validate API key for streamer
    if api_key != API_KEY:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # Validate room password if set
    room = rooms[room_id]
    if room["password"] and password != room["password"]:
        await websocket.close(code=4002, reason="Wrong password")
        return

    await websocket.accept()
    room["streamer"] = websocket
    log.info("Streamer joined room %s", room_id)

    # Notify all waiting viewers
    for viewer in room["viewers"]:
        await _send(viewer, {"type": "streamer-ready"})

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "offer":
                # Forward offer to all viewers
                for viewer in room["viewers"]:
                    await _send(viewer, {"type": "offer", "sdp": msg.get("sdp")})
                log.info("Offer forwarded in room %s to %d viewers", room_id, len(room["viewers"]))

            elif msg_type == "ice-candidate":
                for viewer in room["viewers"]:
                    await _send(viewer, {"type": "ice-candidate", "candidate": msg.get("candidate")})

            else:
                log.warning("Unknown message type from streamer: %s", msg_type)

    except WebSocketDisconnect:
        log.info("Streamer disconnected from room %s", room_id)
    except Exception as e:
        log.error("Streamer error in room %s: %s", room_id, e)
    finally:
        room["streamer"] = None
        for viewer in room["viewers"]:
            await _send(viewer, {"type": "error", "message": "Streamer disconnected"})
        # Clean up empty rooms
        if not room["viewers"] and not room["streamer"]:
            rooms.pop(room_id, None)


# ── WebSocket — Viewer (web browser) ──────────────────────
@app.websocket("/ws/viewer/{room_id}")
async def viewer_endpoint(
    websocket: WebSocket,
    room_id: str,
    password: Optional[str] = Query(default=None),
):
    if not _room_id_valid(room_id):
        await websocket.close(code=4000, reason="Invalid room ID")
        return

    room = rooms[room_id]

    # Validate room password if set
    if room["password"] and password != room["password"]:
        await websocket.close(code=4002, reason="Wrong password")
        return

    await websocket.accept()
    room["viewers"].append(websocket)
    log.info("Viewer joined room %s (%d viewers total)", room_id, len(room["viewers"]))

    # Tell viewer if streamer is already present
    if room["streamer"]:
        await _send(websocket, {"type": "streamer-ready"})
        # Ask streamer to re-send offer for this viewer
        await _send(room["streamer"], {"type": "viewer-joined"})

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "answer":
                # Forward answer to streamer
                if room["streamer"]:
                    await _send(room["streamer"], {"type": "answer", "sdp": msg.get("sdp")})

            elif msg_type == "ice-candidate":
                if room["streamer"]:
                    await _send(room["streamer"], {"type": "ice-candidate", "candidate": msg.get("candidate")})

            else:
                log.warning("Unknown message type from viewer: %s", msg_type)

    except WebSocketDisconnect:
        log.info("Viewer disconnected from room %s", room_id)
    except Exception as e:
        log.error("Viewer error in room %s: %s", room_id, e)
    finally:
        room["viewers"] = [v for v in room["viewers"] if v != websocket]
        if not room["viewers"] and not room["streamer"]:
            rooms.pop(room_id, None)
