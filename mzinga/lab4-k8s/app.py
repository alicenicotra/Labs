import json
import os
import socket
import datetime
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

VERSION = os.getenv("APP_VERSION", "1.0.0")
APP_COLOR = os.getenv("APP_COLOR", "blue")
PORT = int(os.getenv("PORT", 8080))


def get_uptime_info():
    """Returns current timestamp as a simple uptime indicator."""
    return datetime.datetime.utcnow().isoformat() + "Z"


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Override to use our custom logger instead of default."""
        log.info(f"{self.address_string()} - {format % args}")

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "version": VERSION})

        elif self.path == "/":
            self._respond(200, {
                "version": VERSION,
                "color": APP_COLOR,
                "hostname": socket.gethostname(),
                "message": f"Hello from version {VERSION}",
                "timestamp": get_uptime_info(),
            })

        elif self.path == "/info":
            self._respond(200, {
                "version": VERSION,
                "color": APP_COLOR,
                "hostname": socket.gethostname(),
                "port": PORT,
                "timestamp": get_uptime_info(),
            })

        else:
            self._respond(404, {"error": "not found", "path": self.path})

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    log.info(f"Starting server — version={VERSION} color={APP_COLOR} port={PORT}")
    server = HTTPServer(("", PORT), Handler)
    server.serve_forever()