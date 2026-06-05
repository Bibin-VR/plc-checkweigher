#!/usr/bin/env python3
"""
PDF Receiver — run this on the PC that should receive reports from the Pi.

Usage:
  python3 pdf_receiver.py                         # default port 9090, saves to ~/received_reports/
  python3 pdf_receiver.py --port 9090 --dir C:\\Reports --open
  python3 pdf_receiver.py --port 9090 --dir /home/user/reports --open

Options:
  --port  PORT   Port to listen on (default: 9090)
  --dir   DIR    Folder to save PDFs into (created if missing)
  --open         Auto-open each PDF in the default viewer when it arrives
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_PORT = 9090
DEFAULT_DIR  = os.path.join(os.path.expanduser("~"), "received_reports")


class ReceiverHandler(BaseHTTPRequestHandler):
    save_dir  = DEFAULT_DIR
    auto_open = False

    def log_message(self, *_):
        pass  # silence default access log

    def do_POST(self):
        filename = os.path.basename(self.path)
        if not filename.lower().endswith(".pdf"):
            self._respond(400, b"Only .pdf files accepted")
            return

        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._respond(400, b"Empty body")
            return

        data = self.rfile.read(length)
        os.makedirs(self.save_dir, exist_ok=True)
        dest = os.path.join(self.save_dir, filename)

        with open(dest, "wb") as f:
            f.write(data)

        now = datetime.now().strftime("%H:%M:%S")
        kb  = len(data) / 1024
        print(f"[{now}]  ✓ {filename}  ({kb:.1f} KB)  →  {dest}")

        if self.auto_open:
            _open_file(dest)

        self._respond(200, b"OK")

    def do_GET(self):
        # Simple status page so you can check the receiver is alive from a browser
        body = (
            b"<h2>PDF Receiver running</h2>"
            b"<p>POST a .pdf to <code>/&lt;filename&gt;.pdf</code></p>"
        )
        self._respond(200, body, "text/html")

    def _respond(self, code, body, ct="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _open_file(path: str):
    try:
        if sys.platform == "win32":
            os.startfile(path)          # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"  [open] {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Receive PDFs pushed from the check-weigher Pi"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--dir",  default=DEFAULT_DIR,
                        help="Directory to save received PDFs")
    parser.add_argument("--open", action="store_true",
                        help="Auto-open each PDF in the default viewer")
    args = parser.parse_args()

    ReceiverHandler.save_dir  = args.dir
    ReceiverHandler.auto_open = args.open

    os.makedirs(args.dir, exist_ok=True)
    server = HTTPServer(("0.0.0.0", args.port), ReceiverHandler)

    ip = _local_ip()
    print("─" * 52)
    print(f"  PDF Receiver  listening on  port {args.port}")
    print(f"  Saving to     {args.dir}")
    print(f"  Auto-open     {'yes' if args.open else 'no'}")
    print(f"  Your IP       {ip}")
    print("─" * 52)
    print(f"  On the Pi set:  PUSH_HOST = \"{ip}\"  in pdf_push.py")
    print("─" * 52)
    print("Waiting for reports…  (Ctrl+C to stop)\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def _local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


if __name__ == "__main__":
    main()
