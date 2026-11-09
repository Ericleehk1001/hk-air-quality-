#!/usr/bin/env python3
"""HK-Air Quality PWA: static files + EPD XML proxy.

aqhi.gov.hk is signed by Hongkong Post. Browsers complete that chain via
Schannel/AIA; Windows Python often cannot, because the Hongkong Post root
is not in the trust store urllib actually loads. The failure is
``unable to get local issuer certificate``, the proxy returns 502, and the
page falls back to the data.gov.hk archive (yesterday only).

``certs/hongkong-post-aqhi.pem`` supplies the current intermediate and the
Hongkong Post Root CA 3 certificate that GlobalSign cross-signed. OpenSSL
can then build leaf → intermediate → cross-signed root → GlobalSign.
Verification stays on (hostname, expiry, and a trusted root).
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener, urlopen
import http.client
import os
import socket
import ssl
import time
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
HKPOST_CA_FILE = ROOT / 'certs' / 'hongkong-post-aqhi.pem'
ALLOWED = {
  '24aqhi': 'https://www.aqhi.gov.hk/epd/ddata/html/out/24aqhi_Eng.xml',
  '24pc': 'https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_Eng.xml',
  '24aqhi_tc': 'https://www.aqhi.gov.hk/epd/ddata/html/out/24aqhi_ChT.xml',
  '24pc_tc': 'https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_ChT.xml',
}
EPD_HEADERS = {
  'User-Agent': 'HK-Air-Quality-PWA/1.0',
  'Accept': 'application/xml, text/xml, */*',
}
_SSL_CONTEXT = None


def _windows_store_pems():
  """PEMs from the Windows trust stores. No-op off Windows.

  Python's OpenSSL does not see the Windows store unless we load it.
  Enterprise TLS inspection roots live there; browsers already trust them.
  Explicitly distrusted certificates are skipped.
  """
  if os.name != 'nt' or not hasattr(ssl, 'enum_certificates'):
    return []
  server_auth = '1.3.6.1.5.5.7.3.1'
  pems = []
  for store in ('ROOT', 'CA'):
    try:
      entries = ssl.enum_certificates(store)
    except OSError:
      continue
    for cert, encoding, trust in entries:
      if encoding != 'x509_asn' or trust is False:
        continue
      if trust is not True and not (isinstance(trust, (set, frozenset, tuple, list)) and server_auth in trust):
        continue
      try:
        pems.append(ssl.DER_cert_to_PEM_cert(cert))
      except Exception:
        continue
  return pems


def _load_pems(ctx, pems):
  chunk = []
  for pem in pems:
    chunk.append(pem.strip())
    if len(chunk) >= 40:
      _load_chunk(ctx, chunk)
      chunk = []
  if chunk:
    _load_chunk(ctx, chunk)


def _load_chunk(ctx, chunk):
  blob = '\n'.join(chunk)
  try:
    ctx.load_verify_locations(cadata=blob)
  except Exception:
    for pem in chunk:
      try:
        ctx.load_verify_locations(cadata=pem)
      except Exception:
        continue


def build_epd_ssl_context():
  """Verified TLS context that can chain aqhi.gov.hk to a trusted root."""
  ctx = ssl.create_default_context()
  if not HKPOST_CA_FILE.is_file():
    raise FileNotFoundError(f'缺少憑證檔 {HKPOST_CA_FILE.name}')
  ctx.load_verify_locations(cafile=str(HKPOST_CA_FILE))
  _load_pems(ctx, _windows_store_pems())
  return ctx


def epd_ssl_context():
  global _SSL_CONTEXT
  if _SSL_CONTEXT is None:
    _SSL_CONTEXT = build_epd_ssl_context()
  return _SSL_CONTEXT


def _clip(text, limit=160):
  text = ' '.join(str(text).split())
  if len(text) <= limit:
    return text
  return text[:limit - 1] + '…'


def explain_fetch_error(exc):
  """One short line for the HTTP 502 body and the page status."""
  reason = exc.reason if isinstance(exc, URLError) and getattr(exc, 'reason', None) is not None else exc
  if isinstance(reason, ssl.SSLCertVerificationError):
    detail = getattr(reason, 'verify_message', '') or str(reason)
    return _clip(f'SSL 憑證驗證失敗：{detail}')
  if isinstance(reason, ssl.SSLError):
    return _clip(f'SSL 連線失敗：{reason}')
  if isinstance(exc, HTTPError):
    return f'環保署 HTTP {exc.code}'
  text = str(reason)
  low = text.lower()
  if 'timed out' in low or 'timeout' in low:
    return '連線環保署逾時'
  if 'getaddrinfo' in low or 'name or service not known' in low or 'nodename nor servname' in low or 'temporary failure in name resolution' in low:
    return '無法解析 www.aqhi.gov.hk'
  if 'certificate verify failed' in low or 'certificate_verify_failed' in low:
    return _clip(f'SSL 憑證驗證失敗：{text}')
  return _clip(text or exc.__class__.__name__)


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
  """HTTPS connection that never tries IPv6 (broken stacks time out)."""

  def connect(self):
    infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
    if not infos:
      raise OSError(f'no IPv4 address for {self.host}')
    last = None
    for af, socktype, proto, _canon, sa in infos:
      sock = socket.socket(af, socktype, proto)
      try:
        if self.timeout is not None:
          sock.settimeout(self.timeout)
        sock.connect(sa)
        self.sock = sock
        server_hostname = self._tunnel_host or self.host
        if self._tunnel_host:
          self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)
        return
      except Exception as e:
        last = e
        try:
          sock.close()
        except Exception:
          pass
    raise last or OSError(f'IPv4 connect failed for {self.host}')


class _IPv4HTTPSHandler(HTTPSHandler):
  def https_open(self, req):
    kwargs = {'context': self._context}
    # Some Python builds keep this on the handler; 3.12 folds it into the context.
    if hasattr(self, '_check_hostname'):
      kwargs['check_hostname'] = self._check_hostname
    return self.do_open(_IPv4HTTPSConnection, req, **kwargs)


def _ipv4_opener(context):
  return build_opener(_IPv4HTTPSHandler(context=context))


def _read_http(url, timeout, context, force_ipv4):
  req = Request(url, headers=EPD_HEADERS)
  if force_ipv4:
    opener = _ipv4_opener(context)
    resp_cm = opener.open(req, timeout=timeout)
  else:
    resp_cm = urlopen(req, timeout=timeout, context=context)
  with resp_cm as resp:
    body = resp.read()
    ctype = resp.headers.get('Content-Type', 'application/xml')
  return body, ctype


def _looks_like_epd_xml(body):
  sample = body.lstrip().removeprefix(b'\xef\xbb\xbf')[:800]
  if sample.lower().startswith((b'<!doctype html', b'<html')):
    return False
  head = sample.lower()
  return (
    b'<aqhi24hrpollutantconcentration' in head
    or b'<aqhi24hrreport' in head
    or (sample.startswith(b'<?xml') and (b'aqhi' in head or b'pollutant' in head))
  )


def fetch_epd(url, timeout=15):
  """GET one EPD XML document. Retries once over IPv4 on network errors."""
  context = epd_ssl_context()
  attempts = ((False, timeout), (True, timeout))
  last = None
  for index, (force_ipv4, attempt_timeout) in enumerate(attempts):
    try:
      body, ctype = _read_http(url, attempt_timeout, context, force_ipv4)
    except ssl.SSLError:
      raise
    except HTTPError as e:
      if 400 <= e.code < 500:
        raise
      last = e
    except URLError as e:
      # urlopen wraps handshake failures in URLError. Retrying will not fix trust.
      if isinstance(getattr(e, 'reason', None), ssl.SSLError):
        raise
      last = e
    except Exception as e:
      last = e
    else:
      if not body or not _looks_like_epd_xml(body):
        raise RuntimeError('環保署回應唔係 AQHI XML')
      return body, (ctype.split(';')[0].strip() or 'application/xml')
    if index + 1 < len(attempts):
      time.sleep(0.4)
  raise last or RuntimeError('無法連線環保署')


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
        body, ctype = fetch_epd(target)
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
      except Exception as e:
        msg = explain_fetch_error(e).encode('utf-8')
        print(f'[epd] {key} failed: {msg.decode("utf-8")}', flush=True)
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
