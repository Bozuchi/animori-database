"""
apply_migration.py — Canlıya Alma ve Yedekleme Betiği

1. Mevcut MAL odaklı api/anime klasörünü api/anime_backup_mal olarak yedekler.
2. Mevcut MAL odaklı api/animes.json dosyasını api/animes_backup_mal.json olarak yedekler.
3. Yeni TMDB odaklı api/anime_tmdb klasörünü canlı api/anime konumuna taşır.
4. Yeni TMDB odaklı api/animes_tmdb.json dosyasını canlı api/animes.json konumuna taşır.
5. Sonuçları doğrular.
"""

import os
import shutil
from logger import setup_logger

logger = setup_logger("ApplyMigration")

SOURCE_ANIME_DIR = "api/anime"
SOURCE_INDEX_PATH = "api/animes.json"

BACKUP_ANIME_DIR = "api/anime_backup_mal"
BACKUP_INDEX_PATH = "api/animes_backup_mal.json"

NEW_ANIME_DIR = "api/anime_tmdb"
NEW_INDEX_PATH = "api/animes_tmdb.json"

def apply():
    logger.info("=" * 60)
    logger.info("🚀 TMDB Canlıya Alma ve Yedekleme İşlemi Başlıyor...")
    logger.info("=" * 60)

    # Doğrulama: Yeni veriler hazır mı?
    if not os.path.exists(NEW_ANIME_DIR):
        logger.error(f"HATA: '{NEW_ANIME_DIR}' klasörü bulunamadı!")
        return False
    if not os.path.exists(NEW_INDEX_PATH):
        logger.error(f"HATA: '{NEW_INDEX_PATH}' dosyası bulunamadı!")
        return False

    # 1. Eski api/anime klasörünü yedekle
    if os.path.exists(SOURCE_ANIME_DIR):
        if not os.path.exists(BACKUP_ANIME_DIR):
            logger.info(f"📦 '{SOURCE_ANIME_DIR}' -> '{BACKUP_ANIME_DIR}' olarak yedekleniyor...")
            shutil.move(SOURCE_ANIME_DIR, BACKUP_ANIME_DIR)
            logger.info(f"✅ Klasör yedeği alındı ({len(os.listdir(BACKUP_ANIME_DIR))} dosya).")
        else:
            logger.warning(f"⚠️ '{BACKUP_ANIME_DIR}' zaten mevcut, üzerine yazılmıyor.")
            shutil.rmtree(SOURCE_ANIME_DIR)
    else:
        logger.info(f"'{SOURCE_ANIME_DIR}' mevcut değildi.")

    # 2. Eski api/animes.json dosyasını yedekle
    if os.path.exists(SOURCE_INDEX_PATH):
        if not os.path.exists(BACKUP_INDEX_PATH):
            logger.info(f"📦 '{SOURCE_INDEX_PATH}' -> '{BACKUP_INDEX_PATH}' olarak yedekleniyor...")
            shutil.move(SOURCE_INDEX_PATH, BACKUP_INDEX_PATH)
            logger.info("✅ Vitrin indeks yedeği alındı.")
        else:
            logger.warning(f"⚠️ '{BACKUP_INDEX_PATH}' zaten mevcut.")
            os.remove(SOURCE_INDEX_PATH)

    # 3. Yeni api/anime_tmdb -> api/anime olarak taşı
    logger.info(f"🚚 '{NEW_ANIME_DIR}' -> '{SOURCE_ANIME_DIR}' konumuna taşınıyor...")
    shutil.move(NEW_ANIME_DIR, SOURCE_ANIME_DIR)
    logger.info("✅ Canlı anime klasörü taşındı.")

    # 4. Yeni api/animes_tmdb.json -> api/animes.json olarak taşı
    logger.info(f"🚚 '{NEW_INDEX_PATH}' -> '{SOURCE_INDEX_PATH}' konumuna taşınıyor...")
    shutil.move(NEW_INDEX_PATH, SOURCE_INDEX_PATH)
    logger.info("✅ Canlı vitrin indeksi taşındı.")

    # 5. Doğrulama
    live_files = len([f for f in os.listdir(SOURCE_ANIME_DIR) if f.endswith('.json')])
    backup_files = len([f for f in os.listdir(BACKUP_ANIME_DIR) if f.endswith('.json')]) if os.path.exists(BACKUP_ANIME_DIR) else 0

    logger.info("=" * 60)
    logger.info("🎉 CANLIYA ALMA BAŞARIYLA TAMAMLANDI!")
    logger.info("=" * 60)
    logger.info(f"Canlı Anime Dosyaları ('{SOURCE_ANIME_DIR}') : {live_files:,} adet")
    logger.info(f"Yedek Anime Dosyaları ('{BACKUP_ANIME_DIR}'): {backup_files:,} adet")
    logger.info(f"Canlı Vitrin İndeksi   ('{SOURCE_INDEX_PATH}')    : {os.path.getsize(SOURCE_INDEX_PATH):,} bayt")
    logger.info(f"Yedek Vitrin İndeksi   ('{BACKUP_INDEX_PATH}'): {os.path.getsize(BACKUP_INDEX_PATH):,} bayt")
    logger.info("=" * 60)
    return True

if __name__ == "__main__":
    apply()
