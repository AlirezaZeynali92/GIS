"""Request size guard.

Multipart uploads are streamed by Starlette; without this middleware an
oversized body is fully received before the size check in storage rejects it.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int, path_prefix: str = "/images") -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.path_prefix):
            await self.app(scope, receive, send)
            return
        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            response = JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": f"Request body exceeds the {self.max_bytes} byte limit",
                        "details": {
                            "limit_bytes": self.max_bytes,
                            "declared_bytes": int(declared),
                        },
                    }
                },
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge(self.max_bytes, received)
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge as exc:
            response = JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": f"Request body exceeds the {self.max_bytes} byte limit",
                        "details": {"limit_bytes": exc.limit, "received_bytes": exc.received},
                    }
                },
            )
            await response(scope, receive, send)


class _TooLarge(Exception):
    def __init__(self, limit: int, received: int) -> None:
        self.limit = limit
        self.received = received
        super().__init__(f"body exceeded {limit} bytes")
