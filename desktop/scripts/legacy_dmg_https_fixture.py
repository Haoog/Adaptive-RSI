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
    missing_packages = []
    release_missing_package_body = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/missing-package":
                missing_packages.append(self.headers.get("Range"))
                body = b"Package not found\n"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                # Release the ordinary error body only when the native test exits.
                if release_missing_package_body.wait(timeout=45):
                    try:
                        self.wfile.write(body)
                    except OSError:
                        pass  # The downloader already closed the rejected response.
                return
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
        origin = f"https://127.0.0.1:{server.server_port}"
        env = {
            **os.environ,
            "RSI_DMG_FIXTURE": str(fixture),
            "RSI_DMG_URL": f"{origin}/fixture.dmg",
            "RSI_NATIVE_MISSING_PACKAGE_URL": f"{origin}/missing-package",
        }
        try:
            for test in (
                "regression_tests::legacy_dmg_https_resume_and_real_mount",
                "native_update::response_policy_tests::native_download_rejects_normal_missing_package_before_staging",
            ):
                subprocess.run([*command, test, "--", "--ignored", "--exact", "--nocapture"],
                               env=env, check=True)
        finally:
            release_missing_package_body.set()
            server.shutdown()
            worker.join()
    assert requests == EXPECTED_RANGES, requests
    assert missing_packages == [None], missing_packages
    print(f"Verified HTTPS requests: {requests}; image size {len(image)} bytes", flush=True)


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2:])
