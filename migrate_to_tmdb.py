"""
migrate_to_tmdb.py — TMDB-Centric Veritabanı Taşıma (Migration) Motoru

Mevcut api/anime/*.json (6.020 adet MAL odaklı) dosyalarındaki tüm anime,
sezon, bölüm ve video verilerini TMDB merkezli Unified şemaya dönüştürür.

Eşleme Hiyerarşisi:
1. tmdb_manual_mappings.json (Kullanıcı tanımlı zorunlu eşlemeler)
2. Fribb/anime-lists (Doğrudan TMDB TV/Movie eşlemesi)
3. MAL Relations (Parent Story, Prequel, Sequel vb. üzerinden ana diziye bağlama)

Bölüm & Video Kuralları:
- Kural 1: Tek bölümlü veya filmlerde episode_number null ise episode_number = 1 kabul edilir.
- Kural 4: Numaralandırılamayan bölümler silinmez; o sezonun altındaki 'unassigned_videos'
           havuzunda başlığı ve videolarıyla birlikte korunur.
- Başlıklar doğrudan İngilizce (en-US), özetler Türkçe (fallback en-US) kaydedilir.
"""

import os
import sys
import json
import time
import argparse
import shutil
from concurrent.futures import ThreadPoolExecutor
from logger import setup_logger
from fribb_mapper import FribbMapper
from tmdb_client import TMDBClient

_logger = setup_logger("Migration")


class TMDBMigrator:
    """MAL merkezli veritabanını TMDB merkezli şemaya dönüştüren motor."""

    def __init__(
        self,
        source_dir: str = "api/anime",
        target_dir: str = "api/anime_tmdb",
        cache_dir: str = "data/tmdb_cache",
        manual_mappings_path: str = "tmdb_manual_mappings.json"
    ):
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.cache_dir = cache_dir
        self.manual_mappings_path = manual_mappings_path
        self.logger = _logger

        self.mapper = FribbMapper()
        self.client = TMDBClient()
        self.manual_mappings = self._load_manual_mappings()

        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.target_dir, exist_ok=True)

    def _load_manual_mappings(self) -> dict:
        """tmdb_manual_mappings.json dosyasını yükler."""
        if os.path.exists(self.manual_mappings_path):
            try:
                with open(self.manual_mappings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {str(k): v for k, v in data.items() if not str(k).startswith("_")}
            except Exception as e:
                self.logger.warning(f"tmdb_manual_mappings.json okunamadı: {e}")
        return {}

    def resolve_anime(self, mal_id: int | None, relations: list[dict] | None = None) -> tuple[dict | None, str]:
        """
        Bir MAL ID'yi 3 kademeli hiyerarşi ile TMDB ID'sine çözer:
        1. Manual mappings
        2. Fribb direct mapping
        3. MAL Relations
        """
        if not mal_id:
            return None, "none"

        str_id = str(mal_id)

        # 1. Manual Mappings
        if str_id in self.manual_mappings:
            m = self.manual_mappings[str_id]
            return {
                "tmdb_id": m["tmdb_id"],
                "type": m.get("type", "tv"),
                "season_number": m.get("season_number", 1)
            }, "manual"

        # 2. Fribb Mapping
        info = self.mapper.get_tmdb_info_by_mal(mal_id)
        if info:
            return {
                "tmdb_id": info["tmdb_id"],
                "type": info["type"],
                "season_number": info["season_number"]
            }, "fribb"

        # 3. MAL Relations (İlişkili ana seri üzerinden)
        if relations:
            priority = [
                "Parent Story", "Prequel", "Full Story", "Sequel",
                "Side Story", "Spin-off", "Summary", "Other", "Alternative Version"
            ]
            for p in priority:
                for rel in relations:
                    if rel.get("relation", "").strip().lower() == p.lower():
                        for entry_id in rel.get("entries", []):
                            p_info = self.mapper.get_tmdb_info_by_mal(entry_id)
                            if not p_info and str(entry_id) in self.manual_mappings:
                                p_info = self.manual_mappings[str(entry_id)]
                            if p_info:
                                return {
                                    "tmdb_id": p_info["tmdb_id"],
                                    "type": p_info.get("type", "tv"),
                                    "season_number": 0, # Specials / İlişkili yan yapım
                                    "relation_type": p,
                                    "related_mal_id": entry_id
                                }, "relations"

        return None, "none"

    def scan_and_group(self) -> tuple[dict[int, dict], list[dict]]:
        """
        api/anime/ klasöründeki tüm dosyaları tarar ve TMDB ID'lerine göre gruplar.
        """
        self.logger.info("Tüm anime dosyaları taranıyor ve TMDB ID'lerine göre gruplanıyor...")

        files = [f for f in os.listdir(self.source_dir) if f.endswith(".json")]
        tmdb_groups: dict[int, dict] = {}
        unmatched_list: list[dict] = []

        def parse_file(fname):
            fpath = os.path.join(self.source_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except Exception:
                    return None
            
            name_no_ext = fname[:-5]
            mal_id = int(name_no_ext) if name_no_ext.isdigit() else None
            if not mal_id and data.get("jikan"):
                mal_id = data["jikan"].get("mal_id")

            relations = (data.get("jikan") or {}).get("relations") or []
            res_info, method = self.resolve_anime(mal_id, relations)

            return {
                "filename": fname,
                "mal_id": mal_id,
                "title": (data.get("turkanime") or {}).get("isim") or (data.get("jikan") or {}).get("title") or name_no_ext,
                "slug": (data.get("turkanime") or {}).get("slug") or "",
                "episodes": data.get("episodes") or [],
                "res_info": res_info,
                "method": method
            }

        with ThreadPoolExecutor(max_workers=32) as executor:
            parsed_items = [item for item in executor.map(parse_file, files) if item]

        for item in parsed_items:
            res = item["res_info"]
            if res:
                tid = res["tmdb_id"]
                mtype = res["type"]
                s_num = res["season_number"]
                key = (mtype, tid)

                if key not in tmdb_groups:
                    tmdb_groups[key] = {
                        "tmdb_id": tid,
                        "type": mtype,
                        "seasons_data": {}
                    }

                if s_num not in tmdb_groups[key]["seasons_data"]:
                    tmdb_groups[key]["seasons_data"][s_num] = []

                tmdb_groups[key]["seasons_data"][s_num].append({
                    "mal_id": item["mal_id"],
                    "filename": item["filename"],
                    "title": item["title"],
                    "episodes": item["episodes"],
                    "method": item["method"],
                    "fribb_season": s_num
                })
            else:
                unmatched_list.append({
                    "filename": item["filename"],
                    "mal_id": item["mal_id"],
                    "title": item["title"],
                    "slug": item["slug"],
                    "episode_count": len(item["episodes"]),
                    "video_count": sum(len(ep.get("videos") or []) for ep in item["episodes"])
                })

        self.logger.info(
            f"✅ Tarama tamamlandı: {len(parsed_items)} dosyadan {len(tmdb_groups)} tekil TMDB serisi oluşturuldu. "
            f"Eşleşmeyen: {len(unmatched_list)} anime."
        )
        return tmdb_groups, unmatched_list

    def _get_cached_tmdb_details(self, tmdb_id: int, is_movie: bool) -> dict | None:
        """TMDB detaylarını yerel önbellekten veya API'den çeker."""
        cache_path = os.path.join(self.cache_dir, f"details_{tmdb_id}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        details = self.client.get_details(tmdb_id, is_movie=is_movie)
        if details:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(details, f, ensure_ascii=False, indent=2)
        return details

    def _get_cached_season_details(self, tmdb_id: int, season_number: int) -> dict | None:
        """Sezon detaylarını yerel önbellekten veya API'den çeker."""
        cache_path = os.path.join(self.cache_dir, f"season_{tmdb_id}_{season_number}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        s_details = self.client.get_season_details(tmdb_id, season_number)
        if s_details:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(s_details, f, ensure_ascii=False, indent=2)
        return s_details

    def build_unified_document(self, tmdb_id: int, group_info: dict) -> dict | None:
        """
        Bir TMDB ID'si için eski video linklerini birleştirerek nihai Unified JSON dokümanını oluşturur.
        """
        is_movie = (group_info["type"] == "movie")
        details = self._get_cached_tmdb_details(tmdb_id, is_movie=is_movie)
        if not details:
            return None

        result = {
            "id": details["id"],
            "type": details["type"],
            "name": details["name"],
            "original_name": details["original_name"],
            "overview": details["overview"],
            "poster_path": details["poster_path"],
            "backdrop_path": details["backdrop_path"],
            "first_air_date": details["first_air_date"],
            "status": details["status"],
            "vote_average": details["vote_average"],
            "vote_count": details["vote_count"],
            "genres": details["genres"],
            "original_language": details["original_language"],
            "external_ids": details["external_ids"],
            "seasons": []
        }

        # ── MOVIE (Film) Yapısı ──
        if is_movie:
            result["runtime"] = details.get("runtime", 0)
            
            # Tüm eşleşen dosyalardaki videoları topla
            videos = []
            unassigned_videos = []
            skip_times = None
            mal_id = None

            for s_files in group_info["seasons_data"].values():
                for f_info in s_files:
                    if not mal_id:
                        mal_id = f_info["mal_id"]
                    
                    old_eps = f_info["episodes"]
                    # Kural 1: Tek bölüm varsa ve episode_number None ise -> 1 kabul et
                    if len(old_eps) == 1:
                        ep0 = old_eps[0]
                        videos.extend(ep0.get("videos") or [])
                        if not skip_times:
                            skip_times = ep0.get("skip_times")
                    else:
                        for ep in old_eps:
                            ep_num = ep.get("episode_number")
                            if ep_num == 1:
                                videos.extend(ep.get("videos") or [])
                                if not skip_times:
                                    skip_times = ep.get("skip_times")
                            else:
                                # Kural 4: unassigned_videos
                                unassigned_videos.append({
                                    "title": ep.get("turkanime_title") or f"Episode {ep_num}",
                                    "videos": ep.get("videos") or []
                                })

            anilist_id = self.mapper.get_anilist_id(tmdb_id, is_movie=True)
            if not mal_id:
                mal_id = self.mapper.get_mal_id(tmdb_id, is_movie=True)

            # Filmlerde seasons yapısı yerine doğrudan kök düzeyde tut
            del result["seasons"]
            result["mal_id"] = mal_id
            result["anilist_id"] = anilist_id
            result["skip_times"] = skip_times
            result["videos"] = videos
            if unassigned_videos:
                result["unassigned_videos"] = unassigned_videos

            return result

        # ── TV SERIES Yapısı ──
        # Tüm normal TV dosyalarını topla (fribb_season > 0)
        tv_files = []
        for s_num, files in group_info["seasons_data"].items():
            if s_num > 0:
                for f in files:
                    tv_files.append((s_num, f))
        tv_files.sort(key=lambda x: (x[0], x[1]["mal_id"]))
        tv_files_only = [x[1] for x in tv_files]

        # 1. Episode Groups (Bölüm Grupları) var mı kontrol et
        raw_seasons = self.client.get_episode_group_seasons(tmdb_id)
        reg_groups = [g for g in raw_seasons if g.get("season_number", 0) > 0 and len(g.get("episodes", [])) > 0] if raw_seasons else []

        # ── Sezon 0 (Specials) Ekleme ──
        s0_files = group_info["seasons_data"].get(0, [])
        if s0_files:
            s0_details = self._get_cached_season_details(tmdb_id, 0)
            if s0_details:
                s0_map = {}
                s0_unassigned = []
                s0_mal_id = s0_files[0]["mal_id"] if s0_files else None

                for f_info in s0_files:
                    for ep in f_info.get("episodes", []):
                        ep_num = ep.get("episode_number")
                        if ep_num is not None:
                            if ep_num not in s0_map:
                                s0_map[ep_num] = []
                            s0_map[ep_num].append(ep)
                        else:
                            s0_unassigned.append({
                                "title": ep.get("turkanime_title") or f_info.get("title") or "OVA",
                                "videos": ep.get("videos") or []
                            })

                s0_episodes = []
                s0_tmdb_nums = set()
                for ep in s0_details.get("episodes", []):
                    ep_num = ep["episode_number"]
                    s0_tmdb_nums.add(ep_num)
                    merged_v = []
                    skip_t = None
                    for old_ep in s0_map.get(ep_num, []):
                        merged_v.extend(old_ep.get("videos") or [])
                        if not skip_t and old_ep.get("skip_times"):
                            skip_t = old_ep.get("skip_times")
                    s0_episodes.append({
                        "episode_number": ep_num,
                        "name": ep["name"],
                        "overview": ep.get("overview", ""),
                        "still_path": ep.get("still_path"),
                        "air_date": ep.get("air_date"),
                        "skip_times": skip_t,
                        "videos": merged_v
                    })

                for old_num, old_eps in s0_map.items():
                    if old_num not in s0_tmdb_nums:
                        for old_ep in old_eps:
                            s0_unassigned.append({
                                "title": old_ep.get("turkanime_title") or f"Special {old_num}",
                                "videos": old_ep.get("videos") or []
                            })

                s0_anilist_id = self.mapper.get_anilist_id(tmdb_id, season_number=0)
                result["seasons"].append({
                    "season_number": 0,
                    "name": s0_details.get("name") or "Specials",
                    "overview": s0_details.get("overview") or "",
                    "poster_path": s0_details.get("poster_path"),
                    "air_date": s0_details.get("air_date"),
                    "episode_count": len(s0_episodes),
                    "mal_id": s0_mal_id,
                    "anilist_id": s0_anilist_id,
                    "episodes": s0_episodes,
                    "unassigned_videos": s0_unassigned
                })

        # ── Senaryo A: Episode Groups ile Çok Sezonlu Eşleşme (AOT, SAO, JJK vb.) ──
        if reg_groups and len(reg_groups) > 1 and len(tv_files_only) > 1:
            matched_group_files = {}
            unmatched_files = list(tv_files_only)
            unmatched_groups = list(reg_groups)

            # 1. Tam bölüm sayısı eşleşmesi
            for g in list(unmatched_groups):
                g_cnt = len(g.get("episodes", []))
                for f in list(unmatched_files):
                    f_cnt = len([e for e in f.get("episodes", []) if e.get("episode_number") is not None])
                    if f_cnt == g_cnt:
                        matched_group_files[g["season_number"]] = [f]
                        unmatched_groups.remove(g)
                        unmatched_files.remove(f)
                        break

            # 2. Kalanları sırayla eşle
            for g, f in zip(unmatched_groups, unmatched_files):
                matched_group_files[g["season_number"]] = [f]

            for g in reg_groups:
                s_num = g["season_number"]
                s_files = matched_group_files.get(s_num, [])

                old_episodes_map = {}
                unassigned_videos = []
                season_mal_id = s_files[0]["mal_id"] if s_files else None

                for f_info in s_files:
                    old_eps = f_info.get("episodes", [])
                    if len(old_eps) == 1 and old_eps[0].get("episode_number") is None:
                        old_eps[0]["episode_number"] = 1

                    for ep in old_eps:
                        ep_num = ep.get("episode_number")
                        if ep_num is not None:
                            if ep_num not in old_episodes_map:
                                old_episodes_map[ep_num] = []
                            old_episodes_map[ep_num].append(ep)
                        else:
                            unassigned_videos.append({
                                "title": ep.get("turkanime_title") or "Bilinmeyen Bölüm",
                                "videos": ep.get("videos") or []
                            })

                if not season_mal_id:
                    season_mal_id = self.mapper.get_mal_id(tmdb_id, season_number=s_num)
                season_anilist_id = self.mapper.get_anilist_id(tmdb_id, season_number=s_num)

                episodes = []
                tmdb_episodes = g.get("episodes") or []
                tmdb_ep_nums = set()

                for ep in tmdb_episodes:
                    ep_num = ep["episode_number"]
                    tmdb_ep_nums.add(ep_num)
                    merged_videos = []
                    skip_times = None

                    for old_ep in old_episodes_map.get(ep_num, []):
                        merged_videos.extend(old_ep.get("videos") or [])
                        if not skip_times and old_ep.get("skip_times"):
                            skip_times = old_ep.get("skip_times")

                    episodes.append({
                        "episode_number": ep_num,
                        "name": ep["name"],
                        "overview": ep.get("overview", ""),
                        "still_path": ep.get("still_path"),
                        "air_date": ep.get("air_date"),
                        "skip_times": skip_times,
                        "videos": merged_videos
                    })

                for old_num, old_eps_list in old_episodes_map.items():
                    if old_num not in tmdb_ep_nums:
                        for old_ep in old_eps_list:
                            unassigned_videos.append({
                                "title": old_ep.get("turkanime_title") or f"Bölüm {old_num}",
                                "videos": old_ep.get("videos") or []
                            })

                season_obj = {
                    "season_number": s_num,
                    "name": g.get("name") or f"Season {s_num}",
                    "overview": g.get("overview") or "",
                    "poster_path": g.get("poster_path"),
                    "air_date": g.get("air_date"),
                    "episode_count": len(episodes),
                    "mal_id": season_mal_id,
                    "anilist_id": season_anilist_id,
                    "episodes": episodes
                }
                if unassigned_videos:
                    season_obj["unassigned_videos"] = unassigned_videos

                result["seasons"].append(season_obj)

        # ── Senaryo B: Standart Sezonlar & Çok Parçalı Sezonlarda Kümülatif Öteleme ──
        else:
            for s_sum in details.get("seasons_summary", []):
                s_num = s_sum["season_number"]
                if s_num == 0:
                    continue

                s_det = self._get_cached_season_details(tmdb_id, s_num)
                if not s_det:
                    continue

                s_files = group_info["seasons_data"].get(s_num, [])
                s_files.sort(key=lambda x: x["mal_id"])

                old_episodes_map = {}
                unassigned_videos = []
                offset = 0
                season_mal_id = s_files[0]["mal_id"] if s_files else self.mapper.get_mal_id(tmdb_id, season_number=s_num)
                season_anilist_id = self.mapper.get_anilist_id(tmdb_id, season_number=s_num)

                for f_info in s_files:
                    old_eps = f_info.get("episodes", [])
                    if len(old_eps) == 1 and old_eps[0].get("episode_number") is None:
                        old_eps[0]["episode_number"] = 1

                    for ep in old_eps:
                        ep_num = ep.get("episode_number")
                        if ep_num is not None:
                            target_num = ep_num + offset
                            if target_num not in old_episodes_map:
                                old_episodes_map[target_num] = []
                            old_episodes_map[target_num].append(ep)
                        else:
                            unassigned_videos.append({
                                "title": ep.get("turkanime_title") or "Bilinmeyen Bölüm",
                                "videos": ep.get("videos") or []
                            })

                    valid_eps = [e for e in old_eps if e.get("episode_number") is not None]
                    if len(s_files) > 1:
                        offset += len(valid_eps)

                episodes = []
                tmdb_episodes = s_det.get("episodes") or []
                tmdb_ep_nums = set()

                for ep in tmdb_episodes:
                    ep_num = ep["episode_number"]
                    tmdb_ep_nums.add(ep_num)
                    merged_videos = []
                    skip_times = None

                    for old_ep in old_episodes_map.get(ep_num, []):
                        merged_videos.extend(old_ep.get("videos") or [])
                        if not skip_times and old_ep.get("skip_times"):
                            skip_times = old_ep.get("skip_times")

                    episodes.append({
                        "episode_number": ep_num,
                        "name": ep["name"],
                        "overview": ep.get("overview", ""),
                        "still_path": ep.get("still_path"),
                        "air_date": ep.get("air_date"),
                        "skip_times": skip_times,
                        "videos": merged_videos
                    })

                for old_num, old_eps_list in old_episodes_map.items():
                    if old_num not in tmdb_ep_nums:
                        for old_ep in old_eps_list:
                            unassigned_videos.append({
                                "title": old_ep.get("turkanime_title") or f"Bölüm {old_num}",
                                "videos": old_ep.get("videos") or []
                            })

                season_obj = {
                    "season_number": s_num,
                    "name": s_sum.get("name") or f"Season {s_num}",
                    "overview": s_det.get("overview") or "",
                    "poster_path": s_det.get("poster_path") or s_sum.get("poster_path"),
                    "air_date": s_det.get("air_date"),
                    "episode_count": len(episodes),
                    "mal_id": season_mal_id,
                    "anilist_id": season_anilist_id,
                    "episodes": episodes
                }
                if unassigned_videos:
                    season_obj["unassigned_videos"] = unassigned_videos

                result["seasons"].append(season_obj)

        return result

    def run_migration(self, limit: int | None = None, apply_changes: bool = False, workers: int = 8):
        """Tüm migration sürecini baştan sona yürütür."""
        t0 = time.time()
        self.logger.info("=" * 60)
        self.logger.info("🚀 TMDB-Centric Veritabanı Taşıma İşlemi Başlıyor...")
        self.logger.info("=" * 60)

        # 1. Tara ve grupla
        tmdb_groups, unmatched_list = self.scan_and_group()

        # Eşleşmeyenleri kaydet
        unmatched_report_path = "data/unmatched_migration.json"
        with open(unmatched_report_path, "w", encoding="utf-8") as f:
            json.dump(unmatched_list, f, ensure_ascii=False, indent=2)
        self.logger.info(f"💾 Eşleşmeyen {len(unmatched_list)} anime '{unmatched_report_path}' dosyasına kaydedildi.")

        tv_ids = {g["tmdb_id"] for g in tmdb_groups.values() if g["type"] == "tv"}
        group_items = list(tmdb_groups.items())
        if limit:
            group_items = group_items[:limit]
            self.logger.info(f"⚠️ Limit uygulandı: Yalnızca ilk {limit} seri işlenecek.")

        total_groups = len(group_items)
        success_count = 0
        total_migrated_videos = 0
        catalog_index = []

        self.logger.info(f"🔄 {total_groups} adet TMDB serisi inşa ediliyor (İş parçacığı sayısı: {workers})...")

        def process_group(item):
            (mtype, tmdb_id), group_info = item
            try:
                doc = self.build_unified_document(tmdb_id, group_info)
                if not doc:
                    return None

                is_collision = (mtype == "movie" and tmdb_id in tv_ids)
                out_filename = f"m_{tmdb_id}.json" if is_collision else f"{tmdb_id}.json"
                cat_id = f"m{tmdb_id}" if is_collision else tmdb_id
                doc["id"] = cat_id

                out_path = os.path.join(self.target_dir, out_filename)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=2)

                if mtype == "movie":
                    doc_videos = len(doc.get("videos", [])) + sum(len(u.get("videos", [])) for u in doc.get("unassigned_videos", []))
                    all_mal_ids = [doc["mal_id"]] if doc.get("mal_id") else []
                    season_count = 0
                else:
                    doc_videos = sum(
                        sum(len(ep.get("videos", [])) for ep in s.get("episodes", []))
                        + sum(len(u.get("videos", [])) for u in s.get("unassigned_videos", []))
                        for s in doc.get("seasons", [])
                    )
                    all_mal_ids = [s["mal_id"] for s in doc.get("seasons", []) if s.get("mal_id")]
                    season_count = len(doc.get("seasons", []))

                year = None
                fad = doc.get("first_air_date")
                if fad and "-" in fad:
                    try:
                        year = int(fad.split("-")[0])
                    except Exception:
                        pass

                cat_entry = {
                    "id": cat_id,
                    "name": doc["name"],
                    "original_name": doc["original_name"],
                    "type": doc["type"],
                    "poster_path": doc["poster_path"],
                    "backdrop_path": doc["backdrop_path"],
                    "vote_average": doc["vote_average"],
                    "year": year,
                    "genres": doc["genres"],
                    "season_count": season_count,
                    "mal_ids": sorted(list(set(all_mal_ids)))
                }
                return (cat_id, doc_videos, cat_entry)
            except Exception as e:
                self.logger.error(f"Hata (TMDB {mtype.upper()} ID {tmdb_id}): {e}")
                return None

        from concurrent.futures import as_completed

        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_item = {executor.submit(process_group, item): item for item in group_items}
            for future in as_completed(future_to_item):
                completed += 1
                res = future.result()
                if res:
                    _, doc_videos, cat_entry = res
                    success_count += 1
                    total_migrated_videos += doc_videos
                    catalog_index.append(cat_entry)

                if completed % 50 == 0 or completed == total_groups:
                    self.logger.info(f"İlerleme: {completed}/{total_groups} seri tamamlandı. (%{completed/total_groups*100:.1f})")

        # Vitrin indeksi sırala ve kaydet (animes.json)
        catalog_index.sort(key=lambda x: x["name"])

        # Vitrin indeksi kaydet (animes.json)
        index_path = os.path.join(os.path.dirname(self.target_dir), "animes_tmdb.json")
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(catalog_index, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - t0
        self.logger.info("=" * 60)
        self.logger.info("🎉 MIGRATION İŞLEMİ TAMAMLANDI!")
        self.logger.info("=" * 60)
        self.logger.info(f"Üretilen TMDB Dosyası  : {success_count}")
        self.logger.info(f"Aktarılan Toplam Video: {total_migrated_videos:,}")
        self.logger.info(f"Hedef Klasör          : {self.target_dir}")
        self.logger.info(f"Vitrin İndeksi        : {index_path}")
        self.logger.info(f"Geçen Süre            : {elapsed:.2f} saniye")
        self.logger.info("=" * 60)

        # Canlıya alma (Apply) adımı
        if apply_changes:
            self.logger.info("🚀 Değişiklikler canlıya alınıyor (Apply)...")
            backup_dir = "api/anime_backup_mal"
            if not os.path.exists(backup_dir):
                self.logger.info(f"Eski 'api/anime' -> '{backup_dir}' olarak yedekleniyor...")
                shutil.move(self.source_dir, backup_dir)
            else:
                self.logger.info(f"Mevcut '{backup_dir}' yedeği zaten mevcut.")
                shutil.rmtree(self.source_dir)

            self.logger.info(f"Yeni '{self.target_dir}' -> '{self.source_dir}' olarak taşınıyor...")
            shutil.move(self.target_dir, self.source_dir)

            # animes.json güncelle
            live_index_path = "api/animes.json"
            if os.path.exists(index_path):
                shutil.move(index_path, live_index_path)
                self.logger.info(f"✅ 'api/animes.json' vitrin dosyası güncellendi.")

            self.logger.info("✅ Canlıya alma başarıyla tamamlandı!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TMDB Migration Betiği")
    parser.add_argument("--limit", type=int, default=None, help="Yalnızca belirtilen sayıda seriyi işle")
    parser.add_argument("--workers", type=int, default=8, help="Eşzamanlı iş parçacığı sayısı (varsayılan: 8)")
    parser.add_argument("--apply", action="store_true", help="İşlem bitince api/anime klasörünü yeni TMDB verileriyle değiştir")
    args = parser.parse_args()

    migrator = TMDBMigrator()
    migrator.run_migration(limit=args.limit, apply_changes=args.apply, workers=args.workers)
