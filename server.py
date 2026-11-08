#!/usr/bin/env python3
"""HK-Air Quality PWA: static files + EPD XML proxy."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
import os

ROOT = Path(__file__).resolve().parent
ALLOWED = {
  '24aqhi': 'https://www.aqhi.gov.hk/epd/ddata/html/out/24aqhi_Eng.xml',
  '24pc': 'https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_Eng.xml',
  '24aqhi_tc': 'https://www.aqhi.gov.hk/epd/ddata/html/out/24aqhi_ChT.xml',
  '24pc_tc': 'https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_ChT.xml',
}

class Handler(SimpleHTTPRequestHandler):
  def __init__(self, *a, **k):
    super().__init__(*a, directory=str(ROOT), **k)

  def end_headers(self):
    self.send_header('Access-Control-Allow-Origin', '*')
    self.send_header('Cache-Control', 'no-store' if self.path.startswith('/api/') else 'public, max-age=300')
    super().end_headers()

  def do_OPTIONS(self):
    self.send_response(204)
    self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
    self.send_header('Access-Control-Allow-Headers', '*')
    self.end_headers()

  def do_GET(self):
    u = urlparse(self.path)
    if u.path == '/api/epd':
      key = (parse_qs(u.query).get('f') or [''])[0]
      target = ALLOWED.get(key)
      if not target:
        self.send_error(400, 'unknown f')
        return
      try:
        req = Request(target, headers={'User-Agent': 'HK-Air-Quality-PWA/1.0'})
        with urlopen(req, timeout=30) as r:
          body = r.read()
          ctype = r.headers.get('Content-Type', 'application/xml')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
      except Exception as e:
        msg = str(e).encode()
        self.send_response(502)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)
      return
    if u.path in ('/', ''):
      self.path = '/index.html'
    return SimpleHTTPRequestHandler.do_GET(self)

  def log_message(self, fmt, *args):
    print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

if __name__ == '__main__':
  port = int(os.environ.get('PORT', '8765'))
  print(f'Serving PWA on 0.0.0.0:{port}', flush=True)
  ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
