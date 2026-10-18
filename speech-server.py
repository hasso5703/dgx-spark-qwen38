#!/usr/bin/env python3
"""The speech lane's front: OpenAI-compatible audio routes, on the CPU.

  POST /v1/audio/transcriptions  multipart: file, language, response_format (json, text,
                                 verbose_json). Read by ffmpeg whatever its format, cut
                                 into pieces of 30 s at quiet moments, each one
                                 heard by the llama-server that runs Qwen3-ASR beside this
                                 process, with the model's own "language French<asr_text>"
                                 prefix taken off (verbose_json reports that language and
                                 the duration, as OpenAI does).
  POST /v1/audio/speech          JSON: input, voice, speed, response_format (wav, pcm),
                                 language. Spoken by Kokoro-82M in this process.
  GET  /v1/models, /v1/voices, /health

The qwen38-speech unit runs it (install-speech.sh), on loopback by default: there is no
key here, as for the image and video lanes, and neither the cockpit nor the proxy relays
it (programs on the box reach it, another machine through an SSH tunnel). Measured and designed
in docs/speech-lane.md: no GPU (the launcher hides it), the small cores only, one speech
request at a time, since two would share the same five cores and both slow down.
"""
import argparse
import array
import email.parser
import email.policy
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_UPLOAD = 100 * 1024 * 1024    # bytes of audio one transcription accepts
MAX_BODY = 64 * 1024              # bytes of JSON one speech request accepts
MAX_INPUT = 4096                  # characters one speech request accepts (OpenAI's limit)
ASR_TIMEOUT = 900                 # seconds for one piece, which takes about 15 here
RATE = 16000                      # what the recognizer hears: 16 kHz, mono, 16-bit
# One pass of the recognizer holds its context (-c), audio and text together: measured on
# 2026-10-05, 13 audio tokens and 4 to 5 of text per second of speech, so 8192 hold about
# seven minutes, and a longer recording sent whole came back cut, with a 200 (582 s: 353
# words of 1482). Long passes also lose words before that: on 291 s of French, pieces of
# 20, 30 and 60 s came back with a WER of 8.9 to 9.7%, as the clips heard one by one, and
# pieces of 120 s with 16.2%, the recognizer having skipped 61 words in the middle of one,
# after a sentence close to an earlier one; the whole 291 s in one pass, 9.0% at 0.78 of
# real time against 0.51 in pieces. So pieces of at most 30 s, each cut at the quietest
# tenth of a second of its last 7.5, so that no word is cut in two.
PIECE_S = 30
SEARCH_S = 7.5
# Qwen3-ASR writes the language it heard before the text, and llama-server passes it on.
PREFIX = re.compile(r"^\s*language\s+([^<\s]*)\s*<asr_text>")
# OpenAI's voice names, which clients such as Open WebUI send by default: the lane's own
# default voice answers them, as no Kokoro voice has those names.
OPENAI_VOICES = {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx",
                 "sage", "shimmer", "verse"}
# Kokoro's voice prefix gives its language (af_heart is American English, ff_siwis
# French); the espeak code is what kokoro-onnx phonemizes with. Each of these came back
# word for word through the recognizer on 2026-10-05 (docs/speech-lane.md). Two are left
# out on purpose, and a request for their voices is refused rather than spoken wrong:
# Japanese, which espeak reads out as "Chinese letter" for every kanji, and Mandarin, whose
# voices were trained on misaki's pinyin phonemes and get espeak's instead ("zho" for 这,
# no tones): over four voices and four sentences, 21% of the characters came back wrong
# through the 0.6B recognizer and 15% through the 1.7B, the same words for both (这, 说, 会议).
LANG_OF_PREFIX = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
                  "i": "it", "p": "pt-br"}
LANG_NAMES = {"en": "en-us", "english": "en-us", "en-us": "en-us", "en-gb": "en-gb",
              "fr": "fr-fr", "french": "fr-fr", "fr-fr": "fr-fr", "es": "es",
              "spanish": "es", "it": "it", "italian": "it", "pt": "pt-br",
              "portuguese": "pt-br", "pt-br": "pt-br", "hi": "hi", "hindi": "hi"}


def log(line):                     # one line per request; the tests silence it
    sys.stderr.write(line)


def split_prefix(text):
    """(the language the model says it heard, lower-cased as OpenAI writes it, or None for
    silence and for a text without the prefix; the text without the prefix)"""
    m = PREFIX.match(text)
    if not m:
        return None, text.strip()
    heard = m.group(1).lower()
    return (heard if heard not in ("", "none") else None), text[m.end():].strip()


def strip_prefix(text):
    return split_prefix(text)[1]


# The container demuxers an upload may be, and no others: an upload is a sound file, and some
# of ffmpeg's demuxers read the file as a playlist or manifest that names somewhere else. A
# crafted .audio in a DASH manifest made ffmpeg fetch an attacker's URL (a request from the box
# the sender chooses the target of), and an ffconcat one made it read a second local file
# (found in a cloud review, 2026-10-18, measured with ffmpeg 6.1.1). Pinning the input format
# to the sound containers refuses those: ffmpeg says "Format not on whitelist" before it opens
# anything. The protocol list is the second wall, for a demuxer on the list that still reaches
# out: the upload is read from a file and the samples written to the pipe, nothing else.
AUDIO_DEMUXERS = ("mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,ogg,wav,mp3,flac,aac,"
                  "aiff,au,caf,w64,amr,amrnb,amrwb,wv")


def decode_ffmpeg(data):
    """The upload as 16 kHz mono 16-bit samples, whatever its (sound) format: llama.cpp's own
    decoder reads WAV, MP3 and FLAC, not the m4a of a phone or the webm and ogg of a browser.
    From a file, not a pipe: an m4a whose index is at its end cannot be read from a pipe. Only
    the sound containers (AUDIO_DEMUXERS), and only the file and pipe protocols: an upload
    names its own bytes, never another file or a host."""
    with tempfile.NamedTemporaryFile(prefix="qwen38-speech-", suffix=".audio") as f:
        f.write(data)
        f.flush()
        p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                            "-protocol_whitelist", "file,pipe", "-format_whitelist", AUDIO_DEMUXERS,
                            "-i", f.name,
                            "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                           capture_output=True, timeout=300)
    if p.returncode != 0:
        lines = p.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(lines[-1] if lines else "not audio that ffmpeg can read")
    return p.stdout


def pieces(pcm, rate=RATE, piece_s=PIECE_S, search_s=SEARCH_S):
    """16-bit mono samples cut into pieces of at most piece_s seconds, each cut in the middle
    of the quietest tenth of a second of its last search_s seconds. Nothing is dropped."""
    samples = array.array("h", pcm[:len(pcm) // 2 * 2])
    step, limit = max(1, rate // 10), max(2 * max(1, rate // 10), int(piece_s * rate))
    search = min(int(search_s * rate), limit - step)   # every cut lands past the piece's start
    out, start = [], 0
    while len(samples) - start > limit:
        best, best_energy = start + limit, None
        for i in range(start + limit - search, start + limit - step + 1, step):
            frame = samples[i:i + step]
            energy = sum(x * x for x in frame)
            if best_energy is None or energy < best_energy:
                best, best_energy = i + step // 2, energy
        out.append(samples[start:best].tobytes())
        start = best
    out.append(samples[start:].tobytes())
    return out


# Chinese and Japanese write no space between words, so neither between two pieces.
NO_SPACE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def join_pieces(texts):
    out = ""
    for t in (t for t in texts if t):
        if out and not (NO_SPACE.match(out[-1]) and NO_SPACE.match(t[0])):
            out += " "
        out += t
    return out


def voices_by_language(voices):
    """{espeak language: [voices]} of the voices whose language this lane speaks."""
    out = {}
    for v in voices:
        if v[:1] in LANG_OF_PREFIX:
            out.setdefault(LANG_OF_PREFIX[v[:1]], []).append(v)
    return out


def wav_bytes(pcm16, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def parse_multipart(content_type, body):
    """{name: (filename or None, bytes)} of a multipart/form-data body."""
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    if not msg.is_multipart():
        raise ValueError("expected multipart/form-data")
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            out[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return out


def build_multipart(fields, filename, data):
    boundary = uuid.uuid4().hex
    chunks = []
    for k, v in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                  f'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class KokoroTTS:
    """Kokoro-82M through onnxruntime on the CPU, a fixed number of threads, one at a time."""

    def __init__(self, model, voices, threads):
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        session = ort.InferenceSession(model, opts, providers=["CPUExecutionProvider"])
        self.engine = Kokoro.from_session(session, voices)
        self.voices = sorted(self.engine.get_voices())
        self.lock = threading.Lock()

    def synth(self, text, voice, speed, lang):
        import numpy as np
        with self.lock:
            samples, rate = self.engine.create(text, voice=voice, speed=speed, lang=lang)
        return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes(), rate


def make_handler(tts, asr_url, default_voice, asr_name, tts_name="kokoro-82m", asr_ctx=8192,
                 decode=decode_ffmpeg, piece_s=PIECE_S, search_s=SEARCH_S):
    """decode: the upload to 16 kHz mono samples, or None to send it as it came, in one pass."""
    asr_lock = threading.Lock()
    by_language = voices_by_language(tts.voices)

    class Handler(BaseHTTPRequestHandler):
        server_version = "qwen38-speech"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):          # one line per request, below
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            log(f"{self.command} {self.path} {code} {len(body)}B {time.time() - self._t0:.2f}s\n")

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

        def _err(self, code, message, kind="invalid_request_error"):
            self._json(code, {"error": {"message": message, "type": kind}})

        def _body(self, cap):
            """The body, b"" without one, or None when it is over cap or its length is not a
            length (a negative one would read until the client hangs up)."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n > cap or n < 0:
                self.close_connection = True
                return None
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            self._t0 = time.time()
            path = self.path.split("?", 1)[0]
            if path == "/health":
                try:
                    with urllib.request.urlopen(asr_url + "/health", timeout=3) as r:
                        asr_ok = r.status == 200
                except (urllib.error.URLError, OSError):
                    asr_ok = False
                self._json(200 if asr_ok else 503, {"status": "ok" if asr_ok else "asr not ready",
                                                    "asr": asr_ok, "tts": True})
            elif path == "/v1/models":
                self._json(200, {"object": "list", "data": [
                    {"id": asr_name, "object": "model", "owned_by": "qwen38-speech"},
                    {"id": tts_name, "object": "model", "owned_by": "qwen38-speech"}]})
            elif path == "/v1/voices":
                # the voices this lane speaks with, so not the Japanese or Mandarin ones (above)
                self._json(200, {"default": default_voice, "languages": by_language,
                                 "voices": [v for vs in by_language.values() for v in vs]})
            else:
                self._err(404, f"no route {path}")

        def do_POST(self):
            self._t0 = time.time()
            path = self.path.split("?", 1)[0]
            if path == "/v1/audio/speech":
                return self._speech()
            if path == "/v1/audio/transcriptions":
                return self._transcribe()
            self._err(404, f"no route {path}")

        def _speech(self):
            raw = self._body(MAX_BODY)
            if raw is None:
                return self._err(413, f"the request body is over {MAX_BODY} bytes, or has no valid length")
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._err(400, "the body must be a JSON object")
            text = req.get("input")
            if not isinstance(text, str) or not text.strip():
                return self._err(400, "'input' must be a non-empty string")
            if len(text) > MAX_INPUT:
                return self._err(400, f"'input' is {len(text)} characters, over {MAX_INPUT}")
            voice = req.get("voice") or default_voice
            if not isinstance(voice, str):
                return self._err(400, "'voice' must be a string")
            if voice in OPENAI_VOICES:
                voice = default_voice
            if voice not in tts.voices:
                return self._err(400, f"no voice '{voice}': GET /v1/voices lists the {len(tts.voices)} there are")
            try:
                speed = float(req.get("speed", 1.0))
            except (TypeError, ValueError):
                return self._err(400, "'speed' must be a number")
            if not 0.5 <= speed <= 2.0:
                return self._err(400, "'speed' must be between 0.5 and 2.0")
            fmt = req.get("response_format") or "wav"
            if fmt not in ("wav", "pcm"):
                return self._err(400, f"'response_format' {fmt!r}: this lane returns wav or pcm")
            asked = str(req.get("language") or req.get("lang") or "").strip().lower()
            lang = LANG_NAMES.get(asked) if asked else LANG_OF_PREFIX.get(voice[:1])
            if not lang:
                return self._err(400, f"no language for {asked or voice!r}: pass 'language' as one of "
                                      + ", ".join(sorted(set(LANG_NAMES.values()))))
            try:
                pcm, rate = tts.synth(text, voice, speed, lang)
            except Exception as e:                    # the engine's own message, not a trace
                return self._err(500, f"synthesis failed: {type(e).__name__}: {e}", "server_error")
            if fmt == "pcm":
                return self._send(200, pcm, "audio/pcm", {"X-Sample-Rate": str(rate)})
            self._send(200, wav_bytes(pcm, rate), "audio/wav")

        def _transcribe(self):
            raw = self._body(MAX_UPLOAD)
            if raw is None:
                return self._err(413, f"the upload is over {MAX_UPLOAD // (1024 * 1024)} MB, or has no valid length")
            try:
                form = parse_multipart(self.headers.get("Content-Type", ""), raw)
            except ValueError as e:
                return self._err(400, str(e))
            if "file" not in form or not form["file"][1]:
                return self._err(400, "no 'file' in the form")
            filename, data = form["file"]
            language = form.get("language", (None, b""))[1].decode(errors="replace").strip()
            fmt = form.get("response_format", (None, b"json"))[1].decode(errors="replace").strip() or "json"
            if fmt not in ("json", "text", "verbose_json"):
                return self._err(400, f"'response_format' {fmt!r}: json, text or verbose_json")
            duration = None
            if decode is None:
                parts = [(filename or "audio", data, None)]
            else:
                try:
                    pcm = decode(data)
                except (ValueError, OSError, subprocess.TimeoutExpired) as e:
                    return self._err(400, f"could not read the audio: {e}")
                if len(pcm) < 2:
                    return self._err(400, "the file holds no audio")
                duration = len(pcm) / 2 / RATE
                parts = [(f"piece-{i}.wav", wav_bytes(p, RATE), len(p) / 2 / RATE)
                         for i, p in enumerate(pieces(pcm, RATE, piece_s, search_s))]
            texts, heard = [], None
            for name, audio, seconds in parts:
                # one piece at a time in the recognizer, and a short request can take its
                # turn between two pieces of a long one
                with asr_lock:
                    fields = {"response_format": "json"}
                    if language:
                        fields["language"] = language
                    if seconds is not None:
                        # a bound on the text, far above speech (4 to 5 tokens a second), so
                        # that a recognizer looping on noise stops there
                        fields["max_tokens"] = str(int(64 + 16 * seconds))
                    ctype, body = build_multipart(fields, name, audio)
                    upstream = urllib.request.Request(asr_url + "/v1/audio/transcriptions", data=body,
                                                      headers={"Content-Type": ctype}, method="POST")
                    try:
                        with urllib.request.urlopen(upstream, timeout=ASR_TIMEOUT) as r:
                            answer = json.loads(r.read() or b"{}")
                    except urllib.error.HTTPError as e:
                        detail = e.read()[:300].decode(errors="replace")
                        return self._err(502 if e.code >= 500 else 400, f"the recognizer refused it ({e.code}): {detail}")
                    except (urllib.error.URLError, OSError, ValueError) as e:
                        return self._err(502, f"the recognizer did not answer: {e}", "server_error")
                used = (answer.get("usage") or {}).get("total_tokens") or 0
                if used >= asr_ctx - 1 and decode is None:
                    return self._err(413, f"the recording is longer than the recognizer holds in one pass "
                                          f"({asr_ctx} tokens, about seven minutes of speech), and without "
                                          f"ffmpeg this lane cannot cut it into pieces: install ffmpeg, or "
                                          f"send it in parts")
                if used >= asr_ctx - 1:
                    return self._err(500, f"a piece of {seconds:.0f} s filled the recognizer's context "
                                          f"({asr_ctx} tokens), so its text would come back cut", "server_error")
                h, t = split_prefix(answer.get("text", ""))
                heard = heard or h
                texts.append(t)
            text = join_pieces(texts)
            if fmt == "text":
                return self._send(200, (text + "\n").encode(), "text/plain; charset=utf-8")
            out = {"text": text}
            if fmt == "verbose_json":
                out.update({"task": "transcribe", "language": heard or language or None})
                if duration is not None:
                    out["duration"] = round(duration, 2)
            self._json(200, out)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="speech lane front (see the module docstring)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=30024)
    ap.add_argument("--asr-url", default="http://127.0.0.1:30025")
    ap.add_argument("--asr-name", default="qwen3-asr")
    ap.add_argument("--asr-ctx", type=int, default=8192, help="the recognizer's -c")
    ap.add_argument("--model", required=True, help="kokoro-v1.0.onnx")
    ap.add_argument("--voices", required=True, help="voices-v1.0.bin")
    ap.add_argument("--threads", type=int, default=5)
    ap.add_argument("--default-voice", default="af_heart")
    a = ap.parse_args()
    tts = KokoroTTS(a.model, a.voices, a.threads)
    if a.default_voice[:1] not in LANG_OF_PREFIX or a.default_voice not in tts.voices:
        sys.exit(f"default voice {a.default_voice!r} is not one this lane speaks with: "
                 + " ".join(v for vs in voices_by_language(tts.voices).values() for v in vs))
    decode = decode_ffmpeg if shutil.which("ffmpeg") else None
    if decode is None:
        sys.stderr.write("ffmpeg is not installed: uploads go to the recognizer as they come, in one "
                         "pass (WAV, MP3 or FLAC, about seven minutes at most)\n")
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(tts, a.asr_url, a.default_voice, a.asr_name,
                                                             asr_ctx=a.asr_ctx, decode=decode))
    srv.daemon_threads = True
    sys.stderr.write(f"speech front on {a.host}:{a.port}, recognizer at {a.asr_url}, "
                     f"{len(tts.voices)} voices (default {a.default_voice})\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
