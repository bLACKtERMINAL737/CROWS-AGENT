import hmac
import ipaddress
import os

from .config import get_env
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


EXEMPT_PATHS = {"/health", "/ui", "/ui/"}
WEBHOOK_PATHS = {"/messaging/webhooks/telegram", "/messaging/webhooks/discord"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
AMBIGUOUS_HEADERS = {"host", "origin", "authorization", "x-crows-token", "x-aether-token", "x-telegram-bot-api-secret-token", "x-signature-ed25519", "x-signature-timestamp", "content-length", "transfer-encoding"}


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str, request_host: str, request_scheme: str | None = None) -> bool:
    try:
        parsed_origin = urlsplit(origin)
        parsed_request = urlsplit("//" + request_host)
        origin_host = parsed_origin.hostname
        request_name = parsed_request.hostname
        if not parsed_origin.scheme or not origin_host or not request_name:
            return False
        # Reject any userinfo syntax, including empty username/password forms.
        if "@" in parsed_origin.netloc or parsed_origin.username is not None or parsed_origin.password is not None:
            return False
        if parsed_origin.path not in {"", "/"}:
            return False
        if parsed_origin.query or parsed_origin.fragment:
            return False
        if request_scheme and parsed_origin.scheme.lower() != request_scheme.lower():
            return False
        default_port = 443 if parsed_origin.scheme.lower() == "https" else 80
        origin_port = parsed_origin.port if parsed_origin.port is not None else default_port
        request_port = parsed_request.port if parsed_request.port is not None else default_port
        if origin_port == 0 or request_port == 0:
            return False
        return (
            _is_loopback(origin_host)
            and _is_loopback(request_name)
            and origin_host.lower().strip("[]") == request_name.lower().strip("[]")
            and origin_port == request_port
        )
    except (TypeError, ValueError):
        return False


class LocalAPIProtection:
    """Protect API routes with a shared token and reject cross-origin mutations.

    Health and static UI are intentionally public so the UI can load and report status.
    When no token is configured, API calls are restricted to loopback clients as a
    development fallback. Configure CROWS_API_TOKEN for an explicit bearer token.
    """

    def __init__(self, app):
        self.app = app
        self.token = (get_env("CROWS_API_TOKEN", "") or "").strip()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        raw_headers = scope.get("headers", [])
        header_names = [key.decode("latin-1").lower() for key, _ in raw_headers]
        if any(header_names.count(name) > 1 for name in AMBIGUOUS_HEADERS):
            response = JSONResponse({"detail": "Ambiguous duplicate request header"}, status_code=400)
            await response(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}
        webhook = path in WEBHOOK_PATHS
        exempt = path in EXEMPT_PATHS or path.startswith("/ui/")
        if not exempt and not webhook:
            if method in UNSAFE_METHODS and "origin" in headers:
                origin = headers["origin"]
                host = headers.get("host", "")
                if not _origin_is_local(origin, host, scope.get("scheme")):
                    response = JSONResponse({"detail": "Cross-origin API mutations are not allowed"}, status_code=403)
                    await response(scope, receive, send)
                    return

            client = scope.get("client")
            client_host = client[0] if client else None
            if self.token:
                auth = headers.get("authorization", "")
                bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                supplied = headers.get("x-crows-token", "") or headers.get("x-aether-token", "") or bearer
                if not supplied or not hmac.compare_digest(supplied, self.token):
                    response = JSONResponse({"detail": "Valid Crows API token required"}, status_code=401,
                                             headers={"WWW-Authenticate": "Bearer"})
                    await response(scope, receive, send)
                    return
            elif not _is_loopback(client_host):
                response = JSONResponse({"detail": "Configure CROWS_API_TOKEN for non-loopback access"}, status_code=403)
                await response(scope, receive, send)
                return

        if exempt:
            await self.app(scope, receive, send)
            return

        # Validate declared lengths, but do not rely on Content-Length: chunked
        # requests may omit it entirely. Buffer only up to the configured limit
        # before dispatch so FastAPI cannot parse an oversized JSON/base64 body.
        raw_length = headers.get("content-length")
        if raw_length is not None:
            if not raw_length.isdecimal():
                response = JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
                await response(scope, receive, send)
                return
            if int(raw_length) > MAX_REQUEST_BODY_BYTES:
                response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                await response(scope, receive, send)
                return

        messages = []
        total_bytes = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                messages.append(message)
                break
            if message["type"] != "http.request":
                continue
            total_bytes += len(message.get("body", b""))
            if total_bytes > MAX_REQUEST_BODY_BYTES:
                response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                await response(scope, receive, send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        message_index = 0

        async def replay_receive():
            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)
