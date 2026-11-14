"""Plain TCP forwarder for the Docker gateway container (standard library only).

  python tcp_forward.py LISTEN_PORT=HOST:PORT [LISTEN_PORT=HOST:PORT ...]

Listens on 0.0.0.0 inside the gateway container; compose publishes those ports on
the host's 127.0.0.1 only. Bytes are relayed unchanged, so the browser's Host and
Origin headers reach MiroFish and its host/CORS checks still apply.
"""

import asyncio
import sys


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


def _handler(target_host: str, target_port: int):
    async def handle(client_reader, client_writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(target_host, target_port)
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))
    return handle


async def main(routes):
    servers = []
    for spec in routes:
        listen, target = spec.split("=", 1)
        host, port = target.rsplit(":", 1)
        servers.append(await asyncio.start_server(_handler(host, int(port)), "0.0.0.0", int(listen)))
        print(f"forwarding :{listen} -> {host}:{port}", flush=True)
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(sys.argv[1:]))
