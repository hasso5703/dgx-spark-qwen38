#!/usr/bin/env python3
"""The speech lane's routes, against a voice and a recognizer of the test's own.

speech-server.py is what the qwen38-speech unit serves: OpenAI-compatible audio routes in
front of Kokoro (in its process) and of Qwen3-ASR in llama-server (beside it). These run its
handler on a free port with a fake voice that returns known samples and a fake llama-server
that answers the way the real one does, the model's "language French<asr_text>" prefix
included, and records what it was sent. No model, no numpy, no network.
"""
import importlib.util
import io
import json
import math
import pathlib
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("speech_server", REPO / "speech-server.py")
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)
ss.log = lambda line: None


class FakeTTS:
    voices = ["af_heart", "bf_emma", "ff_siwis", "jf_alpha", "zf_xiaobei"]

    def __init__(self):
        self.calls = []

    def synth(self, text, voice, speed, lang):
        self.calls.append((text, voice, speed, lang))
        return b"\x01\x00\xff\x7f" * 2400, 24000           # 0.2 s of 16-bit samples


class FakeASR(BaseHTTPRequestHandler):
    seen = []
    answer = "language French<asr_text>Bonjour, tout le monde."   # or f(n) for the n-th request
    status = 200
    used = 300                                                   # usage.total_tokens

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200 if FakeASR.status == 200 else 503)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        FakeASR.seen.append(ss.parse_multipart(self.headers["Content-Type"], raw))
        text = FakeASR.answer(len(FakeASR.seen)) if callable(FakeASR.answer) else FakeASR.answer
        body = json.dumps({"text": text, "usage": {"total_tokens": FakeASR.used}}).encode()
        self.send_response(FakeASR.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def form(fields, filename="clip.wav", data=b"RIFF....WAVEfake"):
    ctype, body = ss.build_multipart(fields, filename, data)
    return ctype, body


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.asr = serve(FakeASR)
        cls.tts = FakeTTS()
        asr_url = f"http://127.0.0.1:{cls.asr.server_address[1]}"
        # decode=None: the upload goes to the recognizer as it came, as on a box without
        # ffmpeg; the pieces have a class of their own below
        cls.front = serve(ss.make_handler(cls.tts, asr_url, "af_heart", "qwen3-asr-0.6b", decode=None))
        cls.base = f"http://127.0.0.1:{cls.front.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.front, cls.asr):
            s.shutdown(); s.server_close()

    def setUp(self):
        FakeASR.seen.clear(); FakeASR.status = 200; FakeASR.used = 300
        FakeASR.answer = "language French<asr_text>Bonjour, tout le monde."
        self.tts.calls.clear()

    def call(self, path, data=None, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": ctype} if data is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def speak(self, **req):
        return self.call("/v1/audio/speech", json.dumps(req).encode())

    # ── speech ────────────────────────────────────────────────────────────────
    def test_speech_returns_a_wav_of_what_the_voice_made(self):
        code, headers, body = self.speak(input="Hello there", voice="af_heart")
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        with wave.open(io.BytesIO(body)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                             (1, 2, 24000, 4800))
        self.assertEqual(self.tts.calls, [("Hello there", "af_heart", 1.0, "en-us")])

    def test_pcm_is_the_raw_samples_with_their_rate(self):
        code, headers, body = self.speak(input="x", response_format="pcm")
        self.assertEqual(code, 200)
        self.assertEqual((headers["Content-Type"], headers["X-Sample-Rate"], len(body)), ("audio/pcm", "24000", 9600))

    def test_openai_voice_names_get_the_default_voice(self):
        for name in ("alloy", "nova", "shimmer"):
            with self.subTest(name):
                self.assertEqual(self.speak(input="hi", voice=name)[0], 200)
        self.assertTrue(all(c[1] == "af_heart" for c in self.tts.calls))

    def test_the_language_follows_the_voice_unless_asked(self):
        self.speak(input="Bonjour", voice="ff_siwis")
        self.speak(input="Hello", voice="bf_emma")
        self.speak(input="Hola", voice="af_heart", language="Spanish")
        self.assertEqual([c[3] for c in self.tts.calls], ["fr-fr", "en-gb", "es"])

    def test_a_voice_whose_language_is_not_served_is_refused_not_garbled(self):
        # Japanese (kanji read as "Chinese letter") and Mandarin (21% of characters heard
        # back wrong): docs/speech-lane.md
        for text, voice in (("こんにちは", "jf_alpha"), ("你好", "zf_xiaobei")):
            with self.subTest(voice=voice):
                code, _, body = self.speak(input=text, voice=voice)
                self.assertEqual(code, 400)
                self.assertIn("no language", json.loads(body)["error"]["message"])
        code, _, _ = self.speak(input="你好", voice="af_heart", language="zh")
        self.assertEqual(code, 400)
        self.assertEqual(self.tts.calls, [])

    def test_bad_requests_are_refused_before_the_voice_runs(self):
        cases = [({"input": ""}, "non-empty"), ({"input": "  "}, "non-empty"), ({"voice": "af_heart"}, "non-empty"),
                 ({"input": "x" * 4097}, "4097"), ({"input": "x", "voice": "nobody"}, "no voice"),
                 ({"input": "x", "speed": 3}, "between"), ({"input": "x", "speed": "fast"}, "number"),
                 ({"input": "x", "response_format": "mp3"}, "wav or pcm"),
                 # a voice that is not a string dropped the connection with no answer, its
                 # lookup raising in the handler (found in review, 2026-10-14)
                 ({"input": "x", "voice": ["af_heart"]}, "'voice' must be a string"),
                 ({"input": "x", "voice": {"id": "af_heart"}}, "'voice' must be a string")]
        for req, needle in cases:
            with self.subTest(req=req):
                code, _, body = self.speak(**req)
                self.assertEqual(code, 400)
                self.assertIn(needle, json.loads(body)["error"]["message"])
        self.assertEqual(self.call("/v1/audio/speech", b"[1, 2]")[0], 400)
        self.assertEqual(self.call("/v1/audio/speech", b"{not json")[0], 400)
        self.assertEqual(self.tts.calls, [])

    def test_a_body_over_its_cap_is_refused(self):
        code, _, _ = self.call("/v1/audio/speech", json.dumps({"input": "x" * (ss.MAX_BODY + 10)}).encode())
        self.assertEqual(code, 413)

    def test_a_length_that_is_not_one_is_refused_at_once(self):
        import socket
        port = self.front.server_address[1]
        for length in ("-1", "lots"):
            with self.subTest(length=length), socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                s.sendall(f"POST /v1/audio/speech HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {length}\r\n\r\n".encode())
                self.assertIn(b" 413 ", s.recv(4096).split(b"\r\n")[0] + b" ")

    # ── transcriptions ────────────────────────────────────────────────────────
    def test_the_models_prefix_is_taken_off(self):
        code, _, body = self.call("/v1/audio/transcriptions", *reversed(form({"language": "fr"})))
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body), {"text": "Bonjour, tout le monde."})
        sent = FakeASR.seen[0]
        self.assertEqual(sent["language"][1], b"fr")
        self.assertEqual(sent["response_format"][1], b"json")
        self.assertEqual(sent["file"], ("clip.wav", b"RIFF....WAVEfake"))

    def test_text_and_verbose_json_formats(self):
        code, headers, body = self.call("/v1/audio/transcriptions", *reversed(form({"response_format": "text"})))
        self.assertEqual((code, headers["Content-Type"], body), (200, "text/plain; charset=utf-8", "Bonjour, tout le monde.\n".encode()))
        code, _, body = self.call("/v1/audio/transcriptions", *reversed(form({"response_format": "verbose_json", "language": "fr"})))
        self.assertEqual(json.loads(body), {"text": "Bonjour, tout le monde.", "task": "transcribe", "language": "french"})

    def test_verbose_json_says_the_language_the_model_heard(self):
        FakeASR.answer = "language English<asr_text>Hello."
        code, _, body = self.call("/v1/audio/transcriptions", *reversed(form({"response_format": "verbose_json"})))
        self.assertEqual(json.loads(body)["language"], "english")
        # silence: the model heard no language, so the one asked for, or none at all
        FakeASR.answer = "language None<asr_text>"
        for fields, want in (({"response_format": "verbose_json", "language": "de"}, "de"),
                             ({"response_format": "verbose_json"}, None)):
            with self.subTest(fields=fields):
                code, _, body = self.call("/v1/audio/transcriptions", *reversed(form(fields)))
                self.assertEqual(json.loads(body), {"text": "", "task": "transcribe", "language": want})

    def test_a_text_without_the_prefix_is_left_as_it_is(self):
        FakeASR.answer = "Hello, language model."
        code, _, body = self.call("/v1/audio/transcriptions", *reversed(form({})))
        self.assertEqual(json.loads(body)["text"], "Hello, language model.")

    def test_no_file_and_bad_formats_are_refused(self):
        boundary = uuid.uuid4().hex
        no_file = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nfr\r\n--{boundary}--\r\n").encode()
        self.assertEqual(self.call("/v1/audio/transcriptions", no_file, f"multipart/form-data; boundary={boundary}")[0], 400)
        self.assertEqual(self.call("/v1/audio/transcriptions", b"{}", "application/json")[0], 400)
        self.assertEqual(self.call("/v1/audio/transcriptions", *reversed(form({"response_format": "srt"})))[0], 400)
        self.assertEqual(FakeASR.seen, [])

    def test_a_recognizer_that_fails_is_a_502(self):
        FakeASR.status = 500
        code, _, body = self.call("/v1/audio/transcriptions", *reversed(form({})))
        self.assertEqual(code, 502)
        self.assertIn("refused", json.loads(body)["error"]["message"])

    def test_without_ffmpeg_a_transcript_cut_by_the_context_is_refused_not_returned(self):
        # 582 s sent whole came back as 353 words of 1482, with a 200 (2026-10-05)
        FakeASR.used = 8191
        code, _, body = self.call("/v1/audio/transcriptions", *reversed(form({})))
        self.assertEqual(code, 413)
        self.assertIn("seven minutes", json.loads(body)["error"]["message"])

    # ── the rest ──────────────────────────────────────────────────────────────
    def test_health_follows_the_recognizer(self):
        self.assertEqual(self.call("/health")[0], 200)
        FakeASR.status = 503
        code, _, body = self.call("/health")
        self.assertEqual((code, json.loads(body)["asr"]), (503, False))

    def test_models_and_voices(self):
        ids = [m["id"] for m in json.loads(self.call("/v1/models")[2])["data"]]
        self.assertEqual(ids, ["qwen3-asr-0.6b", "kokoro-82m"])
        v = json.loads(self.call("/v1/voices")[2])
        # the Japanese and Mandarin voices are not ones this lane speaks with: not offered
        self.assertEqual((v["default"], v["voices"]), ("af_heart", ["af_heart", "bf_emma", "ff_siwis"]))
        self.assertEqual(v["languages"], {"en-us": ["af_heart"], "en-gb": ["bf_emma"], "fr-fr": ["ff_siwis"]})
        self.assertEqual(self.call("/nowhere")[0], 404)


def tone_wav(seconds, quiet=(), rate=16000):
    """A WAV of a loud tone, silent over the (start, end) spans in quiet."""
    samples = []
    for i in range(int(seconds * rate)):
        t = i / rate
        loud = not any(a <= t < b for a, b in quiet)
        samples.append(int(8000 * math.sin(2 * math.pi * 440 * t)) if loud else 0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(x.to_bytes(2, "little", signed=True) for x in samples))
    return buf.getvalue()


def wav_decoder(data):
    """What decode_ffmpeg returns, for a WAV that is already 16 kHz mono: its samples."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise ValueError(f"Invalid data found when processing input ({e})")


class Pieces(unittest.TestCase):
    """A recording longer than a piece is heard piece by piece, cut where it is quiet."""

    @classmethod
    def setUpClass(cls):
        cls.asr = serve(FakeASR)
        asr_url = f"http://127.0.0.1:{cls.asr.server_address[1]}"
        cls.decode = {"fn": wav_decoder}
        cls.front = serve(ss.make_handler(FakeTTS(), asr_url, "af_heart", "qwen3-asr-0.6b",
                                          decode=lambda data: cls.decode["fn"](data),
                                          piece_s=1.2, search_s=0.5))
        cls.base = f"http://127.0.0.1:{cls.front.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.front, cls.asr):
            s.shutdown(); s.server_close()

    def setUp(self):
        FakeASR.seen.clear(); FakeASR.status = 200; FakeASR.used = 300
        FakeASR.answer = lambda n: f"language English<asr_text>part {n}"
        self.decode["fn"] = wav_decoder

    def transcribe(self, data, **fields):
        ctype, body = form(fields, "talk.wav", data)
        req = urllib.request.Request(self.base + "/v1/audio/transcriptions", data=body,
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_long_recording_is_heard_in_pieces_cut_in_its_silences(self):
        quiet = [(0.9, 1.1), (1.9, 2.1), (2.9, 3.1)]
        # the language is the first piece's that heard one, not the last's
        FakeASR.answer = lambda n: ("language English" if n == 1 else "language None") + f"<asr_text>part {n}"
        code, out = self.transcribe(tone_wav(3.5, quiet), response_format="verbose_json", language="en")
        self.assertEqual(code, 200, out)
        self.assertEqual(out, {"text": "part 1 part 2 part 3 part 4", "task": "transcribe",
                               "language": "english", "duration": 3.5})
        lengths, at = [], 0.0
        for sent in FakeASR.seen:
            with wave.open(io.BytesIO(sent["file"][1])) as w:
                self.assertEqual((w.getframerate(), w.getnchannels()), (16000, 1))
                seconds = w.getnframes() / 16000
            lengths.append(seconds)
            at += seconds
            self.assertLessEqual(seconds, 1.2)
            self.assertEqual(sent["language"][1], b"en")
            self.assertEqual(sent["max_tokens"][1], str(int(64 + 16 * seconds)).encode())
            if at < 3.5 - 1e-6:                     # every cut but the end is in a silence
                self.assertTrue(any(a <= at <= b for a, b in quiet), (at, lengths))
        self.assertAlmostEqual(sum(lengths), 3.5)

    def test_a_short_recording_is_one_piece(self):
        code, out = self.transcribe(tone_wav(0.8))
        self.assertEqual((code, out), (200, {"text": "part 1"}))
        self.assertEqual(len(FakeASR.seen), 1)

    def test_audio_ffmpeg_cannot_read_is_a_400_that_says_why(self):
        code, out = self.transcribe(b"not audio at all")
        self.assertEqual(code, 400)
        self.assertIn("could not read the audio: Invalid data found", out["error"]["message"])
        self.assertEqual(FakeASR.seen, [])

    def test_a_file_with_no_audio_is_refused(self):
        self.decode["fn"] = lambda data: b""
        code, out = self.transcribe(b"RIFF")
        self.assertEqual(code, 400)
        self.assertIn("no audio", out["error"]["message"])

    def test_a_piece_that_fills_the_context_is_an_error_not_a_cut_text(self):
        FakeASR.used = 8191
        code, out = self.transcribe(tone_wav(0.5))
        self.assertEqual(code, 500)
        self.assertIn("filled the recognizer's context", out["error"]["message"])


class Helpers(unittest.TestCase):
    def test_strip_prefix(self):
        for raw, want in (("language French<asr_text>Bonjour.", "Bonjour."),
                          ("  language None<asr_text>", ""),
                          ("language Chinese<asr_text>你好", "你好"),
                          ("No prefix here.", "No prefix here."),
                          ("language French<asr_text>a language French<asr_text>b", "a language French<asr_text>b")):
            with self.subTest(raw):
                self.assertEqual(ss.strip_prefix(raw), want)

    def test_split_prefix_gives_the_heard_language(self):
        for raw, want in (("language French<asr_text>Bonjour.", ("french", "Bonjour.")),
                          ("language None<asr_text>", (None, "")),
                          ("language <asr_text>x", (None, "x")),
                          ("No prefix here.", (None, "No prefix here."))):
            with self.subTest(raw):
                self.assertEqual(ss.split_prefix(raw), want)

    def test_pieces_lose_nothing_and_respect_the_limit(self):
        # a deterministic noise with no silence at all: cuts fall anywhere, nothing is lost
        x, samples = 12345, []
        for _ in range(16000 * 5 + 7):
            x = (1103515245 * x + 12345) % 2**31
            samples.append(x % 20001 - 10000)
        pcm = b"".join(v.to_bytes(2, "little", signed=True) for v in samples)
        parts = ss.pieces(pcm, 16000, 1.5, 0.4)
        self.assertEqual(b"".join(parts), pcm)
        self.assertTrue(all(len(p) <= 2 * int(1.5 * 16000) for p in parts))
        self.assertEqual(len(parts), 4)
        self.assertEqual(ss.pieces(pcm[:3200], 16000, 1.5, 0.4), [pcm[:3200]])
        self.assertEqual(ss.pieces(b"", 16000, 1.5, 0.4), [b""])
        # a search as long as the piece, or longer, still moves forward and loses nothing
        for search in (1.5, 9.0):
            with self.subTest(search=search):
                parts = ss.pieces(pcm, 16000, 1.5, search)
                self.assertEqual(b"".join(parts), pcm)
                self.assertTrue(all(0 < len(p) <= 2 * int(1.5 * 16000) for p in parts))

    def test_join_pieces_spaces_words_not_chinese(self):
        self.assertEqual(ss.join_pieces(["Hello there.", "", "General Kenobi."]), "Hello there. General Kenobi.")
        self.assertEqual(ss.join_pieces(["你说的一切。", "都留在这台机器上"]), "你说的一切。都留在这台机器上")
        self.assertEqual(ss.join_pieces(["日本語です", "Okay"]), "日本語です Okay")
        self.assertEqual(ss.join_pieces([]), "")

    def test_decode_ffmpeg_reads_a_file_into_16k_mono_samples(self):
        calls = []

        def run(argv, **kw):
            path = argv[argv.index("-i") + 1]
            calls.append((argv, pathlib.Path(path).read_bytes()))
            return subprocess.CompletedProcess(argv, 0, b"\x01\x00\x02\x00", b"")

        real = ss.subprocess.run
        ss.subprocess.run = run
        try:
            self.assertEqual(ss.decode_ffmpeg(b"m4a bytes"), b"\x01\x00\x02\x00")
        finally:
            ss.subprocess.run = real
        argv, seen = calls[0]
        self.assertEqual(seen, b"m4a bytes")               # from a file, not a pipe
        self.assertEqual(argv[-8:], ["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
        self.assertIn("-nostdin", argv)
        # the input is pinned to the sound containers and to the file and pipe protocols, so a
        # crafted upload cannot make ffmpeg fetch a URL or read another file (found 2026-10-18).
        # Both options come before -i, where ffmpeg reads them as input options.
        i = argv.index("-i")
        self.assertEqual(argv[argv.index("-format_whitelist") + 1], ss.AUDIO_DEMUXERS)
        self.assertLess(argv.index("-format_whitelist"), i)
        self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "file,pipe")
        self.assertLess(argv.index("-protocol_whitelist"), i)
        # the dangerous demuxers are not on the list, the sound ones are
        demuxers = ss.AUDIO_DEMUXERS.split(",")
        self.assertEqual(set(demuxers) & {"hls", "dash", "concat", "sdp", "rtp", "rtsp", "image2", "ffconcat"}, set())
        self.assertLessEqual({"wav", "mp3", "flac", "ogg", "matroska,webm".split(",")[0], "mov"}, set(demuxers))

    def test_decode_ffmpeg_says_ffmpegs_last_word(self):
        real = ss.subprocess.run
        ss.subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, b"", b"[in] something\n/tmp/x.audio: Invalid data found when processing input\n")
        try:
            with self.assertRaisesRegex(ValueError, "Invalid data found"):
                ss.decode_ffmpeg(b"junk")
        finally:
            ss.subprocess.run = real

    def test_multipart_round_trip(self):
        ctype, body = ss.build_multipart({"language": "en"}, "a b.wav", b"\x00\x01binary\r\n--x")
        parsed = ss.parse_multipart(ctype, body)
        self.assertEqual(parsed["language"], (None, b"en"))
        self.assertEqual(parsed["file"], ("a b.wav", b"\x00\x01binary\r\n--x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
