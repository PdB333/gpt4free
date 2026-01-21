from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

OPENWEBUI_BASE_URL = os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:8080").rstrip("/")
PROXY_HOST = os.environ.get("OPENWEBUI_PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.environ.get("OPENWEBUI_PROXY_PORT", "18080"))
CHAT_TAG_TEMPLATE = os.environ.get("OPENWEBUI_CHAT_TAG", "<!-- chat_id: {chat_id} -->")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}

app = FastAPI()


def _inject_chat_id(payload: Dict[str, Any]) -> Optional[str]:
    chat_id = payload.get("chat_id")
    if not chat_id and isinstance(payload.get("chat"), dict):
        chat_id = payload["chat"].get("id")
    if not chat_id:
        chat_id = payload.get("id")
    if not chat_id:
        return None

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return chat_id

    tag = CHAT_TAG_TEMPLATE.format(chat_id=chat_id)
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if tag in content:
            return chat_id
        message["content"] = f"{content}\n{tag}"
        return chat_id

    last_message = messages[-1]
    if isinstance(last_message, dict):
        content = last_message.get("content")
        if isinstance(content, str) and tag not in content:
            last_message["content"] = f"{content}\n{tag}"
    return chat_id


def _forward_headers(request: Request) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        headers[key] = value
    return headers


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/chat/completions")
async def proxy_chat_completions(request: Request) -> Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    injected = _inject_chat_id(payload)
    if injected:
        print(f"Proxy: injected chat_id={injected}")

    url = f"{OPENWEBUI_BASE_URL}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = _forward_headers(request)
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            resp_headers = {
                key: value
                for key, value in resp.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
            }
            content_type = resp.headers.get("content-type") or "application/octet-stream"
            if content_type and content_type.startswith("text/event-stream"):
                async def stream_body():
                    async for chunk in resp.content.iter_any():
                        yield chunk
                return StreamingResponse(
                    stream_body(),
                    status_code=resp.status,
                    headers=resp_headers,
                    media_type=content_type,
                )
            body = await resp.read()
            return Response(
                content=body,
                status_code=resp.status,
                headers=resp_headers,
                media_type=content_type,
            )


def main() -> None:
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="info")


if __name__ == "__main__":
    main()
