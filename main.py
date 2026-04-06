from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import random
import string
import json

app = FastAPI()

# Store active sessions in memory
sessions = {}

def generate_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/host")
async def host_page():
    return FileResponse("static/host.html")

@app.get("/session")
async def session_page():
    return FileResponse("static/session.html")

@app.post("/create-session")
async def create_session():
    code = generate_code()
    sessions[code] = {
        "host": None,
        "clients": [],          # list of websockets
        "names": {},            # websocket id -> name
        "workout": [],          # list of intervals
        "started": False,
        "pause_event": None,    # asyncio.Event — set=running, clear=paused
        "block_event": None,    # asyncio.Event — set when host continues to next block
        "ended": False,
        "current_interval": None,  # last interval_start payload, for late-joining clients
        "last_tick": None,         # last tick payload, for late-joining clients
    }
    return {"code": code}

@app.websocket("/ws/{code}/{name}/{role}")
async def websocket_endpoint(websocket: WebSocket, code: str, name: str, role: str):
    await websocket.accept()

    if code not in sessions:
        await websocket.send_text(json.dumps({"type": "error", "message": "Session not found"}))
        await websocket.close()
        return

    session = sessions[code]
    session["clients"].append(websocket)
    ws_id = id(websocket)
    session["names"][ws_id] = name

    if role == "host":
        session["host"] = websocket

    # Notify all clients of updated participant list
    await broadcast_participants(session)

    # If a workout is already running, catch this client up immediately
    if session.get("started") and session.get("current_interval"):
        await websocket.send_text(json.dumps(session["current_interval"]))
        if session.get("last_tick"):
            await websocket.send_text(json.dumps(session["last_tick"]))
        if session.get("pause_event") and not session["pause_event"].is_set():
            await websocket.send_text(json.dumps({"type": "paused"}))

    try:
        async for message in websocket.iter_text():
            data = json.loads(message)

            # Host sends workout definition
            if data["type"] == "set_workout":
                session["workout"] = data["intervals"]
                await broadcast(session, {"type": "workout_ready", "intervals": data["intervals"]})

            # Host starts the workout
            elif data["type"] == "start_workout":
                session["started"] = True
                session["ended"] = False
                session["pause_event"] = asyncio.Event()
                session["pause_event"].set()  # start in running state
                asyncio.create_task(run_workout(session))
                # Tell the host to navigate now that the server has the workout
                await websocket.send_text(json.dumps({"type": "starting"}))

            # Host pauses the workout
            elif data["type"] == "pause":
                if session.get("pause_event"):
                    session["pause_event"].clear()
                    await broadcast(session, {"type": "paused"})

            # Host resumes the workout
            elif data["type"] == "resume":
                if session.get("pause_event"):
                    session["pause_event"].set()
                    await broadcast(session, {"type": "resumed"})

            # Host continues to next block after a block break
            elif data["type"] == "continue_block":
                if session.get("block_event"):
                    session["block_event"].set()

            # Emoji reaction — broadcast to all clients
            elif data["type"] == "emote":
                emoji = data.get("emoji", "")
                if emoji in {"👍", "🥵", "🤘", "🙌", "😝"}:
                    await broadcast(session, {"type": "emote", "emoji": emoji})

            # Host ends the workout early
            elif data["type"] == "end":
                session["ended"] = True
                if session.get("pause_event"):
                    session["pause_event"].set()  # unblock if currently paused
                if session.get("block_event"):
                    session["block_event"].set()  # unblock if waiting between blocks
                session["started"] = False
                await broadcast(session, {"type": "workout_complete"})

    except WebSocketDisconnect:
        session["clients"].remove(websocket)
        del session["names"][ws_id]
        await broadcast_participants(session)

async def broadcast(session, message):
    data = json.dumps(message)
    for client in session["clients"]:
        try:
            await client.send_text(data)
        except:
            pass

async def broadcast_participants(session):
    seen = set()
    names = []
    for n in session["names"].values():
        if n not in seen:
            seen.add(n)
            names.append(n)
    await broadcast(session, {"type": "participants", "names": names})

async def run_workout(session):
    intervals = session["workout"]
    total = len(intervals)

    # Group consecutive intervals by their block index
    blocks = []
    for iv in intervals:
        b = iv.get("block", 0)
        if not blocks or blocks[-1][0] != b:
            blocks.append((b, []))
        blocks[-1][1].append(iv)

    # 5-second countdown before the first interval
    for count in range(5, 0, -1):
        if session.get("ended"):
            return
        await broadcast(session, {"type": "countdown", "count": count})
        await asyncio.sleep(1)

    global_idx = 0  # position across all intervals

    for block_pos, (block_idx, block_intervals) in enumerate(blocks):
        for iv in block_intervals:
            if session.get("ended"):
                return

            duration = iv["duration"]
            interval_msg = {
                "type": "interval_start",
                "index": global_idx,
                "total": total,
                "label": iv["label"],
                "effort": iv["effort"],
                "duration": duration,
                "rep": iv.get("rep", 1),
                "totalReps": iv.get("totalReps", 1),
                "block": iv.get("block", 0),
            }
            session["current_interval"] = interval_msg
            session["last_tick"] = None
            await broadcast(session, interval_msg)

            for remaining in range(duration, 0, -1):
                if session.get("ended"):
                    return
                await session["pause_event"].wait()
                if session.get("ended"):
                    return
                tick_msg = {"type": "tick", "remaining": remaining, "duration": duration}
                session["last_tick"] = tick_msg
                await broadcast(session, tick_msg)
                await asyncio.sleep(1)

            global_idx += 1

        # After each block except the last: pause and wait for host to continue
        if block_pos < len(blocks) - 1:
            session["block_event"] = asyncio.Event()
            await broadcast(session, {
                "type": "block_complete",
                "block": block_pos + 1,
                "totalBlocks": len(blocks),
            })
            await session["block_event"].wait()
            if session.get("ended"):
                return

    # Workout complete (natural finish)
    if not session.get("ended"):
        await broadcast(session, {"type": "workout_complete"})
    session["started"] = False
