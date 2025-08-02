import os
import time
import threading
import queue
import numpy as np
import sounddevice as sd
import webrtcvad
import collections
from faster_whisper import WhisperModel
import edge_tts
import tempfile
import asyncio
import subprocess

# --- Config ---
SAMPLE_RATE = 16000
FRAME_DURATION = 30  # ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION / 1000)
FRAME_BYTES = FRAME_SIZE * 2  # 2 bytes per sample (int16)
MAX_SILENCE_DURATION = 0.5  # seconds
VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# --- Helper for debug logs ---

def debug_log(message: str):
    """Print debug messages only when DEBUG flag is enabled."""
    if DEBUG:
        print(f"[🪲 DEBUG] {message}")

# --- State Flags ---
speaking_event = threading.Event()
stop_event = threading.Event()

# --- TTS Queue ---
tts_queue = queue.Queue()

# --- Whisper Model ---
whisper_model = WhisperModel("base.en", device="cpu", compute_type="int8")

debug_log("Whisper model loaded: base.en (int8, CPU)")

# --- Audio Frame Buffer ---
class AudioBuffer:
    def __init__(self, max_seconds: int = 10):
        self.max_frames = int(SAMPLE_RATE / FRAME_SIZE * max_seconds)
        self.frames = collections.deque(maxlen=self.max_frames)

    def add_frame(self, frame_bytes: bytes):
        self.frames.append(frame_bytes)

    def get_audio(self) -> bytes:
        return b"".join(self.frames)

# --- WebRTC VAD ---
vad = webrtcvad.Vad(3)  # Most aggressive mode

debug_log("WebRTC VAD initialised in aggressive mode (3)")

# --- TTS Async ---
async def speak_async(text: str):
    if not text.strip():
        return
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmpfile:
            tmp_path = tmpfile.name

        communicate = edge_tts.Communicate(text, VOICE)
        await communicate.save(tmp_path)

        process = await asyncio.create_subprocess_exec(
            "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.wait()
        os.remove(tmp_path)

    except Exception as e:
        print(f"❌ TTS Error: {e}")

# --- TTS Worker ---

def tts_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def process_queue():
        while True:
            text = await loop.run_in_executor(None, tts_queue.get)
            if text is None:
                print("🛑 TTS worker shutting down.")
                tts_queue.task_done()
                break

            speaking_event.set()
            print(f"[🔊 speak()] Speaking: {text}", flush=True)

            try:
                await speak_async(text)
            except Exception as e:
                print(f"❌ TTS async error: {e}")
            finally:
                speaking_event.clear()
                tts_queue.task_done()

    try:
        loop.run_until_complete(process_queue())
    finally:
        loop.close()

# --- Speak Wrapper ---

def speak(text: str):
    if isinstance(text, str) and text.strip():
        debug_log(f"Queued TTS: '{text.strip()[:60]}'")
        tts_queue.put(text)

# --- Listen Function ---

def listen() -> str:
    print("🎤 Listening...")
    debug_log("Ready to listen (waiting for speech trigger)")

    ring_buffer = collections.deque(maxlen=30)
    pre_trigger_buffer = collections.deque(maxlen=int(1000 / FRAME_DURATION))  # ~1 sec buffer
    triggered = False
    audio_buffer = AudioBuffer()

    stream = sd.InputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SIZE, dtype='int16', channels=1)
    with stream:
        while not stop_event.is_set():
            if speaking_event.is_set():
                time.sleep(0.1)
                continue

            frame, _ = stream.read(FRAME_SIZE)
            frame_bytes = frame.tobytes()

            if len(frame_bytes) != FRAME_BYTES:
                if DEBUG:
                    debug_log(f"Invalid frame length: {len(frame_bytes)} (expected {FRAME_BYTES}) – skipping")
                continue

            is_speech = vad.is_speech(frame_bytes, SAMPLE_RATE)
            pre_trigger_buffer.append(frame_bytes)

            if not triggered:
                ring_buffer.append((frame_bytes, is_speech))
                num_voiced = len([f for f, speech in ring_buffer if speech])

                if num_voiced > 0.9 * ring_buffer.maxlen:
                    triggered = True
                    debug_log("Speech detected – recording started")

                    # prepend the 1‑second pre-buffer and ring buffer into audio_buffer
                    for f in pre_trigger_buffer:
                        audio_buffer.add_frame(f)
                    for f, _ in ring_buffer:
                        audio_buffer.add_frame(f)
                    ring_buffer.clear()
            else:
                audio_buffer.add_frame(frame_bytes)
                ring_buffer.append((frame_bytes, is_speech))
                num_unvoiced = len([f for f, speech in ring_buffer if not speech])

                if num_unvoiced > MAX_SILENCE_DURATION * 1000 / FRAME_DURATION:
                    debug_log("Silence threshold reached – recording stopped")
                    break

    audio_data = np.frombuffer(audio_buffer.get_audio(), dtype=np.int16).astype(np.float32) / 32768.0


    if len(audio_data) < 1000:
        debug_log("Audio too short (<1000 samples) – returning timeout")
        return "timeout"

    try:
        start_ts = time.time()
        segments, _ = whisper_model.transcribe(
            audio=audio_data,
            language="en",
            beam_size=1,
            temperature=0.0,
            vad_filter=True,
        )
        latency = time.time() - start_ts
        text = " ".join([seg.text for seg in segments]).strip().lower()
        debug_log(f"Transcription finished in {latency:.2f}s – Heard: '{text}'")
    except Exception as e:
        print(f"❌ Whisper error: {e}")
        return "timeout"

    return text if text else "timeout"

# --- Init & Stop ---

def init_voice_engine():
    threading.Thread(target=tts_worker, daemon=True).start()
    debug_log("Voice engine initialised (TTS worker started)")


def stop_listening():
    stop_event.set()
    tts_queue.put(None)
    debug_log("Stop event set – shutting down listening & TTS")
