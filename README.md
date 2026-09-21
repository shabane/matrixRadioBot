# Matrix Radio Voice Bot 📻

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MatrixRTC](https://img.shields.io/badge/MatrixRTC-MSC3401-green.svg)](https://github.com/matrix-org/matrix-spec-proposals/pull/3401)
[![LiveKit](https://img.shields.io/badge/LiveKit-WebRTC-orange.svg)](https://livekit.io/)

A high-performance, asynchronous music and radio voice bot for **Matrix** and **LiveKit** voice calls (MatrixRTC / Element Call / Cinny). 

Powered by **LiveKit WebRTC**, **yt-dlp**, and **FFmpeg**, it supports real-time audio streaming from **YouTube**, **Spotify**, **SoundCloud**, direct audio URLs, and plain text search queries.

---

## 🌟 Key Features

- 🎙️ **Native MatrixRTC Integration:**
  - Connects directly to **LiveKit SFU** using Matrix OpenID authentication and the MatrixRTC (MSC3401) protocol.
  - Automatically publishes call membership state so the bot's avatar and name appear directly in the voice call UI.

- 🎵 **Multi-Platform Music Support:**
  - 🔴 **YouTube:** Stream any YouTube video or YouTube Music track directly.
  - 🟢 **Spotify:** Resolves Spotify track URLs (`open.spotify.com/track/...`) via public metadata matching and seamlessly streams the best audio match.
  - 🟠 **SoundCloud:** Full support for SoundCloud tracks and playlists.
  - 🔍 **Text Search:** Search by song title or artist without needing a link (`@radio play Bohemian Rhapsody`).
  - 📁 **Direct Audio Streams:** Supports raw audio formats (`.mp3`, `.opus`, `.ogg`, `.flac`, `.wav`, `.aac`, `.m3u8`).

- ⚡ **Zero-Disk Streaming Pipeline:**
  - Streams audio on-the-fly by piping `yt-dlp` output directly into `FFmpeg` without storing large files on disk.
  - Real-time transcoding to pristine 48kHz 16-bit signed PCM audio frames (standard WebRTC Opus format).

- 🔒 **Configurable Proxy Support (SOCKS5 / HTTP):**
  - Bypass censorship and geo-restrictions by routing extraction and streaming requests through a proxy.
  - Configurable via `config.yaml` or dynamically through the `RADIO_PROXY_URL` environment variable.

- 👥 **Isolated Queues per Room / Chat:**
  - Every group room or direct message (PV) maintains its own independent queue and playback worker.
  - Music played in one room never mixes with another.

- ⏳ **Smart Idle Management:**
  - Automatically disconnects from voice calls after the queue finishes to conserve server resources.

- 🤝 **Auto-Accept Invites:**
  - Automatically accepts invites to new rooms and direct messages.

---

## 🎮 Bot Commands

> **Note:** In group rooms, mention the bot with `@radio`. In direct messages (PV), mentions are optional.

| Command | Description |
| :--- | :--- |
| `@radio <url>` | Enqueue a song/video from YouTube, Spotify, SoundCloud, or direct audio link |
| `@radio play <query>` | Search YouTube for `<query>` and enqueue the first match |
| `@radio pause` | Pause current playback |
| `@radio resume` | Resume paused playback |
| `@radio skip` | Skip the currently playing track and start the next one |
| `@radio queue` | View the list of tracks in the room's playback queue |
| `@radio np` | Display information about the currently playing track (title, duration, source) |
| `@radio leave` | Clear the queue and immediately disconnect from the voice call |
| `@radio help` | Show available commands and usage guide |

---

## ⚡ Bang (`!`) Commands

In addition to the `@radio` mention convention above, the bot supports a second, terser
command style prefixed with `!`. These work in **any** room, group or PV, **without** needing
to mention the bot.

### Playback

| Command | Alias | Description |
| :--- | :--- | :--- |
| `!help` | `!h` | Show this help |
| `!join` | `!j` | Join Element Call in this room |
| `!leave` | `!lv` | Leave current Element Call |
| `!play <url-or-query>` | `!p` | Add track and auto-join call if needed |
| `!queue` | `!q` | Show queue with ETA |
| `!nowplaying` | `!np` | Show current track |
| `!skip` | `!s` | Skip current track |
| `!stop` | `!x` | Stop playback and clear queue (stays connected to the call) |
| `!loop` | `!lp` | Toggle loop mode (repeats the current track) |
| `!history` | `!hist` | Show recent playback history |

### Saved Queues

| Command | Alias | Description |
| :--- | :--- | :--- |
| `!save <name> [--force]` | `!sv` | Save current+upcoming queue |
| `!load <name>` | `!ld` | Load a saved queue |
| `!queues` | `!qs` | List saved queues |
| `!deletequeue <name>` | `!dq` | Delete a saved queue |
| `!renamequeue <old> <new>` | `!rq` | Rename a saved queue |

### Audio & Info

| Command | Alias | Description |
| :--- | :--- | :--- |
| `!audio` | `!a` | Show current audio settings |
| `!normalize on\|off` | `!norm` | Toggle normalization (FFmpeg `loudnorm`) |
| `!fadein <ms>` | `!fi` | Set fade-in (0-5000ms) |
| `!volume <0-200>` | `!v` | Set playback volume percent |
| `!status` | `!st` | Show bot status |
| `!diag` | `!d` | Show diagnostics |
| `!config` | `!cfg` | Show active config |
| `!defaults` | `!df` | Show default config values |

Saved queues and playback history persist to JSON files under a gitignored `data/` directory,
namespaced per room.

---

## ⚙️ Configuration

Copy the example configuration file and adjust it with your Matrix and LiveKit details:

```bash
cp config.example.yaml config.yaml
```

### Configuration Options (`config.yaml`)

```yaml
matrix:
  # Matrix Homeserver URL
  homeserver_url: "https://matrix.example.com"
  
  # Matrix Bot credentials
  user_id: "@radio:example.com"
  access_token: "syt_YOUR_MATRIX_ACCESS_TOKEN_HERE"
  
  # Bot display name / trigger mention
  bot_name: "radio"
  
  # Auto-join incoming room invites
  auto_join_invites: true

livekit:
  # LiveKit JWT service endpoint (MatrixRTC)
  jwt_service_url: "https://matrix.example.com/livekit/jwt/sfu/get"
  
  # LiveKit API credentials (optional if using JWT service)
  api_key: "YOUR_LIVEKIT_API_KEY"
  api_secret: "YOUR_LIVEKIT_API_SECRET"
  
  # LiveKit SFU WebSocket endpoint
  sfu_url: "wss://matrix.example.com/livekit/sfu"

proxy:
  # Optional: SOCKS5 or HTTP proxy URL (e.g. socks5://127.0.0.1:1080)
  # Can also be set via RADIO_PROXY_URL environment variable
  url: ""

audio:
  # Audio streaming parameters (48kHz Mono PCM)
  sample_rate: 48000
  channels: 1
  frame_duration_ms: 20
  idle_leave_timeout_sec: 15
```

---

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+**
- **FFmpeg** installed on the host system:
  - Debian/Ubuntu: `sudo apt install ffmpeg`
  - Arch Linux: `sudo pacman -S ffmpeg`
  - macOS: `brew install ffmpeg`

---

### Option 1: Running Locally with Python

```bash
# Clone the repository
git clone https://github.com/your-username/matrix-radio-bot.git
cd matrix-radio-bot

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the bot
python -m bot
```

---

### Option 2: Running with Docker

```bash
# Build the Docker image
docker build -t matrix-radio-bot .

# Run the container with your config mounted
docker run -d \
  --name matrix-radio-bot \
  --restart unless-stopped \
  -v $(pwd)/config.yaml:/app/config.yaml \
  matrix-radio-bot
```

---

### Option 3: Deploying on Kubernetes / k3s

A ready-to-use Kubernetes deployment manifest is included in `deploy/kubernetes.yaml`:

```bash
# Create ConfigMaps from your code and configuration
kubectl create configmap radio-bot-code --from-file=bot/
kubectl create configmap radio-bot-config --from-file=config.yaml --from-file=requirements.txt

# Apply deployment
kubectl apply -f deploy/kubernetes.yaml
```

---

## 📂 Project Architecture

```text
matrix-radio-bot/
├── bot/
│   ├── __init__.py
│   ├── __main__.py          # Application entrypoint & signal handling
│   ├── matrix_client.py     # Matrix API synchronization & event listener
│   ├── command_handler.py   # Command parser and query dispatcher
│   ├── queue_manager.py     # Isolated queue registry and playback workers
│   ├── song_resolver.py     # Multi-source metadata extractor (yt-dlp/Spotify)
│   ├── livekit_voice.py     # LiveKit WebRTC client & MSC3401 call publisher
│   └── audio_streamer.py    # Zero-disk yt-dlp to FFmpeg streaming pipe
├── deploy/
│   ├── kubernetes.yaml      # Generic Kubernetes deployment manifest
│   └── deploy.sh            # Automated deployment helper script
├── config.example.yaml      # Configuration template with placeholders
├── Dockerfile               # Production container image
├── requirements.txt         # Python package dependencies
├── LICENSE                  # MIT License
└── README.md
```

---

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/your-username/matrix-radio-bot/issues).

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
