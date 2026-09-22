"""
ingest_missing_animecix.py — AnimeciX'teki Yeni Animeleri Veritabanına Kazandırma Motoru

AnimeciX arşivinde olup yerel veritabanımızda bulunmayan geçerli TMDB ID'li (~416)
başlıkları TMDB'den künyeleriyle çeker, sadeleştirilmiş film/dizi şemasına oturtur,
AnimeciX TAU, SIBNET ve UQLOAD videolarını bağlar ve hem api/anime/*.json dosyalarını
hem de api/animes.json vitrin indeksini günceller.
"""

import os
import sys
import json
import time
import argparse
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from animecix_provider import AnimecixProvider
from tmdb_client import TMDBClient
from logger import setup_logger

sys.stdout.reconfigure(encoding="utf-8")
logger = setup_logger("AnimecixIngest")

ALLOWED_PLAYERS = {"TAU", "SIBNET", "UQLOAD"}


class AnimecixIngestEngine:
    def __init__(
        self,
        base_dir: str = "api",
        report_path: str = "data/animecix_backfill_report.json",
        catalog_path: str = "data/animecix_catalog.json",
        checkpoint_path: str = "data/animecix_ingest_processed_ids.json",
        cache_dir: str = "data/tmdb_cache",
        workers: int = 3,
    ):
        self.base_dir = base_dir
        self.anime_dir = os.path.join(base_dir, "anime")
        self.metadata_path = os.path.join(base_dir, "metadata.json")
        self.animes_json_path = os.path.join(base_dir, "animes.json")
        self.report_path = report_path
        self.catalog_path = catalog_path
        self.checkpoint_path = checkpoint_path
        self.cache_dir = cache_dir
        self.workers = workers

        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.anime_dir, exist_ok=True)

        self.provider = AnimecixProvider()
        self.tmdb = TMDBClient()

        # Thread-safe kilitler
        self._meta_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._cache_lock = threading.Lock()

        # Veriler
        self.metadata = self._load_metadata()
        self.fansubs = self.metadata.get("fansubs", {})
        self.name_to_fansub_id = {v["name"].lower(): int(k) for k, v in self.fansubs.items()}
        self.max_fansub_id = max(int(k) for k in self.fansubs.keys()) if self.fansubs else 0

        self.animes_index = self._load_animes_index()
        self.indexed_ids = {item.get("id") for item in self.animes_index if item.get("id") is not None}

        self.processed_ids = self._load_checkpoint()

        # İstatistikler
        self.stats = {
            "total_candidates": 0,
            "already_exists": 0,
            "tmdb_not_found": 0,
            "successfully_ingested": 0,
            "movies_added": 0,
            "series_added": 0,
            "added_videos": {"TAU": 0, "SIBNET": 0, "UQLOAD": 0, "TOTAL": 0},
            "unassigned_videos_count": 0,
            "errors": 0,
        }

    def _load_metadata(self) -> dict:
        if os.path.exists(self.metadata_path):
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"metadata.json okunamadı: {e}")
        return {"genres": {}, "fansubs": {}}

    def save_metadata(self):
        with self._meta_lock:
            try:
                with open(self.metadata_path, "w", encoding="utf-8") as f:
                    json.dump(self.metadata, f, ensure_ascii=False, indent=4)
                logger.debug("💾 metadata.json kaydedildi.")
            except Exception as e:
                logger.error(f"metadata.json kaydedilemedi: {e}")

    def _load_animes_index(self) -> list[dict]:
        if os.path.exists(self.animes_json_path):
            try:
                with open(self.animes_json_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"animes.json okunamadı: {e}")
        return []

    def save_animes_index(self):
        with self._index_lock:
            try:
                with open(self.animes_json_path, "w", encoding="utf-8") as f:
                    json.dump(self.animes_index, f, ensure_ascii=False, indent=2)
                logger.debug("💾 animes.json kaydedildi.")
            except Exception as e:
                logger.error(f"animes.json kaydedilemedi: {e}")

    def _load_checkpoint(self) -> set[int]:
        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info(f"📂 Checkpoint: {len(data)} başlık önceden işlenmiş.")
                    return set(data)
            except Exception as e:
                logger.warning(f"Checkpoint okunamadı: {e}")
        return set()

    def _save_checkpoint(self):
        with self._stats_lock:
            try:
                with open(self.checkpoint_path, "w", encoding="utf-8") as f:
                    json.dump(list(self.processed_ids), f)
            except Exception as e:
                logger.warning(f"Checkpoint kaydedilemedi: {e}")

    def get_or_create_fansub_id(self, raw_extra: str | None) -> int:
        if not raw_extra:
            return 3
        clean_name = self.provider.extract_fansub_name(raw_extra)
        if not clean_name:
            return 3

        clean_lower = clean_name.lower()
        with self._meta_lock:
            if clean_lower in self.name_to_fansub_id:
                return self.name_to_fansub_id[clean_lower]

            self.max_fansub_id += 1
            new_id = self.max_fansub_id
            self.name_to_fansub_id[clean_lower] = new_id
            self.fansubs[str(new_id)] = {"name": clean_name, "url": ""}
            return new_id

    def _get_cached_tmdb_details(self, tmdb_id: int, is_movie: bool) -> dict | None:
        cache_file = os.path.join(self.cache_dir, f"details_{'m_' if is_movie else ''}{tmdb_id}.json")
        with self._cache_lock:
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass

        details = self.tmdb.get_details(tmdb_id, is_movie=is_movie)
        if details:
            with self._cache_lock:
                try:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(details, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
        return details

    def _get_cached_season_details(self, tmdb_id: int, season_number: int) -> dict | None:
        cache_file = os.path.join(self.cache_dir, f"season_{tmdb_id}_{season_number}.json")
        with self._cache_lock:
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass

        s_details = self.tmdb.get_season_details(tmdb_id, season_number)
        if s_details:
            with self._cache_lock:
                try:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(s_details, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
        return s_details

    def load_candidates(self) -> list[dict]:
        """435 eşleşmeyen arasından tmdb_id > 0 olan geçerli adayları çıkarır."""
        if not os.path.exists(self.report_path):
            logger.error(f"Rapor dosyası bulunamadı: {self.report_path}")
            return []

        with open(self.report_path, "r", encoding="utf-8") as f:
            rep = json.load(f)

        unmatched = rep.get("unmatched_list", [])

        # Catalog detaylarını da bağla
        cat_map = {}
        if os.path.exists(self.catalog_path):
            try:
                with open(self.catalog_path, "r", encoding="utf-8") as f:
                    catalog = json.load(f)
                cat_map = {item["id"]: item for item in catalog}
            except Exception:
                pass

        candidates = []
        for item in unmatched:
            tid = item.get("tmdb_id")
            if tid and isinstance(tid, int) and tid > 0:
                c_info = cat_map.get(item["acx_id"], {})
                cand = {
                    "acx_id": item["acx_id"],
                    "name": item.get("name"),
                    "tmdb_id": tid,
                    "mal_id": item.get("mal_id") or c_info.get("mal_id"),
                    "acx_type": c_info.get("type"),
                }
                candidates.append(cand)

        logger.info(f"📋 Toplam geçerli aktarılacak aday sayısı: {len(candidates)}")
        return candidates

    def ingest_candidate(self, candidate: dict, dry_run: bool = False) -> dict:
        acx_id = candidate["acx_id"]
        tmdb_id = candidate["tmdb_id"]
        name = candidate["name"]
        mal_id = candidate.get("mal_id")
        acx_type = candidate.get("acx_type")

        res_stat = {
            "acx_id": acx_id,
            "tmdb_id": tmdb_id,
            "name": name,
            "status": "success",
            "type": None,
            "videos_added": {"TAU": 0, "SIBNET": 0, "UQLOAD": 0, "TOTAL": 0},
            "unassigned_count": 0,
            "file": None,
        }

        # 1. Zaten var mı kontrolü
        tv_file = os.path.join(self.anime_dir, f"{tmdb_id}.json")
        movie_file = os.path.join(self.anime_dir, f"m_{tmdb_id}.json")
        if os.path.exists(tv_file) or os.path.exists(movie_file):
            res_stat["status"] = "already_exists"
            with self._stats_lock:
                self.stats["already_exists"] += 1
            return res_stat

        # 2. TMDB Tür Tespiti ve Künye Çekimi
        is_movie = (acx_type == "movie")
        details = self._get_cached_tmdb_details(tmdb_id, is_movie=is_movie)
        if not details:
            # Diğer türü dene
            is_movie = not is_movie
            details = self._get_cached_tmdb_details(tmdb_id, is_movie=is_movie)

        if not details:
            res_stat["status"] = "tmdb_not_found"
            with self._stats_lock:
                self.stats["tmdb_not_found"] += 1
            return res_stat

        media_type = "movie" if is_movie else "tv"
        res_stat["type"] = media_type

        # ─────────────────────────────────────────────────────────────
        # A) FİLM OLUŞTURMA
        # ─────────────────────────────────────────────────────────────
        if is_movie:
            target_file = tv_file  # Standart film dosya adı {tmdb_id}.json
            videos = []
            existing_urls = set()

            # AnimeciX'ten video oynatıcılarını çek
            vids = self.provider.fetch_episode_videos(acx_id, season_num=1, episode_num=1)
            # Ayrıca title detail kontrol et
            t_detail = self.provider.fetch_title_detail(acx_id, season_number=1)
            if t_detail and t_detail.get("videos"):
                vids.extend(t_detail["videos"])

            for v in vids:
                if not v.get("approved", True):
                    continue
                url = v.get("url")
                if not url or "youtube.com" in url or "fragman" in (v.get("name") or "").lower():
                    continue

                player = self.provider.normalize_player(v.get("name"), url)
                if player not in ALLOWED_PLAYERS:
                    continue
                if url in existing_urls:
                    continue

                fid = self.get_or_create_fansub_id(v.get("extra"))
                videos.append({
                    "fansub_id": fid,
                    "player": player,
                    "url": url,
                })
                existing_urls.add(url)
                res_stat["videos_added"][player] += 1
                res_stat["videos_added"]["TOTAL"] += 1

            movie_doc = {
                "id": tmdb_id,
                "type": "movie",
                "name": details["name"],
                "original_name": details.get("original_name") or "",
                "overview": details.get("overview") or "",
                "poster_path": details.get("poster_path"),
                "backdrop_path": details.get("backdrop_path"),
                "first_air_date": details.get("first_air_date"),
                "runtime": details.get("runtime", 0),
                "status": details.get("status", "Released"),
                "vote_average": details.get("vote_average", 0.0),
                "vote_count": details.get("vote_count", 0),
                "genres": details.get("genres", []),
                "original_language": details.get("original_language", "ja"),
                "external_ids": details.get("external_ids", {}),
                "mal_id": mal_id,
                "anilist_id": None,
                "skip_times": None,
                "videos": videos,
            }

            if not dry_run:
                with open(target_file, "w", encoding="utf-8") as f:
                    json.dump(movie_doc, f, ensure_ascii=False, indent=2)

            res_stat["file"] = os.path.basename(target_file)
            index_entry = {
                "id": tmdb_id,
                "name": movie_doc["name"],
                "original_name": movie_doc["original_name"],
                "type": "movie",
                "poster_path": movie_doc["poster_path"],
                "backdrop_path": movie_doc["backdrop_path"],
                "vote_average": movie_doc["vote_average"],
                "year": int(movie_doc["first_air_date"][:4]) if movie_doc.get("first_air_date") and movie_doc["first_air_date"][:4].isdigit() else None,
                "genres": movie_doc["genres"],
                "season_count": 0,
                "mal_ids": [mal_id] if mal_id else [],
                "status": movie_doc["status"],
            }

        # ─────────────────────────────────────────────────────────────
        # B) DİZİ (TV) OLUŞTURMA
        # ─────────────────────────────────────────────────────────────
        else:
            target_file = tv_file
            seasons_summary = details.get("seasons_summary", [])
            if not seasons_summary:
                # En az 1 sezon varsay
                seasons_summary = [{"season_number": 1, "name": "Season 1", "overview": ""}]

            seasons_data = []
            for s_info in seasons_summary:
                s_num = s_info["season_number"]
                s_detail = self._get_cached_season_details(tmdb_id, s_num)
                if s_detail:
                    seasons_data.append(s_detail)
                else:
                    # Fallback basit sezon
                    seasons_data.append({
                        "season_number": s_num,
                        "name": s_info.get("name") or f"Season {s_num}",
                        "overview": s_info.get("overview") or "",
                        "poster_path": s_info.get("poster_path"),
                        "air_date": s_info.get("air_date"),
                        "episode_count": s_info.get("episode_count", 0),
                        "episodes": [],
                    })

            # AnimeciX sezon detaylarını ve videolarını çek
            acx_detail = self.provider.fetch_title_detail(acx_id)
            acx_seasons = acx_detail.get("seasons", []) if acx_detail else [{"number": 1}]
            if not acx_seasons:
                acx_seasons = [{"number": 1}]

            unassigned_videos = []
            unassigned_urls = set()

            for acx_s in acx_seasons:
                acx_s_num = acx_s.get("number", 1)
                s_res = self.provider.fetch_title_detail(acx_id, season_number=acx_s_num)
                if not s_res:
                    continue

                raw_vids = s_res.get("videos", [])
                by_ep = defaultdict(list)
                for v in raw_vids:
                    if not v.get("approved", True):
                        continue
                    url = v.get("url")
                    if not url or "youtube.com" in url or "fragman" in (v.get("name") or "").lower():
                        continue
                    ep_num = v.get("episode_num")
                    if ep_num is not None:
                        by_ep[int(ep_num)].append(v)

                # Hedef TMDB sezonunu bul
                target_s = next((s for s in seasons_data if s.get("season_number") == acx_s_num), None)
                if not target_s and seasons_data:
                    target_s = seasons_data[0]

                if not target_s:
                    continue

                episodes_list = target_s.get("episodes", [])
                ep_map = {ep.get("episode_number"): ep for ep in episodes_list}

                for ep_num, ep_vids in by_ep.items():
                    target_ep = ep_map.get(ep_num)
                    for v in ep_vids:
                        url = v.get("url")
                        player = self.provider.normalize_player(v.get("name"), url)
                        if player not in ALLOWED_PLAYERS:
                            continue

                        fid = self.get_or_create_fansub_id(v.get("extra"))

                        if target_ep:
                            if "videos" not in target_ep or target_ep["videos"] is None:
                                target_ep["videos"] = []
                            existing_urls = {item.get("url") for item in target_ep["videos"]}
                            if url not in existing_urls:
                                target_ep["videos"].append({
                                    "fansub_id": fid,
                                    "player": player,
                                    "url": url,
                                })
                                res_stat["videos_added"][player] += 1
                                res_stat["videos_added"]["TOTAL"] += 1
                        else:
                            # Taşan bölüm -> unassigned_videos
                            if url not in unassigned_urls:
                                unassigned_videos.append({
                                    "source": "animecix",
                                    "raw_season": acx_s_num,
                                    "raw_episode": ep_num,
                                    "player": player,
                                    "url": url,
                                    "fansub_id": fid,
                                })
                                unassigned_urls.add(url)
                                res_stat["unassigned_count"] += 1
                                res_stat["videos_added"][player] += 1
                                res_stat["videos_added"]["TOTAL"] += 1

            tv_doc = {
                "id": tmdb_id,
                "type": "tv",
                "name": details["name"],
                "original_name": details.get("original_name") or "",
                "overview": details.get("overview") or "",
                "poster_path": details.get("poster_path"),
                "backdrop_path": details.get("backdrop_path"),
                "first_air_date": details.get("first_air_date"),
                "status": details.get("status", "Ended"),
                "vote_average": details.get("vote_average", 0.0),
                "vote_count": details.get("vote_count", 0),
                "genres": details.get("genres", []),
                "original_language": details.get("original_language", "ja"),
                "external_ids": details.get("external_ids", {}),
                "seasons": seasons_data,
            }
            if unassigned_videos:
                tv_doc["unassigned_videos"] = unassigned_videos

            if not dry_run:
                with open(target_file, "w", encoding="utf-8") as f:
                    json.dump(tv_doc, f, ensure_ascii=False, indent=2)

            res_stat["file"] = os.path.basename(target_file)
            index_entry = {
                "id": tmdb_id,
                "name": tv_doc["name"],
                "original_name": tv_doc["original_name"],
                "type": "tv",
                "poster_path": tv_doc["poster_path"],
                "backdrop_path": tv_doc["backdrop_path"],
                "vote_average": tv_doc["vote_average"],
                "year": int(tv_doc["first_air_date"][:4]) if tv_doc.get("first_air_date") and tv_doc["first_air_date"][:4].isdigit() else None,
                "genres": tv_doc["genres"],
                "season_count": len([s for s in tv_doc["seasons"] if s.get("season_number", 0) > 0]),
                "mal_ids": [mal_id] if mal_id else [],
                "status": tv_doc["status"],
            }

        # İndekse ekle
        if not dry_run:
            with self._index_lock:
                if tmdb_id not in self.indexed_ids:
                    self.animes_index.append(index_entry)
                    self.indexed_ids.add(tmdb_id)

        with self._stats_lock:
            self.stats["successfully_ingested"] += 1
            if is_movie:
                self.stats["movies_added"] += 1
            else:
                self.stats["series_added"] += 1
            for pl, cnt in res_stat["videos_added"].items():
                self.stats["added_videos"][pl] += cnt
            self.stats["unassigned_videos_count"] += res_stat["unassigned_count"]

        return res_stat

    def run(self, limit: int | None = None, dry_run: bool = False):
        t0 = time.time()
        mode_str = "DRY-RUN (Yazma Kapalı)" if dry_run else "CANLI ÇALIŞTIRMA (Veritabanı Genişletiliyor)"
        logger.info(f"🚀 Yeni Animeleri İçeri Aktarma Motoru Başlatılıyor — {mode_str}")
        logger.info(f"🎯 İzin Verilen Oynatıcılar: {', '.join(sorted(ALLOWED_PLAYERS))}")

        candidates = self.load_candidates()

        # Checkpoint filtreleme
        if not dry_run and self.processed_ids:
            candidates = [c for c in candidates if c["acx_id"] not in self.processed_ids]
            logger.info(f"💾 Checkpoint: {len(self.processed_ids)} aday önceden tamamlanmış. Kalan aday: {len(candidates)}")

        if limit:
            candidates = candidates[:limit]
            logger.info(f"⏱️ Limit uygulandı: İlk {limit} aday işlenecek.")

        self.stats["total_candidates"] = len(candidates)
        total = len(candidates)
        processed = 0

        logger.info(f"⚙️ {self.workers} iş parçacığı ile aktarım başlıyor...")

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_item = {executor.submit(self.ingest_candidate, c, dry_run): c for c in candidates}

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                processed += 1
                try:
                    res = future.result()
                    acx_id = item["acx_id"]
                    if not dry_run and acx_id:
                        with self._stats_lock:
                            self.processed_ids.add(acx_id)

                        if processed % 15 == 0:
                            self._save_checkpoint()
                            self.save_metadata()
                            self.save_animes_index()

                    status = res["status"]
                    if status == "success":
                        v_total = res["videos_added"]["TOTAL"]
                        logger.info(
                            f"[{processed}/{total}] ({processed/total*100:.1f}%) "
                            f"✅ Eklendi: {res['name']} ({res['type'].upper()} - TMDB:{res['tmdb_id']}) -> +{v_total} video "
                            f"(TAU:{res['videos_added']['TAU']}, SIBNET:{res['videos_added']['SIBNET']}, UQLOAD:{res['videos_added']['UQLOAD']})"
                        )
                    elif status == "already_exists":
                        logger.debug(f"[{processed}/{total}] ⏭️ {item.get('name')} (TMDB:{item.get('tmdb_id')}) zaten veritabanında mevcut.")
                    elif status == "tmdb_not_found":
                        logger.warning(f"[{processed}/{total}] ⚠️ {item.get('name')} (TMDB:{item.get('tmdb_id')}) TMDB API'de bulunamadı.")
                except Exception as e:
                    logger.error(f"Aday işleme hatası ({item.get('name')}): {e}")
                    with self._stats_lock:
                        self.stats["errors"] += 1

        # Final kaydetme
        if not dry_run:
            self._save_checkpoint()
            self.save_metadata()
            self.save_animes_index()

        elapsed = time.time() - t0
        logger.info("=" * 60)
        logger.info(f"🏁 İçeri Aktarma Tamamlandı! Süre: {elapsed:.2f} saniye")
        logger.info(f"  Toplam İşlenen Aday: {total}")
        logger.info(f"  Başarıyla Eklenen Yeni Anime: {self.stats['successfully_ingested']}")
        logger.info(f"    - Eklenen Diziler (TV): {self.stats['series_added']}")
        logger.info(f"    - Eklenen Filmler (Movie): {self.stats['movies_added']}")
        logger.info(f"  Zaten Mevcut Olan: {self.stats['already_exists']}")
        logger.info(f"  TMDB'de Bulunamayan: {self.stats['tmdb_not_found']}")
        logger.info(f"  Eklenen Toplam Video: {self.stats['added_videos']['TOTAL']}")
        logger.info(f"    - TAU Video: {self.stats['added_videos']['TAU']}")
        logger.info(f"    - SIBNET: {self.stats['added_videos']['SIBNET']}")
        logger.info(f"    - UQLOAD: {self.stats['added_videos']['UQLOAD']}")
        logger.info(f"  Unassigned Videos (Taşan Bölümler): {self.stats['unassigned_videos_count']}")
        logger.info(f"  Hatalar: {self.stats['errors']}")
        logger.info(f"  Güncel Canlı Anime Dosya Sayısı: {len([f for f in os.listdir(self.anime_dir) if f.endswith('.json')])}")
        logger.info(f"  Güncel animes.json İndeks Eleman Sayısı: {len(self.animes_index)}")
        logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="AnimeciX Yeni Başlıkları İçeri Aktarma Motoru")
    parser.add_argument("--workers", type=int, default=3, help="Thread sayısı (varsayılan: 3)")
    parser.add_argument("--limit", type=int, default=None, help="İşlenecek aday sayısı limiti")
    parser.add_argument("--dry-run", action="store_true", help="Yazma yapmadan simülasyon çalıştır")
    args = parser.parse_args()

    engine = AnimecixIngestEngine(workers=args.workers)
    engine.run(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
