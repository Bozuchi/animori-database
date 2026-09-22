"""
simplify_movies.py — api/anime_tmdb klasöründeki filmleri sadeleştirir.

Filmlerdeki yapay seasons: [{ episodes: [{ videos: [...] }] }] yapısını kaldırarak
doğrudan doküman köküne mal_id, anilist_id, skip_times ve videos ekler.
Ayrıca api/animes_tmdb.json vitrin dosyasını günceller.
"""

import os
import json
from logger import setup_logger

logger = setup_logger("SimplifyMovies")

TARGET_DIR = "api/anime_tmdb"
INDEX_PATH = "api/animes_tmdb.json"

def simplify_all_movies():
    if not os.path.exists(TARGET_DIR):
        logger.error(f"Hedef klasör bulunamadı: {TARGET_DIR}")
        return

    files = [f for f in os.listdir(TARGET_DIR) if f.endswith(".json")]
    logger.info(f"{len(files)} dosya inceleniyor...")

    movie_count = 0
    updated_files = 0
    movie_ids = set()

    for fname in files:
        fpath = os.path.join(TARGET_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                doc = json.load(f)

            if doc.get("type") == "movie":
                movie_count += 1
                movie_ids.add(doc["id"])

                # Eğer zaten sadeleştirilmişse atla
                if "videos" in doc and "seasons" not in doc:
                    continue

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

                # Yeni temiz sözlük oluştur (anahtarların mantıklı bir sırada olması için)
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

                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(clean_doc, f, ensure_ascii=False, indent=2)

                updated_files += 1

        except Exception as e:
            logger.error(f"Hata ({fname}): {e}")

    logger.info(f"✅ Toplam {movie_count} filmden {updated_files} tanesi sadeleştirildi.")

    # animes_tmdb.json vitrin dosyasını güncelle
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                catalog = json.load(f)

            for item in catalog:
                if item.get("id") in movie_ids or item.get("type") == "movie":
                    item["season_count"] = 0

            with open(INDEX_PATH, "w", encoding="utf-8") as f:
                json.dump(catalog, f, ensure_ascii=False, indent=2)
            logger.info(f"✅ '{INDEX_PATH}' vitrin indeksi güncellendi (Filmlerin season_count değeri 0 yapıldı).")
        except Exception as e:
            logger.error(f"Vitrin indeksi güncellenirken hata: {e}")

if __name__ == "__main__":
    simplify_all_movies()
