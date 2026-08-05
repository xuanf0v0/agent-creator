from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Echo Agent")


class EchoRequest(BaseModel):
    message: str


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "agent": "echo"}


@app.post("/echo")
async def echo(body: EchoRequest) -> dict[str, str]:
    print(f"echo: {body.message}", flush=True)
    return {"message": body.message, "prefix": os.getenv("ECHO_PREFIX", "Echo")}


@app.get("/events")
async def events() -> StreamingResponse:
    async def generate() -> AsyncIterator[str]:
        for index in range(3):
            yield f"event: tick\ndata: {json.dumps({'index': index})}\n\n"
            await asyncio.sleep(0.02)

    return StreamingResponse(generate(), media_type="text/event-stream")
