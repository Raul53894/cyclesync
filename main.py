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
                asyncio.create_task(run_workout(session))

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
    names = list(session["names"].values())
    await broadcast(session, {"type": "participants", "names": names})

async def run_workout(session):
    intervals = session["workout"]
    total = len(intervals)

    for i, interval in enumerate(intervals):
        duration = interval["duration"]  # seconds
        label = interval["label"]
        effort = interval["effort"]

        # Announce interval start
        await broadcast(session, {
            "type": "interval_start",
            "index": i,
            "total": total,
            "label": label,
            "effort": effort,
            "duration": duration,
            "rep": interval.get("rep", 1),
            "totalReps": interval.get("totalReps", 1),
        })

        # Count down
        for remaining in range(duration, 0, -1):
            await broadcast(session, {
                "type": "tick",
                "remaining": remaining,
                "duration": duration,
            })
            await asyncio.sleep(1)

    # Workout complete
    await broadcast(session, {"type": "workout_complete"})
    session["started"] = False
