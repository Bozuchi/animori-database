"""
main.py — Orkestratör

Tüm modülleri sırasıyla çalıştırarak Serverless Anime API'yi günceller.

Akış:
    1. Türkanime arşivini tara (scraper.py)
    2. Her anime için Jikan API ile zenginleştir (mal_scraper.py)
    2.5. Bölüm & Video çekimi — delta güncelleme (episode_scraper.py)
    3. JSON dosyalarını oluştur/güncelle (storage_manager.py)
    4. İndeks ve versiyon dosyalarını güncelle

Akıllı Güncelleme:
    - Türkanime'den gelen bolum_durumu bilgisi değişmediği sürece dosya güncellenmez.
    - Jikan'dan daha önce başarıyla zenginleştirilmiş animeler tekrar sorgulanmaz.
    - Sadece yeni eklenen veya Jikan kısmı null olan animeler Jikan'a sorulur.
    - Bölüm/video taraması sadece bolum_durumu değiştiğinde yapılır.
    - Jikan verisi olmayan (jikan: null) animelerin bölümleri çekilmez.
"""

import sys
import os
import signal
from scraper import TurkanimeScraper
from mal_scraper import MalHtmlEnricher as JikanEnricher
from storage_manager import StorageManager
from episode_scraper import EpisodeScraper


# Graceful shutdown (Ctrl+C desteği)
_shutdown_requested = False


def _handle_shutdown(signum, frame):
    """Ctrl+C sinyalini yakalar ve güvenli kapatma bayrağını ayarlar."""
    global _shutdown_requested
    if _shutdown_requested:
        print("\n\n⚠️  İkinci Ctrl+C algılandı, zorla kapatılıyor...")
        os._exit(1)
    _shutdown_requested = True
    print("\n\n⏹️  Kapatma isteği alındı! Mevcut işlem tamamlandıktan sonra güvenli şekilde kapanacak...")


def main():
    global _shutdown_requested
    signal.signal(signal.SIGINT, _handle_shutdown)

    print("=" * 60)
    print("  🎌 Serverless Anime API — Veri Güncelleme")
    print("=" * 60)

    # ─────────────────────────────────────────────
    # ADIM 1: Türkanime Arşiv Taraması
    # ─────────────────────────────────────────────
    print("\n📡 ADIM 1: Türkanime arşivi taranıyor...")
    print("-" * 40)

    scraper = TurkanimeScraper()
    turkanime_data = scraper.scrape_all()

    if not turkanime_data:
        print("\n❌ Türkanime'den veri çekilemedi! İşlem sonlandırılıyor.")
        sys.exit(1)

    # ─────────────────────────────────────────────
    # ADIM 2: Jikan Zenginleştirme + Kaydetme
    # ─────────────────────────────────────────────
    print("\n🔍 ADIM 2: Jikan API ile zenginleştirme ve kaydetme...")
    print("-" * 40)

    jikan = JikanEnricher()
    storage = StorageManager()

    toplam = len(turkanime_data)
    islenen = 0
    jikan_atlanan = 0
    jikan_basarili = 0
    jikan_basarisiz = 0
    turkanime_guncellenen = 0
    ozet_cekilen = 0
    ozet_atlanan = 0
    bolum_taranan = 0
    bolum_atlanan = 0
    bolum_bos = 0
    bolum_jikan_null = 0

    for slug, tk_data in turkanime_data.items():
        if _shutdown_requested:
            print("\n🛑 Kullanıcı tarafından durduruldu (ADIM 2).")
            break

        islenen += 1
        progress = f"[{islenen}/{toplam}]"

        # Eğer anime manual_mappings içinde -1 olarak işaretlendiyse, komple atla
        if jikan.manual_mappings.get(slug) == -1:
            print(f"{progress} 🚫 {tk_data['isim']} — Kara listede, tamamen atlanıyor.")
            continue

        # Mevcut veriyi kontrol et
        existing = storage.load_anime_detail(slug)

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
                ozet_cekilen += 1
                if full_ozet:
                    print(f"{progress} 📝 {tk_data['isim']} — Tam özet çekildi.")
                else:
                    print(f"{progress} 📭 {tk_data['isim']} — Anime'de özet bilgisi yok.")
        else:
            # Yeni anime → hem özet hem Jikan gerekecek, anime objesini paylaş
            anime_obj = scraper.create_anime_object(slug)
            full_ozet = scraper.fetch_full_ozet(slug, anime_obj=anime_obj)
            tk_data["ozet"] = full_ozet  # "" bile olsa kaydet
            ozet_cekilen += 1
            if full_ozet:
                print(f"{progress} 📝 {tk_data['isim']} — Tam özet çekildi.")
            else:
                print(f"{progress} 📭 {tk_data['isim']} — Anime'de özet bilgisi yok.")

        # ── Jikan Zenginleştirme Kontrolü ──
        if existing is not None:
            # Mevcut anime: Türkanime verilerini GEREKTİĞİNDE güncelle
            existing_jikan = existing.get("jikan")

            if existing_jikan is not None:
                # Jikan verisi zaten var → Türkanime kısmını kontrol et
                existing_turkanime = existing.get("turkanime", {})
                
                # --- AKILLI DELTA KONTROLÜ ---
                # Bölüm durumu birebir aynıysa VE özet değişmemişse diske yazma!
                bolum_ayni = existing_turkanime.get("bolum_durumu") == tk_data.get("bolum_durumu")
                ozet_ayni = existing_turkanime.get("ozet") == tk_data.get("ozet")
                
                if bolum_ayni and ozet_ayni:
                    
                    jikan_atlanan += 1
                    # print(f"{progress} ⏭️  {tk_data['isim']} — Değişiklik yok, atlandı.")
                    continue

                # Bölüm durumu verisi değişmişse diske yaz
                storage.save_anime_detail(slug, tk_data, existing_jikan)
                jikan_atlanan += 1
                turkanime_guncellenen += 1
                print(f"{progress} 💾 {tk_data['isim']} — Bölüm durumu farklı, yeni hali kaydedildi.")
                continue

            # Jikan verisi null → tekrar deneyelim
            print(f"{progress} 🔄 {tk_data['isim']} — Jikan verisi eksik, tekrar deneniyor...")

        else:
            # Yeni anime
            print(f"{progress} 🆕 {tk_data['isim']} — Yeni anime, Jikan sorgulanıyor...")

        # ── MAL ID çekimi (paylaşılan anime objesi kullanılır) ──
        mal_id = scraper.fetch_mal_id(slug, anime_obj=anime_obj)

        # ── Jikan zenginleştirme ──
        jikan_data = jikan.enrich(mal_id=mal_id, slug=slug, name=tk_data["isim"])

        if jikan_data is not None:
            jikan_basarili += 1
            print(f"{progress} ✅ {tk_data['isim']} — Zenginleştirildi. (mal_id: {jikan_data['mal_id']})")
        else:
            jikan_basarisiz += 1
            print(f"{progress} ⚠️  {tk_data['isim']} — Jikan verisi alınamadı, null olarak kaydedildi.")

        storage.save_anime_detail(slug, tk_data, jikan_data)

    # Slug haritasını kaydet (ADIM 2 sonrası)
    storage.save_slug_map()

    # ─────────────────────────────────────────────
    # ADIM 2.5: Bölüm & Video Çekimi (Delta Güncelleme)
    # ─────────────────────────────────────────────
    if _shutdown_requested:
        print("\n⏭️  ADIM 2.5 atlandı (kapatma isteği).")
    else:
        print("\n🎬 ADIM 2.5: Bölüm ve video verileri çekiliyor...")
        print("-" * 40)

    ep_scraper = EpisodeScraper()
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
            bolum_jikan_null += 1
            continue

        mal_id = current_jikan.get("mal_id")

        # ── Delta kontrolü: bolum_durumu değişti mi? ──
        new_bolum_durumu = tk_data.get("bolum_durumu")
        old_bolum_durumu = current_data.get("turkanime", {}).get("bolum_durumu")
        existing_episodes = current_data.get("episodes", [])
        has_existing_episodes = current_data.get("episodes") is not None

        if old_bolum_durumu == new_bolum_durumu and has_existing_episodes:
            # Bölüm durumu aynı VE episodes zaten var → ATLA
            bolum_atlanan += 1
            continue

        # Değişmiş veya yeni → bölüm/video çek
        if has_existing_episodes:
            print(f"{progress} 🔄 {tk_data['isim']} — Bölüm durumu değişti, sadece eksik bölümler taranıyor...")
        else:
            print(f"{progress} 🎬 {tk_data['isim']} — Bölüm taraması başlatılıyor...")
            
        episodes = ep_scraper.scrape_episodes(slug, mal_id, jikan, existing_episodes, anime_name=tk_data['isim'])

        if episodes:
            storage.save_episodes(slug, episodes)
            bolum_taranan += 1
            print(f"{progress} ✅ {tk_data['isim']} — {len(episodes)} bölüm kaydedildi.")
        else:
            bolum_bos += 1
            print(f"{progress} 📭 {tk_data['isim']} — Bölüm bulunamadı.")

    # Slug haritasını kaydet (ADIM 2.5 sonrası)
    storage.save_slug_map()

    # ─────────────────────────────────────────────
    # ADIM 3: İndeks ve Versiyon Güncelleme
    # ─────────────────────────────────────────────
    print("\n📦 ADIM 3: İndeks ve versiyon dosyaları oluşturuluyor...")
    print("-" * 40)

    total_in_index = storage.build_index()
    storage.update_version(total_in_index)

    # ─────────────────────────────────────────────
    # ÖZET
    # ─────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  📊 İŞLEM ÖZETİ")
    print("=" * 60)
    print(f"  Türkanime Toplam        : {toplam}")
    print(f"  Türkanime Güncellenen   : {turkanime_guncellenen}")
    print(f"  Özet Çekilen (yeni/tam) : {ozet_cekilen}")
    print(f"  Özet Atlandı (mevcut)   : {ozet_atlanan}")
    print(f"  Jikan Başarılı (yeni)   : {jikan_basarili}")
    print(f"  Jikan Atlandı (mevcut)  : {jikan_atlanan}")
    print(f"  Jikan Başarısız         : {jikan_basarisiz}")
    print(f"  Bölüm Taranan           : {bolum_taranan}")
    print(f"  Bölüm Atlandı (delta)   : {bolum_atlanan}")
    print(f"  Bölüm Boş               : {bolum_bos}")
    print(f"  Bölüm Atlandı (no Jikan): {bolum_jikan_null}")
    print(f"  İndeks Toplam Anime     : {total_in_index}")
    print(f"  Çıktı Klasörü           : api/")
    print("=" * 60)
    print("  ✅ İşlem başarıyla tamamlandı!")
    print("=" * 60)


if __name__ == "__main__":
    main()
