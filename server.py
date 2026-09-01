#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================================
 EDGE TTS BACKEND SERVER — untuk aplikasi Android "Kitab Reader" (fitur Dengarkan /
 auto-baca "Surahan Lazim" & "Makna Gandul").

 Server ini dijalankan lokal di Termux (bisa di HP Android yang sama dengan aplikasi,
 atau di perangkat lain pada jaringan WiFi yang sama). Tugasnya sederhana:

     1. Menerima teks (Bahasa Indonesia / Arab) dari aplikasi Android lewat HTTP POST.
     2. Membersihkan teks dari simbol markdown (**, *, #, -, --- dst) supaya tidak
        ikut terbaca oleh mesin suara.
     3. Memecah teks panjang menjadi potongan kalimat yang wajar (chunking) supaya
        proses sintesis lebih cepat, lebih stabil, dan tahan gangguan jaringan.
     4. Mengubah tiap potongan menjadi audio MP3 memakai suara neural gratis
        Microsoft Edge Read Aloud (lewat pustaka open-source "edge-tts"), dengan
        mekanisme retry otomatis jika sekali gagal.
     5. Menggabungkan semua potongan audio menjadi satu file MP3 utuh dan
        mengirimkannya balik ke aplikasi Android sebagai respons HTTP.

 Alur kerja end-to-end (sesuai permintaan):
     [Tombol ▶ di app] -> [POST /tts berisi teks] -> [server sintesis via edge-tts]
     -> [server balas audio MP3] -> [app memutar audio itu langsung / real-time]

 Menjalankan:
     python server.py
 atau
     bash run.sh

 Lihat PANDUAN_TERMUX.md untuk langkah instalasi lengkap dari nol di Termux.
=====================================================================================
"""

import asyncio
import json
import logging
import re
import socket
import sys
import time
import uuid
from datetime import datetime
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

# -------------------------------------------------------------------------------------
# PENGECEKAN DEPENDENSI — beri pesan yang jelas & langsung ke solusinya kalau ada yang
# belum terpasang, alih-alih traceback Python yang membingungkan bagi pengguna awam.
# -------------------------------------------------------------------------------------
_MISSING = []

try:
    import edge_tts
except ImportError:
    _MISSING.append("edge-tts")

try:
    from flask import Flask, Response, jsonify, request
except ImportError:
    _MISSING.append("flask")

if _MISSING:
    print("=" * 78)
    print("[FATAL] Modul Python berikut belum terpasang:")
    for m in _MISSING:
        print(f"    - {m}")
    print()
    print("Jalankan dulu perintah ini di Termux:")
    print(f"    pip install {' '.join(_MISSING)}")
    print()
    print("Atau jalankan installer otomatis:  bash install.sh")
    print("=" * 78)
    sys.exit(1)

try:
    from flask_cors import CORS
    _HAS_CORS = True
except ImportError:
    _HAS_CORS = False

try:
    from waitress import serve as _waitress_serve
    _HAS_WAITRESS = True
except ImportError:
    _HAS_WAITRESS = False


# =====================================================================================
# KONFIGURASI
# =====================================================================================
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 5005,
    "api_key": "",
    "default_voice_id": "id-ID-ArdiNeural",
    "default_voice_ar": "ar-SA-HamedNeural",
    # Batas panjang teks per permintaan dibuat sangat longgar (± 300.000 karakter, setara
    # ratusan halaman) supaya penjelasan "Surahan Lazim" yang panjang TIDAK PERNAH ditolak
    # server. Batas ini hanya jaring pengaman terakhir (mencegah permintaan yang benar-benar
    # tidak wajar/salah kirim) — teks tetap dipecah otomatis jadi potongan kecil (lihat
    # "chunk_max_chars") sebelum dikirim ke mesin edge-tts, jadi seberapa pun panjang
    # teksnya tetap diproses dengan aman & stabil.
    "max_text_length": 300000,
    # Ukuran maksimal tiap potongan kalimat yang benar-benar dikirim ke mesin edge-tts.
    # Sengaja dibuat KECIL (bukan besar) — ini justru kunci ketahanan untuk teks yang
    # sangat panjang: potongan kecil selesai lebih cepat, lebih kecil kemungkinan gagal/
    # timeout, dan kalau satu potongan gagal, retry-nya murah (tidak perlu mengulang
    # seluruh naskah panjang dari awal).
    "chunk_max_chars": 700,
    "synthesis_retry": 4,
    "synthesis_retry_delay_seconds": 1.5,
    "request_rate_default": "+0%",
    "request_pitch_default": "+0Hz",
    "request_volume_default": "+0%",
    "log_level": "INFO",
}


def load_config() -> dict:
    """Muat config.json. Kalau belum ada, buat otomatis dengan nilai bawaan."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in loaded.items() if v is not None})
        return merged
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] config.json tidak terbaca ({exc}). Memakai konfigurasi bawaan.")
        return dict(DEFAULT_CONFIG)


CONFIG = load_config()

# =====================================================================================
# LOGGING — ke konsol (agar terlihat live di Termux) + ke file (untuk audit/troubleshoot)
# =====================================================================================
logger = logging.getLogger("edge_tts_backend")
logger.setLevel(getattr(logging, str(CONFIG.get("log_level", "INFO")).upper(), logging.INFO))
logger.propagate = False

if not logger.handlers:
    _console = logging.StreamHandler(sys.stdout)
    _console.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%H:%M:%S"))
    logger.addHandler(_console)

    _file = RotatingFileHandler(LOG_DIR / "server.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _file.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s"))
    logger.addHandler(_file)


# =====================================================================================
# PEMBERSIH TEKS & PEMOTONG KALIMAT (CHUNKER)
# =====================================================================================
_RE_HORIZONTAL_RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.MULTILINE)
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_RE_UNDERSCORE_EMPH = re.compile(r"_{1,2}(.+?)_{1,2}", re.DOTALL)
_RE_BULLET = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_RE_INLINE_CODE = re.compile(r"[`>~]")
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_RE_MULTI_NEWLINE = re.compile(r"\n{2,}")
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟;])\s+")


def clean_text_for_speech(raw: str) -> str:
    """Buang semua simbol markdown dari teks AI sebelum dikirim ke mesin TTS,
    supaya suara tidak ikut membaca 'bintang bintang' / 'pagar' / 'strip' dsb."""
    text = raw or ""
    text = _RE_HORIZONTAL_RULE.sub(" ", text)
    text = _RE_HEADING.sub("", text)
    text = _RE_BOLD.sub(lambda m: m.group(1), text)
    text = _RE_ITALIC.sub(lambda m: m.group(1), text)
    text = _RE_UNDERSCORE_EMPH.sub(lambda m: m.group(1), text)
    text = _RE_BULLET.sub("", text)
    text = _RE_INLINE_CODE.sub("", text)
    text = _RE_MULTI_NEWLINE.sub(". ", text)
    text = text.replace("\n", " ")
    text = _RE_MULTI_SPACE.sub(" ", text)
    return text.strip()


def split_into_chunks(text: str, max_chars: int) -> list:
    """Pecah teks bersih jadi daftar kalimat maksimal `max_chars` karakter tanpa
    memutus di tengah kalimat (kecuali kalimat itu sendiri lebih panjang dari batas)."""
    if not text.strip():
        return []

    sentences = [s.strip() for s in _RE_SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        sentences = [text.strip()]

    chunks = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for sentence in sentences:
        s = sentence
        while len(s) > max_chars:
            cut = s.rfind(",", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            part = s[: cut + 1].strip()
            if len(current) + len(part) + 1 > max_chars:
                flush()
            current = f"{current} {part}".strip()
            flush()
            s = s[cut + 1 :].strip()
        if not s:
            continue
        if len(current) + len(s) + 1 > max_chars:
            flush()
        current = f"{current} {s}".strip()

    flush()
    return chunks


# =====================================================================================
# ENGINE SINTESIS SUARA (edge-tts) DENGAN RETRY OTOMATIS
# =====================================================================================
class EdgeTtsSynthesisError(Exception):
    """Dilempar kalau sintesis suara gagal total setelah beberapa kali percobaan."""


async def _synthesize_chunk(chunk_text: str, voice: str, rate: str, pitch: str, volume: str,
                             retries: int, delay: float, request_id: str, chunk_no: int) -> bytes:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            communicator = edge_tts.Communicate(
                text=chunk_text, voice=voice, rate=rate, pitch=pitch, volume=volume
            )
            buffer = bytearray()
            async for message in communicator.stream():
                if message.get("type") == "audio" and message.get("data"):
                    buffer.extend(message["data"])
            if len(buffer) == 0:
                raise EdgeTtsSynthesisError(
                    "Server Edge TTS tidak mengembalikan data audio sama sekali "
                    "(kemungkinan nama voice tidak valid atau layanan Microsoft sedang bermasalah)."
                )
            return bytes(buffer)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                f"[{request_id}] percobaan {attempt}/{retries} gagal pada potongan #{chunk_no}: {exc}"
            )
            if attempt < retries:
                await asyncio.sleep(delay * attempt)
    raise EdgeTtsSynthesisError(
        f"Gagal mensintesis potongan #{chunk_no} setelah {retries}x percobaan. "
        f"Penyebab terakhir: {last_exc}"
    )


async def synthesize_full_async(text: str, voice: str, rate: str, pitch: str, volume: str,
                                 request_id: str) -> bytes:
    cleaned = clean_text_for_speech(text)
    if not cleaned:
        raise EdgeTtsSynthesisError(
            "Setelah dibersihkan dari simbol markdown, tidak ada teks tersisa untuk dibacakan."
        )

    chunks = split_into_chunks(cleaned, int(CONFIG["chunk_max_chars"]))
    if not chunks:
        raise EdgeTtsSynthesisError("Teks tidak bisa dipecah menjadi kalimat yang valid.")

    logger.info(
        f"[{request_id}] total {len(cleaned)} karakter teks bersih -> dipecah menjadi "
        f"{len(chunks)} potongan kalimat (voice={voice})"
    )

    audio_parts = []
    for i, chunk in enumerate(chunks, start=1):
        pct = int((i - 1) / len(chunks) * 100)
        logger.info(f"[{request_id}] mensintesis potongan {i}/{len(chunks)} ({len(chunk)} karakter) — {pct}%")
        part = await _synthesize_chunk(
            chunk, voice, rate, pitch, volume,
            int(CONFIG["synthesis_retry"]), float(CONFIG["synthesis_retry_delay_seconds"]),
            request_id, i,
        )
        audio_parts.append(part)

    logger.info(f"[{request_id}] seluruh {len(chunks)} potongan selesai disintesis (100%)")
    return b"".join(audio_parts)


# =====================================================================================
# DAFTAR SUARA (fallback statis + percobaan ambil daftar resmi terbaru dari Microsoft)
# =====================================================================================
_CURATED_VOICES = [
    {"id": "id-ID-ArdiNeural", "gender": "Male", "lang": "id-ID", "label": "Ardi (Pria) — Indonesia"},
    {"id": "id-ID-GadisNeural", "gender": "Female", "lang": "id-ID", "label": "Gadis (Wanita) — Indonesia"},
    {"id": "ar-SA-HamedNeural", "gender": "Male", "lang": "ar-SA", "label": "Hamed (Pria) — Arab Saudi"},
    {"id": "ar-SA-ZariyahNeural", "gender": "Female", "lang": "ar-SA", "label": "Zariyah (Wanita) — Arab Saudi"},
    {"id": "ar-EG-ShakirNeural", "gender": "Male", "lang": "ar-EG", "label": "Shakir (Pria) — Mesir"},
    {"id": "ar-EG-SalmaNeural", "gender": "Female", "lang": "ar-EG", "label": "Salma (Wanita) — Mesir"},
    {"id": "jv-ID-DimasNeural", "gender": "Male", "lang": "jv-ID", "label": "Dimas / Putra (Pria) — Jawa"},
    {"id": "jv-ID-SitiNeural", "gender": "Female", "lang": "jv-ID", "label": "Siti (Wanita) — Jawa"},
]


async def _fetch_all_voices_async():
    return await edge_tts.list_voices()


def get_voice_list():
    """Kembalikan daftar suara: coba ambil daftar resmi terbaru; kalau gagal
    (mis. tidak ada internet), pakai daftar kurasi statis supaya endpoint tetap jalan."""
    try:
        all_voices = asyncio.run(_fetch_all_voices_async())
        result = []
        for v in all_voices:
            short_name = v.get("ShortName", "")
            if short_name.startswith("id-ID-") or short_name.startswith("ar-") or short_name.startswith("jv-ID-"):
                result.append({
                    "id": short_name,
                    "gender": v.get("Gender", ""),
                    "lang": v.get("Locale", ""),
                    "label": f"{short_name} ({v.get('Gender', '')})",
                })
        if result:
            return sorted(result, key=lambda x: x["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Gagal mengambil daftar voice terbaru dari Microsoft ({exc}), memakai daftar kurasi.")
    return _CURATED_VOICES


# =====================================================================================
# UTILITAS JARINGAN — tampilkan alamat IP lokal supaya mudah dimasukkan ke aplikasi
# =====================================================================================
def get_local_ip_addresses():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        _, _, addr_list = socket.gethostbyname_ex(hostname)
        for ip in addr_list:
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


# =====================================================================================
# APLIKASI FLASK
# =====================================================================================
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

if _HAS_CORS:
    CORS(app)


def require_api_key(view_func):
    """Kalau CONFIG['api_key'] diisi, wajibkan header X-API-Key yang sesuai.
    Kalau dikosongkan (bawaan), endpoint terbuka untuk siapa saja di jaringan lokal."""
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        configured_key = str(CONFIG.get("api_key", "")).strip()
        if configured_key:
            provided = request.headers.get("X-API-Key", "")
            if provided != configured_key:
                return jsonify(error="API key tidak valid atau tidak disertakan (header X-API-Key)."), 401
        return view_func(*args, **kwargs)
    return wrapper


@app.route("/", methods=["GET"])
def index():
    html = f"""
    <html>
    <head><meta charset="utf-8"><title>Edge TTS Backend</title></head>
    <body style="font-family: sans-serif; background:#FAF7EE; padding:32px; color:#1A1C1A;">
        <h2 style="color:#0B6B57;">✅ Edge TTS Backend sedang berjalan</h2>
        <p>Server siap menerima permintaan dari aplikasi <b>Kitab Reader</b>.</p>
        <p>Endpoint yang tersedia:</p>
        <ul>
            <li><code>GET /health</code> — cek status server</li>
            <li><code>GET /voices</code> — daftar suara yang tersedia</li>
            <li><code>POST /tts</code> — sintesis teks jadi audio MP3</li>
        </ul>
        <p>Waktu server: {datetime.now().isoformat(timespec='seconds')}</p>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        status="ok",
        engine="edge-tts",
        server_time=datetime.now().isoformat(timespec="seconds"),
        default_voice_id=CONFIG["default_voice_id"],
        default_voice_ar=CONFIG["default_voice_ar"],
        api_key_required=bool(str(CONFIG.get("api_key", "")).strip()),
    )


@app.route("/voices", methods=["GET"])
@require_api_key
def voices():
    return jsonify(voices=get_voice_list())


@app.route("/tts", methods=["POST"])
@require_api_key
def tts_endpoint():
    request_id = uuid.uuid4().hex[:8]
    payload = request.get_json(silent=True) or {}

    text = str(payload.get("text", "")).strip()
    voice = str(payload.get("voice") or CONFIG["default_voice_id"]).strip()
    rate = str(payload.get("rate") or CONFIG["request_rate_default"])
    pitch = str(payload.get("pitch") or CONFIG["request_pitch_default"])
    volume = str(payload.get("volume") or CONFIG["request_volume_default"])

    if not text:
        return jsonify(error="Teks kosong, tidak ada yang bisa dibacakan."), 400

    max_len = int(CONFIG["max_text_length"])
    if len(text) > max_len:
        return jsonify(error=f"Teks terlalu panjang (maksimum {max_len} karakter, diterima {len(text)})."), 413

    logger.info(f"[{request_id}] permintaan TTS diterima | voice={voice} | panjang_teks={len(text)}")
    t0 = time.time()

    try:
        audio_bytes = asyncio.run(
            synthesize_full_async(text, voice, rate, pitch, volume, request_id)
        )
    except EdgeTtsSynthesisError as exc:
        logger.error(f"[{request_id}] GAGAL: {exc}")
        return jsonify(error=str(exc)), 502
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[{request_id}] kesalahan internal tak terduga")
        return jsonify(error=f"Kesalahan internal server: {exc}"), 500

    elapsed = time.time() - t0
    logger.info(f"[{request_id}] SUKSES | {len(audio_bytes)} bytes audio dihasilkan dalam {elapsed:.2f} detik")

    return Response(
        audio_bytes,
        mimetype="audio/mpeg",
        headers={
            "Content-Length": str(len(audio_bytes)),
            "X-Request-Id": request_id,
            "X-Synthesis-Time-Seconds": f"{elapsed:.2f}",
            "Cache-Control": "no-store",
        },
    )


@app.errorhandler(404)
def not_found(_e):
    return jsonify(error="Endpoint tidak ditemukan. Endpoint tersedia: /, /health, /voices, /tts"), 404


@app.errorhandler(405)
def method_not_allowed(_e):
    return jsonify(error="Metode HTTP tidak diizinkan untuk endpoint ini."), 405


@app.errorhandler(500)
def internal_error(_e):
    return jsonify(error="Kesalahan internal server yang tidak terduga."), 500


# =====================================================================================
# ENTRYPOINT
# =====================================================================================
def print_banner():
    host = CONFIG["host"]
    port = CONFIG["port"]
    local_ips = get_local_ip_addresses()

    print("=" * 78)
    print(" EDGE TTS BACKEND SERVER — Kitab Reader")
    print("=" * 78)
    print(f" Mesin server   : {'waitress (production)' if _HAS_WAITRESS else 'Flask dev server'}")
    print(f" Alamat bind    : {host}:{port}")
    print(f" API key        : {'AKTIF (wajib header X-API-Key)' if CONFIG.get('api_key') else 'nonaktif (bebas diakses di jaringan lokal)'}")
    print(f" Suara default  : {CONFIG['default_voice_id']}  &  {CONFIG['default_voice_ar']}")
    print("-" * 78)
    print(" >>> MASUKKAN SALAH SATU URL BERIKUT KE PENGATURAN APLIKASI ANDROID <<<")
    print()
    print(f"     Jika Termux berjalan DI HP YANG SAMA dengan aplikasi:")
    print(f"         http://127.0.0.1:{port}")
    print()
    if local_ips:
        print(f"     Jika Termux berjalan di PERANGKAT LAIN pada WiFi yang sama:")
        for ip in local_ips:
            print(f"         http://{ip}:{port}")
    else:
        print("     (Tidak terdeteksi alamat IP WiFi lokal — pastikan WiFi aktif jika")
        print("      ingin diakses dari perangkat lain.)")
    print()
    print("-" * 78)
    print(" Tekan CTRL+C untuk menghentikan server.")
    print("=" * 78)


def main():
    print_banner()
    host = CONFIG["host"]
    # Platform hosting gratis (Render, Railway, dll) memberi tahu port yang WAJIB
    # dipakai lewat environment variable PORT. Kalau ada, itu yang diprioritaskan;
    # kalau tidak ada (misalnya dijalankan manual di komputer), pakai config.json.
    import os as _os
    port = int(_os.environ.get("PORT", CONFIG["port"]))

    if _HAS_WAITRESS:
        _waitress_serve(app, host=host, port=port, threads=8)
    else:
        logger.warning(
            "Paket 'waitress' belum terpasang, memakai server bawaan Flask "
            "(cukup untuk pemakaian pribadi). Untuk performa lebih baik: pip install waitress"
        )
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
