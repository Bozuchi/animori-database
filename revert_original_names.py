"""
revert_original_names.py — original_name alanını orijinal TMDB Japonca/Kanji haline döndürür.
status bilgisi animes_tmdb.json dosyasında korunur.
"""

import os
import json
from logger import setup_logger

logger = setup_logger("RevertOriginalNames")

TARGET_DIR = "api/anime_tmdb"
CACHE_DIR = "data/tmdb_cache"
INDEX_PATH = "api/animes_tmdb.json"

def run():
    if not os.path.exists(TARGET_DIR):
        logger.error(f"Klasör bulunamadı: {TARGET_DIR}")
        return

    files = [f for f in os.listdir(TARGET_DIR) if f.endswith(".json")]
    logger.info(f"{len(files):,} adet dosya eski orijinal isimlerine döndürülüyor...")

    id_to_orig = {}
    reverted_count = 0

    for fname in files:
        fpath = os.path.join(TARGET_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                doc = json.load(f)

            raw_id = doc["id"]
            # Eğer m_ prefixli ise asıl tmdb_id'yi çıkar
            str_id = str(raw_id)
            tid = int(str_id[1:]) if str_id.startswith("m") and str_id[1:].isdigit() else (int(raw_id) if str(raw_id).isdigit() else raw_id)

            orig = None

            # 1. Kaydettiğimiz japanese_name alanı
            if doc.get("japanese_name"):
                orig = doc["japanese_name"]
                del doc["japanese_name"]

            # 2. TMDB Cache dosyasından doğrula
            if not orig:
                cache_file = os.path.join(CACHE_DIR, f"details_{tid}.json")
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, "r", encoding="utf-8") as cf:
                            cdet = json.load(cf)
                        orig = cdet.get("original_name") or cdet.get("original_title")
                    except Exception:
                        pass

            if orig:
                doc["original_name"] = orig
                id_to_orig[raw_id] = orig
                reverted_count += 1

                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"Hata ({fname}): {e}")

    logger.info(f"✅ {reverted_count:,} adet dosya orijinal ismine döndürüldü.")

    # animes_tmdb.json güncelle (status korunur, original_name döndürülür)
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                catalog = json.load(f)

            for item in catalog:
                cid = item["id"]
                if cid in id_to_orig:
                    item["original_name"] = id_to_orig[cid]

            with open(INDEX_PATH, "w", encoding="utf-8") as f:
                json.dump(catalog, f, ensure_ascii=False, indent=2)
            logger.info(f"✅ '{INDEX_PATH}' vitrin indeksi güncellendi (original_name eski haline döndü, status korundu).")

        except Exception as e:
            logger.error(f"Vitrin güncellenirken hata: {e}")

if __name__ == "__main__":
    run()
