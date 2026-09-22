"""
backfill_animecix_videos.py — AnimeciX Video Aktarım Motoru

AnimeciX arşivindeki (~3.077 başlık) video kaynaklarını yerel
TMDB-merkezli veritabanımıza (api/anime/*.json) aktarır.

Kural:
    - YALNIZCA şu oynatıcılar kabul edilir: TAU, SIBNET, UQLOAD.
    - Diğer tüm oynatıcılar (Vidmoly, SendVid, Mega vb.) görmezden gelinir / atlanır.
    - Mükerrerlik koruması: Aynı URL zaten varsa tekrar eklenmez.
    - Eşleşmeyen / taşan bölümler unassigned_videos altına güvenle alınır.
"""

import os
import sys
import json
import time
import argparse
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

from animecix_provider import AnimecixProvider
from fribb_mapper import FribbMapper
from logger import setup_logger

sys.stdout.reconfigure(encoding="utf-8")
logger = setup_logger("AnimecixBackfill")

# İzin verilen oynatıcılar (Kullanıcı talebi doğrultusunda kesin kural)
ALLOWED_PLAYERS = {"TAU", "SIBNET", "UQLOAD"}


class AnimecixBackfillEngine:
    def __init__(
        self,
        base_dir: str = "api",
        catalog_cache_path: str = "data/animecix_catalog.json",
        report_path: str = "data/animecix_backfill_report.json",
        checkpoint_path: str = "data/animecix_processed_ids.json",
        workers: int = 3,
    ):
        self.base_dir = base_dir
        self.anime_dir = os.path.join(base_dir, "anime")
        self.catalog_cache_path = catalog_cache_path
        self.report_path = report_path
        self.checkpoint_path = checkpoint_path
        self.metadata_path = os.path.join(base_dir, "metadata.json")
        self.animes_json_path = os.path.join(base_dir, "animes.json")
        self.workers = workers

        self.provider = AnimecixProvider()
        self.fribb = FribbMapper()

        # Thread-safe kilitler
        self._meta_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._file_locks = defaultdict(threading.Lock)

        # Checkpoint yükle
        self.processed_ids = self._load_checkpoint()

        # Metadata yükle
        self.metadata = self._load_metadata()
        self.fansubs = self.metadata.get("fansubs", {})
        self.name_to_fansub_id = {v["name"].lower(): int(k) for k, v in self.fansubs.items()}
        self.max_fansub_id = max(int(k) for k in self.fansubs.keys()) if self.fansubs else 0

        # İndeksler
        self.tmdb_to_file = {}
        self.mal_to_file = {}
        self._build_db_lookups()

        # İstatistikler
        self.stats = {
            "total_processed": 0,
            "matched_anime": 0,
            "unmatched_anime": 0,
            "skipped_other_players": 0,
            "added_videos": {
                "TAU": 0,
                "SIBNET": 0,
                "UQLOAD": 0,
                "TOTAL": 0,
            },
            "skipped_existing_videos": 0,
            "unassigned_videos_count": 0,
            "files_modified": 0,
            "unmatched_list": [],
        }

    def _load_checkpoint(self) -> set[int]:
        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info(f"📂 Checkpoint dosyası yüklendi: {len(data)} başlık zaten işlenmiş.")
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
                logger.info("💾 metadata.json kaydedildi.")
            except Exception as e:
                logger.error(f"metadata.json kaydedilemedi: {e}")

    def get_or_create_fansub_id(self, raw_extra: str | None) -> int:
        if not raw_extra:
            return 3  # Varsayılan
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

    def _build_db_lookups(self):
        """api/animes.json ve api/anime/*.json dosyalarından hızlı arama haritası kurar."""
        logger.info("🔍 Yerel veritabanı indeksleri kuruluyor...")
        if not os.path.exists(self.animes_json_path):
            logger.error(f"animes.json bulunamadı: {self.animes_json_path}")
            return

        with open(self.animes_json_path, "r", encoding="utf-8") as f:
            catalog = json.load(f)

        for entry in catalog:
            raw_id = entry.get("id")
            if raw_id is None:
                continue

            # Dosya adı tespiti: "m60843" -> "m_60843.json" veya 76075 -> "76075.json"
            str_id = str(raw_id)
            if str_id.startswith("m") and str_id[1:].isdigit():
                fn = f"m_{str_id[1:]}.json"
                int_id = int(str_id[1:])
            else:
                fn = f"{str_id}.json"
                int_id = int(str_id) if str_id.isdigit() else None

            fp = os.path.join(self.anime_dir, fn)
            if os.path.exists(fp):
                if int_id is not None:
                    self.tmdb_to_file[int_id] = fp
                self.tmdb_to_file[str_id] = fp

                # MAL ID'leri de haritala
                for mid in entry.get("mal_ids", []):
                    if isinstance(mid, int):
                        self.mal_to_file[mid] = fp

        logger.info(f"✅ İndeks hazır: {len(self.tmdb_to_file)} TMDB ID, {len(self.mal_to_file)} MAL ID haritalandı.")

    def fetch_or_load_catalog(self, force_refresh: bool = False) -> list[dict]:
        """AnimeciX'teki tüm başlıkları (32 sayfa, ~3.077 başlık) çeker veya önbellekten okur."""
        os.makedirs(os.path.dirname(self.catalog_cache_path), exist_ok=True)

        if not force_refresh and os.path.exists(self.catalog_cache_path):
            try:
                with open(self.catalog_cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"📂 AnimeciX kataloğu önbellekten yüklendi ({len(data)} başlık).")
                return data
            except Exception as e:
                logger.warning(f"Önbellek okunamadı, yeniden çekiliyor: {e}")

        logger.info("📡 AnimeciX kataloğu indiriliyor (tüm sayfalar taranıyor)...")
        all_titles = []
        page = 1
        per_page = 100

        while True:
            logger.info(f"  Sayfa {page} çekiliyor...")
            res = self.provider._get("/secure/titles", params={"page": page, "perPage": per_page})
            if not isinstance(res, dict):
                break

            pagination = res.get("pagination", {})
            titles = pagination.get("data", [])
            if not titles:
                break

            all_titles.extend(titles)
            current_page = pagination.get("current_page", page)
            last_page = pagination.get("last_page", page)

            if current_page >= last_page:
                break
            page += 1
            time.sleep(0.1)

        logger.info(f"✅ Toplam {len(all_titles)} AnimeciX başlığı indirildi.")
        try:
            with open(self.catalog_cache_path, "w", encoding="utf-8") as f:
                json.dump(all_titles, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Katalog önbelleğe kaydedildi: {self.catalog_cache_path}")
        except Exception as e:
            logger.warning(f"Katalog kaydedilemedi: {e}")

        return all_titles

    def find_target_db_file(self, acx_item: dict) -> str | None:
        """AnimeciX başlığını yerel veritabanımızdaki dosya yoluyla eşleştirir."""
        # 1. TMDB ID
        tmdb_id = acx_item.get("tmdb_id")
        if tmdb_id and isinstance(tmdb_id, (int, str)):
            try:
                t_int = int(tmdb_id)
                if t_int in self.tmdb_to_file:
                    return self.tmdb_to_file[t_int]
            except ValueError:
                pass
            if str(tmdb_id) in self.tmdb_to_file:
                return self.tmdb_to_file[str(tmdb_id)]

        # 2. MAL ID
        mal_id = acx_item.get("mal_id")
        if mal_id and isinstance(mal_id, (int, str)):
            try:
                m_int = int(mal_id)
                if m_int in self.mal_to_file:
                    return self.mal_to_file[m_int]
                # Fribb üzerinden dene
                m_info = self.fribb.get_tmdb_info_by_mal(m_int)
                if m_info:
                    mapped_tmdb = m_info.get("tmdb_id")
                    if mapped_tmdb in self.tmdb_to_file:
                        return self.tmdb_to_file[mapped_tmdb]
                    if str(mapped_tmdb) in self.tmdb_to_file:
                        return self.tmdb_to_file[str(mapped_tmdb)]
            except (ValueError, TypeError):
                pass

        return None

    def build_season_mapping(self, db_seasons: list[dict], acx_seasons: list[dict]) -> dict[int, list[dict]]:
        """
        AnimeciX sezon numaraları (1, 2, 3...) ile yerel DB sezon nesnelerini haritalar.
        
        Örnekler:
        - AoT:
            ACX 1 -> [DB Season 1]
            ACX 2 -> [DB Season 2]
            ACX 3 -> [DB Season 3 Part 1, DB Season 3 Part 2]
            ACX 4 -> [DB Final Season Part 1, DB Final Season Part 2, DB Final Season THE FINAL CHAPTERS]
        - 86:
            ACX 1 -> [DB Season 1, DB Season 2]
        - Mushoku Tensei:
            ACX 1 -> [DB Season 1 Part 1, DB Season 1 Part 2]
            ACX 2 -> [DB Season 2 Part 1, DB Season 2 Part 2]
        """
        mapping = defaultdict(list)

        active_db = [s for s in db_seasons if s.get("season_number", 0) != 0]
        active_acx = [s for s in acx_seasons if s.get("number", 0) != 0]

        if not active_db or not active_acx:
            return dict(mapping)

        def parse_key(name: str):
            if not name:
                return None
            if re.search(r'\bfinal\s*season\b|\bson\s*sezon\b', name, re.IGNORECASE):
                return "final"
            m = re.search(r'\b(?:season|sezon|s)\s*(\d+)\b', name, re.IGNORECASE)
            if m:
                return int(m.group(1))
            m = re.search(r'\b(\d+)(?:st|nd|rd|th)\s*season\b', name, re.IGNORECASE)
            if m:
                return int(m.group(1))
            m = re.search(r'\b(\d+)\.\s*sezon\b', name, re.IGNORECASE)
            if m:
                return int(m.group(1))
            return None

        acx_nums = sorted([s.get("number") for s in active_acx if s.get("number") is not None])
        max_acx_num = max(acx_nums) if acx_nums else 1

        # 1) Özel Durum: ACX tek sezon ama DB birden fazla sezon (örn: 86 Eighty-Six, ACX=23 ep, DB=11+12 ep)
        if len(active_acx) == 1 and len(active_db) > 1:
            acx_s = active_acx[0]
            acx_ep_count = acx_s.get("episode_count") or len(acx_s.get("videos", [])) or 0
            s1_db_cap = len(active_db[0].get("episodes", []))
            total_db_cap = sum(len(s.get("episodes", [])) for s in active_db)

            if acx_ep_count > s1_db_cap and acx_ep_count <= total_db_cap + 5:
                for s in active_db:
                    mapping[acx_s.get("number", 1)].append(s)
                return dict(mapping)

        # 2) DB sezon isimlerinden semantik numaraları çıkaralım
        db_parsed = []
        has_semantic_keys = False
        for s in active_db:
            key = parse_key(s.get("name", ""))
            if key is not None:
                has_semantic_keys = True
            db_parsed.append((s, key))

        if has_semantic_keys and len(active_db) > len(active_acx):
            for s, key in db_parsed:
                if key == "final":
                    target_num = max_acx_num
                elif isinstance(key, int):
                    target_num = key
                else:
                    s_num = s.get("season_number")
                    if s_num in acx_nums:
                        target_num = s_num
                    else:
                        target_num = None

                if target_num is not None and target_num in acx_nums:
                    mapping[target_num].append(s)

            if any(mapping.values()):
                return dict(mapping)

        # 3) Birebir (1:1) veya Sezon Numarasına Göre Eşleşme
        db_by_num = {s.get("season_number"): s for s in active_db}
        matched_exact = True
        for acx_s in active_acx:
            num = acx_s.get("number")
            if num in db_by_num:
                mapping[num].append(db_by_num[num])
            else:
                matched_exact = False

        if matched_exact and len(mapping) == len(active_acx):
            return dict(mapping)

        # 4) Fallback: Sıralı (Sequential) Haritalama
        mapping.clear()
        for i, acx_s in enumerate(active_acx):
            num = acx_s.get("number", i + 1)
            if i < len(active_db):
                mapping[num].append(active_db[i])

        return dict(mapping)

    def process_title(self, acx_item: dict, dry_run: bool = False) -> dict:
        """Tek bir AnimeciX başlığını işleyip videolarını yerel dosyaya aktarır."""
        acx_id = acx_item.get("id")
        acx_name = acx_item.get("name", "Bilinmeyen")

        res_stat = {
            "acx_id": acx_id,
            "name": acx_name,
            "matched": False,
            "added_videos": {"TAU": 0, "SIBNET": 0, "UQLOAD": 0, "TOTAL": 0},
            "skipped_existing": 0,
            "skipped_other_players": 0,
            "unassigned_count": 0,
            "file": None,
        }

        target_file = self.find_target_db_file(acx_item)
        if not target_file or not os.path.exists(target_file):
            with self._stats_lock:
                self.stats["unmatched_anime"] += 1
                self.stats["unmatched_list"].append({
                    "acx_id": acx_id,
                    "name": acx_name,
                    "tmdb_id": acx_item.get("tmdb_id"),
                    "mal_id": acx_item.get("mal_id"),
                })
            return res_stat

        res_stat["matched"] = True
        res_stat["file"] = os.path.basename(target_file)

        # Dosyayı thread-safe aç ve oku
        file_lock = self._file_locks[target_file]
        with file_lock:
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    db_data = json.load(f)
            except Exception as e:
                logger.error(f"Dosya okunamadı ({target_file}): {e}")
                return res_stat

            is_movie = db_data.get("type") == "movie" or not acx_item.get("is_series", True)

            # ─────────────────────────────────────────────────────────────
            # A) FİLM İŞLEME
            # ─────────────────────────────────────────────────────────────
            if is_movie:
                vids = self.provider.fetch_episode_videos(acx_id, season_num=1, episode_num=1)
                if "videos" not in db_data or db_data["videos"] is None:
                    db_data["videos"] = []

                existing_urls = {v.get("url") for v in db_data["videos"]}
                file_changed = False

                for v in vids:
                    url = v.get("url")
                    if not url or "youtube.com" in url or "fragman" in (v.get("name") or "").lower():
                        continue

                    player = self.provider.normalize_player(v.get("name"), url)

                    # KESİN KURAL: Sadece TAU, SIBNET, UQLOAD kabul et
                    if player not in ALLOWED_PLAYERS:
                        res_stat["skipped_other_players"] += 1
                        continue

                    if url in existing_urls:
                        res_stat["skipped_existing"] += 1
                        continue

                    fid = self.get_or_create_fansub_id(v.get("extra"))
                    db_data["videos"].append({
                        "fansub_id": fid,
                        "player": player,
                        "url": url,
                    })
                    existing_urls.add(url)
                    res_stat["added_videos"][player] += 1
                    res_stat["added_videos"]["TOTAL"] += 1
                    file_changed = True

                if file_changed and not dry_run:
                    with open(target_file, "w", encoding="utf-8") as f:
                        json.dump(db_data, f, ensure_ascii=False, indent=2)
                    with self._stats_lock:
                        self.stats["files_modified"] += 1

                self._accumulate_stats(res_stat)
                return res_stat

            # ─────────────────────────────────────────────────────────────
            # B) DİZİ İŞLEME
            # ─────────────────────────────────────────────────────────────
            # AnimeciX başlık künyesini çek
            detail = self.provider.fetch_title_detail(acx_id)
            if not detail:
                self._accumulate_stats(res_stat)
                return res_stat

            acx_seasons = detail.get("seasons", [])
            if not acx_seasons:
                acx_seasons = [{"number": 1}]

            db_seasons = [s for s in db_data.get("seasons", []) if s.get("season_number") != 0]
            specials_season = next((s for s in db_data.get("seasons", []) if s.get("season_number") == 0), None)

            # Akıllı Sezon Haritalama (Bucket Mapping)
            season_mapping = self.build_season_mapping(db_seasons, acx_seasons)
            file_changed = False

            if "unassigned_videos" not in db_data or db_data["unassigned_videos"] is None:
                db_data["unassigned_videos"] = []
            unassigned_urls = {v.get("url") for v in db_data["unassigned_videos"]}

            for acx_s in acx_seasons:
                acx_s_num = acx_s.get("number", 1)
                s_detail = self.provider.fetch_title_detail(acx_id, season_number=acx_s_num)
                if not s_detail:
                    continue

                vids = s_detail.get("videos", [])
                by_ep = defaultdict(list)
                for v in vids:
                    if not v.get("approved", True):
                        continue
                    url = v.get("url")
                    if not url or "youtube.com" in url or "fragman" in (v.get("name") or "").lower():
                        continue
                    ep_num = v.get("episode_num")
                    if ep_num is not None:
                        by_ep[int(ep_num)].append(v)

                if not by_ep:
                    continue

                sorted_eps = sorted(by_ep.keys())

                # A) Özel Sezon (Season 0)
                if acx_s_num == 0:
                    if specials_season:
                        ep_map = {e.get("episode_number"): e for e in specials_season.get("episodes", [])}
                        for ep_num in sorted_eps:
                            target_ep_obj = ep_map.get(ep_num)
                            for v in by_ep[ep_num]:
                                url = v.get("url")
                                player = self.provider.normalize_player(v.get("name"), url)
                                if player not in ALLOWED_PLAYERS:
                                    res_stat["skipped_other_players"] += 1
                                    continue
                                fid = self.get_or_create_fansub_id(v.get("extra"))
                                if target_ep_obj:
                                    if "videos" not in target_ep_obj or target_ep_obj["videos"] is None:
                                        target_ep_obj["videos"] = []
                                    existing_urls = {item.get("url") for item in target_ep_obj["videos"]}
                                    if url not in existing_urls:
                                        target_ep_obj["videos"].append({"fansub_id": fid, "player": player, "url": url})
                                        existing_urls.add(url)
                                        res_stat["added_videos"][player] += 1
                                        res_stat["added_videos"]["TOTAL"] += 1
                                        file_changed = True
                                    else:
                                        res_stat["skipped_existing"] += 1
                                else:
                                    if url not in unassigned_urls:
                                        db_data["unassigned_videos"].append({
                                            "source": "animecix",
                                            "raw_season": 0,
                                            "raw_episode": ep_num,
                                            "player": player,
                                            "url": url,
                                            "fansub_id": fid,
                                        })
                                        unassigned_urls.add(url)
                                        res_stat["unassigned_count"] += 1
                                        res_stat["added_videos"][player] += 1
                                        res_stat["added_videos"]["TOTAL"] += 1
                                        file_changed = True
                    else:
                        for ep_num in sorted_eps:
                            for v in by_ep[ep_num]:
                                url = v.get("url")
                                player = self.provider.normalize_player(v.get("name"), url)
                                if player not in ALLOWED_PLAYERS:
                                    res_stat["skipped_other_players"] += 1
                                    continue
                                fid = self.get_or_create_fansub_id(v.get("extra"))
                                if url not in unassigned_urls:
                                    db_data["unassigned_videos"].append({
                                        "source": "animecix",
                                        "raw_season": 0,
                                        "raw_episode": ep_num,
                                        "player": player,
                                        "url": url,
                                        "fansub_id": fid,
                                    })
                                    unassigned_urls.add(url)
                                    res_stat["unassigned_count"] += 1
                                    res_stat["added_videos"][player] += 1
                                    res_stat["added_videos"]["TOTAL"] += 1
                                    file_changed = True
                    continue

                # B) Normal Sezonlar
                target_db_seasons = season_mapping.get(acx_s_num, [])
                if not target_db_seasons:
                    for ep_num in sorted_eps:
                        for v in by_ep[ep_num]:
                            url = v.get("url")
                            player = self.provider.normalize_player(v.get("name"), url)
                            if player not in ALLOWED_PLAYERS:
                                res_stat["skipped_other_players"] += 1
                                continue
                            fid = self.get_or_create_fansub_id(v.get("extra"))
                            if url not in unassigned_urls:
                                db_data["unassigned_videos"].append({
                                    "source": "animecix",
                                    "raw_season": acx_s_num,
                                    "raw_episode": ep_num,
                                    "player": player,
                                    "url": url,
                                    "fansub_id": fid,
                                })
                                unassigned_urls.add(url)
                                res_stat["unassigned_count"] += 1
                                res_stat["added_videos"][player] += 1
                                res_stat["added_videos"]["TOTAL"] += 1
                                file_changed = True
                    continue

                total_capacity = sum(len(s.get("episodes", [])) for s in target_db_seasons)

                offset = 0
                for target_s in target_db_seasons:
                    s_cap = len(target_s.get("episodes", []))
                    for target_ep_obj in target_s.get("episodes", []):
                        db_ep_num = target_ep_obj.get("episode_number")
                        acx_source_ep_num = offset + db_ep_num

                        if acx_source_ep_num in by_ep:
                            if "videos" not in target_ep_obj or target_ep_obj["videos"] is None:
                                target_ep_obj["videos"] = []
                            existing_urls = {v.get("url") for v in target_ep_obj["videos"]}

                            for v in by_ep[acx_source_ep_num]:
                                url = v.get("url")
                                player = self.provider.normalize_player(v.get("name"), url)
                                if player not in ALLOWED_PLAYERS:
                                    res_stat["skipped_other_players"] += 1
                                    continue
                                if url in existing_urls:
                                    res_stat["skipped_existing"] += 1
                                    continue

                                fid = self.get_or_create_fansub_id(v.get("extra"))
                                target_ep_obj["videos"].append({
                                    "fansub_id": fid,
                                    "player": player,
                                    "url": url,
                                })
                                existing_urls.add(url)
                                res_stat["added_videos"][player] += 1
                                res_stat["added_videos"]["TOTAL"] += 1
                                file_changed = True
                    offset += s_cap

                # Sezon sınırını aşan bölümler (Örn: S1'deki OVAs) -> KESİNLİKLE UNASSIGNED_VIDEOS (Asla Season 0 değil!)
                if len(sorted_eps) > total_capacity:
                    for overflow_ep in range(total_capacity + 1, max(sorted_eps) + 1):
                        if overflow_ep in by_ep:
                            for v in by_ep[overflow_ep]:
                                url = v.get("url")
                                player = self.provider.normalize_player(v.get("name"), url)
                                if player not in ALLOWED_PLAYERS:
                                    res_stat["skipped_other_players"] += 1
                                    continue
                                fid = self.get_or_create_fansub_id(v.get("extra"))
                                if url not in unassigned_urls:
                                    db_data["unassigned_videos"].append({
                                        "source": "animecix",
                                        "raw_season": acx_s_num,
                                        "raw_episode": overflow_ep,
                                        "player": player,
                                        "url": url,
                                        "fansub_id": fid,
                                    })
                                    unassigned_urls.add(url)
                                    res_stat["unassigned_count"] += 1
                                    res_stat["added_videos"][player] += 1
                                    res_stat["added_videos"]["TOTAL"] += 1
                                    file_changed = True
                                else:
                                    res_stat["skipped_existing"] += 1

            if file_changed and not dry_run:
                with open(target_file, "w", encoding="utf-8") as f:
                    json.dump(db_data, f, ensure_ascii=False, indent=2)
                with self._stats_lock:
                    self.stats["files_modified"] += 1

            self._accumulate_stats(res_stat)
            return res_stat

    def _accumulate_stats(self, res_stat: dict):
        with self._stats_lock:
            self.stats["total_processed"] += 1
            if res_stat["matched"]:
                self.stats["matched_anime"] += 1
            self.stats["skipped_other_players"] += res_stat["skipped_other_players"]
            self.stats["skipped_existing_videos"] += res_stat["skipped_existing"]
            self.stats["unassigned_videos_count"] += res_stat["unassigned_count"]
            for pl, cnt in res_stat["added_videos"].items():
                self.stats["added_videos"][pl] += cnt

    def run(self, limit: int | None = None, dry_run: bool = False, force_refresh: bool = False):
        start_time = time.time()
        mode_str = "DRY-RUN (Yazma Kapalı)" if dry_run else "CANLI ÇALIŞTIRMA (Veritabanı Güncelleniyor)"
        logger.info(f"🚀 AnimeciX Backfill Başlatılıyor — {mode_str}")
        logger.info(f"🎯 İzin Verilen Oynatıcılar: {', '.join(sorted(ALLOWED_PLAYERS))}")

        catalog = self.fetch_or_load_catalog(force_refresh=force_refresh)

        # Checkpoint filtreleme (daha önce işlenenleri atla)
        if not dry_run and self.processed_ids:
            initial_count = len(catalog)
            catalog = [item for item in catalog if item.get("id") not in self.processed_ids]
            logger.info(f"💾 Checkpoint: {len(self.processed_ids)} başlık önceden tamamlanmış. Kalan {len(catalog)} başlık işlenecek.")

        if limit:
            catalog = catalog[:limit]
            logger.info(f"⏱️ Limit uygulandı: İlk {limit} başlık işlenecek.")

        total_items = len(catalog)
        processed = 0

        logger.info(f"⚙️ {self.workers} iş parçacığı (worker) ile işleme başlanıyor...")

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_item = {executor.submit(self.process_title, item, dry_run): item for item in catalog}

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                processed += 1
                try:
                    res = future.result()
                    if not dry_run and item.get("id"):
                        with self._stats_lock:
                            self.processed_ids.add(item["id"])
                        if processed % 25 == 0:
                            self._save_checkpoint()
                            self.save_metadata()

                    if res["matched"]:
                        v_added = res["added_videos"]["TOTAL"]
                        logger.info(
                            f"[{processed}/{total_items}] ({processed/total_items*100:.1f}%) "
                            f"✅ {res['name']} ({res['file']}) -> +{v_added} video (TAU:{res['added_videos']['TAU']}, SIBNET:{res['added_videos']['SIBNET']}, UQLOAD:{res['added_videos']['UQLOAD']})"
                        )
                    else:
                        logger.debug(f"[{processed}/{total_items}] ⏭️ {item.get('name')} -> DB'de bulunamadı.")
                except Exception as e:
                    logger.error(f"Başlık işleme hatası ({item.get('name')}): {e}")

        # Metadata ve checkpoint kaydet
        if not dry_run:
            self._save_checkpoint()
            self.save_metadata()

        elapsed = time.time() - start_time
        logger.info("=" * 60)
        logger.info(f"🏁 AnimeciX Backfill Tamamlandı! Süre: {elapsed:.2f} saniye")
        logger.info(f"  Toplam İşlenen Başlık: {self.stats['total_processed']}")
        logger.info(f"  Eşleşen Anime: {self.stats['matched_anime']}")
        logger.info(f"  Eşleşmeyen Anime: {self.stats['unmatched_anime']}")
        logger.info(f"  Güncellenen Dosya: {self.stats['files_modified']}")
        logger.info(f"  Eklenen Toplam Video: {self.stats['added_videos']['TOTAL']}")
        logger.info(f"    - TAU Video: {self.stats['added_videos']['TAU']}")
        logger.info(f"    - SIBNET: {self.stats['added_videos']['SIBNET']}")
        logger.info(f"    - UQLOAD: {self.stats['added_videos']['UQLOAD']}")
        logger.info(f"  Zaten Mevcut (Atlanan): {self.stats['skipped_existing_videos']}")
        logger.info(f"  Diğer Oynatıcılar (Filtrelenen): {self.stats['skipped_other_players']}")
        logger.info(f"  Unassigned Videos (Emniyet Kemeri): {self.stats['unassigned_videos_count']}")
        logger.info("=" * 60)

        # Rapor dosyasını kaydet
        os.makedirs(os.path.dirname(self.report_path), exist_ok=True)
        try:
            with open(self.report_path, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
            logger.info(f"📄 Özet rapor kaydedildi: {self.report_path}")
        except Exception as e:
            logger.warning(f"Rapor kaydedilemedi: {e}")


def main():
    parser = argparse.ArgumentParser(description="AnimeciX Video Backfill Script")
    parser.add_argument("--workers", type=int, default=3, help="Çalışacak thread sayısı (varsayılan: 3)")
    parser.add_argument("--limit", type=int, default=None, help="İşlenecek başlık adedi limiti (test için)")
    parser.add_argument("--dry-run", action="store_true", help="Dosyalara yazmadan simülasyon yap")
    parser.add_argument("--refresh-catalog", action="store_true", help="Kataloğu önbellekten değil canlıdan tekrar çek")
    args = parser.parse_args()

    engine = AnimecixBackfillEngine(workers=args.workers)
    engine.run(limit=args.limit, dry_run=args.dry_run, force_refresh=args.refresh_catalog)


if __name__ == "__main__":
    main()
