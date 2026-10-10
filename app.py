"""Sinyal Emas XAUUSD - backend Flask.

Sumber data: halaman kuotasi publik https://www.mql5.com/en/quotes/metals/xauusd
Halaman itu hanya memuat cuplikan harga saat ini (tanpa riwayat candle), jadi keputusan
dihitung dari "bias harian" sederhana, bukan dari indikator seperti EMA/RSI/MACD.
"""
import html as html_lib
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, send_from_directory

BASE = Path(__file__).parent
app = Flask(__name__, static_folder=None)

URL = "https://www.mql5.com/en/quotes/metals/xauusd"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
CACHE_TTL = 15  # detik; jaga agar klik beruntun tidak membebani mql5.com
TIMEOUT = 10    # detik

_cache = (0.0, None)
_kunci = threading.Lock()  # satu permintaan ke mql5.com pada satu waktu

NUM = r"(-?\d[\d,]*(?:\.\d+)?)"


class SinyalError(Exception):
    """Galat yang sudah punya status HTTP dan pesan Indonesia."""

    def __init__(self, status, pesan):
        super().__init__(pesan)
        self.status, self.pesan = status, pesan


# ---------- Ambil & urai halaman ----------
def unduh_halaman():
    try:
        r = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        raise SinyalError(504, "mql5.com terlalu lama merespons. Coba lagi sebentar lagi.")
    except requests.exceptions.RequestException:
        raise SinyalError(503, "Tidak dapat terhubung ke mql5.com. Periksa koneksi internet komputer Anda.")
    if r.status_code in (403, 429):
        raise SinyalError(429, f"mql5.com menolak permintaan (kode {r.status_code}); kemungkinan membatasi akses otomatis. Tunggu beberapa menit lalu coba lagi.")
    if r.status_code != 200:
        raise SinyalError(502, f"mql5.com membalas dengan kode {r.status_code}.")
    return r.text


def angka(m):
    return float(m.group(1).replace(",", "")) if m else None


def urai(h):
    """Ambil angka-angka kuotasi dari teks halaman. Mengembalikan dict atau melempar SinyalError."""
    meta = " ".join(
        html_lib.unescape(c.group(1))
        for tag in re.findall(r"<meta\b[^>]*>", h, re.I) if "description" in tag.lower()
        for c in [re.search(r'content=["\']([^"\']*)', tag, re.I)] if c
    )
    badan = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", h)
    badan = html_lib.unescape(re.sub(r"<[^>]+>", " ", badan))
    teks = re.sub(r"\s+", " ", meta + " " + badan)

    cari = lambda pola: re.search(pola, teks, re.I)
    harga = angka(cari(r"priced at\s+" + NUM)) or angka(cari(r"\bBid\s+" + NUM))
    rentang = cari(r"trades within\s+" + NUM + r"\s*-\s*" + NUM) or \
        cari(r"low of\s+" + NUM + r"\s*(?:USD)?\s+and at a high of\s+" + NUM)
    if harga is None or rentang is None:
        raise SinyalError(502, "Format halaman mql5.com tidak dikenali (mungkin sudah berubah, atau halaman yang diterima bukan halaman kuotasi).")
    low, high = float(rentang.group(1).replace(",", "")), float(rentang.group(2).replace(",", ""))
    if not (0 < low <= high):
        raise SinyalError(502, "Data harga dari mql5.com tidak masuk akal.")

    prev = angka(cari(r"yesterday.s close was\s+" + NUM)) or angka(cari(r"Previous Close\s+" + NUM))
    harian = (harga - prev) / prev * 100 if prev else angka(cari(r"changed by\s+" + NUM + r"%\s+for today"))
    ask = cari(r"\bBid\s+" + NUM + r"\s+Ask\s+" + NUM)
    return {
        "price": harga, "low": low, "high": high, "prev_close": prev,
        "open": angka(cari(r"\bOpen\s+" + NUM)),
        "ask": float(ask.group(2).replace(",", "")) if ask else None,
        "change_pct": harian,
        "month_change_pct": angka(cari(r"Month Change\s+" + NUM + "%")),
    }


# ---------- Aturan keputusan (bias harian) ----------
def beri_suara(d):
    """Empat aturan sederhana pada data hari ini; tiap aturan memberi suara buy / sell / neutral."""
    suara = []
    tambah = lambda arah, teks: suara.append({"arah": arah, "teks": teks})

    p = d["change_pct"]
    if p is not None:
        if p > 0.15: tambah("buy", f"Naik {p:+.2f}% dari penutupan kemarin")
        elif p < -0.15: tambah("sell", f"Turun {p:+.2f}% dari penutupan kemarin")
        else: tambah("neutral", f"Perubahan harian kecil ({p:+.2f}%)")

    lebar = d["high"] - d["low"]
    if lebar > 0:
        pos = (d["price"] - d["low"]) / lebar * 100
        if pos >= 66: tambah("buy", f"Harga dekat batas atas kisaran hari ini ({pos:.0f}%)")
        elif pos <= 34: tambah("sell", f"Harga dekat batas bawah kisaran hari ini ({pos:.0f}%)")
        else: tambah("neutral", f"Harga di tengah kisaran hari ini ({pos:.0f}%)")

    if d["open"]:
        if d["price"] > d["open"]: tambah("buy", "Harga di atas harga pembukaan hari ini")
        elif d["price"] < d["open"]: tambah("sell", "Harga di bawah harga pembukaan hari ini")
        else: tambah("neutral", "Harga sama dengan pembukaan hari ini")

    m = d["month_change_pct"]
    if m is not None:
        if m > 0.5: tambah("buy", f"Tren sebulan naik ({m:+.2f}%)")
        elif m < -0.5: tambah("sell", f"Tren sebulan turun ({m:+.2f}%)")
        else: tambah("neutral", f"Tren sebulan datar ({m:+.2f}%)")
    return suara


def rekomendasi(suara):
    skor = sum({"buy": 1, "sell": -1}.get(s["arah"], 0) for s in suara)
    if skor >= 3: return "STRONG_BUY"
    if skor == 2: return "BUY"
    if skor <= -3: return "STRONG_SELL"
    if skor == -2: return "SELL"
    return "NEUTRAL"


def bangun_sinyal():
    d = urai(unduh_halaman())
    suara = beri_suara(d)
    if not suara:
        raise SinyalError(502, "Data dari mql5.com tidak cukup untuk menentukan keputusan.")
    n = lambda arah: sum(1 for s in suara if s["arah"] == arah)
    r = lambda v: None if v is None else round(v, 2)
    return {
        "symbol": "XAUUSD", "sumber": "mql5.com",
        "recommendation": rekomendasi(suara),
        "summary": {"buy": n("buy"), "sell": n("sell"), "neutral": n("neutral")},
        "alasan": suara,
        "price": r(d["price"]), "low": r(d["low"]), "high": r(d["high"]),
        "prev_close": r(d["prev_close"]), "open": r(d["open"]), "ask": r(d["ask"]),
        "change_pct": r(d["change_pct"]), "month_change_pct": r(d["month_change_pct"]),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def sinyal_dengan_cache():
    global _cache
    with _kunci:
        waktu, data = _cache
        if data and time.monotonic() - waktu < CACHE_TTL:
            return data
        data = bangun_sinyal()
        _cache = (time.monotonic(), data)
        return data


# ---------- Route ----------
@app.get("/api/signal")
def api_signal():
    try:
        resp = jsonify(sinyal_dengan_cache())
    except SinyalError as e:
        resp = jsonify(error=e.pesan)
        resp.status_code = e.status
    except Exception:
        app.logger.exception("Galat tak terduga")
        resp = jsonify(error="Terjadi kesalahan tak terduga di server.")
        resp.status_code = 500
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def beranda():
    return send_from_directory(BASE, "index.html")


# Berkas pendukung agar bisa dipasang di layar utama HP (hanya daftar ini yang disajikan)
BERKAS_APLIKASI = {"manifest.webmanifest", "ikon-192.png", "ikon-512.png"}


@app.get("/<nama>")
def berkas_aplikasi(nama):
    if nama not in BERKAS_APLIKASI:
        return jsonify(error="Tidak ditemukan."), 404
    return send_from_directory(BASE, nama)


def alamat_lan():
    """Alamat IP komputer di jaringan lokal (tidak mengirim data apa pun)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", 8000))
    if host == "0.0.0.0":
        ip = alamat_lan()
        print(f"\n>>> Buka dari HP (harus di Wi-Fi yang sama): http://{ip or '<IP-komputer>'}:{port}\n")
    app.run(host=host, port=port)
