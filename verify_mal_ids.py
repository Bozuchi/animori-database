"""
verify_mal_ids.py — MAL ID Doğrulama Scripti

Mevcut api/anime/*.json dosyalarındaki MAL ID'leri,
Türkanime'nin "Dış Bağlantılar" sekmesinden çekilen MAL ID'lerle karşılaştırır.

Çıktı:
    ✅ Eşleşen (doğru)
    ❌ Uyuşmayan (eski mal_id ≠ yeni mal_id)
    ⚠️  Türkanime'de MAL linki yok (fetch_mal_id → None)

Sonuçlar: verify_results.json dosyasına kaydedilir.
"""

import os
import json
import time
from scraper import TurkanimeScraper

API_DIR = os.path.join("api", "anime")


def main():
    print("=" * 60)
    print("  🔍 MAL ID Doğrulama — Yeni Yöntem ile Karşılaştırma")
    print("=" * 60)

    scraper = TurkanimeScraper()

    # Tüm anime dosyalarını yükle
    files = sorted(f for f in os.listdir(API_DIR) if f.endswith(".json"))
    toplam = len(files)
    print(f"\n📁 {toplam} anime dosyası bulundu.\n")

    eslesen = 0
    uyusmayan = []
    mal_yok = []
    jikan_null = 0
    hata = 0

    for idx, filename in enumerate(files, 1):
        filepath = os.path.join(API_DIR, filename)

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[{idx}/{toplam}] ⚠️  Dosya okunamadı: {filename} — {e}")
            hata += 1
            continue

        jikan = data.get("jikan")
        turkanime = data.get("turkanime", {})
        slug = turkanime.get("slug")
        isim = turkanime.get("isim", "?")

        if not slug:
            hata += 1
            continue

        # Jikan verisi null olan animeleri atla
        if jikan is None:
            jikan_null += 1
            continue

        mevcut_mal_id = jikan.get("mal_id")

        # Türkanime'den yeni MAL ID çek
        yeni_mal_id = scraper.fetch_mal_id(slug)

        if yeni_mal_id is None:
            mal_yok.append({"slug": slug, "isim": isim, "mevcut_mal_id": mevcut_mal_id})
            print(f"[{idx}/{toplam}] ⚠️  {isim} — Türkanime'de MAL linki yok (mevcut: {mevcut_mal_id})")
        elif yeni_mal_id == mevcut_mal_id:
            eslesen += 1
            # Sessiz geç, sadece hata/uyuşmazlık durumlarını göster
        else:
            uyusmayan.append({
                "slug": slug,
                "isim": isim,
                "mevcut_mal_id": mevcut_mal_id,
                "turkanime_mal_id": yeni_mal_id,
            })
            print(
                f"[{idx}/{toplam}] ❌ {isim} — "
                f"UYUŞMAZLIK! Mevcut: {mevcut_mal_id} ≠ Türkanime: {yeni_mal_id}"
            )

        # Her 50 animede bir ilerleme bildir
        if idx % 50 == 0:
            print(f"--- İlerleme: {idx}/{toplam} işlendi ---")

        time.sleep(0.1)

    # ── Sonuçları kaydet ──
    results = {
        "toplam_dosya": toplam,
        "eslesen": eslesen,
        "uyusmayan_sayisi": len(uyusmayan),
        "mal_linki_yok_sayisi": len(mal_yok),
        "jikan_null_atlanan": jikan_null,
        "hata": hata,
        "uyusmayan_detay": uyusmayan,
        "mal_linki_yok_detay": mal_yok,
    }

    with open("verify_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # ── Özet ──
    print("\n" + "=" * 60)
    print("  📊 DOĞRULAMA ÖZETİ")
    print("=" * 60)
    print(f"  Toplam Dosya          : {toplam}")
    print(f"  Jikan Null (atlandı)  : {jikan_null}")
    print(f"  ✅ Eşleşen            : {eslesen}")
    print(f"  ❌ Uyuşmayan          : {len(uyusmayan)}")
    print(f"  ⚠️  MAL Linki Yok     : {len(mal_yok)}")
    print(f"  Hata                  : {hata}")
    print(f"\n  Detaylar: verify_results.json")
    print("=" * 60)


if __name__ == "__main__":
    main()
