from __future__ import annotations

from typing import Dict, List
import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, job_id: str) -> None:
        await websocket.accept()
        self.active_connections.setdefault(job_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, job_id: str) -> None:
        conns = self.active_connections.get(job_id)
        if not conns:
            return
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self.active_connections.pop(job_id, None)

    async def broadcast(self, job_id: str, data: dict) -> None:
        conns = self.active_connections.get(job_id, [])
        to_remove: List[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(data)
            except Exception:
                to_remove.append(ws)
        for ws in to_remove:
            self.disconnect(ws, job_id)


manager = ConnectionManager()


@router.websocket("/ws/progress/{job_id}")
async def websocket_progress(websocket: WebSocket, job_id: str):
    """Minimal progress WebSocket. 
    For now, emits a heartbeat so the frontend can integrate.
    Refinement endpoints can call manager.broadcast(job_id, {...}).
    """
    await manager.connect(websocket, job_id)
    try:
        # Heartbeat until client disconnects
        while True:
            await websocket.send_json({
                "type": "heartbeat",
                "jobId": job_id,
                "ts": time.time(),
            })
            await asyncio.sleep(5.0)
    except WebSocketDisconnect:
        manager.disconnect(websocket, job_id)


from typing import Dict, List
import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, job_id: str) -> None:
        await websocket.accept()
        self.active_connections.setdefault(job_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, job_id: str) -> None:
        conns = self.active_connections.get(job_id)
        if not conns:
            return
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self.active_connections.pop(job_id, None)

    async def broadcast(self, job_id: str, data: dict) -> None:
        conns = self.active_connections.get(job_id, [])
        to_remove: List[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(data)
            except Exception:
                to_remove.append(ws)
        for ws in to_remove:
            self.disconnect(ws, job_id)


manager = ConnectionManager()


@router.websocket("/ws/progress/{job_id}")
async def websocket_progress(websocket: WebSocket, job_id: str):
    """Minimal progress WebSocket. 
    For now, emits a heartbeat so the frontend can integrate.
    Refinement endpoints can call manager.broadcast(job_id, {...}).
    """
    await manager.connect(websocket, job_id)
    try:
        # Heartbeat until client disconnects
        while True:
            await websocket.send_json({
                "type": "heartbeat",
                "jobId": job_id,
                "ts": time.time(),
            })
            await asyncio.sleep(5.0)
    except WebSocketDisconnect:
        manager.disconnect(websocket, job_id)









