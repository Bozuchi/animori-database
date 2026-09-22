"""
reset_baseline_pre_animecix.py

Orijinal 3.549 TMDB dosyasını api/anime_backup_mal yedeğinden sıfırlar.
- 400 yeni AnimeciX animesine kesinlikle dokunmaz.
- metadata.json dosyasından AnimeciX fansublarını (id >= 724) temizler.
- data/animecix_processed_ids.json dosyasını temizler.
"""

import os
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from logger import setup_logger
from migrate_to_tmdb import TMDBMigrator

logger = setup_logger("ResetBaseline")

SOURCE_MAL_DIR = "api/anime_backup_mal"
ANIME_DIR = "api/anime"
METADATA_PATH = "api/metadata.json"
CHECKPOINT_PATH = "data/animecix_processed_ids.json"


def reset_all_original_files():
    t0 = time.time()
    logger.info("📡 Orijinal MAL yedeği taranıyor...")
    migrator = TMDBMigrator(source_dir=SOURCE_MAL_DIR)
    groups, _ = migrator.scan_and_group()
    logger.info(f"✅ {len(groups)} grup hazırlandı. Dosyalar yeniden üretiliyor...")

    def process_one(item):
        (mtype, tid), group = item
        fn = f"m_{tid}.json" if mtype == "movie" else f"{tid}.json"
        fp = os.path.join(ANIME_DIR, fn)
        
        # Eğer dosya diğer isimlendirmeyle varsa onu bul
        if not os.path.exists(fp):
            alt_fn = f"{tid}.json" if mtype == "movie" else f"m_{tid}.json"
            alt_fp = os.path.join(ANIME_DIR, alt_fn)
            if os.path.exists(alt_fp):
                fn = alt_fn
                fp = alt_fp

        doc = migrator.build_unified_document(tid, group)
        if not doc:
            return False

        # Film sadeleştirme
        if mtype == "movie" or doc.get("type") == "movie":
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

        with open(fp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        return True

    success_count = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(process_one, item) for item in groups.items()]
        for fut in as_completed(futures):
            if fut.result():
                success_count += 1

    logger.info(f"✅ {success_count} orijinal anime dosyası saf TMDB haline sıfırlandı ({time.time() - t0:.2f} sn).")

    # Metadata temizle
    if os.path.exists(METADATA_PATH):
        with open(METADATA_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
        fansubs = meta.get("fansubs", {})
        cleaned_fansubs = {k: v for k, v in fansubs.items() if int(k) <= 723}
        logger.info(f"Fansub temizliği: {len(fansubs)} -> {len(cleaned_fansubs)} fansub.")
        meta["fansubs"] = cleaned_fansubs
        with open(METADATA_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=4)

    # Checkpoint temizle
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump([], f)
        logger.info(f"✅ Checkpoint sıfırlandı: {CHECKPOINT_PATH}")


if __name__ == "__main__":
    reset_all_original_files()
