"""
restore_pre_animecix.py — AnimeciX Öncesi Temiz TMDB Veritabanını Geri Yükleme ve Yedekleme Betiği

1. AnimeciX aktarımı sırasında video eklenmiş dosyaları tespit eder (tau-video, fansub_id >= 724 vb.).
2. Bu dosyaları api/anime_backup_mal klasöründeki orijinal Türkanime verilerini kullanarak
   birebir saf TMDB şemasına (film sadeleştirmesi dahil) sıfırlar.
3. api/metadata.json dosyasından AnimeciX kaynaklı yeni fansubları (id >= 724) temizler.
4. Tüm api/anime klasörünü doğrular (0 tau-video, 0 fansub >= 724).
5. Tüm veri tabanının AnimeciX öncesi saf halini 'api/anime_backup_pre_animecix' olarak kopyalar.
"""

import os
import sys
import glob
import json
import shutil
import time
from logger import setup_logger
from migrate_to_tmdb import TMDBMigrator
from simplify_movies import simplify_all_movies

logger = setup_logger("RestorePreAnimecix")

SOURCE_MAL_DIR = "api/anime_backup_mal"
ANIME_DIR = "api/anime"
METADATA_PATH = "api/metadata.json"
INDEX_PATH = "api/animes.json"

BACKUP_PRE_ANIMECIX_DIR = "api/anime_backup_pre_animecix"
BACKUP_PRE_ANIMECIX_INDEX = "api/animes_backup_pre_animecix.json"
BACKUP_PRE_ANIMECIX_META = "api/metadata_backup_pre_animecix.json"


def find_animecix_modified_files():
    """AnimeciX videosu veya fansub'ı içeren tüm dosyaları tespit eder."""
    modified_files = []
    all_files = glob.glob(os.path.join(ANIME_DIR, "*.json"))
    
    for fpath in all_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            
            if "tau-video.xyz" in content:
                modified_files.append(fpath)
                continue
            
            doc = json.loads(content)
            found = False
            
            # TV Sezonlarını kontrol et
            for s in doc.get("seasons", []):
                for ep in s.get("episodes", []):
                    for v in ep.get("videos", []):
                        if v.get("fansub_id", 0) >= 724:
                            found = True
                            break
                    if found:
                        break
                if found:
                    break
            
            # Film videolarını kontrol et
            if not found:
                for v in doc.get("videos", []):
                    if v.get("fansub_id", 0) >= 724:
                        found = True
                        break
            
            # unassigned_videos kontrol et
            if not found:
                for u in doc.get("unassigned_videos", []):
                    for v in u.get("videos", []):
                        if v.get("fansub_id", 0) >= 724:
                            found = True
                            break
                    if found:
                        break
            
            if found:
                modified_files.append(fpath)
        except Exception as e:
            logger.warning(f"Dosya okunamadı: {fpath} — {e}")
            
    return modified_files


def restore_files(files_to_restore):
    """Belirtilen dosyaları MAL yedeğinden temiz TMDB şemasına yeniden üretir."""
    logger.info(f"Yeniden üretilecek dosya sayısı: {len(files_to_restore)}")
    if not files_to_restore:
        logger.info("Yeniden üretilecek dosya bulunamadı.")
        return

    migrator = TMDBMigrator(source_dir=SOURCE_MAL_DIR)
    groups, _ = migrator.scan_and_group()

    # Dosya adından (mtype, tid) çıkar
    restored_count = 0
    for fpath in files_to_restore:
        fname = os.path.basename(fpath)
        is_movie = fname.startswith("m_")
        raw_id = fname[2:-5] if is_movie else fname[:-5]
        
        if not raw_id.isdigit():
            logger.warning(f"Geçersiz dosya adı formatı: {fname}")
            continue
            
        tid = int(raw_id)
        mtype = "movie" if is_movie else "tv"
        key = (mtype, tid)

        group = groups.get(key)
        if not group:
            # Belki film tv olarak veya tam tersi kaydedilmiştir
            alt_key = ("tv", tid) if is_movie else ("movie", tid)
            group = groups.get(alt_key)
            if group:
                key = alt_key
                mtype = alt_key[0]
                is_movie = (mtype == "movie")

        if not group:
            logger.error(f"Grup bulunamadı: {fname} (tid: {tid})")
            continue

        doc = migrator.build_unified_document(tid, group)
        if not doc:
            logger.error(f"Doküman üretilemedi: {fname}")
            continue

        # Film ise sadeleştir
        if is_movie or doc.get("type") == "movie":
            doc["type"] = "movie"
            seasons = doc.get("seasons") or []
            mal_id = doc.get("mal_id")
            anilist_id = doc.get("anilist_id")
            skip_times = doc.get("skip_times")
            videos = doc.get("videos") or []
            unassigned_videos = doc.get("unassigned_videos") or []

            if seasons:
                s0 = seasons[0]
                if not mal_id:
                    mal_id = s0.get("mal_id")
                if not anilist_id:
                    anilist_id = s0.get("anilist_id")
                if s0.get("unassigned_videos"):
                    unassigned_videos.extend(s0["unassigned_videos"])
                eps = s0.get("episodes") or []
                if eps:
                    ep0 = eps[0]
                    if not skip_times:
                        skip_times = ep0.get("skip_times")
                    if not videos:
                        videos = ep0.get("videos") or []

            clean_doc = {
                "id": doc["id"],
                "type": "movie",
                "name": doc["name"],
                "original_name": doc.get("original_name", ""),
                "overview": doc.get("overview", ""),
                "poster_path": doc.get("poster_path"),
                "backdrop_path": doc.get("backdrop_path"),
                "first_air_date": doc.get("first_air_date"),
                "runtime": doc.get("runtime", 0),
                "status": doc.get("status", "Released"),
                "vote_average": doc.get("vote_average", 0.0),
                "vote_count": doc.get("vote_count", 0),
                "genres": doc.get("genres", []),
                "original_language": doc.get("original_language", "ja"),
                "external_ids": doc.get("external_ids", {}),
                "mal_id": mal_id,
                "anilist_id": anilist_id,
                "skip_times": skip_times,
                "videos": videos
            }
            if unassigned_videos:
                clean_doc["unassigned_videos"] = unassigned_videos
            doc = clean_doc

        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        restored_count += 1

    logger.info(f"✅ {restored_count}/{len(files_to_restore)} dosya başarıyla orijinal TMDB haline sıfırlandı.")


def clean_metadata():
    """metadata.json'dan id >= 724 olan AnimeciX fansublarını kaldırır."""
    if not os.path.exists(METADATA_PATH):
        return
    with open(METADATA_PATH, "r", encoding="utf-8") as f:
        meta = json.load(f)

    fansubs = meta.get("fansubs", {})
    cleaned_fansubs = {k: v for k, v in fansubs.items() if int(k) <= 723}
    logger.info(f"Fansub temizliği: {len(fansubs)} -> {len(cleaned_fansubs)} fansub (AnimeciX eklemeleri kaldırıldı).")
    meta["fansubs"] = cleaned_fansubs

    with open(METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=4)


def create_pre_animecix_backup():
    """Tüm TMDB veritabanını api/anime_backup_pre_animecix klasörüne yedekler."""
    logger.info("📦 AnimeciX öncesi saf TMDB veritabanı yedekleniyor...")
    
    if os.path.exists(BACKUP_PRE_ANIMECIX_DIR):
        logger.info(f"Mevcut '{BACKUP_PRE_ANIMECIX_DIR}' silinip yeniden oluşturuluyor...")
        shutil.rmtree(BACKUP_PRE_ANIMECIX_DIR)
        
    shutil.copytree(ANIME_DIR, BACKUP_PRE_ANIMECIX_DIR)
    logger.info(f"✅ '{ANIME_DIR}' -> '{BACKUP_PRE_ANIMECIX_DIR}' ({len(os.listdir(BACKUP_PRE_ANIMECIX_DIR))} dosya) kopyalandı.")

    if os.path.exists(INDEX_PATH):
        shutil.copy2(INDEX_PATH, BACKUP_PRE_ANIMECIX_INDEX)
        logger.info(f"✅ '{INDEX_PATH}' -> '{BACKUP_PRE_ANIMECIX_INDEX}' kopyalandı.")

    if os.path.exists(METADATA_PATH):
        shutil.copy2(METADATA_PATH, BACKUP_PRE_ANIMECIX_META)
        logger.info(f"✅ '{METADATA_PATH}' -> '{BACKUP_PRE_ANIMECIX_META}' kopyalandı.")


def verify():
    """Yedeğin ve mevcut veritabanının temizliğini ve doğruluğunu kontrol eder."""
    logger.info("🔍 Doğrulama yapılıyor...")
    mod_files = find_animecix_modified_files()
    if mod_files:
        logger.error(f"HATA: Hâlâ AnimeciX içeren {len(mod_files)} dosya var!")
        return False
    
    total_live = len([f for f in os.listdir(ANIME_DIR) if f.endswith(".json")])
    total_backup = len([f for f in os.listdir(BACKUP_PRE_ANIMECIX_DIR) if f.endswith(".json")])
    
    logger.info("=" * 60)
    logger.info("🎉 DOĞRULAMA BAŞARILI!")
    logger.info(f"Canlı Dosya Sayısı              : {total_live:,}")
    logger.info(f"Saf TMDB Yedek Dosya Sayısı     : {total_backup:,}")
    logger.info(f"AnimeciX Traces Kalan Dosya     : {len(mod_files)}")
    logger.info("=" * 60)
    return True


if __name__ == "__main__":
    t0 = time.time()
    logger.info("Adım 1: AnimeciX izi taşıyan dosyalar aranıyor...")
    mod_files = find_animecix_modified_files()
    logger.info(f"Tespit edilen dosya sayısı: {len(mod_files)}")

    logger.info("Adım 2: Dosyalar saf TMDB haline sıfırlanıyor...")
    restore_files(mod_files)

    logger.info("Adım 3: metadata.json temizleniyor...")
    clean_metadata()

    logger.info("Adım 4: Tam yedek alınıyor...")
    create_pre_animecix_backup()

    logger.info("Adım 5: Doğrulama...")
    success = verify()

    elapsed = time.time() - t0
    logger.info(f"Toplam süre: {elapsed:.2f} saniye")
