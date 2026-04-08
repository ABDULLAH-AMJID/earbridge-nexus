# 🎧 EarBridge NEXUS

**Stream your laptop's audio to any Bluetooth earbuds via your phone — over WiFi.**

Most Bluetooth earbuds only connect to one device at a time. EarBridge solves this by streaming your laptop audio over your local WiFi network to a web app on your phone, which plays it through your earbuds. You hear both your laptop and phone audio simultaneously — no special hardware required.

```
[ Laptop Audio ] ──WiFi──▶ [ Phone Web App ] ──Bluetooth──▶ [ 🎧 Earbuds ]
                                   ▲
                          [ Phone calls / music ]
```

---

## ✨ Features

- **Works with any Bluetooth earbuds** — uses A2DP music profile, not HFP call profile
- **Continues in background** — AudioWorklet engine runs in a dedicated audio thread the OS never suspends
- **Auto-mutes laptop speaker** while streaming, restores on exit
- **Phone plays audio?** Laptop auto-mutes, phone audio takes over — automatically
- **Adaptive jitter buffer** — measures real network jitter and tunes buffer depth live
- **RTT measurement** — live ping/pong with signal quality indicator
- **Real waveform visualizer** driven by actual audio data
- **Auto-reconnects** with exponential backoff on any connection drop
- **Multi-client** — multiple phones can connect simultaneously
- **Per-client isolated send queues** — a slow phone never affects others

---

## 🚀 Quick Start

### Requirements
- Windows 10/11 (laptop)
- Python 3.10+
- Both devices on the **same WiFi network**

### 1 — Install dependencies

```bash
pip install -r desktop/requirements.txt
```

### 2 — Run the desktop sender

```bash
python desktop/sender.py
```

You'll see your laptop's local IP printed:
```
║  ➤  192.168.1.42
```

### 3 — Open the receiver on your phone

1. Connect your Bluetooth earbuds to your **phone**
2. Transfer `mobile/receiver.html` to your phone (AirDrop, email, Google Drive)
3. Open it in Chrome or Safari
4. Enter the IP from Step 2 → tap **LINK**
5. Play audio on your laptop — it plays through your earbuds 🎉

---

## 📁 Project Structure

```
earbridge-nexus/
├── desktop/
│   ├── sender.py          ← Run on your laptop
│   └── requirements.txt
└── mobile/
    └── receiver.html      ← Open on your phone
```

---

## 🔧 Troubleshooting

| Problem | Solution |
|---------|----------|
| No audio / silence | Check that `sender.py` shows `✅ WASAPI loopback` in the output |
| Speaker not muting | Run `pip install pycaw comtypes` — PowerShell fallback is used automatically |
| Connection refused | Firewall may be blocking port 8765 — allow it in Windows Defender |
| Audio choppy | Both devices must be on 5GHz WiFi for best results |
| Earbuds in call quality | Disconnect and reconnect earbuds while receiver tab is open |

### Enable Windows Firewall rule (if needed)
```powershell
New-NetFirewallRule -DisplayName "EarBridge" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
```

---

## 🌐 Different Networks?

If your phone is on mobile data and your laptop is on WiFi, use [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```bash
# Install once
winget install Cloudflare.cloudflared

# Run alongside sender.py
cloudflared tunnel --url ws://localhost:8765
```

Cloudflare gives you a public `wss://` URL to enter in the receiver instead of a local IP.

---

## 🛠 How It Works

**Sender (`sender.py`)**
- Captures system audio via **WASAPI loopback** (speaker output only — zero mic bleed)
- Encodes PCM float32 → int16 (half bandwidth)
- Serves fixed 20ms frames over WebSocket
- Per-client isolated send queues — slow clients drop their own frames without affecting others
- All COM/pycaw speaker mute calls happen on the capture thread (avoids `CoInitialize` errors)

**Receiver (`receiver.html`)**
- **AudioWorklet** ring buffer — dedicated audio thread, never suspended by the OS
- **Adaptive jitter buffer** — RFC 3550 jitter estimation, auto-tunes from 80ms to 600ms
- **A2DP routing** — `AudioWorklet → GainNode → AnalyserNode → MediaStreamDestination → HTMLAudioElement` forces the OS to use A2DP (music), not HFP (call)
- **MediaSession API** — registers as a media app, maintains Now Playing
- **Silent WAV keepalive** — prevents BT earbuds from switching to HFP during silence
- **RTT measurement** — timestamped ping/pong, 20-sample rolling average
- **WakeLock API** — prevents the phone screen from sleeping

---

## 📋 Dependencies

```
pyaudiowpatch   — WASAPI loopback audio capture
websockets      — WebSocket server
numpy           — PCM encoding
pycaw           — Windows speaker mute control
comtypes        — COM interface for pycaw
```

---

## 📄 License

MIT License — free to use, modify, and distribute.

---

*Built with ❤️ — EarBridge NEXUS*
