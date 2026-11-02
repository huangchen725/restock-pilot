import os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args):
        pass
print(os.environ.get("INTERN_API_KEY", ""), flush=True)
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
