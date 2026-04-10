#!/usr/bin/env python3
"""
yt2strm - Lightweight YouTube-to-STRM creator for Emby/Jellyfin
Creates .strm files with direct YouTube URLs, NFO metadata, and thumbnails
"""

from flask import Flask, request, jsonify, render_template_string
from html import escape as html_escape
import yt_dlp
import json
import os
import re
import requests as http_req
import threading
import time
import logging
from datetime import datetime, timezone

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('yt2strm')

VERSION = '1.0.1'  # Date handling + NFO compatibility fixes

# ── Config from environment ──────────────────────────────────────
HOST          = os.environ.get('YT2STRM_HOST', '0.0.0.0')
PORT          = int(os.environ.get('YT2STRM_PORT', 5000))
MEDIA_DIR     = os.environ.get('YT2STRM_MEDIA', '/media/YouTube')
DATA_DIR      = os.environ.get('YT2STRM_DATA', '/data')
SCAN_INTERVAL = int(os.environ.get('YT2STRM_INTERVAL', 1)) * 3600   # hours between scans, 0=off
VIDEO_LIMIT   = int(os.environ.get('YT2STRM_LIMIT', 50))
METADATA      = os.environ.get('YT2STRM_METADATA', 'true').lower() in ('true', '1', 'yes')
COOKIES_FILE  = os.environ.get('YT2STRM_COOKIES', '').strip()     # path to cookies.txt file

CHANNELS_FILE = os.path.join(DATA_DIR, 'channels.json')

# ── Runtime state ────────────────────────────────────────────────
state = {
    'scanning': False,
    'last_scan': None,
    'results': [],
    'logs': []
}

def add_log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    state['logs'].append({'time': ts, 'msg': msg, 'level': level})
    state['logs'] = state['logs'][-500:]  # Keep last 500 logs
    getattr(logger, level, logger.info)(msg)

def get_ytdlp_base_opts():
    """Return base yt-dlp options including cookies if configured."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
    }
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    return opts

# ── Channel persistence ─────────────────────────────────────────
def load_channels():
    try:
        if os.path.exists(CHANNELS_FILE):
            with open(CHANNELS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        add_log(f'Error loading channels: {e}', 'error')
    return []

def save_channels(channels):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHANNELS_FILE, 'w', encoding='utf-8') as f:
        json.dump(channels, f, indent=2)

# ── Helpers ──────────────────────────────────────────────────────
def sanitize(name):
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', '', str(name))
    name = name.strip('. ')
    return name[:200] if name else 'Untitled'

def xml_escape(text):
    """Escape text for safe inclusion in XML content."""
    return html_escape(str(text), quote=False)

def normalize_yt_date(date_value=None, timestamp_value=None):
    """
    Normalize different date representations into YYYYMMDD.
    Supports:
    - 'YYYYMMDD'
    - 'YYYY-MM-DD'
    - 'YYYY/MM/DD'
    - ISO timestamps (e.g., 2024-05-09T12:34:56Z)
    - unix timestamps (seconds)
    """
    if date_value is not None:
        s = str(date_value).strip()
        if s:
            # Exact YYYYMMDD
            if re.fullmatch(r'\d{8}', s):
                return s

            # Common date formats
            for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
                try:
                    return datetime.strptime(s, fmt).strftime('%Y%m%d')
                except Exception:
                    pass

            # ISO-like format: take first 10 chars if date prefix exists
            m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
            if m:
                return f'{m.group(1)}{m.group(2)}{m.group(3)}'

    # Fallback to timestamp
    if timestamp_value is not None:
        try:
            ts = int(timestamp_value)
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y%m%d')
        except Exception:
            pass

    return ''

def yyyymmdd_to_iso(d):
    """Convert YYYYMMDD -> YYYY-MM-DD, else empty string."""
    if d and re.fullmatch(r'\d{8}', str(d)):
        s = str(d)
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    return ''

def nfo_needs_update(nfo_path):
    """
    Returns True if NFO should be rewritten to include/normalize release tags.
    Rewrites if:
    - file does not exist
    - missing <releasedate> or <premiered>
    - legacy DD/MM/YYYY releasedate format is present
    """
    if not os.path.exists(nfo_path):
        return True

    try:
        with open(nfo_path, 'r', encoding='utf-8') as f:
            content = f.read()

        lower = content.lower()
        has_releasedate = '<releasedate>' in lower
        has_premiered = '<premiered>' in lower

        legacy_ddmmyyyy = re.search(
            r'<releasedate>\s*\d{2}/\d{2}/\d{4}\s*</releasedate>',
            content,
            flags=re.IGNORECASE
        ) is not None

        if (not has_releasedate) or (not has_premiered) or legacy_ddmmyyyy:
            return True
        return False
    except Exception:
        # If unreadable, try to rewrite it
        return True

def download_thumbnail(video_id, dest_path):
    """Download the best available YouTube thumbnail for a video."""
    urls = [
        f'https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg',
        f'https://i.ytimg.com/vi/{video_id}/sddefault.jpg',
        f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
    ]
    for url in urls:
        try:
            r = http_req.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200 and len(r.content) > 2000:
                with open(dest_path, 'wb') as f:
                    f.write(r.content)
                return True
        except Exception:
            continue
    return False

def download_image(url, dest_path):
    """Download an image from an arbitrary URL."""
    if not url:
        return False
    try:
        r = http_req.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200 and len(r.content) > 2000:
            with open(dest_path, 'wb') as f:
                f.write(r.content)
            return True
    except Exception:
        pass
    return False

def format_duration(seconds):
    """Convert seconds to compact duration format (e.g., '1min 43sec' or '1h 23min 45sec')."""
    if not seconds:
        return None

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}min")
    if secs > 0 or not parts:  # Always show seconds if no hours/minutes
        parts.append(f"{secs}sec")

    return " ".join(parts)

def write_movie_nfo(path, title, video_id, upload_date=None, description=None, duration=None):
    """Write a Kodi/Emby-compatible movie NFO file."""
    d_norm = normalize_yt_date(upload_date)
    iso_date = yyyymmdd_to_iso(d_norm)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<movie>']
    lines.append(f'  <title>{xml_escape(title)}</title>')

    # Build tagline: "YYYY-MM-DD, Xmin Ysec" or just "Xmin Ysec" if no date
    tagline_parts = []
    if iso_date:
        tagline_parts.append(iso_date)
    if duration:
        duration_str = format_duration(duration)
        if duration_str:
            tagline_parts.append(duration_str)
    if tagline_parts:
        tagline = ", ".join(tagline_parts)
        lines.append(f'  <tagline>{xml_escape(tagline)}</tagline>')

    if description:
        lines.append(f'  <plot>{xml_escape(description)}</plot>')

    # Use ISO date fields for broad compatibility
    if iso_date:
        lines.append(f'  <releasedate>{iso_date}</releasedate>')
        lines.append(f'  <premiered>{iso_date}</premiered>')
        lines.append(f'  <year>{iso_date[:4]}</year>')

    lines.append(f'  <uniqueid type="youtube">{xml_escape(video_id)}</uniqueid>')
    lines.append('</movie>')

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

def scan_channel(channel_url, custom_name=None, folder=None):
    """List videos in a channel/playlist, create .strm + metadata files.

    Args:
        channel_url: YouTube channel or playlist URL
        custom_name: Optional custom display name
        folder: Optional folder to organize channels
    """
    opts = get_ytdlp_base_opts()
    opts['extract_flat'] = 'in_playlist'
    opts['playlistend'] = VIDEO_LIMIT

    if '/@' in channel_url and '/videos' not in channel_url and '/playlist' not in channel_url:
        channel_url = channel_url.rstrip('/') + '/videos'

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    name = custom_name or info.get('channel') or info.get('uploader') or info.get('title') or 'Unknown'
    name = sanitize(name)

    # Build output path — optionally nested inside a folder
    if folder:
        channel_dir = os.path.join(MEDIA_DIR, sanitize(folder), name)
    else:
        channel_dir = os.path.join(MEDIA_DIR, name)
    os.makedirs(channel_dir, exist_ok=True)

    # ── Channel-level metadata ───────────────────────────────────
    if METADATA:
        poster_path = os.path.join(channel_dir, 'poster.jpg')
        if not os.path.exists(poster_path):
            thumbs = info.get('thumbnails') or []
            # Prefer larger thumbnails (usually sorted ascending by size)
            for thumb in reversed(thumbs):
                url = thumb.get('url', '')
                if url and download_image(url, poster_path):
                    add_log(f'  Downloaded poster for {name}')
                    break

    # ── Per-video files ──────────────────────────────────────────
    entries = info.get('entries') or []
    new_count = 0
    meta_count = 0
    thumb_count = 0
    total_entries = len([e for e in entries if e and (e.get('id') or e.get('url'))])

    add_log(f'  Found {total_entries} videos to process')

    for idx, entry in enumerate(entries, 1):
        if not entry:
            continue

        vid_id = entry.get('id') or entry.get('url') or ''
        vid_title = sanitize(entry.get('title') or vid_id)
        if not vid_id or not vid_title:
            continue

        # ── STRM file ────────────────────────────────────────────
        strm_path = os.path.join(channel_dir, f'{vid_title}.strm')
        if not os.path.exists(strm_path):
            try:
                with open(strm_path, 'w', encoding='utf-8') as f:
                    f.write(f'https://www.youtube.com/watch?v={vid_id}')
                new_count += 1
                add_log(f'  [{idx}/{total_entries}] Created: {vid_title}.strm')
            except OSError as e:
                add_log(f'  [{idx}/{total_entries}] Write error {vid_title}: {e}', 'error')
        else:
            add_log(f'  [{idx}/{total_entries}] Exists: {vid_title}.strm')

        # ── Metadata (NFO + thumbnail) ───────────────────────────
        if METADATA:
            nfo_path = os.path.join(channel_dir, f'{vid_title}.nfo')

            try:
                should_write_nfo = nfo_needs_update(nfo_path)
                if should_write_nfo:
                    # Date from flat extraction (with fallbacks)
                    raw_date = (
                        entry.get('upload_date')
                        or entry.get('release_date')
                        or entry.get('release_timestamp')
                    )
                    upload_date = normalize_yt_date(raw_date, entry.get('timestamp'))
                    description = entry.get('description')
                    duration = entry.get('duration')

                    # Full extraction if critical fields are missing
                    if (not upload_date) or (not duration) or (not description):
                        try:
                            full_opts = {
                                'quiet': True,
                                'no_warnings': True,
                                'socket_timeout': 30,
                            }
                            if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
                                full_opts['cookiefile'] = COOKIES_FILE

                            with yt_dlp.YoutubeDL(full_opts) as ydl_full:
                                full_info = ydl_full.extract_info(
                                    f'https://www.youtube.com/watch?v={vid_id}',
                                    download=False
                                )

                                if not upload_date:
                                    raw_full_date = (
                                        full_info.get('upload_date')
                                        or full_info.get('release_date')
                                        or full_info.get('release_timestamp')
                                    )
                                    upload_date = normalize_yt_date(raw_full_date, full_info.get('timestamp'))

                                if not description:
                                    description = full_info.get('description')

                                if not duration:
                                    duration = full_info.get('duration')

                        except Exception as e:
                            add_log(f'      Full metadata extraction failed: {e}', 'error')
                            # Last fallback from entry timestamp
                            if not upload_date:
                                upload_date = normalize_yt_date(None, entry.get('timestamp'))

                    write_movie_nfo(
                        nfo_path,
                        entry.get('title') or vid_id,
                        vid_id,
                        upload_date,
                        description,
                        duration
                    )
                    meta_count += 1
                    if os.path.exists(nfo_path):
                        add_log(f'      + NFO metadata{" (updated)" if nfo_needs_update(nfo_path) is False else ""}')
                else:
                    add_log(f'      NFO up-to-date')
            except Exception as e:
                add_log(f'      NFO error: {e}', 'error')

            thumb_path = os.path.join(channel_dir, f'{vid_title}-thumb.jpg')
            if not os.path.exists(thumb_path):
                try:
                    if download_thumbnail(vid_id, thumb_path):
                        thumb_count += 1
                        add_log(f'      + Thumbnail')
                except Exception as e:
                    add_log(f'      Thumb error: {e}', 'error')

    add_log(f'  Summary: {new_count} new STRM files, {meta_count} NFO files, {thumb_count} thumbnails')
    return new_count, name

def run_full_scan():
    if state['scanning']:
        return state['results']
    state['scanning'] = True
    state['results'] = []
    channels = load_channels()
    add_log(
        f'Scan started — {len(channels)} channel(s)'
        + (' [metadata enabled]' if METADATA else '')
        + (' [cookies loaded]' if COOKIES_FILE and os.path.isfile(COOKIES_FILE) else ' [no cookies]')
    )

    for i, ch in enumerate(channels):
        label = ch.get('name') or ch['url']
        folder = ch.get('folder')
        if folder:
            label = f'{folder}/{label}'
        add_log(f'[{i+1}/{len(channels)}] Scanning: {label}')
        try:
            count, resolved = scan_channel(ch['url'], ch.get('name'), folder)
            display = f'{folder}/{resolved}' if folder else resolved
            state['results'].append({'channel': display, 'new': count, 'status': 'ok'})
            add_log(f'[{i+1}/{len(channels)}] ✓ {display} complete')
        except Exception as e:
            state['results'].append({'channel': label, 'error': str(e), 'status': 'error'})
            add_log(f'[{i+1}/{len(channels)}] ✗ Error: {e}', 'error')

    state['scanning'] = False
    state['last_scan'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    add_log('Scan complete')
    return state['results']

def background_scanner():
    time.sleep(30)
    while True:
        if SCAN_INTERVAL > 0:
            run_full_scan()
        time.sleep(max(SCAN_INTERVAL, 60))

# ── API routes ───────────────────────────────────────────────────

@app.route('/api/channels', methods=['GET'])
def api_get_channels():
    return jsonify(load_channels())

@app.route('/api/channels', methods=['POST'])
def api_add_channel():
    data = request.json or {}
    url = data.get('url', '').strip()
    name = data.get('name', '').strip() or None
    folder = data.get('folder', '').strip() or None
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    channels = load_channels()
    channels.append({'url': url, 'name': name, 'folder': folder})
    save_channels(channels)
    label = f'{folder}/{name or url}' if folder else (name or url)
    add_log(f'Added: {label}')
    return jsonify({'status': 'ok', 'channels': channels})

@app.route('/api/channels/<int:idx>', methods=['PUT'])
def api_edit_channel(idx):
    channels = load_channels()
    if not (0 <= idx < len(channels)):
        return jsonify({'error': 'Invalid index'}), 400
    data = request.json or {}
    if 'url' in data:
        channels[idx]['url'] = data['url'].strip()
    if 'name' in data:
        channels[idx]['name'] = data['name'].strip() or None
    if 'folder' in data:
        channels[idx]['folder'] = data['folder'].strip() or None
    save_channels(channels)
    add_log(f'Updated channel {idx}')
    return jsonify({'status': 'ok', 'channels': channels})

@app.route('/api/channels/<int:idx>', methods=['DELETE'])
def api_del_channel(idx):
    channels = load_channels()
    if 0 <= idx < len(channels):
        removed = channels.pop(idx)
        save_channels(channels)
        add_log(f'Removed: {removed.get("name") or removed["url"]}')
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Invalid index'}), 400

@app.route('/api/scan', methods=['POST'])
def api_scan():
    if state['scanning']:
        return jsonify({'error': 'Scan already running'}), 409
    threading.Thread(target=run_full_scan, daemon=True).start()
    return jsonify({'status': 'started'})

@app.route('/api/scan/<int:idx>', methods=['POST'])
def api_scan_single(idx):
    """Scan a single channel by index."""
    if state['scanning']:
        return jsonify({'error': 'Scan already running'}), 409

    channels = load_channels()
    if not (0 <= idx < len(channels)):
        return jsonify({'error': 'Invalid index'}), 400

    def scan_single_channel():
        state['scanning'] = True
        state['results'] = []
        ch = channels[idx]
        label = ch.get('name') or ch['url']
        folder = ch.get('folder')
        if folder:
            label = f'{folder}/{label}'
        add_log(f'Scanning single channel: {label}')
        try:
            count, resolved = scan_channel(ch['url'], ch.get('name'), folder)
            display = f'{folder}/{resolved}' if folder else resolved
            state['results'].append({'channel': display, 'new': count, 'status': 'ok'})
            add_log(f'✓ {display} complete')
        except Exception as e:
            state['results'].append({'channel': label, 'error': str(e), 'status': 'error'})
            add_log(f'✗ Error: {e}', 'error')
        state['scanning'] = False
        state['last_scan'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        add_log('Scan complete')

    threading.Thread(target=scan_single_channel, daemon=True).start()
    return jsonify({'status': 'started'})

@app.route('/api/regenerate', methods=['POST'])
def api_regenerate():
    """Rewrite every .strm file to use direct YouTube URL format."""
    updated = 0
    skipped = 0
    errors = 0
    for root, _dirs, files in os.walk(MEDIA_DIR):
        for fname in files:
            if not fname.endswith('.strm'):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    content = fh.read().strip()

                # Extract video ID from various formats
                vid_id = None

                # Format 1: Direct YouTube URL
                if 'youtube.com/watch?v=' in content or 'youtu.be/' in content:
                    m = re.search(r'(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]+)', content)
                    if m:
                        vid_id = m.group(1)

                # Format 2: Old server format (play/bridge/proxy)
                if not vid_id:
                    m = re.search(r'/(play|bridge|proxy)/([A-Za-z0-9_-]+)', content)
                    if m:
                        vid_id = m.group(2)

                if vid_id:
                    new_content = f'https://www.youtube.com/watch?v={vid_id}'
                    if content != new_content:
                        with open(path, 'w', encoding='utf-8') as fh:
                            fh.write(new_content)
                        updated += 1
                    else:
                        skipped += 1
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                add_log(f'Regenerate error {path}: {e}', 'error')
    add_log(f'Regenerate done: {updated} updated, {skipped} already correct, {errors} errors')
    return jsonify({'status': 'ok', 'updated': updated, 'skipped': skipped, 'errors': errors})

@app.route('/api/regenerate-nfo', methods=['POST'])
def api_regenerate_nfo():
    """Delete all NFO files to force regeneration on next scan."""
    deleted = 0
    errors = 0
    for root, _dirs, files in os.walk(MEDIA_DIR):
        for fname in files:
            if not fname.endswith('.nfo'):
                continue
            path = os.path.join(root, fname)
            try:
                os.remove(path)
                deleted += 1
            except Exception as e:
                errors += 1
                add_log(f'Delete NFO error {path}: {e}', 'error')
    add_log(f'Deleted {deleted} NFO files, {errors} errors')
    return jsonify({'status': 'ok', 'deleted': deleted, 'errors': errors})

@app.route('/api/debug/<video_id>', methods=['GET'])
def api_debug(video_id):
    """Verify that video metadata can be fetched."""
    result = {
        'video_id': video_id,
        'cookies_loaded': bool(COOKIES_FILE and os.path.isfile(COOKIES_FILE))
    }

    try:
        opts = get_ytdlp_base_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f'https://www.youtube.com/watch?v={video_id}',
                download=False
            )
            normalized_date = normalize_yt_date(
                info.get('upload_date') or info.get('release_date') or info.get('release_timestamp'),
                info.get('timestamp')
            )
            result['metadata'] = {
                'title': info.get('title'),
                'uploader': info.get('uploader'),
                'upload_date': info.get('upload_date'),
                'release_date': info.get('release_date'),
                'timestamp': info.get('timestamp'),
                'normalized_date': normalized_date,
                'normalized_iso_date': yyyymmdd_to_iso(normalized_date) if normalized_date else None,
                'duration': info.get('duration'),
                'description': info.get('description', '')[:200] + '...' if info.get('description') else None,
                'height': info.get('height'),
                'format': info.get('format'),
            }
            result['status'] = 'ok'
    except Exception as e:
        result['status'] = 'error'
        result['error'] = str(e)

    return jsonify(result)

@app.route('/api/status', methods=['GET'])
def api_status():
    return jsonify(state)

# ── Web UI ───────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML, conf={
        'media_dir': MEDIA_DIR,
        'scan_interval': SCAN_INTERVAL // 3600 if SCAN_INTERVAL > 0 else 0,  # Convert to hours
        'video_limit': VIDEO_LIMIT,
        'metadata': METADATA,
        'version': VERSION,
        'cookies': bool(COOKIES_FILE and os.path.isfile(COOKIES_FILE)),
        'cookies_path': COOKIES_FILE or '(not set)',
    })

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yt2strm</title>
<style>
  :root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;--accent2:#4caf93;
        --red:#e74c5f;--text:#e0e0e8;--muted:#888;--warn:#f0ad4e}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--text);padding:1rem;max-width:900px;margin:0 auto}
  h1{font-size:1.6rem;margin-bottom:.3rem}
  h1 span{color:var(--accent)}
  .subtitle{color:var(--muted);font-size:.85rem;margin-bottom:1.5rem}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;
        padding:1.2rem;margin-bottom:1rem}
  .card h2{font-size:1rem;margin-bottom:.8rem;color:var(--accent)}
  label{display:block;font-size:.8rem;color:var(--muted);margin-bottom:.3rem}
  input[type=text]{width:100%;padding:.55rem .7rem;border-radius:6px;border:1px solid var(--border);
        background:#12141c;color:var(--text);font-size:.9rem;margin-bottom:.6rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  .row{display:flex;gap:.6rem;flex-wrap:wrap}
  .row input{flex:1;min-width:0}
  button{padding:.55rem 1.1rem;border-radius:6px;border:none;cursor:pointer;
         font-size:.85rem;font-weight:600;transition:opacity .2s}
  button:hover{opacity:.85}
  .btn-primary{background:var(--accent);color:#fff}
  .btn-scan{background:var(--accent2);color:#fff}
  .btn-warn{background:var(--warn);color:#000}
  .btn-danger{background:var(--red);color:#fff;padding:.35rem .7rem;font-size:.75rem}
  .btn-sm{padding:.35rem .7rem;font-size:.75rem}
  .ch-list{list-style:none;max-height:400px;overflow-y:auto}
  .ch-item{display:flex;justify-content:space-between;align-items:center;
           padding:.55rem .7rem;border-bottom:1px solid var(--border);font-size:.85rem}
  .ch-item:last-child{border-bottom:none}
  .ch-name{font-weight:600;margin-right:.5rem}
  .ch-url{color:var(--muted);font-size:.78rem;word-break:break-all}
  .ch-folder{display:inline-block;padding:.1rem .45rem;border-radius:3px;font-size:.7rem;
             font-weight:600;background:#2a2640;color:#a78bfa;margin-right:.5rem}
  .ch-actions{display:flex;gap:.3rem;flex-shrink:0}
  .log-box{background:#0a0c12;border-radius:6px;padding:.7rem;max-height:400px;
           overflow-y:auto;font-family:monospace;font-size:.78rem;line-height:1.6}
  .log-line .t{color:var(--muted)}
  .log-line .e{color:var(--red)}
  .conf-grid{display:grid;grid-template-columns:1fr 1fr;gap:.4rem .8rem;font-size:.82rem}
  .conf-grid dt{color:var(--muted)}
  .conf-grid dd{color:var(--text);word-break:break-all}
  .badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.75rem;font-weight:600}
  .badge-ok{background:#1b3d2f;color:#4caf93}
  .badge-err{background:#3d1b25;color:#e74c5f}
  .badge-run{background:#2a2640;color:#a78bfa}
  .empty{color:var(--muted);font-style:italic;padding:.5rem 0;font-size:.85rem}
  .tool-result{margin-top:.6rem;padding:.5rem .7rem;border-radius:6px;font-size:.82rem;display:none}
  .tool-result.ok{display:block;background:#1b3d2f;color:#4caf93}
  .tool-result.err{display:block;background:#3d1b25;color:#e74c5f}
  pre.debug{background:#0a0c12;border-radius:6px;padding:.7rem;font-size:.75rem;
            overflow-x:auto;max-height:300px;color:var(--text);margin-top:.6rem;
            white-space:pre-wrap;word-break:break-all;display:none}
  .modal-bg{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);
            display:flex;align-items:center;justify-content:center;z-index:100;display:none}
  .modal-bg.open{display:flex}
  .modal{background:var(--card);border:1px solid var(--border);border-radius:10px;
         padding:1.5rem;width:90%;max-width:450px}
  .modal h3{font-size:1rem;margin-bottom:1rem;color:var(--accent)}
  .modal .row{margin-bottom:.3rem}
  .modal-actions{display:flex;gap:.5rem;justify-content:flex-end;margin-top:1rem}
  .hint{font-size:.75rem;color:var(--muted);margin-top:-.3rem;margin-bottom:.5rem}
  .conf-badge{display:inline-block;padding:.1rem .4rem;border-radius:3px;font-size:.75rem;font-weight:600}
  .conf-on{background:#1b3d2f;color:#4caf93}
  .conf-off{background:#3d1b25;color:#e74c5f}
  @media(max-width:500px){.row{flex-direction:column}.conf-grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<h1><span>yt2strm</span> <small style="color:var(--muted);font-size:.7rem">v{{ conf.version }}</small></h1>
<div class="subtitle">YouTube → STRM for Emby / Jellyfin</div>

<!-- Add Channel -->
<div class="card">
  <h2>➕ Add Channel or Playlist</h2>
  <div class="row">
    <input type="text" id="chUrl" placeholder="https://www.youtube.com/@ChannelName">
  </div>
  <div class="row">
    <input type="text" id="chName" placeholder="Display name (optional)">
    <input type="text" id="chFolder" placeholder="Folder (optional)">
    <button class="btn-primary" onclick="addChannel()">Add</button>
  </div>
  <div class="hint">Folder nests inside the media root. STRM files contain direct YouTube URLs.</div>
</div>

<!-- Channel List -->
<div class="card">
  <h2>📺 Channels
    <button class="btn-scan" style="float:right" onclick="startScan()">▶ Scan Now</button>
  </h2>
  <ul class="ch-list" id="channelList"><li class="empty">Loading...</li></ul>
</div>

<!-- Status -->
<div class="card">
  <h2>📊 Status</h2>
  <div id="statusInfo" style="font-size:.85rem;margin-bottom:.7rem"></div>
  <div class="log-box" id="logBox"></div>
</div>

<!-- Tools -->
<div class="card">
  <h2>🛠️ Tools</h2>
  <div style="margin-bottom:1rem">
    <p style="font-size:.82rem;color:var(--muted);margin-bottom:.5rem">
      Rewrite all existing .strm files to use direct YouTube URLs (https://www.youtube.com/watch?v=VIDEO_ID).
    </p>
    <button class="btn-warn" onclick="regenerateStrms()">🔄 Regenerate All STRMs</button>
    <div class="tool-result" id="regenResult"></div>
  </div>
  <hr style="border-color:var(--border);margin-bottom:1rem">
  <div style="margin-bottom:1rem">
    <p style="font-size:.82rem;color:var(--muted);margin-bottom:.5rem">
      Delete all NFO files and run a full scan to regenerate metadata with the latest format (tagline, releasedate, etc.).
    </p>
    <button class="btn-warn" onclick="regenerateNfo()">🔄 Regenerate All NFO Files</button>
    <div class="tool-result" id="nfoResult"></div>
  </div>
  <hr style="border-color:var(--border);margin-bottom:1rem">
  <label>Debug — paste a YouTube video ID to verify metadata can be fetched</label>
  <div class="row">
    <input type="text" id="debugId" placeholder="e.g. dQw4w9WgXcQ">
    <button class="btn-primary" onclick="debugVideo()">Debug</button>
  </div>
  <pre class="debug" id="debugOutput"></pre>
</div>

<!-- Config -->
<div class="card">
  <h2>⚙️ Configuration</h2>
  <dl class="conf-grid">
    <dt>Media folder</dt><dd>{{ conf.media_dir }}</dd>
    <dt>Scan interval</dt><dd>{{ conf.scan_interval }}h</dd>
    <dt>Video limit</dt><dd>{{ conf.video_limit }} per channel</dd>
    <dt>Metadata</dt><dd><span class="conf-badge {{ 'conf-on' if conf.metadata else 'conf-off' }}">{{ 'NFO + thumbnails' if conf.metadata else 'disabled' }}</span></dd>
    <dt>Cookies</dt><dd><span class="conf-badge {{ 'conf-on' if conf.cookies else 'conf-off' }}">{{ 'loaded ✓' if conf.cookies else 'not loaded' }}</span> <span style="font-size:.75rem;color:var(--muted)">{{ conf.cookies_path }}</span></dd>
  </dl>
</div>

<!-- Edit Modal -->
<div class="modal-bg" id="editModal">
  <div class="modal">
    <h3>✏️ Edit Channel</h3>
    <input type="hidden" id="editIdx">
    <label>URL</label>
    <input type="text" id="editUrl">
    <label>Display Name</label>
    <input type="text" id="editName" placeholder="(auto-detect)">
    <label>Folder</label>
    <input type="text" id="editFolder" placeholder="(root)">
    <div class="modal-actions">
      <button class="btn-primary" onclick="saveEdit()">Save</button>
      <button style="background:var(--border);color:var(--text)" onclick="closeEdit()">Cancel</button>
    </div>
  </div>
</div>

<script>
const API = '';

async function fetchJSON(url, opts){
  const r = await fetch(API + url, opts);
  return r.json();
}

function esc(s){ const d=document.createElement('div');d.textContent=s;return d.innerHTML; }

async function loadChannels(){
  const chs = await fetchJSON('/api/channels');
  const ul = document.getElementById('channelList');
  if(!chs.length){ ul.innerHTML='<li class="empty">No channels yet. Add one above.</li>'; return; }
  ul.innerHTML = chs.map((c,i)=>{
    const folderBadge = c.folder ? `<span class="ch-folder">${esc(c.folder)}</span>` : '';
    return `
    <li class="ch-item">
      <div>
        ${folderBadge}<span class="ch-name">${esc(c.name||'(auto)')}</span>
        <span class="ch-url">${esc(c.url)}</span>
      </div>
      <div class="ch-actions">
        <button class="btn-scan btn-sm" onclick="scanChannel(${i})">▶</button>
        <button class="btn-primary btn-sm" onclick="openEdit(${i})">✎</button>
        <button class="btn-danger" onclick="delChannel(${i})">✕</button>
      </div>
    </li>`;
  }).join('');
}

async function addChannel(){
  const url = document.getElementById('chUrl').value.trim();
  const name = document.getElementById('chName').value.trim();
  const folder = document.getElementById('chFolder').value.trim();
  if(!url) return;
  await fetchJSON('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url, name, folder})});
  document.getElementById('chUrl').value='';
  document.getElementById('chName').value='';
  document.getElementById('chFolder').value='';
  loadChannels();
}

async function scanChannel(idx){
  await fetchJSON('/api/scan/'+idx,{method:'POST'});
  pollStatus();
}

async function delChannel(idx){
  await fetchJSON('/api/channels/'+idx,{method:'DELETE'});
  loadChannels();
}

function openEdit(idx){
  fetchJSON('/api/channels').then(chs=>{
    const c = chs[idx];
    document.getElementById('editIdx').value = idx;
    document.getElementById('editUrl').value = c.url||'';
    document.getElementById('editName').value = c.name||'';
    document.getElementById('editFolder').value = c.folder||'';
    document.getElementById('editModal').classList.add('open');
  });
}

function closeEdit(){
  document.getElementById('editModal').classList.remove('open');
}

async function saveEdit(){
  const idx = document.getElementById('editIdx').value;
  const url = document.getElementById('editUrl').value.trim();
  const name = document.getElementById('editName').value.trim();
  const folder = document.getElementById('editFolder').value.trim();
  await fetchJSON('/api/channels/'+idx,{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url, name, folder})});
  closeEdit();
  loadChannels();
}

async function startScan(){
  await fetchJSON('/api/scan',{method:'POST'});
  pollStatus();
}

async function regenerateStrms(){
  const el = document.getElementById('regenResult');
  el.className='tool-result';el.style.display='block';el.textContent='Working...';
  try{
    const r = await fetchJSON('/api/regenerate',{method:'POST'});
    el.className='tool-result ok';
    el.textContent=`Done — ${r.updated} updated, ${r.skipped} already correct, ${r.errors} errors`;
  }catch(e){
    el.className='tool-result err';
    el.textContent='Error: '+e;
  }
  pollStatus();
}

async function regenerateNfo(){
  if(!confirm('This will delete ALL NFO files and trigger a full rescan. This may take a while. Continue?')) return;
  const el = document.getElementById('nfoResult');
  el.className='tool-result';el.style.display='block';el.textContent='Deleting NFO files...';
  try{
    const r = await fetchJSON('/api/regenerate-nfo',{method:'POST'});
    el.textContent=`Deleted ${r.deleted} NFO files. Starting scan...`;
    await fetchJSON('/api/scan',{method:'POST'});
    el.className='tool-result ok';
    el.textContent=`Deleted ${r.deleted} NFO files. Scan started - check Status section for progress.`;
    pollStatus();
  }catch(e){
    el.className='tool-result err';
    el.textContent='Error: '+e;
  }
}

async function debugVideo(){
  const id = document.getElementById('debugId').value.trim();
  if(!id) return;
  const out = document.getElementById('debugOutput');
  out.style.display='block';
  out.textContent='Fetching metadata...';
  try{
    const r = await fetchJSON('/api/debug/'+id);
    out.textContent = JSON.stringify(r, null, 2);
  }catch(e){
    out.textContent='Error: '+e;
  }
}

async function pollStatus(){
  const s = await fetchJSON('/api/status');
  let html = '';
  if(s.scanning) html += '<span class="badge badge-run">Scanning...</span> ';
  if(s.last_scan) html += 'Last scan: '+s.last_scan+' ';
  if(s.results.length){
    html += '<br>';
    s.results.forEach(r=>{
      if(r.status==='ok') html+=`<span class="badge badge-ok">✓</span> ${esc(r.channel)}: ${r.new} new &nbsp;`;
      else html+=`<span class="badge badge-err">✗</span> ${esc(r.channel)}: ${esc(r.error)} &nbsp;`;
    });
  }
  document.getElementById('statusInfo').innerHTML=html;

  const box = document.getElementById('logBox');
  box.innerHTML = s.logs.map(l=>
    `<div class="log-line"><span class="t">${l.time}</span> `+
    (l.level==='error'?`<span class="e">${esc(l.msg)}</span>`:`${esc(l.msg)}`)+`</div>`
  ).join('');
  box.scrollTop=box.scrollHeight;
}

// Close modal on backdrop click
document.getElementById('editModal').addEventListener('click', function(e){
  if(e.target===this) closeEdit();
});

loadChannels();
pollStatus();
setInterval(pollStatus, 3000);
</script>
</body>
</html>"""

# ── Startup ──────────────────────────────────────────────────────

if __name__ == '__main__':
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    add_log(f'yt2strm v{VERSION} starting')
    add_log(f'Metadata: {"enabled (NFO + thumbnails)" if METADATA else "disabled"}')

    # Cookie status
    if COOKIES_FILE:
        if os.path.isfile(COOKIES_FILE):
            add_log(f'Cookies loaded from {COOKIES_FILE} ✓')
        else:
            add_log(f'Cookies file not found: {COOKIES_FILE} — YouTube may block requests!', 'error')
    else:
        add_log('No cookies configured (YT2STRM_COOKIES) — YouTube may block requests', 'error')

    if SCAN_INTERVAL > 0:
        threading.Thread(target=background_scanner, daemon=True).start()
        add_log(f'Background scanner enabled every {SCAN_INTERVAL // 3600}h')

    app.run(host=HOST, port=PORT, debug=False, threaded=True)
