import json
import secrets

from starlette.requests import Request
from starlette.responses import Response

HOST = "127.0.0.1:8876"
ORIGIN = "http://127.0.0.1:8876"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'"
)


def token_ok(provided: str, expected: str) -> bool:
    if len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


def respond(status: int, payload: object, headers: dict[str, str] | None = None) -> Response:
    response = Response(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        status,
        media_type="application/json; charset=utf-8",
    )
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response


def security_headers(path: str) -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    if path == "/api" or path.startswith("/api/") or path in {"/", "/index.html"}:
        headers["Content-Security-Policy"] = CSP
    return headers


def install_auth(app) -> None:
    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        headers = security_headers(path)
        if request.headers.get("host", "") != HOST:
            return respond(403, {"error": "请使用本机地址 127.0.0.1:8876"}, headers)
        method = request.method.upper()
        is_api = path == "/api" or path.startswith("/api/")
        is_write = method in {"POST", "PUT", "DELETE", "PATCH"}
        if is_api or is_write:
            provided = request.headers.get("x-qa-token", "")
            if not token_ok(provided, request.app.state.token):
                return respond(403, {"error": "请求不来自当前工作台"}, headers)
        if is_write and request.headers.get("origin") != ORIGIN:
            return respond(403, {"error": "请求不来自当前工作台"}, headers)
        response = await call_next(request)
        for key, value in headers.items():
            response.headers[key] = value
        return response
