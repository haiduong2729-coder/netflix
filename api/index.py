"""
Netflix Cookie Checker — Vercel Entry
Serves the SPA HTML. All checker logic is in /api/check.py
"""
from http.server import BaseHTTPRequestHandler
import os, pathlib

HTML_PATH = pathlib.Path(__file__).parent.parent / "public" / "index.html"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            content = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            body = f"<pre>Error: {exc}</pre>".encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *_):
        pass
