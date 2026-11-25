"""Serve a synthetic DMG over trusted loopback TLS for the macOS runner."""

import os
from pathlib import Path
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


CUTOFF = 256 * 1024
SEGMENT_END = 512 * 1024
EXPECTED_RANGES = [None, f"bytes={CUTOFF}-", f"bytes={SEGMENT_END}-"]


def run(fixture, command):
    image = (fixture / "source.dmg").read_bytes()
    assert len(image) > SEGMENT_END
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            position = len(requests)
            requested_range = self.headers.get("Range")
            requests.append(requested_range)
            if (self.path != "/fixture.dmg" or position >= len(EXPECTED_RANGES)
                    or requested_range != EXPECTED_RANGES[position]
                    or self.headers.get("Accept-Encoding") != "identity"):
                self.send_error(400, "Unexpected fixture request")
                return
            if position == 0:
                self.send_response(200)
                self.send_header("Content-Length", str(len(image)))
                body = image[:CUTOFF]
            else:
                start, end = ((CUTOFF, SEGMENT_END) if position == 1
                              else (SEGMENT_END, len(image)))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end - 1}/{len(image)}")
                self.send_header("Content-Length", str(end - start))
                body = image[start:end]
            self.end_headers()
            self.wfile.write(body)
            # The first connection closes after the prefix, before its advertised length.

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(fixture / "server.pem", fixture / "server.key")
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            subprocess.run(command, env={
                **os.environ,
                "RSI_DMG_FIXTURE": str(fixture),
                "RSI_DMG_URL": f"https://127.0.0.1:{server.server_port}/fixture.dmg",
            }, check=True)
        finally:
            server.shutdown()
            worker.join()
    assert requests == EXPECTED_RANGES, requests
    print(f"Verified HTTPS requests: {requests}; image size {len(image)} bytes", flush=True)


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2:])
