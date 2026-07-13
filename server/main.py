#!/usr/bin/env python3
"""Hushcut Server.

Checks a list of channels on a schedule, downloads new videos that fall inside
each channel's date window (yt-dlp), scans their subtitles for profanity
(built-in list + YouTube's censored "[ __ ]" marker + custom words), silences
each hit with ffmpeg (video stream copied), and serves a status dashboard.

Config:  /config/config.yaml   (see config.example.yaml — also editable from
         the dashboard, which writes this file)
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
    'current': None,  # human-readable "what is happening right now"
    'started': datetime.now(timezone.utc).isoformat(),
}

# activity log shown on the dashboard; separate lock so log() can be called
# from code that already holds STATE_LOCK (e.g. save_state failures)
ACT_LOCK = threading.Lock()
ACTIVITY = []

CONFIG_LOCK = threading.Lock()
WAKE = threading.Event()  # set by POST /check (and after config save) to skip the sleep

DEFAULT_SETTINGS = {
    'check_interval_minutes': 360,
    'keep_original': False,
    'mute_lead': 0.4,
    'mute_tail': 0.3,
    'sub_langs': 'en.*',
    'quality': 'bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b',
    'on_missing_subs': 'copy',
    'extra_words': [],
}


def log(*args):
    msg = ' '.join(str(a) for a in args)
    print('[hushcut]', msg, flush=True)
    with ACT_LOCK:
        ACTIVITY.insert(0, {'t': datetime.now(timezone.utc).isoformat(), 'm': msg})
        del ACTIVITY[300:]


def set_current(text):
    with STATE_LOCK:
        STATE['current'] = text


def ensure_dirs():
    for d in ('incoming', 'clean', 'originals', 'state'):
        os.makedirs(os.path.join(DATA, d), exist_ok=True)


def load_config():
    with CONFIG_LOCK:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    cfg.setdefault('settings', {})
    cfg.setdefault('channels', [])
    return cfg


def save_config(cfg):
    with CONFIG_LOCK:
        os.makedirs(os.path.dirname(CONFIG_PATH) or '.', exist_ok=True)
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False,
                           allow_unicode=True)
        os.replace(tmp, CONFIG_PATH)


def ensure_config():
    if not os.path.exists(CONFIG_PATH):
        save_config({'settings': dict(DEFAULT_SETTINGS), 'channels': []})
        log('created default config at', CONFIG_PATH,
            '— add channels from the dashboard')


def clean_config(data):
    """Validate a config submitted from the dashboard. Returns a clean dict
    ready for save_config, or raises ValueError with a user-facing message."""
    if not isinstance(data, dict):
        raise ValueError('config must be an object')
    s_in = data.get('settings') or {}
    if not isinstance(s_in, dict):
        raise ValueError('settings must be an object')
    s = {}
    try:
        s['check_interval_minutes'] = max(5, int(s_in.get('check_interval_minutes', 360)))
        s['mute_lead'] = min(5.0, max(0.0, float(s_in.get('mute_lead', 0.4))))
        s['mute_tail'] = min(5.0, max(0.0, float(s_in.get('mute_tail', 0.3))))
    except (TypeError, ValueError):
        raise ValueError('check interval, mute lead and mute tail must be numbers') from None
    s['keep_original'] = bool(s_in.get('keep_original'))
    s['sub_langs'] = str(s_in.get('sub_langs') or DEFAULT_SETTINGS['sub_langs']).strip()
    s['quality'] = str(s_in.get('quality') or DEFAULT_SETTINGS['quality']).strip()
    s['on_missing_subs'] = str(s_in.get('on_missing_subs') or 'copy')
    if s['on_missing_subs'] not in ('copy', 'skip'):
        raise ValueError("on_missing_subs must be 'copy' or 'skip'")
    words = s_in.get('extra_words') or []
    if not isinstance(words, list):
        raise ValueError('extra_words must be a list')
    s['extra_words'] = [str(w).strip() for w in words if str(w).strip()]

    chans_in = data.get('channels') or []
    if not isinstance(chans_in, list):
        raise ValueError('channels must be a list')
    chans = []
    for i, c in enumerate(chans_in):
        if not isinstance(c, dict):
            raise ValueError('channel %d must be an object' % (i + 1))
        url = str(c.get('url') or '').strip()
        if not re.match(r'https?://', url):
            raise ValueError('channel %d needs an http(s) URL' % (i + 1))
        ch = {'name': str(c.get('name') or '').strip() or url, 'url': url}
        for key in ('from', 'to'):
            v = str(c.get(key) or '').strip()
            if not v:
                continue
            m = re.fullmatch(r'(\d{4})-(\d{2})(?:-(\d{2}))?', v)
            if not m or not 1 <= int(m.group(2)) <= 12 \
                    or (m.group(3) and not 1 <= int(m.group(3)) <= 31):
                raise ValueError(
                    "channel %d: '%s' must be YYYY-MM or YYYY-MM-DD" % (i + 1, key))
            ch[key] = v
        # month shorthand: a bare "to" month means through its last day, and a
        # bare "from" month paired with a "to" means from its first day
        # (from-month with no "to" keeps the single-month meaning, see date_window)
        if ch.get('to') and re.fullmatch(r'\d{4}-\d{2}', ch['to']):
            ch['to'] = month_bounds(ch['to'])[1]
        if ch.get('from') and ch.get('to') and re.fullmatch(r'\d{4}-\d{2}', ch['from']):
            ch['from'] = month_bounds(ch['from'])[0]
        chans.append(ch)
    return {'settings': s, 'channels': chans}


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
    log('sync: %s (window %s → %s)' % (name, frm or 'any', to or 'any'))
    set_current('%s — checking for new videos' % name)
    seen, cur_title = set(), None
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors='replace')
        watchdog = threading.Timer(3 * 3600, p.kill)
        watchdog.start()
        try:
            for line in p.stdout:
                line = line.strip()
                m = re.search(r'\[download\] Destination: (.+)', line)
                if m:
                    base = os.path.basename(m.group(1))
                    if base.lower().endswith(('.vtt', '.srt')):
                        continue
                    cur_title = re.sub(r'(\.f\d+)?\.\w+$', '', base)
                    if cur_title not in seen:
                        seen.add(cur_title)
                        log('downloading:', cur_title)
                    set_current('%s — downloading %s' % (name, cur_title))
                elif line.startswith('[download]') and '%' in line and cur_title:
                    pm = re.search(r'(\d+(?:\.\d+)?%)(?:.*?ETA\s+(\S+))?', line)
                    if pm:
                        prog = pm.group(1) + (', ETA ' + pm.group(2) if pm.group(2) else '')
                        set_current('%s — downloading %s (%s)' % (name, cur_title, prog))
                elif '[download] Downloading item' in line:
                    set_current('%s — %s' % (name, line.replace('[download] ', '')))
                elif line.startswith('ERROR'):
                    log('  ', line[:200])
            p.wait(timeout=300)
        finally:
            watchdog.cancel()
        log('sync done: %s — %s' % (
            name, '%d new video(s)' % len(seen) if seen else 'nothing new'))
    except Exception as e:  # noqa: BLE001
        log('sync error:', name, e)
    finally:
        set_current(None)


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
            set_current('muting: %s' % base)
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
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:#a1a09d;padding:6px 8px;border-bottom:1px solid rgba(0,0,0,.08)}
td{padding:7px 8px;border-bottom:1px solid rgba(0,0,0,.05);vertical-align:top}
.n{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#6e6e73;white-space:nowrap}
.flag{color:#b3261e;font-weight:700}
.ok{color:#2f9e44;font-weight:600}.warning{color:#8a5a12;font-weight:600}
.error{color:#b3261e;font-weight:600}.skipped{color:#6e6e73}
.empty{color:#a1a09d;font-size:12.5px;padding:8px}
input,select,textarea{font:inherit;font-size:12.5px;color:#1d1d1f;background:#fff;border:1px solid #c9c8c5;border-radius:7px;padding:6px 9px;box-sizing:border-box;width:100%}
input:focus,select:focus,textarea:focus{outline:none;border-color:#0a68d6}
label{display:flex;flex-direction:column;gap:4px;font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#86868b}
label.chk{flex-direction:row;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:12.5px;font-weight:600;color:#1d1d1f}
label.chk input{width:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.grid .full{grid-column:1/-1}
.chrow{display:grid;grid-template-columns:1fr 1.6fr 118px 118px 30px;gap:8px;margin-bottom:8px;align-items:center}
.btn{font:inherit;font-size:12.5px;font-weight:600;border:0;border-radius:7px;padding:7px 14px;background:#0a68d6;color:#fff;cursor:pointer}
.btn:hover{background:#085bbd}
.btn.ghost{background:#f6f5f3;color:#1d1d1f;border:1px solid rgba(0,0,0,.12)}
.btn.ghost:hover{background:#ecebe9}
.del{border:0;background:none;color:#b3261e;font-size:17px;cursor:pointer;padding:0 4px;line-height:1}
.saverow{display:flex;align-items:center;gap:12px}
#msg{font-size:12.5px;font-weight:600}
#msg.good{color:#2f9e44}#msg.bad{color:#b3261e}
.curline{font-size:13px;font-weight:600;margin-bottom:10px}
.curline.idle{color:#6e6e73;font-weight:500}
.logbox{max-height:240px;overflow-y:auto;background:#f6f5f3;border:1px solid rgba(0,0,0,.07);border-radius:7px;padding:8px 10px;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;line-height:1.7;word-break:break-word}
.logbox .lt{color:#a1a09d;margin-right:8px;white-space:nowrap}
@media(max-width:700px){.chrow{grid-template-columns:1fr 1fr}}
</style></head><body>
<div class="bar"><div class="glyph">H</div><h1>Hushcut Server</h1>
<span id="busy" class="pill">idle</span><span style="flex:1"></span><span id="next" class="n"></span>
<button id="checknow" class="btn ghost" type="button" style="height:30px;padding:0 12px">Check now</button></div>
<div class="wrap">
<div class="card"><h2>Activity</h2>
<div id="cur" class="curline idle">Loading&hellip;</div>
<div id="log" class="logbox empty">No activity yet.</div></div>
<div class="card"><h2>Channels</h2>
<div id="chrows" class="empty">Loading&hellip;</div>
<button id="addch" class="btn ghost" type="button">+ Add channel</button></div>
<div class="card"><h2>Settings</h2>
<div class="grid">
<label>Check interval (minutes)<input id="s_interval" type="number" min="5" step="1"></label>
<label>Videos without subtitles<select id="s_missing"><option value="copy">copy through unmuted</option><option value="skip">skip</option></select></label>
<label>Mute lead (seconds)<input id="s_lead" type="number" min="0" max="5" step="0.1"></label>
<label>Mute tail (seconds)<input id="s_tail" type="number" min="0" max="5" step="0.1"></label>
<label>Subtitle languages<input id="s_langs" placeholder="en.*"></label>
<label class="chk"><input id="s_keep" type="checkbox">Keep unmuted originals</label>
<label class="full">yt-dlp format (quality)<input id="s_quality"></label>
<label class="full">Extra filter words &mdash; one per line<textarea id="s_words" rows="3" placeholder="frick&#10;shut up"></textarea></label>
</div></div>
<div class="saverow"><button id="save" class="btn" type="button">Save config</button><span id="msg"></span></div>
<div class="card"><h2>Recent videos</h2><div style="overflow-x:auto">
<table><thead><tr><th>When</th><th>Channel</th><th>Title</th><th>Flagged</th><th>Silenced</th><th>Status</th></tr></thead>
<tbody id="rows"></tbody></table>
<div id="empty" class="empty" style="display:none">Nothing processed yet.</div></div></div>
</div>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function fmt(iso){if(!iso)return'';return new Date(iso).toLocaleString()}
function tfmt(iso){if(!iso)return'';return new Date(iso).toLocaleTimeString()}
function $(id){return document.getElementById(id)}
function msg(text,good){var m=$('msg');m.textContent=text;m.className=good?'good':'bad';
 clearTimeout(msg._t);if(text)msg._t=setTimeout(function(){m.textContent=''},6000)}

function chRow(c){
 c=c||{};
 return '<div class="chrow">'+
  '<input class="c_name" placeholder="Name" value="'+esc(c.name)+'">'+
  '<input class="c_url" placeholder="https://www.youtube.com/@channel/videos" value="'+esc(c.url)+'">'+
  '<input class="c_from" placeholder="from YYYY-MM[-DD]" value="'+esc(c.from)+'">'+
  '<input class="c_to" placeholder="to YYYY-MM[-DD]" value="'+esc(c.to)+'">'+
  '<button class="del" type="button" title="Remove channel">\\u00d7</button></div>';
}
function renderConfig(cfg){
 var s=cfg.settings||{};
 $('s_interval').value=s.check_interval_minutes;
 $('s_missing').value=s.on_missing_subs||'copy';
 $('s_lead').value=s.mute_lead;
 $('s_tail').value=s.mute_tail;
 $('s_langs').value=s.sub_langs||'';
 $('s_keep').checked=!!s.keep_original;
 $('s_quality').value=s.quality||'';
 $('s_words').value=(s.extra_words||[]).join('\\n');
 var rows=$('chrows');rows.className='';
 rows.innerHTML=(cfg.channels||[]).map(chRow).join('');
}
function loadConfig(){
 fetch('/config').then(function(r){return r.json()}).then(renderConfig)
 .catch(function(){msg('could not load config',false)});
}
function gather(){
 var chans=[].map.call(document.querySelectorAll('#chrows .chrow'),function(r){
  function v(cl){return r.querySelector(cl).value.trim()}
  return {name:v('.c_name'),url:v('.c_url'),from:v('.c_from'),to:v('.c_to')};
 }).filter(function(c){return c.name||c.url||c.from||c.to});
 return {settings:{
   check_interval_minutes:$('s_interval').value,
   on_missing_subs:$('s_missing').value,
   mute_lead:$('s_lead').value,
   mute_tail:$('s_tail').value,
   sub_langs:$('s_langs').value.trim(),
   keep_original:$('s_keep').checked,
   quality:$('s_quality').value.trim(),
   extra_words:$('s_words').value.split('\\n').map(function(w){return w.trim()}).filter(Boolean)
  },channels:chans};
}
$('addch').onclick=function(){
 var rows=$('chrows');rows.className='';
 rows.insertAdjacentHTML('beforeend',chRow());
 rows.lastElementChild.querySelector('.c_url').focus();
};
$('chrows').addEventListener('click',function(e){
 if(e.target.classList.contains('del'))e.target.closest('.chrow').remove();
});
$('save').onclick=function(){
 fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(gather())})
 .then(function(r){return r.json().then(function(j){return{ok:r.ok,j:j}})})
 .then(function(x){
  msg(x.ok?'Saved \\u2014 applies from the next check.':(x.j.error||'save failed'),x.ok);
  if(x.ok)loadConfig();
 })
 .catch(function(){msg('save failed',false)});
};
$('checknow').onclick=function(){
 fetch('/check',{method:'POST'}).then(function(){msg('Check queued.',true);tick()})
 .catch(function(){msg('request failed',false)});
};
function tick(){
 fetch('/status').then(function(r){return r.json()}).then(function(s){
  var busy=$('busy');
  busy.textContent=s.busy?'checking\\u2026':'idle';
  busy.className='pill'+(s.busy?' busy':'');
  $('next').textContent=s.next_check?('next check '+fmt(s.next_check)):'';
  var cur=$('cur');
  if(s.current){cur.textContent=s.current;cur.className='curline'}
  else{cur.textContent=s.busy?'checking channels\\u2026':'idle \\u2014 waiting for the next check';cur.className='curline idle'}
  if(s.activity&&s.activity.length){
   var lg=$('log');lg.className='logbox';
   lg.innerHTML=s.activity.map(function(a){
    return '<div><span class="lt">'+esc(tfmt(a.t))+'</span>'+esc(a.m)+'</div>';
   }).join('');
  }
  $('rows').innerHTML=(s.recent||[]).map(function(r){
   return '<tr><td class="n">'+fmt(r.time)+'</td><td>'+esc(r.channel)+'</td><td>'+esc(r.title)+
    '</td><td class="'+(r.words?'flag':'')+'">'+(r.words||0)+'</td><td class="n">'+(r.seconds||0)+
    's</td><td class="'+esc(r.status)+'">'+esc(r.status)+(r.note?(' \\u2014 '+esc(r.note)):'')+'</td></tr>';
  }).join('');
  $('empty').style.display=(s.recent&&s.recent.length)?'none':'block';
 }).catch(function(){});
}
loadConfig();tick();setInterval(tick,5000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, 'application/json', json.dumps(obj).encode())

    def do_GET(self):  # noqa: N802
        p = urlparse(self.path).path
        if p == '/status':
            with ACT_LOCK:
                activity = ACTIVITY[:100]
            with STATE_LOCK:
                body = json.dumps({
                    'busy': STATE['busy'], 'next_check': STATE['next_check'],
                    'started': STATE['started'], 'channels': STATE['channels'],
                    'current': STATE['current'], 'activity': activity,
                    'recent': STATE['recent'][:100],
                }).encode()
            return self._send(200, 'application/json', body)
        if p == '/config':
            try:
                cfg = load_config()
            except Exception as e:  # noqa: BLE001
                return self._json(500, {'error': 'could not read config: %s' % e})
            settings = dict(DEFAULT_SETTINGS)
            settings.update({k: v for k, v in cfg['settings'].items() if v is not None})
            return self._json(200, {'settings': settings, 'channels': cfg['channels']})
        if p in ('/', '/index.html'):
            return self._send(200, 'text/html; charset=utf-8', DASH_HTML.encode())
        return self._send(404, 'application/json', b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        p = urlparse(self.path).path
        if p == '/config':
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if not 0 < n <= 1_000_000:
                    raise ValueError('bad request size')
                cfg = clean_config(json.loads(self.rfile.read(n).decode('utf-8')))
            except (ValueError, UnicodeDecodeError) as e:
                return self._json(400, {'error': str(e)})
            try:
                save_config(cfg)
            except OSError as e:
                return self._json(500, {'error': 'could not write config: %s' % e})
            with STATE_LOCK:
                STATE['channels'] = [
                    {'name': c['name'], 'url': c['url'],
                     'from': c.get('from', ''), 'to': c.get('to', '')}
                    for c in cfg['channels']]
            log('config saved from dashboard —',
                len(cfg['channels']), 'channel(s)')
            return self._json(200, {'ok': True})
        if p == '/check':
            WAKE.set()
            return self._json(200, {'ok': True})
        return self._send(404, 'application/json', b'{"error":"not found"}')

    def log_message(self, *args):
        pass


def main():
    ensure_dirs()
    ensure_config()
    load_state()
    threading.Thread(
        target=lambda: ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever(),
        daemon=True).start()
    log('dashboard on http://0.0.0.0:%d' % PORT)

    while True:
        WAKE.clear()
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001
            log('config error (fix %s or resave from the dashboard):' % CONFIG_PATH, e)
            WAKE.wait(60)
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
        set_current(None)
        interval = max(5, int(cfg['settings'].get('check_interval_minutes', 360)))
        with STATE_LOCK:
            STATE['busy'] = False
            STATE['next_check'] = datetime.fromtimestamp(
                time.time() + interval * 60, timezone.utc).isoformat()
        log('done — next check in %d min' % interval)
        if WAKE.wait(interval * 60):
            log('check requested from dashboard')


if __name__ == '__main__':
    main()
