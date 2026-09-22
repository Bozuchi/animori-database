"""
main.py — Anime Veritabanı Orkestratörü (Multi-Source Mimarisi)

Farklı anime kaynaklarını (AnimeciX, ve gelecekte Diziwatch, Anizm vb.)
ayrık klasör mimarisinde (api/sources/<kaynak>/) yönetir ve günceller.

Kullanım:
    python main.py                  # Varsayılan: Aktif kaynaklar için delta güncelleme yapar
    python main.py --full           # Tüm aktif kaynakların arşivini baştan/kaldığı yerden çeker
    python main.py --source animecix --delta
    python main.py --source animecix --full
    python main.py --source animecix --limit 10
    python main.py --build-index
"""

import os
import sys
import signal
import threading
import argparse
from datetime import datetime

from logger import setup_logger
import animecix_scraper
from animecix_scraper import AnimecixScraper
import anizm_scraper
from anizm_scraper import AnizmScraper
from discord_notify import send_report

logger = setup_logger("Main")

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    animecix_scraper._shutdown_requested = True
    anizm_scraper._shutdown_requested = True
    if _shutdown_requested:
        logger.warning("İkinci Ctrl+C algılandı, zorla kapatılıyor...")
        os._exit(1)
    _shutdown_requested = True
    logger.warning("⏹️  Kapatma isteği alındı! Mevcut işlem tamamlandıktan sonra güvenli şekilde kapanacak...")


def _format_elapsed(elapsed) -> str:
    total_seconds = int(elapsed.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}s")
    if minutes > 0:
        parts.append(f"{minutes}dk")
    parts.append(f"{seconds}sn")
    return " ".join(parts) or "0sn"


def run_animecix(args, stats: dict):
    """AnimeciX kaynağını çalıştırır."""
    logger.info("🎬 [Kaynak: AnimeciX] İşlem Başlatılıyor...")
    scraper = AnimecixScraper(workers=args.workers)

    if args.build_index:
        count = scraper.build_animes_index()
        scraper.save_metadata()
        scraper.update_latest_episodes()
        scraper.update_calendar()
        scraper.update_versions()
        stats["animecix_indeks_toplam"] = count
    elif args.full:
        scraper.scrape_all(limit=args.limit, force_catalog=args.force_catalog)
        stats["animecix_taranan"] = scraper.stats["successfully_scraped"]
        stats["animecix_dizi"] = scraper.stats["series_scraped"]
        stats["animecix_film"] = scraper.stats["movies_scraped"]
        stats["animecix_toplam_bolum"] = scraper.stats["total_episodes"]
        stats["animecix_toplam_video"] = scraper.stats["total_videos"]
        stats["animecix_fansub"] = len(scraper.fansubs)
    elif args.delta:
        scraper.delta_sync(pages=args.delta_pages)
        stats["animecix_delta_tamamlandi"] = "Evet"
    else:
        # Varsayılan: Türkanime tarzı akıllı senkronizasyon (smart_sync)
        scraper.smart_sync(force_catalog=args.force_catalog, limit=args.limit)
        stats["animecix_smart_sync"] = "Tamamlandı"
        stats["animecix_toplam_anime"] = len(os.listdir(scraper.anime_dir))


def run_anizm(args, stats: dict):
    """Anizm kaynağını çalıştırır."""
    logger.info("🎬 [Kaynak: Anizm] İşlem Başlatılıyor...")
    scraper = AnizmScraper(workers=args.workers)

    if args.build_index:
        count = scraper.build_animes_index()
        scraper.save_metadata()
        scraper.update_latest_episodes()
        scraper.update_calendar()
        scraper.update_versions()
        stats["anizm_indeks_toplam"] = count
    elif args.full:
        scraper.scrape_all(limit=args.limit, force_catalog=args.force_catalog)
        stats["anizm_taranan"] = scraper.stats["successfully_scraped"]
        stats["anizm_toplam_bolum"] = scraper.stats["total_episodes"]
        stats["anizm_toplam_video"] = scraper.stats["total_videos"]
        stats["anizm_fansub"] = len(scraper.fansubs)
    elif args.delta:
        scraper.delta_sync(pages=args.delta_pages)
        stats["anizm_delta_tamamlandi"] = "Evet"
    else:
        scraper.smart_sync(force_catalog=args.force_catalog, limit=args.limit)
        stats["anizm_smart_sync"] = "Tamamlandı"
        stats["anizm_toplam_anime"] = len(os.listdir(scraper.anime_dir))


def main():
    global _shutdown_requested
    signal.signal(signal.SIGINT, _handle_shutdown)

    parser = argparse.ArgumentParser(description="Anime Veritabanı Multi-Source Orkestratörü")
    parser.add_argument("--source", type=str, default="animecix", choices=["animecix", "anizm", "all"], help="Hedef kaynak (Varsayılan: animecix)")
    parser.add_argument("--full", action="store_true", help="Tüm kataloğu baştan/kaldığı yerden çeker.")
    parser.add_argument("--delta", action="store_true", help="Yalnızca son eklenen bölümleri tarar ve günceller.")
    parser.add_argument("--limit", type=int, default=None, help="İşlenecek maksimum başlık adedi (Test için).")
    parser.add_argument("--workers", type=int, default=2, help="Paralel iş parçacığı adedi (Varsayılan: 2 - Rate limit koruması).")
    parser.add_argument("--delta-pages", type=int, default=3, help="Delta modunda taranacak son bölüm sayfa adedi.")
    parser.add_argument("--force-catalog", action="store_true", help="Kataloğu önbellekten okumak yerine yeniden indirir.")
    parser.add_argument("--build-index", action="store_true", help="animes.json vitrinini yeniden kurar.")
    parser.add_argument("--max-hours", type=float, default=None, help="Maksimum çalışma süresi (Saat). Süre bitiminde güvenli çıkış tetiklenir.")

    args = parser.parse_args()

    start_time = datetime.now()
    stats = {}
    crash_error = None
    timer = None

    if args.max_hours and args.max_hours > 0:
        def _trigger_timeout():
            global _shutdown_requested
            logger.warning(f"⏰ Maksimum çalışma süresi ({args.max_hours} saat) doldu. Güvenli kapanış başlatılıyor...")
            _shutdown_requested = True
            animecix_scraper._shutdown_requested = True
            anizm_scraper._shutdown_requested = True

        timeout_sec = args.max_hours * 3600
        timer = threading.Timer(timeout_sec, _trigger_timeout)
        timer.daemon = True
        timer.start()
        logger.info(f"⏱️  Zamanlayıcı devrede: En fazla {args.max_hours} saat sonra güvenli çıkış yapılacak.")

    logger.info("=" * 65)
    logger.info(f"🎌 ANİME DATABASE ORKESTRATÖRÜ BAŞLADI ({start_time.strftime('%Y-%m-%d %H:%M:%S')})")
    logger.info(f"📌 Kaynak: {args.source.upper()} | Mod: {'FULL' if args.full else ('LIMIT ' + str(args.limit) if args.limit else 'DELTA')}")
    if args.max_hours:
        logger.info(f"⏱️  Maksimum Süre: {args.max_hours} saat")
    logger.info("=" * 65)

    try:
        if args.source in ("animecix", "all"):
            run_animecix(args, stats)

        if args.source in ("anizm", "all"):
            run_anizm(args, stats)

    except Exception as e:
        crash_error = f"{type(e).__name__}: {str(e)}"
        logger.error(f"💥 Kritik çökme hatası: {crash_error}", exc_info=True)

    finally:
        if timer:
            timer.cancel()

        elapsed = datetime.now() - start_time
        elapsed_str = _format_elapsed(elapsed)
        logger.info("=" * 65)
        logger.info(f"🏁 ORKESTRASYON TAMAMLANDI — Süre: {elapsed_str}")
        logger.info("=" * 65)

        # Discord bildirimi gönder
        try:
            send_report(stats=stats, elapsed=elapsed_str, crash_error=crash_error)
        except Exception as e:
            logger.warning(f"Discord bildirimi gönderilemedi: {e}")


if __name__ == "__main__":
    main()
