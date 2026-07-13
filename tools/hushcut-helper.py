#!/usr/bin/env python3
"""Hushcut Helper — fetches videos + subtitles for the Hushcut app.

Requires yt-dlp:   pip install yt-dlp
Run:               python3 hushcut-helper.py
Then use "Fetch straight from a link" inside Hushcut.

Listens on http://127.0.0.1:8787 (loopback only — nothing is exposed
to your network). Files download to a temp folder; Hushcut pulls them
into the browser from there.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8787
JOBS = {}
WORKDIR = tempfile.mkdtemp(prefix='hushcut-')

BRIDGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hushcut Bridge</title></head>
<body style="font-family:system-ui,sans-serif;background:#f2f1ef;margin:0;padding:26px">
<div style="font-size:14px;font-weight:700;color:#1d1d1f">Hushcut Helper Bridge</div>
<div id="s" style="margin-top:8px;font-size:13px;color:#555">Waiting&hellip;</div>
<div id="manual" style="margin-top:16px;max-width:460px">
  <div style="display:flex;gap:6px">
    <input id="u" placeholder="https://www.youtube.com/watch?v=..." style="flex:1;height:30px;padding:0 10px;border:1px solid #c9c8c5;border-radius:7px;font-size:13px;box-sizing:border-box">
    <button id="go" style="height:30px;padding:0 14px;border:0;border-radius:7px;background:#0a68d6;color:#fff;font-size:12.5px;font-weight:600;cursor:pointer">Download</button>
  </div>
  <div id="links" style="margin-top:10px;font-size:13px"></div>
</div>
<script>
var S = function (t) { document.getElementById('s').textContent = t; };
var target = window.opener || (window.parent !== window ? window.parent : null);
function post(m, tr) { if (target) target.postMessage(m, '*', tr || []); }
function esc(x) { return String(x || '').replace(/"/g, ''); }
function runJob(url, embedded) {
  S('Starting download\u2026');
  document.getElementById('links').innerHTML = '';
  fetch('/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: url }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.id) throw new Error(j.error || 'helper rejected the request');
      var id = j.id;
      var iv = setInterval(function () {
        fetch('/status?id=' + id).then(function (r) { return r.json(); }).then(function (st) {
          if (st.state === 'error') {
            clearInterval(iv); S('Error: ' + st.error);
            post({ type: 'hushcut-error', error: st.error });
          } else if (st.state === 'done') {
            clearInterval(iv);
            if (embedded) {
              S('Transferring to Hushcut\u2026');
              fetch('/file?id=' + id).then(function (r) { return r.arrayBuffer(); }).then(function (buf) {
                var finish = function (subs) {
                  post({ type: 'hushcut-file', name: st.name, subsText: subs, buffer: buf }, [buf]);
                  S('Done.');
                };
                if (st.hasSubs) fetch('/subs?id=' + id).then(function (r) { return r.text(); }).then(finish, function () { finish(null); });
                else finish(null);
              });
            } else {
              S('Done \u2014 save both files, then drop them into Hushcut.');
              var h = '<a href="/file?id=' + id + '" download="' + esc(st.name) + '">Save video</a>';
              if (st.hasSubs) h += ' &nbsp;\u00b7&nbsp; <a href="/subs?id=' + id + '" download="subtitles.srt">Save subtitles</a>';
              document.getElementById('links').innerHTML = h;
            }
          } else {
            S('Downloading\u2026 ' + Math.round(st.pct || 0) + '%');
            post({ type: 'hushcut-progress', pct: st.pct || 0 });
          }
        }, function (err) { clearInterval(iv); S('Error: ' + err); post({ type: 'hushcut-error', error: String(err) }); });
      }, 900);
    })
    .catch(function (err) { S('Error: ' + err.message); post({ type: 'hushcut-error', error: err.message }); });
}
window.addEventListener('message', function (e) {
  var d = e.data || {};
  if (d.type !== 'hushcut-download') return;
  document.getElementById('manual').style.display = 'none';
  runJob(d.url, true);
});
document.getElementById('go').onclick = function () {
  var u = document.getElementById('u').value.trim();
  if (u) runJob(u, false);
};
document.getElementById('u').addEventListener('keydown', function (e) { if (e.key === 'Enter') document.getElementById('go').onclick(); });
post({ type: 'hushcut-bridge-ready' });
S(target ? 'Connected. Waiting for download request\u2026' : 'Paste a link to download \u2014 files save to disk, then drop them into Hushcut.');
</script></body></html>
"""

VIDEO_EXTS = ('mp4', 'mkv', 'webm', 'm4v', 'mov')


def run_job(job_id, url):
    job = JOBS[job_id]
    outdir = os.path.join(WORKDIR, job_id)
    os.makedirs(outdir, exist_ok=True)
    cmd = [
        sys.executable, '-m', 'yt_dlp',
        '-f', 'bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4]/b',
        '--merge-output-format', 'mp4',
        '--write-subs', '--write-auto-subs',
        '--sub-langs', 'en.*',
        '--newline', '--no-playlist',
        '-o', os.path.join(outdir, '%(title).120B.%(ext)s'),
        url,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            m = re.search(r'\[download\]\s+([\d.]+)%', line)
            if m:
                job['pct'] = float(m.group(1))
            if 'has already been downloaded' in line:
                job['pct'] = 100.0
        proc.wait()
        if proc.returncode != 0:
            job['state'] = 'error'
            job['error'] = ('yt-dlp exited with code %d — try updating it '
                            '(pip install -U yt-dlp)' % proc.returncode)
            return
        vids = [f for f in os.listdir(outdir)
                if f.rsplit('.', 1)[-1].lower() in VIDEO_EXTS]
        subs = [f for f in os.listdir(outdir)
                if f.endswith('.srt') or f.endswith('.vtt')]
        if not vids:
            job['state'] = 'error'
            job['error'] = 'yt-dlp finished but produced no video file'
            return
        vids.sort(key=lambda f: os.path.getsize(os.path.join(outdir, f)),
                  reverse=True)
        job['file'] = os.path.join(outdir, vids[0])
        job['name'] = vids[0]
        if subs:
            job['subs'] = os.path.join(outdir, subs[0])
        job['pct'] = 100.0
        job['state'] = 'done'
    except FileNotFoundError:
        job['state'] = 'error'
        job['error'] = 'yt-dlp not found — run: pip install yt-dlp'
    except Exception as e:  # noqa: BLE001
        job['state'] = 'error'
        job['error'] = str(e)


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        # Chrome Private/Local Network Access opt-in (https page -> 127.0.0.1)
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Access-Control-Allow-Local-Network', 'true')

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == '/health':
            return self._json({'ok': True, 'app': 'hushcut-helper'})
        if u.path in ('/', '/bridge'):
            body = BRIDGE_HTML.encode()
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        job = JOBS.get((q.get('id') or [''])[0])
        if u.path == '/status':
            if not job:
                return self._json({'state': 'error', 'error': 'unknown job'}, 404)
            return self._json({
                'state': job.get('state'),
                'pct': job.get('pct', 0),
                'error': job.get('error'),
                'hasSubs': bool(job.get('subs')),
                'name': job.get('name', 'video.mp4'),
            })
        if u.path in ('/file', '/subs'):
            key = 'file' if u.path == '/file' else 'subs'
            if not job or not job.get(key):
                return self._json({'error': 'not ready'}, 404)
            path = job[key]
            self.send_response(200)
            self._cors()
            ctype = 'video/mp4' if key == 'file' else 'text/plain; charset=utf-8'
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(os.path.getsize(path)))
            self.end_headers()
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return None
        return self._json({'error': 'not found'}, 404)

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path != '/download':
            return self._json({'error': 'not found'}, 404)
        ln = int(self.headers.get('Content-Length') or 0)
        try:
            data = json.loads(self.rfile.read(ln) or b'{}')
        except ValueError:
            data = {}
        url = (data.get('url') or '').strip()
        if not url:
            return self._json({'error': 'missing url'}, 400)
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {'state': 'downloading', 'pct': 0.0}
        threading.Thread(target=run_job, args=(job_id, url), daemon=True).start()
        return self._json({'id': job_id})

    def log_message(self, *args):  # silence request logging
        pass


if __name__ == '__main__':
    print('Hushcut Helper running on http://127.0.0.1:%d' % PORT)
    print('Leave this window open, then use "Fetch straight from a link" in Hushcut.')
    print('Ctrl+C to stop.')
    try:
        ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
