#!/usr/bin/env python3
"""Hushcut Server.

Checks a list of channels on a schedule, downloads new videos that fall inside
each channel's date window (yt-dlp), scans their subtitles for profanity
(built-in list + YouTube's censored "[ __ ]" marker + custom words), silences
each hit with ffmpeg (video stream copied), and serves a status dashboard.

Config:  /config/config.yaml   (see config.example.yaml)
Data:    /data/{incoming,clean,originals,state}
Web:     http://localhost:8788
"""
import calendar
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    print('pyyaml is required: pip install pyyaml')
    sys.exit(1)

CONFIG_PATH = os.environ.get('HUSHCUT_CONFIG', '/config/config.yaml')
DATA = os.environ.get('HUSHCUT_DATA', '/data')
PORT = int(os.environ.get('HUSHCUT_PORT', '8788'))

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.m4v', '.mov')

BASE_PROFANITY = (
    r"\b(f+u+c+k\w*|s+h+i+t\w*|goddamn\w*|damn(ed|it)?|hell|a+s+s(h+o+l+e\w*|es)?"
    r"|b+i+t+c+h\w*|bastard\w*|crap\w*|d+i+c+k(head\w*)?|p+i+s+s\w*|c+o+c+k(sucker\w*)?"
    r"|motherfuck\w*|bullshit\w*|prick\w*|douche(bag\w*)?|wtf|jackass\w*|dumbass\w*"
    r"|arse\w*|bollocks|twat\w*|wank\w*|slut\w*|whore\w*)\b"
    r"|\[\s*_+\s*\]"  # YouTube auto-caption censor marker
)

TIME_RE = re.compile(
    r'(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})')
INLINE_RE = re.compile(r'<(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})>')

STATE_LOCK = threading.Lock()
STATE = {
    'recent': [], 'channels': [], 'next_check': None, 'busy': False,
    'started': datetime.now(timezone.utc).isoformat(),
}


def log(*args):
    print('[hushcut]', *args, flush=True)


def ensure_dirs():
    for d in ('incoming', 'clean', 'originals', 'state'):
        os.makedirs(os.path.join(DATA, d), exist_ok=True)


def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault('settings', {})
    cfg.setdefault('channels', [])
    return cfg


# ── date windows ─────────────────────────────────────────────

def month_bounds(v):
    y, m = (int(x) for x in v.split('-'))
    last = calendar.monthrange(y, m)[1]
    return '%04d-%02d-01' % (y, m), '%04d-%02d-%02d' % (y, m, last)


def date_window(ch):
    frm, to = ch.get('from'), ch.get('to')
    if frm and re.fullmatch(r'\d{4}-\d{2}', str(frm)) and not to:
        frm, to = month_bounds(str(frm))

    def compact(x):
        return re.sub(r'\D', '', str(x)) if x else None

    return compact(frm), compact(to)


# ── word detection ───────────────────────────────────────────

def build_word_regex(extra_words):
    pats = [BASE_PROFANITY]
    for w in (extra_words or []):
        w = str(w).strip().lower()
        if not w:
            continue
        esc = re.escape(w).replace(r'\ ', r'\s+')
        pre = r'\b' if re.match(r'\w', w) else ''
        post = r'\b' if re.search(r'\w$', w) else ''
        pats.append(pre + esc + post)
    return re.compile('|'.join('(?:%s)' % p for p in pats), re.I)


def _ts(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(str(ms).ljust(3, '0')) / 1000.0


def parse_subs(path):
    """Parse .srt/.vtt. Handles YouTube auto-caption quirks: rolling duplicate
    lines are dropped and inline word timestamps are kept as (pos, time) marks."""
    with open(path, encoding='utf-8', errors='replace') as f:
        text = f.read().replace('\r', '')
    cues = []
    prev_lines = set()

    def norm(line):
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', line)).strip().lower()

    for block in re.split(r'\n\s*\n+', text):
        lines = [l for l in block.split('\n') if l.strip()]
        ti = next((i for i, l in enumerate(lines) if TIME_RE.search(l)), None)
        if ti is None:
            continue
        m = TIME_RE.search(lines[ti])
        s, e = _ts(*m.groups()[0:4]), _ts(*m.groups()[4:8])
        payload = lines[ti + 1:]
        all_norm = {norm(l) for l in payload if norm(l)}
        if any(INLINE_RE.search(l) for l in payload):
            payload = [l for l in payload if INLINE_RE.search(l)]
        payload = [l for l in payload if norm(l) and norm(l) not in prev_lines]
        prev_lines = all_norm

        raw = ' '.join(payload)
        txt, marks, i = '', [], 0
        while i < len(raw):
            ch = raw[i]
            if ch == '<':
                j = raw.find('>', i)
                if j < 0:
                    break
                tm = INLINE_RE.search(raw[i:j + 1])
                if tm:
                    marks.append((len(txt), _ts(*tm.groups())))
                i = j + 1
            elif ch == '{':
                j = raw.find('}', i)
                i = (j + 1) if j >= 0 else (i + 1)
            elif ch.isspace():
                if txt and txt[-1] != ' ':
                    txt += ' '
                i += 1
            else:
                txt += ch
                i += 1
        txt = txt.strip()
        if txt and e > s:
            cues.append({'s': s, 'e': e, 'text': txt, 'marks': marks})
    return cues


def detect(cues, word_re):
    words = []
    for c in cues:
        for mt in word_re.finditer(c['text']):
            marks, mi = c['marks'], -1
            for k, (pos, _t) in enumerate(marks):
                if pos <= mt.start():
                    mi = k
                else:
                    break
            if mi >= 0:
                t = marks[mi][1]
                nxt = marks[mi + 1][1] if mi + 1 < len(marks) else min(c['e'], t + 0.8)
                d = min(1.2, max(0.25, nxt - t))
            elif marks:
                t = c['s']
                d = min(1.2, max(0.25, marks[0][1] - c['s']))
            else:
                ratio = mt.start() / max(1, len(c['text']))
                t = c['s'] + ratio * (c['e'] - c['s'])
                d = min(1.0, max(0.3, 0.2 + len(mt.group(0)) * 0.06))
            words.append({'w': mt.group(0), 't': t, 'd': d})
    return words


def merge_intervals(words, lead, tail):
    spans = sorted((max(0.0, w['t'] - lead), w['t'] + w['d'] + tail) for w in words)
    out = []
    for a, b in spans:
        if out and a <= out[-1][1] + 0.05:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# ── media pipeline ───────────────────────────────────────────

def mute_video(src, dst, intervals):
    if not intervals:
        shutil.copy2(src, dst)
        return
    filters = ','.join(
        "volume=enable='between(t,%.3f,%.3f)':volume=0" % (a, b) for a, b in intervals)
    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', src,
           '-c:v', 'copy', '-af', filters, '-c:a', 'aac', '-b:a', '192k', dst]
    subprocess.run(cmd, check=True)


def sync_channel(ch, cfg):
    st = cfg['settings']
    name = str(ch.get('name') or ch.get('url'))
    outdir = os.path.join(DATA, 'incoming', re.sub(r'[^\w\- ]+', '', name)[:60] or 'channel')
    os.makedirs(outdir, exist_ok=True)
    frm, to = date_window(ch)
    cmd = ['yt-dlp',
           '-f', st.get('quality', 'bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b'),
           '--merge-output-format', 'mp4',
           '--write-subs', '--write-auto-subs', '--sub-langs', st.get('sub_langs', 'en.*'),
           '--download-archive', os.path.join(DATA, 'state', 'archive.txt'),
           '--lazy-playlist', '--newline', '--ignore-errors',
           '-o', os.path.join(outdir, '%(title).150B [%(id)s].%(ext)s')]
    if frm:
        cmd += ['--dateafter', frm,
                '--break-match-filters', 'upload_date >= %s' % frm]
    if to:
        cmd += ['--datebefore', to]
    cmd.append(str(ch['url']))
    log('sync:', name, 'window:', frm or '-', '→', to or '-')
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3 * 3600)
        tail_lines = [l for l in (p.stdout or '').splitlines() if l.strip()][-3:]
        for l in tail_lines:
            log('  ', l)
    except Exception as e:  # noqa: BLE001
        log('sync error:', name, e)


def find_subs(root, base):
    for ext in ('.vtt', '.srt'):
        cands = sorted(glob.glob(glob.escape(os.path.join(root, base)) + '*' + ext))
        if cands:
            return cands[0]
    return None


def process_incoming(cfg):
    st = cfg['settings']
    word_re = build_word_regex(st.get('extra_words'))
    lead = float(st.get('mute_lead', 0.4))
    tail = float(st.get('mute_tail', 0.3))
    keep = bool(st.get('keep_original', False))

    for root, _dirs, files in os.walk(os.path.join(DATA, 'incoming')):
        for fname in sorted(files):
            if not fname.lower().endswith(VIDEO_EXTS):
                continue
            src = os.path.join(root, fname)
            base = os.path.splitext(fname)[0]
            channel = os.path.basename(root)
            dstdir = os.path.join(DATA, 'clean', channel)
            os.makedirs(dstdir, exist_ok=True)
            dst = os.path.join(dstdir, base + ' (clean).mp4')
            if os.path.exists(dst):
                continue
            subs = find_subs(root, base)
            entry = {'time': datetime.now(timezone.utc).isoformat(), 'channel': channel,
                     'title': base, 'words': 0, 'seconds': 0.0, 'status': 'ok', 'note': ''}
            try:
                if subs:
                    words = detect(parse_subs(subs), word_re)
                    spans = merge_intervals(words, lead, tail)
                    mute_video(src, dst, spans)
                    entry['words'] = len(words)
                    entry['seconds'] = round(sum(b - a for a, b in spans), 1)
                    entry['note'] = 'muted' if words else 'nothing found'
                elif st.get('on_missing_subs', 'copy') == 'skip':
                    entry['status'] = 'skipped'
                    entry['note'] = 'no subtitles found'
                    push_recent(entry)
                    continue
                else:
                    shutil.copy2(src, dst)
                    entry['status'] = 'warning'
                    entry['note'] = 'no subtitles — copied unmuted'

                leftovers = [src] + ([subs] if subs else [])
                if keep:
                    odir = os.path.join(DATA, 'originals', channel)
                    os.makedirs(odir, exist_ok=True)
                    for pth in leftovers:
                        shutil.move(pth, os.path.join(odir, os.path.basename(pth)))
                else:
                    for pth in leftovers:
                        try:
                            os.remove(pth)
                        except OSError:
                            pass
                log('processed:', base, '-', entry['words'], 'words,',
                    entry['seconds'], 's silenced')
            except Exception as e:  # noqa: BLE001
                entry['status'] = 'error'
                entry['note'] = str(e)[:300]
                log('process error:', base, e)
            push_recent(entry)


# ── state + dashboard ────────────────────────────────────────

def push_recent(entry):
    with STATE_LOCK:
        STATE['recent'].insert(0, entry)
        del STATE['recent'][200:]
        save_state()


def save_state():
    try:
        with open(os.path.join(DATA, 'state', 'state.json'), 'w', encoding='utf-8') as f:
            json.dump({'recent': STATE['recent']}, f)
    except OSError as e:
        log('state save failed:', e)


def load_state():
    try:
        with open(os.path.join(DATA, 'state', 'state.json'), encoding='utf-8') as f:
            STATE['recent'] = (json.load(f) or {}).get('recent', [])
    except (OSError, ValueError):
        pass


DASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hushcut Server</title>
<style>
body{margin:0;background:#f2f1ef;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;color:#1d1d1f}
.bar{height:52px;display:flex;align-items:center;gap:10px;padding:0 20px;background:#eae9e7;border-bottom:1px solid rgba(0,0,0,.09)}
.glyph{width:24px;height:24px;border-radius:6px;background:#1d1d1f;display:flex;align-items:center;justify-content:center;color:#fff;font-size:12px;font-weight:700}
h1{font-size:14px;margin:0}
.pill{font-size:11px;font-weight:700;padding:3px 9px;border-radius:10px;background:#e3edfb;color:#0a68d6}
.pill.busy{background:#fdf6ec;color:#8a5a12}
.wrap{max-width:980px;margin:20px auto;padding:0 20px;display:flex;flex-direction:column;gap:14px}
.card{background:#fff;border:1px solid rgba(0,0,0,.08);border-radius:10px;padding:14px 16px}
.card h2{font-size:11px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.05em;color:#86868b}
.ch{display:inline-flex;gap:8px;align-items:baseline;background:#f6f5f3;border:1px solid rgba(0,0,0,.07);border-radius:7px;padding:6px 10px;margin:0 8px 8px 0;font-size:12.5px;font-weight:600}
.ch small{font-weight:500;color:#6e6e73}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:#a1a09d;padding:6px 8px;border-bottom:1px solid rgba(0,0,0,.08)}
td{padding:7px 8px;border-bottom:1px solid rgba(0,0,0,.05);vertical-align:top}
.n{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#6e6e73;white-space:nowrap}
.flag{color:#b3261e;font-weight:700}
.ok{color:#2f9e44;font-weight:600}.warning{color:#8a5a12;font-weight:600}
.error{color:#b3261e;font-weight:600}.skipped{color:#6e6e73}
.empty{color:#a1a09d;font-size:12.5px;padding:8px}
</style></head><body>
<div class="bar"><div class="glyph">H</div><h1>Hushcut Server</h1>
<span id="busy" class="pill">idle</span><span style="flex:1"></span><span id="next" class="n"></span></div>
<div class="wrap">
<div class="card"><h2>Channels</h2><div id="channels" class="empty">Loading&hellip;</div></div>
<div class="card"><h2>Recent videos</h2><div style="overflow-x:auto">
<table><thead><tr><th>When</th><th>Channel</th><th>Title</th><th>Flagged</th><th>Silenced</th><th>Status</th></tr></thead>
<tbody id="rows"></tbody></table>
<div id="empty" class="empty" style="display:none">Nothing processed yet.</div></div></div>
</div>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function fmt(iso){if(!iso)return'';return new Date(iso).toLocaleString()}
function tick(){
 fetch('/status').then(function(r){return r.json()}).then(function(s){
  var busy=document.getElementById('busy');
  busy.textContent=s.busy?'checking\\u2026':'idle';
  busy.className='pill'+(s.busy?' busy':'');
  document.getElementById('next').textContent=s.next_check?('next check '+fmt(s.next_check)):'';
  var ch=document.getElementById('channels');
  ch.className='';
  ch.innerHTML=(s.channels||[]).map(function(c){
   var w=c.from?(c.from+(c.to?(' \\u2192 '+c.to):' \\u2192')):'all dates';
   return '<span class="ch">'+esc(c.name)+'<small>'+esc(w)+'</small></span>';
  }).join('')||'<span class="empty">No channels configured \\u2014 edit config/config.yaml</span>';
  document.getElementById('rows').innerHTML=(s.recent||[]).map(function(r){
   return '<tr><td class="n">'+fmt(r.time)+'</td><td>'+esc(r.channel)+'</td><td>'+esc(r.title)+
    '</td><td class="'+(r.words?'flag':'')+'">'+(r.words||0)+'</td><td class="n">'+(r.seconds||0)+
    's</td><td class="'+esc(r.status)+'">'+esc(r.status)+(r.note?(' \\u2014 '+esc(r.note)):'')+'</td></tr>';
  }).join('');
  document.getElementById('empty').style.display=(s.recent&&s.recent.length)?'none':'block';
 }).catch(function(){});
}
tick();setInterval(tick,5000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        p = urlparse(self.path).path
        if p == '/status':
            with STATE_LOCK:
                body = json.dumps({
                    'busy': STATE['busy'], 'next_check': STATE['next_check'],
                    'started': STATE['started'], 'channels': STATE['channels'],
                    'recent': STATE['recent'][:100],
                }).encode()
            return self._send(200, 'application/json', body)
        if p in ('/', '/index.html'):
            return self._send(200, 'text/html; charset=utf-8', DASH_HTML.encode())
        return self._send(404, 'application/json', b'{"error":"not found"}')

    def log_message(self, *args):
        pass


def main():
    ensure_dirs()
    load_state()
    threading.Thread(
        target=lambda: ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever(),
        daemon=True).start()
    log('dashboard on http://0.0.0.0:%d' % PORT)

    while True:
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001
            log('config error (fix %s):' % CONFIG_PATH, e)
            time.sleep(60)
            continue
        with STATE_LOCK:
            STATE['channels'] = [
                {'name': str(c.get('name') or c.get('url')), 'url': c.get('url'),
                 'from': str(c.get('from') or ''), 'to': str(c.get('to') or '')}
                for c in cfg['channels']]
            STATE['busy'] = True
        for ch in cfg['channels']:
            if ch.get('url'):
                sync_channel(ch, cfg)
        process_incoming(cfg)
        interval = max(5, int(cfg['settings'].get('check_interval_minutes', 360)))
        with STATE_LOCK:
            STATE['busy'] = False
            STATE['next_check'] = datetime.fromtimestamp(
                time.time() + interval * 60, timezone.utc).isoformat()
        log('done — next check in %d min' % interval)
        time.sleep(interval * 60)


if __name__ == '__main__':
    main()
