# yt2strm - YouTube to STRM for Emby/Jellyfin

**Version 1.0.0** - Simplified edition for Emby/Jellyfin native YouTube support

A lightweight tool that creates `.strm` files with direct YouTube URLs for your favorite channels and playlists. Perfect for organizing YouTube content in Emby or Jellyfin media servers.

## Features

- 📺 **Direct YouTube URLs** - STRM files contain `https://www.youtube.com/watch?v=VIDEO_ID`
- 📝 **Metadata Support** - Generates NFO files with titles, descriptions, dates, and durations
- 🖼️ **Thumbnails** - Downloads video and channel poster thumbnails
- 🔄 **Automatic Scanning** - Schedule periodic scans to find new videos
- 🌐 **Web Interface** - Manage channels and monitor scans from your browser
- 📁 **Folder Organization** - Organize channels into custom folders
- 🍪 **Cookie Support** - Use cookies to avoid YouTube bot detection

## What Changed from v0.5.0

This simplified version removes all the streaming server functionality (redirect/bridge/proxy modes). Since Emby and Jellyfin now support playing YouTube URLs natively, the app focuses on what it does best: creating and organizing STRM files with metadata.

**Removed:**
- All streaming modes (play/bridge/proxy endpoints)
- ffmpeg dependency
- Video resolution settings
- Content type selection (TV/Movie) - now only creates movie-style NFOs

**Kept:**
- Web UI for channel management
- Metadata generation (NFO + thumbnails)
- Automatic scanning
- Video limit per channel
- Cookie support for YouTube access
