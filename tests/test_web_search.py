import sys
import types

import pytest

from crows.tools import ToolError, ToolRegistry


def test_web_search_is_registered_with_clear_live_search_guidance(tmp_path):
    registry = ToolRegistry(tmp_path)
    web_tool = next(item for item in registry.describe() if item["name"] == "web.search")

    assert "LIVE WEB SEARCH" in web_tool["description"]
    assert "current" in web_tool["description"]
    assert "never send passwords" in web_tool["description"]


def test_web_search_returns_bounded_results_and_timestamp(tmp_path, monkeypatch):
    class FakeDDGS:
        def __init__(self, timeout):
            assert timeout == 10

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, max_results):
            assert query == "latest Python release"
            assert max_results == 2
            return [
                {"title": "Python release", "href": "https://example.com/python", "body": "A release snippet"},
                {"title": "Docs", "url": "https://example.com/docs", "snippet": "Docs snippet"},
                {"title": "Ignored extra", "href": "https://example.com/extra", "body": "Not requested"},
            ]

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    registry = ToolRegistry(tmp_path)

    result = registry.execute(
        "web.search",
        {"query": "latest Python release", "max_results": 2},
    )

    assert result["query"] == "latest Python release"
    assert result["provider"] == "DuckDuckGo web results via ddgs"
    assert result["result_count"] == 2
    assert result["results"][0] == {
        "title": "Python release",
        "url": "https://example.com/python",
        "snippet": "A release snippet",
    }
    assert result["results"][1]["url"] == "https://example.com/docs"
    assert result["searched_at"]
    assert "untrusted" in result["note"]


def test_web_search_hides_backend_errors(tmp_path, monkeypatch):
    class BrokenDDGS:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, max_results):
            raise RuntimeError("backend internals and secret token")

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=BrokenDDGS))
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="Live web search failed or was rate-limited"):
        registry.execute("web.search", {"query": "current weather"})


def test_web_search_rejects_invalid_result_limit(tmp_path):
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="Invalid tool input"):
        registry.execute("web.search", {"query": "latest news", "max_results": 100})


def test_open_web_page_extracts_readable_text_and_ignores_scripts(tmp_path, monkeypatch):
    import socket

    def public_resolution(host, port, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", public_resolution)
    def fake_request(parsed, address):
        assert parsed.hostname == "example.com"
        assert address == "93.184.216.34"
        return 200, {"content-type": "text/html; charset=utf-8"}, (
            b"<html><nav>Menu</nav><h1>Current Release</h1>"
            b"<script>secret()</script><p>Release notes text.</p></html>"
        ), "utf-8"

    monkeypatch.setattr(ToolRegistry, "_request_pinned_page", staticmethod(fake_request))
    result = ToolRegistry(tmp_path).execute("web.open", {"url": "https://example.com/article"})

    assert result["status_code"] == 200
    assert "Current Release" in result["text"]
    assert "Release notes text." in result["text"]
    assert "secret()" not in result["text"]
    assert result["url"] == "https://example.com/article"


def test_open_web_page_rejects_invalid_urls(tmp_path):
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="URL is invalid"):
        registry.execute("web.open", {"url": "http://127.0.0.1/admin"})


def test_open_web_page_rejects_redirects(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, type: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    def fake_request(parsed, address):
        raise ToolError("This page redirects; use a direct public URL from search results.")

    monkeypatch.setattr(ToolRegistry, "_request_pinned_page", staticmethod(fake_request))
    with pytest.raises(ToolError, match="redirects"):
        ToolRegistry(tmp_path).execute("web.open", {"url": "https://example.com/article"})


def test_web_open_uses_validated_address_for_connection(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, type: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    seen = {}

    def fake_request(parsed, address):
        seen["host"] = parsed.hostname
        seen["address"] = address
        return 200, {"content-type": "text/plain"}, b"ok", "utf-8"

    monkeypatch.setattr(ToolRegistry, "_request_pinned_page", staticmethod(fake_request))
    ToolRegistry(tmp_path).execute("web.open", {"url": "https://public.example/path"})
    assert seen == {"host": "public.example", "address": "93.184.216.34"}


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/resource",
        "https://user:password@example.com/",
        "http://localhost./admin",
        "http://service.localhost/admin",
        "http://[::1]/admin",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_web_open_rejects_unsafe_url_forms(tmp_path, url):
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="URL is invalid"):
        registry.execute("web.open", {"url": url})


def test_web_open_rejects_public_hostname_resolving_to_private_address(
    tmp_path, monkeypatch
):
    import socket

    def private_resolution(host, port, type):
        assert host == "public-looking.example"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", private_resolution)
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="URL is invalid"):
        registry.execute(
            "web.open", {"url": "https://public-looking.example/private"}
        )


def test_web_open_rejects_hostname_with_mixed_public_and_private_addresses(
    tmp_path, monkeypatch
):
    import socket

    def mixed_resolution(host, port, type):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed_resolution)
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="URL is invalid"):
        registry.execute(
            "web.open", {"url": "https://mixed-address.example/article"}
        )



def test_pinned_http_connection_connects_to_validated_ip(tmp_path, monkeypatch):
    import socket
    from urllib.parse import urlsplit

    from crows.tools import ToolRegistry

    seen = {}
    fake_socket = object()

    def create_connection(destination, timeout, source_address):
        seen["destination"] = destination
        seen["timeout"] = timeout
        return fake_socket

    monkeypatch.setattr(socket, "create_connection", create_connection)

    class FakeResponse:
        status = 200
        headers = types.SimpleNamespace(get_content_charset=lambda: "utf-8")

        def getheaders(self):
            return [("Content-Type", "text/plain")]

        def read(self, limit):
            seen["read_limit"] = limit
            return b"hello"

    def request_and_connect(connection, method, target, headers):
        connection.connect()
        seen["request"] = (method, target, headers)

    monkeypatch.setattr("http.client.HTTPConnection.request", request_and_connect)
    monkeypatch.setattr("http.client.HTTPConnection.getresponse", lambda self: FakeResponse())
    monkeypatch.setattr("http.client.HTTPConnection.close", lambda self: None)

    status, headers, body, charset = ToolRegistry._request_pinned_page(
        urlsplit("http://public.example/path?q=1"), "93.184.216.34"
    )

    assert seen["destination"] == ("93.184.216.34", 80)
    assert seen["request"][0:2] == ("GET", "/path?q=1")
    assert seen["request"][2]["Host"] == "public.example"
    assert status == 200
    assert body == b"hello"
    assert charset == "utf-8"


def test_pinned_https_connection_pins_ip_but_keeps_hostname_for_tls(tmp_path, monkeypatch):
    import socket
    from urllib.parse import urlsplit

    from crows.tools import ToolRegistry

    seen = {}
    raw_socket = object()
    tls_socket = object()

    def create_connection(destination, timeout, source_address):
        seen["destination"] = destination
        return raw_socket

    class FakeTLSContext:
        def wrap_socket(self, sock, server_hostname):
            seen["wrapped_socket"] = sock
            seen["server_hostname"] = server_hostname
            return tls_socket

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr("ssl.create_default_context", FakeTLSContext)

    class FakeResponse:
        status = 200
        headers = types.SimpleNamespace(get_content_charset=lambda: "utf-8")

        def getheaders(self):
            return [("Content-Type", "text/plain; charset=utf-8")]

        def read(self, limit):
            return b"secure"

    def request_and_connect(connection, method, target, headers):
        connection.connect()
        seen["request"] = (method, target, headers)

    monkeypatch.setattr("http.client.HTTPConnection.request", request_and_connect)
    monkeypatch.setattr("http.client.HTTPConnection.getresponse", lambda self: FakeResponse())
    monkeypatch.setattr("http.client.HTTPConnection.close", lambda self: None)

    status, _, body, charset = ToolRegistry._request_pinned_page(
        urlsplit("https://public.example:8443/docs"), "93.184.216.34"
    )

    assert seen["destination"] == ("93.184.216.34", 8443)
    assert seen["wrapped_socket"] is raw_socket
    assert seen["server_hostname"] == "public.example"
    assert seen["request"][0:2] == ("GET", "/docs")
    assert seen["request"][2]["Host"] == "public.example:8443"
    assert status == 200
    assert body == b"secure"
    assert charset == "utf-8"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_pinned_page_rejects_redirect_responses(tmp_path, monkeypatch, status):
    from urllib.parse import urlsplit

    from crows.tools import ToolRegistry

    class FakeResponse:
        def __init__(self):
            self.status = status
            self.headers = types.SimpleNamespace(get_content_charset=lambda: "utf-8")

        def getheaders(self):
            return [("Location", "http://127.0.0.1/admin"), ("Content-Type", "text/html")]

    monkeypatch.setattr("http.client.HTTPConnection.request", lambda *args, **kwargs: None)
    monkeypatch.setattr("http.client.HTTPConnection.getresponse", lambda self: FakeResponse())
    monkeypatch.setattr("http.client.HTTPConnection.close", lambda self: None)

    with pytest.raises(ToolError, match="redirects"):
        ToolRegistry._request_pinned_page(
            urlsplit("http://public.example/redirect"), "93.184.216.34"
        )


def test_pinned_page_response_body_is_bounded_to_one_megabyte(tmp_path, monkeypatch):
    from urllib.parse import urlsplit

    from crows.tools import ToolRegistry

    class FakeResponse:
        status = 200
        headers = types.SimpleNamespace(get_content_charset=lambda: "utf-8")

        def getheaders(self):
            return [("Content-Type", "text/plain")]

        def read(self, limit):
            assert limit == 1_000_001
            return b"x" * limit

    monkeypatch.setattr("http.client.HTTPConnection.request", lambda *args, **kwargs: None)
    monkeypatch.setattr("http.client.HTTPConnection.getresponse", lambda self: FakeResponse())
    monkeypatch.setattr("http.client.HTTPConnection.close", lambda self: None)

    _, _, body, _ = ToolRegistry._request_pinned_page(
        urlsplit("http://public.example/large"), "93.184.216.34"
    )

    assert len(body) == 1_000_000



@pytest.mark.parametrize(
    "address",
    [
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.8",
        "fc00::1",
        "fe80::1",
    ],
)
def test_web_open_rejects_non_public_ipv6_and_ipv4_mapped_addresses(
    tmp_path, monkeypatch, address
):
    import socket

    def unsafe_resolution(host, port, type):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", unsafe_resolution)

    with pytest.raises(ToolError, match="URL is invalid"):
        ToolRegistry(tmp_path).execute(
            "web.open", {"url": "https://mapped-address.example/resource"}
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:invalid/path",
        "http://[not-an-ipv6-address]/",
        "http://example.com:99999/path",
    ],
)
def test_web_open_rejects_malformed_authorities(tmp_path, url):
    with pytest.raises(ToolError, match="URL is invalid"):
        ToolRegistry(tmp_path).execute("web.open", {"url": url})

@pytest.mark.parametrize(
    "url",
    [
        "https://@example.com/resource",
        "https://:@example.com/resource",
        "http://user:@example.com/resource",
        "http://:password@example.com/resource",
    ],
)
def test_web_open_rejects_url_userinfo_with_empty_components(tmp_path, url):
    with pytest.raises(ToolError, match="URL is invalid"):
        ToolRegistry(tmp_path).execute("web.open", {"url": url})



def test_web_open_rejects_explicit_zero_port_before_dns_lookup(tmp_path, monkeypatch):
    import socket

    def unexpected_resolution(*args, **kwargs):
        raise AssertionError("DNS lookup must not happen for an invalid port")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_resolution)

    with pytest.raises(ToolError, match="URL is invalid"):
        ToolRegistry(tmp_path).execute(
            "web.open", {"url": "http://example.com:0/resource"}
        )


def test_web_open_fails_closed_when_dns_resolution_raises(tmp_path, monkeypatch):
    import socket

    def failed_resolution(host, port, type):
        raise socket.gaierror("simulated resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", failed_resolution)

    with pytest.raises(ToolError, match="URL is invalid"):
        ToolRegistry(tmp_path).execute(
            "web.open", {"url": "https://unresolvable.example/resource"}
        )


def test_web_open_fails_closed_when_dns_returns_no_addresses(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, type: [])

    with pytest.raises(ToolError, match="URL is invalid"):
        ToolRegistry(tmp_path).execute(
            "web.open", {"url": "https://empty-resolution.example/resource"}
        )
