"""
main.py — Orkestratör

Tüm modülleri sırasıyla çalıştırarak Serverless Anime API'yi günceller.

Akış:
    1. Türkanime arşivini tara (scraper.py)
    2. Her anime için Jikan API ile zenginleştir (jikan_api.py)
    3. JSON dosyalarını oluştur/güncelle (storage_manager.py)
    4. İndeks ve versiyon dosyalarını güncelle

Akıllı Güncelleme:
    - Türkanime'den gelen puan ve bolum_durumu her çalışmada güncellenir.
    - Jikan'dan daha önce başarıyla zenginleştirilmiş animeler tekrar sorgulanmaz.
    - Sadece yeni eklenen veya Jikan kısmı null olan animeler Jikan'a sorulur.
"""

import sys
from scraper import TurkanimeScraper
from jikan_api import JikanEnricher
from storage_manager import StorageManager


def main():
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

    for slug, tk_data in turkanime_data.items():
        islenen += 1
        progress = f"[{islenen}/{toplam}]"

        # Mevcut veriyi kontrol et
        existing = storage.load_anime_detail(slug)

        if existing is not None:
            # ── Mevcut anime: Türkanime verilerini HER ZAMAN güncelle ──
            # (puan ve bolum_durumu sık sık değişir)
            existing_jikan = existing.get("jikan")

            if existing_jikan is not None:
                # Jikan verisi zaten var → sadece Türkanime kısmını güncelle
                storage.save_anime_detail(slug, tk_data, existing_jikan)
                jikan_atlanan += 1
                turkanime_guncellenen += 1
                print(f"{progress} ⏭️  {tk_data['isim']} — Türkanime güncellendi, Jikan atlandı.")
                continue

            # Jikan verisi null → tekrar deneyelim
            print(f"{progress} 🔄 {tk_data['isim']} — Jikan verisi eksik, tekrar deneniyor...")

        else:
            # Yeni anime
            print(f"{progress} 🆕 {tk_data['isim']} — Yeni anime, Jikan sorgulanıyor...")

        # ── Jikan zenginleştirme ──
        jikan_data = jikan.enrich(tk_data["isim"])

        if jikan_data is not None:
            jikan_basarili += 1
            print(f"{progress} ✅ {tk_data['isim']} — Zenginleştirildi. (mal_id: {jikan_data['mal_id']})")
        else:
            jikan_basarisiz += 1
            print(f"{progress} ⚠️  {tk_data['isim']} — Jikan verisi alınamadı, null olarak kaydedildi.")

        storage.save_anime_detail(slug, tk_data, jikan_data)

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
    print(f"  Jikan Başarılı (yeni)   : {jikan_basarili}")
    print(f"  Jikan Atlandı (mevcut)  : {jikan_atlanan}")
    print(f"  Jikan Başarısız         : {jikan_basarisiz}")
    print(f"  İndeks Toplam Anime     : {total_in_index}")
    print(f"  Çıktı Klasörü           : api/")
    print("=" * 60)
    print("  ✅ İşlem başarıyla tamamlandı!")
    print("=" * 60)


if __name__ == "__main__":
    main()
