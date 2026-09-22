"""
test_parallel_benchmark.py — Video Çekimi Hız Karşılaştırması

Senkron (eski) ve paralel (yeni) video URL çekimini aynı anime
üzerinde çalıştırarak süre farkını ölçer.

Kullanım:
    python test_parallel_benchmark.py
    python test_parallel_benchmark.py --slug naruto --bolum 3
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from turkanime_api import Anime as TurkanimeAnime

# ─────────────────────────────────────────────
# Ayarlar
# ─────────────────────────────────────────────

ALLOWED_PLAYERS = {"SIBNET", "MAIL", "FILEMOON", "MP4UPLOAD", "UQLOAD", "SENDVID", "BYS", "GDRIVE"}
TURKANIME_DELAY = 0.1   # Senkron versiyonda video başına bekleme
MAX_WORKERS     = 10     # Paralel versiyonda maksimum eş zamanlı istek

DEFAULT_SLUG         = "shingeki-no-kyojin"
DEFAULT_BOLUM_SAYISI = 3   # Kaç bölüm üzerinde test yapılacak


# ─────────────────────────────────────────────
# Senkron versiyon (eski davranış)
# ─────────────────────────────────────────────

def fetch_sequential(bolumler: list, n: int) -> list[dict]:
    """
    İlk n bölümün videolarını eskiden olduğu gibi sırayla çeker.
    Her video isteğinden sonra TURKANIME_DELAY kadar bekler.
    """
    results = []
    for bolum in bolumler[:n]:
        videos_out = []
        try:
            videos = bolum.videos
        except Exception as e:
            print(f"  [SEQ] Videolar alınamadı ({bolum.slug}): {e}")
            results.append({"title": bolum.title, "videos": videos_out})
            continue

        for video in videos:
            if video.player not in ALLOWED_PLAYERS:
                continue
            try:
                url = video.url
            except Exception:
                url = None
            if url:
                videos_out.append({"player": video.player, "url": url})
            time.sleep(TURKANIME_DELAY)

        results.append({"title": bolum.title, "videos": videos_out})
        print(f"  [SEQ] {bolum.title} -> {len(videos_out)} video")

    return results


# ─────────────────────────────────────────────
# Paralel versiyon (yeni davranış)
# ─────────────────────────────────────────────

def _fetch_one_url(video):
    """Tek bir video için URL çeker (thread-safe, sadece HTTP + decrypt)."""
    try:
        return (video, video.url)
    except Exception:
        return (video, None)


def fetch_parallel(bolumler: list, n: int) -> list[dict]:
    """
    İlk n bölümün videolarını ThreadPoolExecutor ile bölüm bazında paralel çeker.
    Bölümler arası sıra korunur, sadece bir bölüm içindeki videolar eş zamanlı çekilir.
    """
    results = []
    for bolum in bolumler[:n]:
        videos_out = []
        try:
            videos = bolum.videos
        except Exception as e:
            print(f"  [PAR] Videolar alınamadı ({bolum.slug}): {e}")
            results.append({"title": bolum.title, "videos": videos_out})
            continue

        allowed = [v for v in videos if v.player in ALLOWED_PLAYERS]

        fetched = []
        if allowed:
            workers = min(len(allowed), MAX_WORKERS)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_fetch_one_url, v): v for v in allowed}
                for future in as_completed(futures):
                    fetched.append(future.result())

        for video, url in fetched:
            if url:
                videos_out.append({"player": video.player, "url": url})

        results.append({"title": bolum.title, "videos": videos_out})
        print(f"  [PAR] {bolum.title} -> {len(videos_out)} video")

    return results


# ─────────────────────────────────────────────
# Benchmark runner
# ─────────────────────────────────────────────

def run_benchmark(slug: str, n: int):
    print(f"\n{'=' * 60}")
    print(f"  Benchmark: '{slug}' — Ilk {n} bolum")
    print(f"{'=' * 60}")

    # ── Test 1: Senkron ──
    # Kendi bagimsiz TurkanimeAnime nesnesiyle baslar (cache yok)
    print(f"\n{'─' * 40}")
    print("TEST 1 — Senkron (eski)")
    print(f"{'─' * 40}")
    print("Bolum listesi cekiliyor (senkron icin)...")
    try:
        anime_seq = TurkanimeAnime(slug, parse_fansubs=True)
        bolumler_seq = anime_seq.bolumler
    except Exception as e:
        print(f"HATA: {e}")
        return
    print(f"Toplam {len(bolumler_seq)} bolum bulundu. Ilk {n} bolum test edilecek.")
    t0 = time.perf_counter()
    seq_results = fetch_sequential(bolumler_seq, n)
    seq_elapsed = time.perf_counter() - t0
    seq_total_videos = sum(len(r["videos"]) for r in seq_results)
    del anime_seq, bolumler_seq  # Bellek temizle, cache tasinmasin

    # ── Test 2: Paralel ──
    # Yeni ve bagimsiz TurkanimeAnime nesnesiyle baslar (cache yok)
    print(f"\n{'─' * 40}")
    print("TEST 2 — Paralel (yeni)")
    print(f"{'─' * 40}")
    print("Bolum listesi cekiliyor (paralel icin)...")
    try:
        anime_par = TurkanimeAnime(slug, parse_fansubs=True)
        bolumler_par = anime_par.bolumler
    except Exception as e:
        print(f"HATA: {e}")
        return
    print(f"Toplam {len(bolumler_par)} bolum bulundu. Ilk {n} bolum test edilecek.")
    t1 = time.perf_counter()
    par_results = fetch_parallel(bolumler_par, n)
    par_elapsed = time.perf_counter() - t1
    par_total_videos = sum(len(r["videos"]) for r in par_results)
    del anime_par, bolumler_par

    # ── Sonuçlar ──
    speedup = seq_elapsed / par_elapsed if par_elapsed > 0 else float("inf")
    gain    = seq_elapsed - par_elapsed

    print(f"\n{'=' * 60}")
    print("  SONUCLAR")
    print(f"{'=' * 60}")
    print(f"  {'':30s} {'Senkron':>10s}   {'Paralel':>10s}")
    print(f"  {'─' * 55}")
    print(f"  {'Toplam sure':30s} {seq_elapsed:>9.2f}s   {par_elapsed:>9.2f}s")
    print(f"  {'Cekilen video sayisi':30s} {seq_total_videos:>10d}   {par_total_videos:>10d}")
    print(f"  {'─' * 55}")
    print(f"  Kazanim  : {gain:.2f}s daha hizli")
    print(f"  Hiz farki: {speedup:.2f}x")
    print(f"{'=' * 60}\n")

    if seq_total_videos != par_total_videos:
        print("UYARI: Iki versiyonun cektigi video sayisi farkli! Sonuclar tutarsiz olabilir.")
    else:
        print("OK: Her iki versiyon ayni sayida video cekti.")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Senkron vs Paralel video cekimi karsilastirmasi")
    parser.add_argument(
        "--slug",
        default=DEFAULT_SLUG,
        help=f"Test edilecek anime slug'i (varsayilan: {DEFAULT_SLUG})",
    )
    parser.add_argument(
        "--bolum",
        type=int,
        default=DEFAULT_BOLUM_SAYISI,
        help=f"Kac bolum test edilsin (varsayilan: {DEFAULT_BOLUM_SAYISI})",
    )
    args = parser.parse_args()
    run_benchmark(slug=args.slug, n=args.bolum)
