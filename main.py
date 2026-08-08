"""
main.py — Orkestratör

Tüm modülleri sırasıyla çalıştırarak Serverless Anime API'yi günceller.

Akış:
    1. Türkanime arşivini tara (scraper.py)
    2. Her anime için Jikan API ile zenginleştir (mal_scraper.py)
       + AniList API ile banner image ve anilist_id çek (anilist_client.py)
    2.5. Bölüm & Video çekimi — delta güncelleme (episode_scraper.py)
    3. JSON dosyalarını oluştur/güncelle (storage_manager.py)
       - İndeks (animes.json), son eklenen bölümler (latest_episodes.json)
       - Hash tabanlı versiyon takibi (version.json) — sadece içerik değiştiğinde güncellenir

Akıllı Güncelleme:
    - Türkanime'den gelen bolum_durumu bilgisi değişmediği sürece dosya güncellenmez.
    - Jikan'dan daha önce başarıyla zenginleştirilmiş animeler tekrar sorgulanmaz.
    - Sadece yeni eklenen veya Jikan kısmı null olan animeler Jikan'a sorulur.
    - AniList verisi mevcut dosyada zaten varsa tekrar sorgulanmaz.
    - Bölüm/video taraması sadece bolum_durumu değiştiğinde yapılır.
    - Jikan verisi olmayan (jikan: null) animelerin bölümleri çekilmez.
"""

import sys
import os
import signal
from datetime import datetime

from logger import setup_logger
from scraper import TurkanimeScraper
from mal_scraper import MalHtmlEnricher as JikanEnricher
from anilist_client import AnilistClient
from storage_manager import StorageManager
from episode_scraper import EpisodeScraper
from discord_notify import send_report

logger = setup_logger("Main")


# Graceful shutdown (Ctrl+C desteği)
_shutdown_requested = False


def _handle_shutdown(signum, frame):
    """Ctrl+C sinyalini yakalar ve güvenli kapatma bayrağını ayarlar."""
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("İkinci Ctrl+C algılandı, zorla kapatılıyor...")
        os._exit(1)
    _shutdown_requested = True
    logger.warning("⏹️  Kapatma isteği alındı! Mevcut işlem tamamlandıktan sonra güvenli şekilde kapanacak...")


def _format_elapsed(elapsed) -> str:
    """timedelta nesnesini okunabilir formata çevirir."""
    total_seconds = int(elapsed.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}s")
    if minutes > 0:
        parts.append(f"{minutes}dk")
    parts.append(f"{seconds}sn")
    return " ".join(parts)


def main():
    global _shutdown_requested
    signal.signal(signal.SIGINT, _handle_shutdown)

    start_time = datetime.now()

    # İstatistik sayaçları (Discord bildirimi ve özet için)
    stats = {
        "toplam": 0,
        "kara_listede": 0,
        "turkanime_guncellenen": 0,
        "ozet_cekilen": 0,
        "jikan_basarili": 0,
        "jikan_atlanan": 0,
        "jikan_airing_guncellenen": 0,
        "jikan_airing_ayni": 0,
        "jikan_basarisiz": 0,
        "anilist_basarili": 0,
        "anilist_basarisiz": 0,
        "anilist_atlanan": 0,
        "bolum_taranan": 0,
        "bolum_atlanan": 0,
        "bolum_bos": 0,
        "bolum_jikan_null": 0,
        "index_toplam": 0,
    }

    storage = None
    crash_error = None

    try:
        logger.info("=" * 60)
        logger.info("  🎌 Serverless Anime API — Veri Güncelleme")
        logger.info("=" * 60)
        logger.info(f"Çalıştırma başladı: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

        # ─────────────────────────────────────────────
        # ADIM 1: Türkanime Arşiv Taraması
        # ─────────────────────────────────────────────
        logger.info("📡 ADIM 1: Türkanime arşivi taranıyor...")

        scraper = TurkanimeScraper()
        turkanime_data = scraper.scrape_all()

        if not turkanime_data:
            logger.error("Türkanime'den veri çekilemedi! İşlem sonlandırılıyor.")
            sys.exit(1)

        # ─────────────────────────────────────────────
        # ADIM 2: Jikan Zenginleştirme + Kaydetme
        # ─────────────────────────────────────────────
        logger.info("🔍 ADIM 2: Jikan API ile zenginleştirme ve kaydetme...")

        storage = StorageManager()
        jikan = JikanEnricher(metadata=storage.metadata)
        anilist = AnilistClient()

        toplam = len(turkanime_data)
        stats["toplam"] = toplam
        islenen = 0
        ozet_atlanan = 0
        bolum_durumu_degisenler = set()

        for slug, tk_data in turkanime_data.items():
            if _shutdown_requested:
                logger.warning("🛑 Kullanıcı tarafından durduruldu (ADIM 2).")
                break

            islenen += 1
            progress = f"[{islenen}/{toplam}]"

            # Eğer anime manual_mappings içinde -1 olarak işaretlendiyse, komple atla
            if jikan.manual_mappings.get(slug) == -1:
                logger.info(f"{progress} 🚫 {tk_data['isim']} — Kara listede, tamamen atlanıyor.")
                stats["kara_listede"] += 1
                continue

            # Mevcut veriyi kontrol et
            existing = storage.load_anime_detail(slug)

            # ── Bölüm Durumu Değişim Takibi ──
            if existing is None:
                bolum_durumu_degisenler.add(slug)
            else:
                old_bd = existing.get("turkanime", {}).get("bolum_durumu")
                new_bd = tk_data.get("bolum_durumu")
                if old_bd != new_bd:
                    bolum_durumu_degisenler.add(slug)

            # Shared anime object: aynı anime için tek HTTP isteği (optimizasyon)
            anime_obj = None
            will_need_jikan = (existing is None) or (existing.get("jikan") is None)

            # ── Tam Özet Kontrolü ──
            # None = henüz denenmedi → çek.  "" veya metin = daha önce çekilmiş → atla.
            if existing is not None:
                stored_ozet = existing.get("turkanime", {}).get("ozet")
                if stored_ozet is not None:
                    # Daha önce çekilmiş (boş veya dolu) → olduğu gibi kullan
                    tk_data["ozet"] = stored_ozet
                    ozet_atlanan += 1
                else:
                    # None → henüz denenmemiş, tam özet çek
                    # Hem özet hem Jikan gerekecekse, anime objesini paylaş
                    if will_need_jikan:
                        anime_obj = scraper.create_anime_object(slug)
                    full_ozet = scraper.fetch_full_ozet(slug, anime_obj=anime_obj)
                    tk_data["ozet"] = full_ozet  # "" bile olsa kaydet, tekrar denemesin
                    stats["ozet_cekilen"] += 1
                    if full_ozet:
                        logger.info(f"{progress} 📝 {tk_data['isim']} — Tam özet çekildi.")
                    else:
                        logger.info(f"{progress} 📭 {tk_data['isim']} — Anime'de özet bilgisi yok.")
            else:
                # Yeni anime → hem özet hem Jikan gerekecek, anime objesini paylaş
                anime_obj = scraper.create_anime_object(slug)
                full_ozet = scraper.fetch_full_ozet(slug, anime_obj=anime_obj)
                tk_data["ozet"] = full_ozet  # "" bile olsa kaydet
                stats["ozet_cekilen"] += 1
                if full_ozet:
                    logger.info(f"{progress} 📝 {tk_data['isim']} — Tam özet çekildi.")
                else:
                    logger.info(f"{progress} 📭 {tk_data['isim']} — Anime'de özet bilgisi yok.")

            # ── Jikan Zenginleştirme Kontrolü ──
            if existing is not None:
                # Mevcut anime: Türkanime verilerini GEREKTİĞİNDE güncelle
                existing_jikan = existing.get("jikan")

                if existing_jikan is not None:
                    # Jikan verisi zaten var → Türkanime kısmını kontrol et
                    existing_turkanime = existing.get("turkanime", {})
                    
                    # --- AKILLI DELTA KONTROLÜ ---
                    # Bölüm durumu birebir aynıysa diske yazma!
                    bolum_ayni = existing_turkanime.get("bolum_durumu") == tk_data.get("bolum_durumu")

                    # Currently Airing kontrolü: score/status/airing değişmiş olabilir
                    # Bölüm durumu değişsin veya değişmesin, her durumda MAL'dan güncel veriyi çek
                    fresh_jikan = None
                    if existing_jikan.get("status") == "Currently Airing":
                        _mal_id = existing_jikan.get("mal_id")
                        if _mal_id:
                            logger.info(f"{progress} 📡 {tk_data['isim']} — Hala yayında, güncel score/status/airing kontrol ediliyor...")
                            fresh_jikan = jikan.enrich(mal_id=_mal_id, name=tk_data["isim"])
                            if fresh_jikan:
                                old_score = existing_jikan.get("score")
                                old_status = existing_jikan.get("status")
                                old_airing = existing_jikan.get("airing")
                                new_score = fresh_jikan.get("score")
                                new_status = fresh_jikan.get("status")
                                new_airing = fresh_jikan.get("airing")

                                if old_score != new_score or old_status != new_status or old_airing != new_airing:
                                    changes = []
                                    if old_score != new_score:
                                        changes.append(f"score: {old_score} → {new_score}")
                                    if old_status != new_status:
                                        changes.append(f"status: {old_status} → {new_status}")
                                    if old_airing != new_airing:
                                        changes.append(f"airing: {old_airing} → {new_airing}")
                                    logger.info(
                                        f"{progress} 🔄 {tk_data['isim']} — "
                                        f"MAL verisi güncellendi ({', '.join(changes)})"
                                    )
                                    stats["jikan_airing_guncellenen"] += 1
                                else:
                                    stats["jikan_airing_ayni"] += 1

                    if bolum_ayni:
                        if fresh_jikan and fresh_jikan is not existing_jikan:
                            # Currently Airing verisi değişmiş → güncel jikan ile kaydet
                            old_score = existing_jikan.get("score")
                            old_status = existing_jikan.get("status")
                            old_airing = existing_jikan.get("airing")
                            new_score = fresh_jikan.get("score")
                            new_status = fresh_jikan.get("status")
                            new_airing = fresh_jikan.get("airing")
                            if old_score != new_score or old_status != new_status or old_airing != new_airing:
                                storage.save_anime_detail(slug, tk_data, fresh_jikan)
                        stats["jikan_atlanan"] += 1
                        continue

                    # Bölüm durumu verisi değişmişse diske yaz (güncel jikan varsa onu kullan)
                    jikan_to_save = fresh_jikan if fresh_jikan else existing_jikan
                    storage.save_anime_detail(slug, tk_data, jikan_to_save)
                    stats["jikan_atlanan"] += 1
                    stats["turkanime_guncellenen"] += 1
                    logger.info(f"{progress} 💾 {tk_data['isim']} — Bölüm durumu farklı, yeni hali kaydedildi.")
                    continue

                # Jikan verisi null → tekrar deneyelim
                logger.info(f"{progress} 🔄 {tk_data['isim']} — Jikan verisi eksik, tekrar deneniyor...")

            else:
                # Yeni anime
                logger.info(f"{progress} 🆕 {tk_data['isim']} — Yeni anime, Jikan sorgulanıyor...")

            # ── MAL ID çekimi (paylaşılan anime objesi kullanılır) ──
            mal_id = scraper.fetch_mal_id(slug, anime_obj=anime_obj)

            # ── Jikan zenginleştirme ──
            jikan_data = jikan.enrich(mal_id=mal_id, slug=slug, name=tk_data["isim"])

            # ── AniList zenginleştirme ──
            anilist_data = None

            if jikan_data is not None:
                stats["jikan_basarili"] += 1
                logger.info(f"{progress} ✅ {tk_data['isim']} — Zenginleştirildi. (mal_id: {jikan_data['mal_id']})")

                # AniList verisi çek (mevcut dosyada yoksa)
                existing_anilist = existing.get("anilist") if existing else None
                if existing_anilist is not None:
                    stats["anilist_atlanan"] += 1
                else:
                    anilist_data = anilist.fetch(jikan_data["mal_id"])
                    if anilist_data is not None:
                        stats["anilist_basarili"] += 1
                        logger.info(f"{progress} 🔗 {tk_data['isim']} — AniList verisi alındı. (anilist_id: {anilist_data['id']})")
                    else:
                        stats["anilist_basarisiz"] += 1
                        logger.warning(f"{progress} ⚠️  {tk_data['isim']} — AniList verisi alınamadı.")
            else:
                stats["jikan_basarisiz"] += 1
                logger.warning(f"{progress} {tk_data['isim']} — Jikan verisi alınamadı, null olarak kaydedildi.")

            storage.save_anime_detail(slug, tk_data, jikan_data, anilist_data)

        # Slug haritasını ve metadata'yı kaydet (ADIM 2 sonrası)
        storage.save_slug_map()
        storage.save_metadata()

        # ─────────────────────────────────────────────
        # ADIM 2.5: Bölüm & Video Çekimi (Delta Güncelleme)
        # ─────────────────────────────────────────────
        if _shutdown_requested:
            logger.warning("⏭️  ADIM 2.5 atlandı (kapatma isteği).")
        else:
            logger.info("🎬 ADIM 2.5: Bölüm ve video verileri çekiliyor...")

        ep_scraper = EpisodeScraper(metadata=storage.metadata)
        islenen_ep = 0

        for slug, tk_data in turkanime_data.items():
            if _shutdown_requested:
                break

            islenen_ep += 1
            progress = f"[{islenen_ep}/{toplam}]"

            # Güncel veriyi storage'dan oku
            current_data = storage.load_anime_detail(slug)
            if current_data is None:
                continue

            # ── Jikan null kontrolü ──
            # Jikan verisi olmayan animelerin bölümlerini çekmeye gerek yok
            current_jikan = current_data.get("jikan")
            if current_jikan is None:
                stats["bolum_jikan_null"] += 1
                continue

            mal_id = current_jikan.get("mal_id")

            # ── Delta kontrolü: bolum_durumu değişti mi? ──
            existing_episodes = current_data.get("episodes", [])
            has_existing_episodes = current_data.get("episodes") is not None
            bolum_durumu_degisti = slug in bolum_durumu_degisenler

            if not bolum_durumu_degisti and has_existing_episodes:
                # Bölüm durumu aynı VE episodes zaten var → ATLA
                stats["bolum_atlanan"] += 1
                continue

            # Değişmiş veya yeni → bölüm/video çek
            if has_existing_episodes:
                logger.info(f"{progress} 🔄 {tk_data['isim']} — Bölüm durumu değişti, sadece eksik bölümler taranıyor...")
            else:
                logger.info(f"{progress} 🎬 {tk_data['isim']} — Bölüm taraması başlatılıyor...")
                
            episodes = ep_scraper.scrape_episodes(slug, mal_id, jikan, existing_episodes, anime_name=tk_data['isim'])

            if episodes:
                storage.save_episodes(slug, episodes, mal_id=mal_id)
                stats["bolum_taranan"] += 1
                logger.info(f"{progress} ✅ {tk_data['isim']} — {len(episodes)} bölüm kaydedildi.")
            else:
                stats["bolum_bos"] += 1
                logger.info(f"{progress} 📭 {tk_data['isim']} — Bölüm bulunamadı.")

        # Slug haritasını ve güncel metadata'yı kaydet (ADIM 2.5 sonrası)
        storage.save_slug_map()
        storage.save_metadata()

        # ─────────────────────────────────────────────
        # ADIM 3: İndeks, Versiyon ve Latest Episodes Güncelleme
        # ─────────────────────────────────────────────
        logger.info("📦 ADIM 3: İndeks, versiyon ve latest episodes dosyaları oluşturuluyor...")

        total_in_index = storage.build_index()
        storage.update_latest_episodes()
        storage.update_versions()
        stats["index_toplam"] = total_in_index

    except KeyboardInterrupt:
        logger.warning("Kullanıcı tarafından durduruldu (KeyboardInterrupt).")
        crash_error = "KeyboardInterrupt — kullanıcı tarafından durduruldu."

    except Exception as e:
        logger.error(f"Beklenmedik hata: {e}", exc_info=True)
        crash_error = f"{type(e).__name__}: {e}"

    finally:
        # ── Kısmi ilerlemeyi her koşulda kaydet ──
        if storage is not None:
            try:
                storage.save_slug_map()
                storage.save_metadata()
                logger.info("Kısmi ilerleme kaydedildi (finally bloğu).")
            except Exception as e:
                logger.error(f"Kısmi ilerleme kaydedilemedi: {e}", exc_info=True)

        # ── Çalıştırma süresi ──
        elapsed = datetime.now() - start_time
        elapsed_str = _format_elapsed(elapsed)

        # ─────────────────────────────────────────────
        # ÖZET
        # ─────────────────────────────────────────────
        logger.info("=" * 60)
        logger.info("  📊 İŞLEM ÖZETİ")
        logger.info("=" * 60)
        logger.info(f"  Türkanime Toplam        : {stats['toplam']}")
        logger.info(f"  Kara Listede            : {stats['kara_listede']}")
        logger.info(f"  Türkanime Güncellenen   : {stats['turkanime_guncellenen']}")
        logger.info(f"  Özet Çekilen (yeni/tam) : {stats['ozet_cekilen']}")
        logger.info(f"  Jikan Başarılı (yeni)   : {stats['jikan_basarili']}")
        logger.info(f"  Jikan Atlandı (mevcut)  : {stats['jikan_atlanan']}")
        logger.info(f"  Jikan Airing Güncellenen: {stats['jikan_airing_guncellenen']}")
        logger.info(f"  Jikan Airing Aynı       : {stats['jikan_airing_ayni']}")
        logger.info(f"  Jikan Başarısız         : {stats['jikan_basarisiz']}")
        logger.info(f"  Anilist Başarılı        : {stats['anilist_basarili']}")
        logger.info(f"  Anilist Başarısız       : {stats['anilist_basarisiz']}")
        logger.info(f"  Anilist Atlandı         : {stats['anilist_atlanan']}")
        logger.info(f"  Bölüm Taranan           : {stats['bolum_taranan']}")
        logger.info(f"  Bölüm Atlandı (delta)   : {stats['bolum_atlanan']}")
        logger.info(f"  Bölüm Boş               : {stats['bolum_bos']}")
        logger.info(f"  Bölüm Atlandı (no Jikan): {stats['bolum_jikan_null']}")
        logger.info(f"  İndeks Toplam Anime     : {stats['index_toplam']}")
        logger.info(f"  Toplam Süre             : {elapsed_str}")
        logger.info(f"  Çıktı Klasörü           : api/")
        logger.info("=" * 60)

        if crash_error:
            logger.error(f"❌ İşlem hatayla sonlandı: {crash_error}")
        else:
            logger.info("✅ İşlem başarıyla tamamlandı!")
        
        logger.info("=" * 60)

        # ── Discord Bildirimi ──
        send_report(
            stats=stats,
            elapsed=elapsed_str,
            crash_error=crash_error,
        )


if __name__ == "__main__":
    main()
