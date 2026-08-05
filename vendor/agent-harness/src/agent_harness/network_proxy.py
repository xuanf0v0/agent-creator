from __future__ import annotations

import asyncio
from urllib.parse import urlsplit


class AllowlistProxy:
    def __init__(self, allowlist: list[str]) -> None:
        self.allowlist = allowlist
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self.port

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def _allowed(self, host: str, port: int) -> bool:
        host = host.rstrip(".").lower()
        for rule in self.allowlist:
            value = rule.strip().lower()
            rule_host, separator, rule_port = value.rpartition(":")
            if not separator or not rule_port.isdigit():
                rule_host, rule_port = value, ""
            if rule_port and int(rule_port) != port: continue
            if rule_host.startswith("*.") and host.endswith(rule_host[1:]) and host != rule_host[2:]: return True
            if host == rule_host: return True
        return False

    async def _handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            header = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), 10)
            if len(header) > 65536: raise ValueError("proxy header too large")
            first, *lines = header.split(b"\r\n")
            method, target, version = first.decode("latin-1").split(" ", 2)
            if method.upper() == "CONNECT":
                host, port = self._host_port(target, 443)
                if not self._allowed(host, port): raise PermissionError("destination is not allowlisted")
                upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n"); await client_writer.drain()
            else:
                parsed = urlsplit(target)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname: raise ValueError("absolute proxy URL required")
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if not self._allowed(parsed.hostname, port): raise PermissionError("destination is not allowlisted")
                upstream_reader, upstream_writer = await asyncio.open_connection(parsed.hostname, port)
                path = parsed.path or "/"
                if parsed.query: path += "?" + parsed.query
                forwarded = [f"{method} {path} {version}".encode("latin-1")]
                forwarded.extend(line for line in lines if not line.lower().startswith(b"proxy-connection:"))
                upstream_writer.write(b"\r\n".join(forwarded)); await upstream_writer.drain()
            await asyncio.gather(self._pipe(client_reader, upstream_writer), self._pipe(upstream_reader, client_writer))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError, ValueError, PermissionError):
            if not client_writer.is_closing():
                client_writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                try: await client_writer.drain()
                except OSError: pass
        finally:
            if upstream_writer:
                upstream_writer.close()
            client_writer.close()

    @staticmethod
    def _host_port(value: str, default: int) -> tuple[str, int]:
        if value.startswith("["):
            host, _, suffix = value[1:].partition("]")
            return host, int(suffix[1:]) if suffix.startswith(":") else default
        host, separator, port = value.rpartition(":")
        return (host, int(port)) if separator and port.isdigit() else (value, default)

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data); await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try: writer.write_eof()
            except (OSError, AttributeError): pass
