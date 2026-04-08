"""
EarBridge Desktop Sender — NEXUS Edition
==========================================
Install: pip install pyaudiowpatch websockets numpy pycaw comtypes
Run:     python sender.py

Architecture:
  - Per-client isolated asyncio.Queue + dedicated send task
    → slow/laggy phone never blocks other clients
  - Capture thread owns ALL COM objects (pycaw)
    → eliminates CoInitialize errors completely
  - Accumulator capped at 80ms, wiped when no clients
    → client always gets current audio, never stale flood
  - Built-in RTT measurement via timestamped ping/pong
  - Adaptive flow control: drops oldest frame if client queue full
"""

import asyncio
import json
import logging
import platform
import socket
import struct
import subprocess
import threading
import time

import numpy as np
import websockets

IS_WINDOWS   = platform.system() == "Windows"
WS_PORT      = 8765
FRAME_SAMPLES = 960        # 20ms @ 48kHz
MAX_ACCUM_MS  = 80         # max accumulator depth before dropping oldest
CLIENT_QUEUE  = 40         # max queued frames per client before dropping

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("EarBridge·NEXUS")

# ── Shared state ───────────────────────────────────────────────────────────────
_pcm_accum      = bytearray()
_accum_lock     = threading.Lock()
_event_loop     = None
_async_notify   = None
_capture_ready  = threading.Event()
_mute_cmd       = threading.Event()   # set = mute, clear = restore
_mute_active    = False

# Per-client queues: { websocket: asyncio.Queue }
_client_queues: dict = {}
_clients_lock   = asyncio.Lock()

SAMPLE_RATE = 48000
CHANNELS    = 2
stats       = {"sent": 0, "clients": 0, "drops": 0}
_seq        = 0
# ──────────────────────────────────────────────────────────────────────────────


# ── COM-safe speaker mute — ALL pycaw calls inside capture thread ──────────────
def _com_init():
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass

def _get_vol():
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    sp  = AudioUtilities.GetSpeakers()
    dev = getattr(sp, "_dev", sp)
    return cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                POINTER(IAudioEndpointVolume))

def _do_mute():
    try:
        v = _get_vol()
        was   = bool(v.GetMute())
        saved = v.GetMasterVolumeLevelScalar()
        if not was:
            v.SetMute(1, None)
        log.info("🔇 Speaker muted")
        return v, was, saved
    except Exception as e:
        log.warning(f"pycaw mute: {e} — PowerShell fallback")
        _ps_mute(True)
        return None, False, 1.0

def _do_restore(v, was, saved):
    try:
        if v:
            v.SetMute(1 if was else 0, None)
            v.SetMasterVolumeLevelScalar(saved, None)
            log.info("🔊 Speaker restored")
            return
    except Exception as e:
        log.warning(f"pycaw restore: {e} — PowerShell fallback")
    _ps_mute(False)

def _ps_mute(mute: bool):
    val = "1" if mute else "0"
    ps = r"""
$ErrorActionPreference='SilentlyContinue'
Add-Type -TypeDefinition @"
using System;using System.Runtime.InteropServices;
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]class E{}
[ComImport,Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IEN{int _1();[PreserveSig]int Def(int f,int r,[MarshalAs(UnmanagedType.IUnknown)]out object d);}
[ComImport,Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ID{[PreserveSig]int Act([MarshalAs(UnmanagedType.LPStruct)]Guid g,int c,IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object i);}
[ComImport,Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IV{void a();void b();void c();void d();void e();void f();
[PreserveSig]int SV(float v,Guid g);void g();[PreserveSig]int GV(out float v);
[PreserveSig]int SM(bool m,Guid g);[PreserveSig]int GM(out bool m);}
"@ -Language CSharp 2>$null
$e=(New-Object E) -as [IEN];$e.Def(0,1,[ref]$d)|Out-Null
($d -as [ID]).Act([Guid]'5CDF2C82-841E-4546-9722-0CF74078229A',23,[IntPtr]::Zero,[ref]$v)|Out-Null
($v -as [IV]).SM([bool]$MV,[Guid]::Empty)|Out-Null""".replace("$MV", val)
    try:
        subprocess.run(["powershell","-WindowStyle","Hidden","-NonInteractive","-Command",ps],
                       capture_output=True, timeout=8)
    except Exception:
        pass


# ── Capture thread ─────────────────────────────────────────────────────────────
def _capture_thread():
    global SAMPLE_RATE, CHANNELS
    _com_init()

    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        log.error("pyaudiowpatch not installed. Run: pip install pyaudiowpatch")
        _capture_ready.set(); return

    pa = pyaudio.PyAudio()
    try:
        wi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        log.error("WASAPI unavailable."); pa.terminate(); _capture_ready.set(); return

    def_out  = wi["defaultOutputDevice"]
    out_info = pa.get_device_info_by_index(def_out)

    lb_idx, lb_info = def_out, out_info
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d.get("isLoopbackDevice") and out_info["name"] in d["name"]:
            lb_idx, lb_info = i, d; break

    SAMPLE_RATE = int(lb_info["defaultSampleRate"])
    CHANNELS    = min(2, int(lb_info.get("maxInputChannels") or 2))

    max_bytes   = int(MAX_ACCUM_MS / 1000 * SAMPLE_RATE * CHANNELS * 2)
    frame_bytes = FRAME_SAMPLES * CHANNELS * 2

    log.info(f"✅ WASAPI loopback: {lb_info['name']}  {SAMPLE_RATE}Hz  ch={CHANNELS}")

    def _cb(in_data, fc, ti, status):
        arr = np.frombuffer(in_data, dtype=np.float32).copy()
        np.clip(arr, -1.0, 1.0, out=arr)
        raw = (arr * 32767).astype(np.int16).tobytes()
        with _accum_lock:
            _pcm_accum.extend(raw)
            if len(_pcm_accum) > max_bytes:
                drop = ((len(_pcm_accum) - max_bytes) // frame_bytes + 1) * frame_bytes
                del _pcm_accum[:drop]
                stats["drops"] += 1
        lp = _event_loop
        if lp and not lp.is_closed() and _async_notify:
            try: lp.call_soon_threadsafe(_async_notify.set)
            except RuntimeError: pass
        return (None, pyaudio.paContinue)

    stream = pa.open(format=pyaudio.paFloat32, channels=CHANNELS, rate=SAMPLE_RATE,
                     frames_per_buffer=FRAME_SAMPLES, input=True,
                     input_device_index=lb_idx, stream_callback=_cb)

    _capture_ready.set()

    # Mute speakers — COM lives on this thread
    vol_obj, was_muted, saved_vol = _do_mute()
    muted_now = True

    stream.start_stream()
    try:
        while stream.is_active():
            # Phone audio signal: toggle mute state
            if _mute_cmd.is_set():
                _mute_cmd.clear()
                if muted_now:
                    _do_restore(vol_obj, was_muted, saved_vol)
                    muted_now = False
                else:
                    vol_obj, was_muted, saved_vol = _do_mute()
                    muted_now = True
            time.sleep(0.02)
    finally:
        stream.stop_stream(); stream.close(); pa.terminate()
        _do_restore(vol_obj, was_muted, saved_vol)


# ── Per-client send task ───────────────────────────────────────────────────────
async def _send_task(websocket, queue: asyncio.Queue):
    """
    Each client gets its own send task running independently.
    A slow client drops its own frames — never blocks others.
    """
    try:
        while True:
            packet = await queue.get()
            if packet is None:   # sentinel — shut down this client
                break
            await websocket.send(packet)
            stats["sent"] += 1
    except Exception:
        pass


# ── Broadcast loop ─────────────────────────────────────────────────────────────
async def _broadcast():
    global _event_loop, _async_notify, _seq

    _event_loop   = asyncio.get_running_loop()
    _async_notify = asyncio.Event()

    threading.Thread(target=_capture_thread, daemon=True).start()
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: _capture_ready.wait(6))

    log.info(f"▶  {SAMPLE_RATE}Hz  {'Stereo' if CHANNELS==2 else 'Mono'}  "
             f"frame={FRAME_SAMPLES}smp/{FRAME_SAMPLES/SAMPLE_RATE*1000:.0f}ms")

    frame_bytes = FRAME_SAMPLES * CHANNELS * 2

    while True:
        await _async_notify.wait()
        _async_notify.clear()

        async with _clients_lock:
            queues = list(_client_queues.values())

        if not queues:
            with _accum_lock:
                _pcm_accum.clear()   # don't let stale data pile up
            continue

        frame = None
        with _accum_lock:
            if len(_pcm_accum) >= frame_bytes:
                frame = bytes(_pcm_accum[:frame_bytes])
                del _pcm_accum[:frame_bytes]

        if not frame:
            continue

        # [4B seq] + [4B server_ts_ms] + [PCM]
        ts     = int(time.time() * 1000) & 0xFFFFFFFF
        packet = struct.pack(">II", _seq, ts) + frame
        _seq   = (_seq + 1) & 0xFFFFFFFF

        for q in queues:
            if q.full():
                try: q.get_nowait()   # drop oldest
                except asyncio.QueueEmpty: pass
            try: q.put_nowait(packet)
            except asyncio.QueueFull: pass


# ── WebSocket handler ──────────────────────────────────────────────────────────
async def _ws_handler(websocket):
    addr  = websocket.remote_address
    queue = asyncio.Queue(maxsize=CLIENT_QUEUE)

    async with _clients_lock:
        _client_queues[websocket] = queue
        stats["clients"] = len(_client_queues)

    log.info(f"📱 Connected: {addr[0]}:{addr[1]}  "
             f"(total={stats['clients']})")

    await asyncio.get_event_loop().run_in_executor(
        None, lambda: _capture_ready.wait(4))

    meta = json.dumps({
        "type":         "meta",
        "sampleRate":   SAMPLE_RATE,
        "channels":     CHANNELS,
        "frameSamples": FRAME_SAMPLES,
        "bitDepth":     16,
        "version":      "nexus",
    })

    send_task = asyncio.ensure_future(_send_task(websocket, queue))

    try:
        await websocket.send(meta)
        async for msg in websocket:
            if not isinstance(msg, str): continue
            try:
                d = json.loads(msg)
                t = d.get("type")

                if t == "ping":
                    # Echo back client timestamp for RTT measurement
                    await websocket.send(json.dumps({
                        "type":       "pong",
                        "client_ts":  d.get("ts", 0),
                        "server_ts":  int(time.time() * 1000),
                    }))

                elif t == "phone_audio":
                    # Signal capture thread to toggle mute — never call COM here
                    log.info(f"📞 phone_audio playing={d.get('playing')}")
                    _mute_cmd.set()

            except Exception:
                pass

    except websockets.ConnectionClosed:
        pass
    finally:
        queue.put_nowait(None)          # stop send task
        send_task.cancel()
        async with _clients_lock:
            _client_queues.pop(websocket, None)
            stats["clients"] = len(_client_queues)
        log.info(f"📵 Disconnected: {addr[0]}:{addr[1]}")


async def _status():
    while True:
        await asyncio.sleep(15)
        with _accum_lock:
            ms = len(_pcm_accum) / max(CHANNELS * 2 * SAMPLE_RATE, 1) * 1000
        log.info(f"◈ clients={stats['clients']}  sent={stats['sent']}"
                 f"  drops={stats['drops']}  accum={ms:.0f}ms")


def _get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "127.0.0.1"


async def main():
    ip = _get_ip()
    w  = 56
    print("╔" + "═"*w + "╗")
    print("║" + "  EarBridge NEXUS — Desktop Sender".center(w) + "║")
    print("╠" + "═"*w + "╣")
    print(f"║  WebSocket  →  ws://{ip}:{WS_PORT}".ljust(w+1) + "║")
    print(f"║  Enter this IP in the receiver on your phone".ljust(w+1) + "║")
    print(f"║  ➤  {ip}".ljust(w+1) + "║")
    print("╚" + "═"*w + "╝\n")

    async with websockets.serve(
        _ws_handler, "0.0.0.0", WS_PORT,
        ping_interval=None, ping_timeout=None, max_size=None,
    ):
        log.info("NEXUS server online — waiting for receiver…")
        await asyncio.gather(_broadcast(), _status())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[EarBridge NEXUS] Stopped.")
