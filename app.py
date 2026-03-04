#!/usr/bin/env python3
"""
yt2strm - Lightweight YouTube-to-STRM server for Emby/Jellyfin
FIXED: Improved audio dropout handling for longer videos
"""

from flask import Flask, redirect, request, jsonify, Response, render_template_string
from html import escape as html_escape
import yt_dlp
import json
import os
import re
import subprocess
import requests as http_req
import threading
import time
import logging
from datetime import datetime, timezone

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('yt2strm')

VERSION = '0.2.0'  # Version number

# ── Config from environment ──────────────────────────────────────
HOST         = os.environ.get('YT2STRM_HOST', '0.0.0.0')
PORT         = int(os.environ.get('YT2STRM_PORT', 5000))
EXTERNAL_URL = os.environ.get('YT2STRM_URL', '').rstrip('/')
MEDIA_DIR    = os.environ.get('YT2STRM_MEDIA', '/media/YouTube')
DATA_DIR     = os.environ.get('YT2STRM_DATA', '/data')
SCAN_INTERVAL= int(os.environ.get('YT2STRM_INTERVAL', 1)) * 3600   # hours between scans, 0=off
VIDEO_LIMIT  = int(os.environ.get('YT2STRM_LIMIT', 50))
MODE         = os.environ.get('YT2STRM_MODE', 'redirect')       # redirect, bridge, or proxy
METADATA     = os.environ.get('YT2STRM_METADATA', 'true').lower() in ('true', '1', 'yes')
PLAY_HEIGHT  = int(os.environ.get('YT2STRM_PLAY_HEIGHT', 0))    # max height for play/bridge mode, 0=auto

if not EXTERNAL_URL:
    EXTERNAL_URL = f'http://localhost:{PORT}'

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
    state['logs'] = state['logs'][-500:]  # Keep more logs for detailed scanning
    getattr(logger, level, logger.info)(msg)

# ── Channel persistence ─────────────────────────────────────────
def load_channels():
    try:
        if os.path.exists(CHANNELS_FILE):
            with open(CHANNELS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        add_log(f'Error loading channels: {e}', 'error')
    return []

def save_channels(channels):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHANNELS_FILE, 'w') as f:
        json.dump(channels, f, indent=2)

# ── Helpers ──────────────────────────────────────────────────────
def sanitize(name):
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', '', str(name))
    name = name.strip('. ')
    return name[:200] if name else 'Untitled'

def get_mode_endpoint():
    """Return the URL path segment for the current mode."""
    if MODE == 'proxy':
        return 'proxy'
    if MODE == 'bridge':
        return 'bridge'
    return 'play'

def xml_escape(text):
    """Escape text for safe inclusion in XML content."""
    return html_escape(str(text), quote=False)

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

def write_movie_nfo(path, title, video_id, upload_date=None, description=None):
    """Write a Kodi/Emby-compatible movie NFO file."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<movie>']
    lines.append(f'  <title>{xml_escape(title)}</title>')
    if description:
        lines.append(f'  <plot>{xml_escape(description)}</plot>')
    if upload_date and len(str(upload_date)) >= 8:
        d = str(upload_date)[:8]
        premiered = f'{d[:4]}-{d[4:6]}-{d[6:8]}'
        lines.append(f'  <premiered>{premiered}</premiered>')
        lines.append(f'  <year>{d[:4]}</year>')
    lines.append(f'  <uniqueid type="youtube">{xml_escape(video_id)}</uniqueid>')
    lines.append('</movie>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

def write_episode_nfo(path, title, video_id, upload_date=None, description=None, show_title=None):
    """Write a Kodi/Emby-compatible episode NFO file."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<episodedetails>']
    lines.append(f'  <title>{xml_escape(title)}</title>')
    if show_title:
        lines.append(f'  <showtitle>{xml_escape(show_title)}</showtitle>')
    if description:
        lines.append(f'  <plot>{xml_escape(description)}</plot>')
    if upload_date and len(str(upload_date)) >= 8:
        d = str(upload_date)[:8]
        aired = f'{d[:4]}-{d[4:6]}-{d[6:8]}'
        lines.append(f'  <aired>{aired}</aired>')
        lines.append(f'  <year>{d[:4]}</year>')
    lines.append(f'  <uniqueid type="youtube">{xml_escape(video_id)}</uniqueid>')
    lines.append('</episodedetails>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

def write_tvshow_nfo(path, name, channel_id=None, description=None):
    """Write a tvshow.nfo for the channel folder."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tvshow>']
    lines.append(f'  <title>{xml_escape(name)}</title>')
    if description:
        lines.append(f'  <plot>{xml_escape(description)}</plot>')
    if channel_id:
        lines.append(f'  <uniqueid type="youtube">{xml_escape(channel_id)}</uniqueid>')
    lines.append('</tvshow>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

def get_video_url(video_id):
    """Resolve a fresh direct stream URL (single muxed file, caps at ~720p)."""
    # Build format selector based on PLAY_HEIGHT config
    if PLAY_HEIGHT > 0:
        # Force specific height: try exact match, then best available up to that height
        format_str = f'best[ext=mp4][height={PLAY_HEIGHT}]/best[ext=mp4][height<={PLAY_HEIGHT}]/best[ext=mp4]/best'
    else:
        # Auto mode: try to get best available (usually caps at 360p or 720p for muxed streams)
        format_str = 'best[ext=mp4][height<=1080]/best[ext=mp4]/best'

    opts = {
        'format': format_str,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f'https://www.youtube.com/watch?v={video_id}',
            download=False
        )
        return info.get('url')

def get_proxy_urls(video_id):
    """Resolve separate video + audio URLs for ffmpeg muxing (up to 1080p).

    Excludes AV1 codec for compatibility with older devices like Nvidia Shield.
    Prefers H.264 (avc1) which is universally supported.
    """
    opts = {
        'format': (
            # Prefer H.264 up to configured height, exclude AV1
            'bestvideo[height<=1080][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
            'bestvideo[height<=1080][ext=mp4][vcodec!=av01][vcodec!=vp9]+bestaudio[ext=m4a]/'
            'bestvideo[height<=1080][ext=mp4][vcodec!=av01]+bestaudio/'
            'best[ext=mp4]/best'
        ),
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f'https://www.youtube.com/watch?v={video_id}',
            download=False
        )
        if 'requested_formats' in info:
            vf = info['requested_formats'][0]
            af = info['requested_formats'][1]
            return vf['url'], af['url'], vf.get('height', '?')
        return info['url'], None, info.get('height', '?')

def scan_channel(channel_url, custom_name=None, folder=None, content_type='movie'):
    """List videos in a channel/playlist, create .strm + metadata files.

    Args:
        channel_url: YouTube channel or playlist URL
        custom_name: Optional custom display name
        folder: Optional folder to organize channels
        content_type: 'movie' or 'tv' - determines NFO format
    """
    opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'playlistend': VIDEO_LIMIT,
        'socket_timeout': 30,
    }
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
        # Create tvshow.nfo only if content_type is 'tv'
        if content_type == 'tv':
            tvshow_nfo_path = os.path.join(channel_dir, 'tvshow.nfo')
            if not os.path.exists(tvshow_nfo_path):
                try:
                    write_tvshow_nfo(
                        tvshow_nfo_path, name,
                        info.get('channel_id') or info.get('id'),
                        info.get('description')
                    )
                    add_log(f'  Created tvshow.nfo for {name}')
                except Exception as e:
                    add_log(f'  tvshow.nfo error: {e}', 'error')

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
    endpoint = get_mode_endpoint()
    new_count = 0
    meta_count = 0
    thumb_count = 0
    total_entries = len([e for e in entries if e and (e.get('id') or e.get('url'))])

    add_log(f'  Found {total_entries} videos to process')

    for idx, entry in enumerate(entries, 1):
        if not entry:
            continue
        vid_id    = entry.get('id') or entry.get('url') or ''
        vid_title = sanitize(entry.get('title') or vid_id)
        if not vid_id or not vid_title:
            continue

        # ── STRM file ────────────────────────────────────────────
        strm_path = os.path.join(channel_dir, f'{vid_title}.strm')
        if not os.path.exists(strm_path):
            try:
                with open(strm_path, 'w', encoding='utf-8') as f:
                    f.write(f'{EXTERNAL_URL}/{endpoint}/{vid_id}')
                new_count += 1
                add_log(f'  [{idx}/{total_entries}] Created: {vid_title}.strm')
            except OSError as e:
                add_log(f'  [{idx}/{total_entries}] Write error {vid_title}: {e}', 'error')
        else:
            add_log(f'  [{idx}/{total_entries}] Exists: {vid_title}.strm')

        # ── Metadata (NFO + thumbnail) ───────────────────────────
        if METADATA:
            nfo_path = os.path.join(channel_dir, f'{vid_title}.nfo')
            if not os.path.exists(nfo_path):
                try:
                    # Fetch full metadata if not available from flat extraction
                    upload_date = entry.get('upload_date') or ''
                    description = entry.get('description')

                    if not upload_date:
                        # Do a full extraction to get upload_date
                        try:
                            full_opts = {
                                'quiet': True,
                                'no_warnings': True,
                                'socket_timeout': 30,
                            }
                            with yt_dlp.YoutubeDL(full_opts) as ydl_full:
                                full_info = ydl_full.extract_info(
                                    f'https://www.youtube.com/watch?v={vid_id}',
                                    download=False
                                )
                                upload_date = full_info.get('upload_date') or ''
                                if not description:
                                    description = full_info.get('description')
                                if not upload_date and full_info.get('timestamp'):
                                    upload_date = datetime.fromtimestamp(
                                        full_info['timestamp'], tz=timezone.utc
                                    ).strftime('%Y%m%d')
                        except Exception:
                            # If full extraction fails, try timestamp from entry
                            if entry.get('timestamp'):
                                upload_date = datetime.fromtimestamp(
                                    entry['timestamp'], tz=timezone.utc
                                ).strftime('%Y%m%d')

                    # Use appropriate NFO format based on content_type
                    if content_type == 'tv':
                        write_episode_nfo(
                            nfo_path,
                            entry.get('title') or vid_id,
                            vid_id,
                            upload_date,
                            description,
                            show_title=name  # Use display name as show title
                        )
                    else:  # default to movie
                        write_movie_nfo(
                            nfo_path,
                            entry.get('title') or vid_id,
                            vid_id,
                            upload_date,
                            description
                        )
                    meta_count += 1
                    add_log(f'      + NFO metadata')
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
    add_log(f'Scan started — {len(channels)} channel(s)' +
            (' [metadata enabled]' if METADATA else ''))

    for i, ch in enumerate(channels):
        label = ch.get('name') or ch['url']
        folder = ch.get('folder')
        content_type = ch.get('content_type', 'movie')  # default to movie if not specified
        if folder:
            label = f'{folder}/{label}'
        add_log(f'[{i+1}/{len(channels)}] Scanning: {label}')
        try:
            count, resolved = scan_channel(ch['url'], ch.get('name'), folder, content_type)
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

# ── Stream routes ────────────────────────────────────────────────

@app.route('/play/<video_id>')
def play(video_id):
    """Redirect mode — fast, lightweight."""
    try:
        url = get_video_url(video_id)
        if url:
            return redirect(url, 302)
        return 'Could not resolve video', 502
    except Exception as e:
        add_log(f'Play error {video_id}: {e}', 'error')
        return f'Error: {e}', 502

@app.route('/bridge/<video_id>')
def bridge(video_id):
    """Bridge mode — uses best available quality, muxing with ffmpeg if needed."""
    try:
        # Try to get high-quality separate streams first
        video_url, audio_url, height = get_proxy_urls(video_id)

        # If we have separate streams and PLAY_HEIGHT is set, use ffmpeg to mux
        if audio_url and PLAY_HEIGHT > 0:
            add_log(f'Bridge muxing {video_id}: {height}p with ffmpeg')
            cmd = [
                'ffmpeg',
                '-hide_banner', '-loglevel', 'error',
                '-headers', 'User-Agent: Mozilla/5.0\r\n',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', video_url,
                '-headers', 'User-Agent: Mozilla/5.0\r\n',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', audio_url,
                '-c:v', 'copy',
                '-c:a', 'copy',
                '-movflags', 'frag_keyframe+empty_moov',
                '-f', 'mp4',
                'pipe:1',
            ]
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            def generate():
                try:
                    fd = process.stdout.fileno()
                    while True:
                        chunk = os.read(fd, 256 * 1024)
                        if not chunk:
                            break
                        yield chunk
                except (GeneratorExit, BrokenPipeError):
                    pass
                except Exception as e:
                    add_log(f'Bridge stream error {video_id}: {e}', 'error')
                finally:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait()

            return Response(
                generate(),
                mimetype='video/mp4',
                headers={'Accept-Ranges': 'none'}
            )
        else:
            # Fall back to simple muxed stream
            url = get_video_url(video_id)
            if not url:
                return 'Could not resolve video', 502

            r = http_req.get(url, stream=True, timeout=30,
                             headers={'User-Agent': 'Mozilla/5.0'})

            def generate():
                try:
                    for chunk in r.iter_content(chunk_size=512 * 1024):
                        if chunk:
                            yield chunk
                except (GeneratorExit, BrokenPipeError):
                    pass
                except Exception as e:
                    add_log(f'Bridge fallback error {video_id}: {e}', 'error')
                finally:
                    r.close()

            headers = {
                'Content-Type': r.headers.get('Content-Type', 'video/mp4'),
                'Accept-Ranges': 'none',
            }
            if 'Content-Length' in r.headers:
                headers['Content-Length'] = r.headers['Content-Length']

            return Response(generate(), headers=headers)
    except Exception as e:
        add_log(f'Bridge error {video_id}: {e}', 'error')
        return f'Error: {e}', 502

@app.route('/proxy/<video_id>')
def proxy(video_id):
    """Proxy mode — muxes best video + best audio via ffmpeg for up to 1080p.

    FIXED: Added reconnection flags to handle longer videos and prevent audio dropouts.
    """
    try:
        video_url, audio_url, height = get_proxy_urls(video_id)
        add_log(f'Proxy resolve {video_id}: {height}p, separate_audio={audio_url is not None}')

        if audio_url:
            # FIX: Added reconnection flags to prevent dropouts on longer videos
            cmd = [
                'ffmpeg',
                '-hide_banner', '-loglevel', 'error',
                # Video input with reconnection support
                '-headers', 'User-Agent: Mozilla/5.0\r\n',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', video_url,
                # Audio input with reconnection support
                '-headers', 'User-Agent: Mozilla/5.0\r\n',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', audio_url,
                # Copy codecs (no re-encoding)
                '-c:v', 'copy',
                '-c:a', 'copy',
                # MP4 fragmentation for streaming
                '-movflags', 'frag_keyframe+empty_moov',
                '-f', 'mp4',
                'pipe:1',
            ]
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            def generate():
                try:
                    fd = process.stdout.fileno()
                    while True:
                        chunk = os.read(fd, 256 * 1024)
                        if not chunk:
                            break
                        yield chunk
                except (GeneratorExit, BrokenPipeError):
                    # Client disconnected - this is normal
                    pass
                except Exception as e:
                    # Log unexpected errors
                    add_log(f'Proxy stream error {video_id}: {e}', 'error')
                finally:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait()

            return Response(
                generate(),
                mimetype='video/mp4',
                headers={'Accept-Ranges': 'none'}
            )
        else:
            r = http_req.get(video_url, stream=True, timeout=30,
                             headers={'User-Agent': 'Mozilla/5.0'})

            def generate():
                try:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            yield chunk
                except (GeneratorExit, BrokenPipeError):
                    pass
                except Exception as e:
                    add_log(f'Proxy fallback error {video_id}: {e}', 'error')
                finally:
                    r.close()

            return Response(
                generate(),
                mimetype='video/mp4',
                headers={'Accept-Ranges': 'none'}
            )
    except Exception as e:
        add_log(f'Proxy error {video_id}: {e}', 'error')
        return f'Error: {e}', 502

# ── API routes ───────────────────────────────────────────────────

@app.route('/api/channels', methods=['GET'])
def api_get_channels():
    return jsonify(load_channels())

@app.route('/api/channels', methods=['POST'])
def api_add_channel():
    data = request.json or {}
    url    = data.get('url', '').strip()
    name   = data.get('name', '').strip() or None
    folder = data.get('folder', '').strip() or None
    content_type = data.get('content_type', 'movie').strip()  # default to movie
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if content_type not in ('movie', 'tv'):
        content_type = 'movie'  # fallback to movie if invalid
    channels = load_channels()
    channels.append({'url': url, 'name': name, 'folder': folder, 'content_type': content_type})
    save_channels(channels)
    label = f'{folder}/{name or url}' if folder else (name or url)
    add_log(f'Added: {label} (type: {content_type})')
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
    if 'content_type' in data:
        content_type = data['content_type'].strip()
        if content_type in ('movie', 'tv'):
            channels[idx]['content_type'] = content_type
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
        content_type = ch.get('content_type', 'movie')
        if folder:
            label = f'{folder}/{label}'
        add_log(f'Scanning single channel: {label}')
        try:
            count, resolved = scan_channel(ch['url'], ch.get('name'), folder, content_type)
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
    """Rewrite every .strm file so it points at the current mode endpoint."""
    endpoint = get_mode_endpoint()
    updated = 0
    skipped = 0
    errors  = 0
    for root, _dirs, files in os.walk(MEDIA_DIR):
        for fname in files:
            if not fname.endswith('.strm'):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    content = fh.read().strip()
                m = re.search(r'/(play|bridge|proxy)/([A-Za-z0-9_-]+)', content)
                if m:
                    vid_id = m.group(2)
                    new_content = f'{EXTERNAL_URL}/{endpoint}/{vid_id}'
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
    add_log(f'Regenerate done: {updated} updated, {skipped} already correct, {errors} errors → /{endpoint}/')
    return jsonify({'status': 'ok', 'updated': updated, 'skipped': skipped,
                    'errors': errors, 'endpoint': endpoint})

@app.route('/api/debug/<video_id>', methods=['GET'])
def api_debug(video_id):
    """Show what yt-dlp resolves for a video in each mode."""
    result = {'video_id': video_id, 'current_mode': MODE, 'play_height_config': PLAY_HEIGHT}

    try:
        # Use the same format logic as get_video_url()
        if PLAY_HEIGHT > 0:
            format_str = f'best[ext=mp4][height={PLAY_HEIGHT}]/best[ext=mp4][height<={PLAY_HEIGHT}]/best[ext=mp4]/best'
        else:
            format_str = 'best[ext=mp4][height<=1080]/best[ext=mp4]/best'

        opts = {
            'format': format_str,
            'quiet': True, 'no_warnings': True, 'socket_timeout': 30,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f'https://www.youtube.com/watch?v={video_id}', download=False)
            result['play_mode'] = {
                'height': info.get('height'),
                'ext': info.get('ext'),
                'format': info.get('format'),
                'vcodec': info.get('vcodec'),
                'acodec': info.get('acodec'),
                'format_selector': format_str,
            }
    except Exception as e:
        result['play_mode'] = {'error': str(e)}

    try:
        opts = {
            'format': (
                'bestvideo[height<=1080][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
                'bestvideo[height<=1080][ext=mp4][vcodec!=av01][vcodec!=vp9]+bestaudio[ext=m4a]/'
                'bestvideo[height<=1080][ext=mp4][vcodec!=av01]+bestaudio/'
                'best[ext=mp4]/best'
            ),
            'quiet': True, 'no_warnings': True, 'socket_timeout': 30,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f'https://www.youtube.com/watch?v={video_id}', download=False)
            if 'requested_formats' in info:
                vf = info['requested_formats'][0]
                af = info['requested_formats'][1]
                result['proxy_mode'] = {
                    'video': {
                        'height': vf.get('height'),
                        'ext': vf.get('ext'),
                        'format': vf.get('format'),
                        'vcodec': vf.get('vcodec'),
                    },
                    'audio': {
                        'ext': af.get('ext'),
                        'format': af.get('format'),
                        'acodec': af.get('acodec'),
                    },
                }
            else:
                result['proxy_mode'] = {
                    'single_stream': True,
                    'height': info.get('height'),
                    'ext': info.get('ext'),
                    'format': info.get('format'),
                }
    except Exception as e:
        result['proxy_mode'] = {'error': str(e)}

    try:
        r = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        result['ffmpeg'] = r.stdout.split('\n')[0] if r.returncode == 0 else 'not working'
    except Exception:
        result['ffmpeg'] = 'not found'

    return jsonify(result)

@app.route('/api/status', methods=['GET'])
def api_status():
    return jsonify(state)

# ── Web UI ───────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML, conf={
        'external_url': EXTERNAL_URL,
        'mode': MODE,
        'media_dir': MEDIA_DIR,
        'scan_interval': SCAN_INTERVAL // 3600 if SCAN_INTERVAL > 0 else 0,  # Convert to hours
        'video_limit': VIDEO_LIMIT,
        'metadata': METADATA,
        'play_height': PLAY_HEIGHT,
        'version': VERSION,
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
    <select id="chContentType" style="padding:.55rem .7rem;border-radius:6px;border:1px solid var(--border);background:#12141c;color:var(--text);font-size:.9rem;">
      <option value="movie">Movie</option>
      <option value="tv">TV Series</option>
    </select>
    <button class="btn-primary" onclick="addChannel()">Add</button>
  </div>
  <div class="hint">Folder nests inside the media root. Content Type: Movie = all in one folder, TV = organized by seasons.</div>
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
      Rewrite all existing .strm files to use the current mode (<b>{{ conf.mode }}</b>).
      Use this after changing YT2STRM_MODE.
    </p>
    <button class="btn-warn" onclick="regenerateStrms()">🔄 Regenerate All STRMs</button>
    <div class="tool-result" id="regenResult"></div>
  </div>
  <hr style="border-color:var(--border);margin-bottom:1rem">
  <label>Debug — paste a YouTube video ID to inspect resolution</label>
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
    <dt>External URL</dt><dd>{{ conf.external_url }}</dd>
    <dt>Mode</dt><dd>{{ conf.mode }}</dd>
    <dt>Media folder</dt><dd>{{ conf.media_dir }}</dd>
    <dt>Scan interval</dt><dd>{{ conf.scan_interval }}h</dd>
    <dt>Video limit</dt><dd>{{ conf.video_limit }} per channel</dd>
    <dt>Metadata</dt><dd><span class="conf-badge {{ 'conf-on' if conf.metadata else 'conf-off' }}">{{ 'NFO + thumbnails' if conf.metadata else 'disabled' }}</span></dd>
    <dt>Play height</dt><dd>{{ conf.play_height if conf.play_height > 0 else 'auto' }}{{ 'p' if conf.play_height > 0 else '' }}</dd>
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
    <label>Content Type</label>
    <select id="editContentType" style="width:100%;padding:.55rem .7rem;border-radius:6px;border:1px solid var(--border);background:#12141c;color:var(--text);font-size:.9rem;margin-bottom:.6rem">
      <option value="movie">Movie</option>
      <option value="tv">TV Series</option>
    </select>
    <div class="hint">Movie = all in one folder. TV = organized by seasons (Emby auto-sorts).</div>
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
    const contentType = c.content_type || 'movie';
    const typeBadge = `<span class="ch-folder" style="background:${contentType==='tv'?'#2a4026':'#402a26'};color:${contentType==='tv'?'#4caf93':'#e74c5f'}">${contentType==='tv'?'TV':'Movie'}</span>`;
    return `
    <li class="ch-item">
      <div>
        ${folderBadge}${typeBadge}<span class="ch-name">${esc(c.name||'(auto)')}</span>
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
  const content_type = document.getElementById('chContentType').value;
  if(!url) return;
  await fetchJSON('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url, name, folder, content_type})});
  document.getElementById('chUrl').value='';
  document.getElementById('chName').value='';
  document.getElementById('chFolder').value='';
  document.getElementById('chContentType').value='movie';
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
    document.getElementById('editContentType').value = c.content_type||'movie';
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
  const content_type = document.getElementById('editContentType').value;
  await fetchJSON('/api/channels/'+idx,{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url, name, folder, content_type})});
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
    el.textContent=`Done — ${r.updated} updated, ${r.skipped} already correct, ${r.errors} errors → /${r.endpoint}/`;
  }catch(e){
    el.className='tool-result err';
    el.textContent='Error: '+e;
  }
  pollStatus();
}

async function debugVideo(){
  const id = document.getElementById('debugId').value.trim();
  if(!id) return;
  const out = document.getElementById('debugOutput');
  out.style.display='block';
  out.textContent='Resolving… (this takes a few seconds)';
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

    add_log(f'yt2strm starting — {EXTERNAL_URL} — mode={MODE} [FIXED VERSION]')
    add_log(f'Metadata: {"enabled (NFO + thumbnails)" if METADATA else "disabled"}')

    if MODE == 'proxy':
        try:
            r = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                add_log(f'ffmpeg found: {r.stdout.split(chr(10))[0]}')
            else:
                add_log('ffmpeg not working — proxy mode will fail!', 'error')
        except FileNotFoundError:
            add_log('ffmpeg not found — proxy mode requires ffmpeg!', 'error')

    if SCAN_INTERVAL > 0:
        threading.Thread(target=background_scanner, daemon=True).start()
        add_log(f'Background scanner enabled every {SCAN_INTERVAL // 3600}h')

    app.run(host=HOST, port=PORT, debug=False, threaded=True)
