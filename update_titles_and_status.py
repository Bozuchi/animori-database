"""
update_titles_and_status.py — TMDB Veritabanında Başlıkları Romaji'ye Çevirme ve Status Ekleme

1. api/anime_tmdb/*.json dosyalarındaki original_name alanını inceler:
   - Japonca (Kanji/Kana) ise, öncelikle MAL veritabanındaki resmi Romaji başlığı (Shingeki no Kyojin vb.) ile değiştirir.
   - Bulunamazsa pykakasi ile otomatik Romaji transliterasyonu yapar.
   - Orijinal Japonca karakterleri 'japanese_name' alanında saklayarak hiçbir veriyi kaybetmez.
2. api/animes_tmdb.json vitrin indeksine hem güncellenmiş 'original_name' (Romaji) hem de 'status' bilgisini ekler.
"""

import os
import json
import pykakasi
from logger import setup_logger

logger = setup_logger("UpdateTitles")

TARGET_DIR = "api/anime_tmdb"
SOURCE_MAL_DIR = "api/anime"
INDEX_PATH = "api/animes_tmdb.json"

kakasi = pykakasi.kakasi()

def has_japanese(text: str) -> bool:
    """Metinde Japonca karakter (Kanji/Hiragana/Katakana) olup olmadığını kontrol eder."""
    if not text:
        return False
    return any(0x3040 <= ord(c) <= 0x9FFF for c in text)

def convert_to_romaji(text: str) -> str:
    """Japonca metni pykakasi ile Romaji'ye çevirir."""
    if not text:
        return ""
    result = kakasi.convert(text)
    words = [item['hepburn'] for item in result if item['hepburn']]
    # Temiz capitalize yap
    return " ".join(w.capitalize() for w in words)

def run():
    if not os.path.exists(TARGET_DIR):
        logger.error(f"Hedef klasör bulunamadı: {TARGET_DIR}")
        return

    # 1. Eski MAL dosyalarından MAL ID -> Romaji Başlık haritası çıkar
    logger.info("Eski MAL dosyalarından resmi Romaji başlıkları indeksleniyor...")
    mal_to_romaji = {}
    if os.path.exists(SOURCE_MAL_DIR):
        for fname in os.listdir(SOURCE_MAL_DIR):
            if not fname.endswith(".json"):
                continue
            name_no_ext = fname[:-5]
            mid = int(name_no_ext) if name_no_ext.isdigit() else None
            fpath = os.path.join(SOURCE_MAL_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    d = json.load(f)
                jt = d.get("jikan", {}).get("title")
                if not mid and d.get("jikan"):
                    mid = d["jikan"].get("mal_id")
                if mid and jt:
                    mal_to_romaji[mid] = jt
            except Exception:
                pass
    logger.info(f"✅ {len(mal_to_romaji):,} adet resmi Romaji başlık yüklendi.")

    # 2. api/anime_tmdb klasörünü güncelle
    files = [f for f in os.listdir(TARGET_DIR) if f.endswith(".json")]
    logger.info(f"{len(files):,} adet TMDB dosyası güncelleniyor...")

    id_to_romaji = {}
    id_to_status = {}
    updated_files = 0

    for fname in files:
        fpath = os.path.join(TARGET_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                doc = json.load(f)

            tid = doc["id"]
            orig_name = doc.get("original_name") or ""
            status = doc.get("status") or "Ended"
            id_to_status[tid] = status

            # İlişkili MAL ID'leri bul (Öncelik: Sezon 1 ana serisi)
            canonical_mal_id = None
            all_mids = []

            if doc.get("type") == "movie":
                canonical_mal_id = doc.get("mal_id")
                if canonical_mal_id:
                    all_mids.append(canonical_mal_id)
            else:
                # Önce Sezon 1'e bak
                s1 = next((s for s in doc.get("seasons", []) if s.get("season_number") == 1), None)
                if s1 and s1.get("mal_id"):
                    canonical_mal_id = s1["mal_id"]
                    all_mids.append(canonical_mal_id)

                # Diğer normal sezonlar (S > 0)
                for s in doc.get("seasons", []):
                    s_num = s.get("season_number", 0)
                    mid = s.get("mal_id")
                    if s_num > 0 and mid and mid not in all_mids:
                        all_mids.append(mid)

                # En son Specials (S0)
                for s in doc.get("seasons", []):
                    s_num = s.get("season_number", 0)
                    mid = s.get("mal_id")
                    if s_num == 0 and mid and mid not in all_mids:
                        all_mids.append(mid)

            romaji_title = None

            # 1. Öncelik: MAL resmi Romaji başlığı (Sezon 1 öncelikli)
            for mid in all_mids:
                if mid in mal_to_romaji:
                    romaji_title = mal_to_romaji[mid]
                    break

            # 2. Öncelik: pykakasi ile çevir (Eğer Japonca ise)
            if not romaji_title:
                if has_japanese(orig_name):
                    romaji_title = convert_to_romaji(orig_name)
                else:
                    romaji_title = orig_name

            # Orijinal Japonca ismi koru
            if has_japanese(orig_name) and "japanese_name" not in doc:
                doc["japanese_name"] = orig_name

            doc["original_name"] = romaji_title
            id_to_romaji[tid] = romaji_title

            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)

            updated_files += 1

        except Exception as e:
            logger.error(f"Hata ({fname}): {e}")

    logger.info(f"✅ {updated_files:,} dosya başarıyla güncellendi.")

    # 3. animes_tmdb.json vitrin indeksini güncelle
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                catalog = json.load(f)

            for item in catalog:
                cid = item["id"]
                if cid in id_to_romaji:
                    item["original_name"] = id_to_romaji[cid]
                if cid in id_to_status:
                    item["status"] = id_to_status[cid]

            with open(INDEX_PATH, "w", encoding="utf-8") as f:
                json.dump(catalog, f, ensure_ascii=False, indent=2)
            logger.info(f"✅ '{INDEX_PATH}' vitrin indeksi güncellendi (status ve Romaji eklendi).")

        except Exception as e:
            logger.error(f"Vitrin dosyası güncellenirken hata: {e}")

if __name__ == "__main__":
    run()
