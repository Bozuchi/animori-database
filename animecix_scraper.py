"""
animecix_scraper.py — AnimeciX Veri Çekme ve Yönetim Motoru

AnimeciX (animecix.tv) platformundaki tüm anime ve film arşivini (~3.077 başlık)
ayrık mimariye uygun olarak api/sources/animecix/ dizinine indirir ve yönetir.

Özellikler:
    - Tam Çekim (--full): Tüm kataloğu çoklu iş parçacığıyla (ThreadPoolExecutor) çeker.
    - Delta Güncelleme (--delta): Son eklenen bölümleri ve takvimi tarayarak yalnızca güncellenenleri çeker.
    - Checkpoint & Kesintisiz Devam: İşlenen ID'leri kaydeder; Ctrl+C veya çökme durumunda kaldığı yerden devam eder.
    - Otomatik Versiyonlama: MD5 hash tabanlı version.json oluşturur.
    - Ayrık Veri Modeli:
        - api/sources/animecix/anime/{id}.json  (Film ve Sezon/Bölüm detayları)
        - api/sources/animecix/animes.json       (Katalog vitrini)
        - api/sources/animecix/metadata.json     (Fansublar ve türler sözlüğü)
        - api/sources/animecix/latest_episodes.json (Son eklenen bölümler)
        - api/sources/animecix/calendar.json     (Haftalık yayın takvimi)
        - api/sources/animecix/version.json      (Hash tabanlı versiyon)
"""

import os
import sys
import json
import time
import signal
import hashlib
import argparse
import threading
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from animecix_provider import AnimecixProvider
from logger import setup_logger

logger = setup_logger("AnimecixScraper")

# Global graceful shutdown flag
_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("İkinci sinyal alındı, zorla durduruluyor...")
        os._exit(1)
    _shutdown_requested = True
    logger.warning("⏹️  Durdurma sinyali alındı! Mevcut iş parçacıkları tamamlandıktan sonra güvenle kaydedilip çıkılacak...")


class AnimecixScraper:
    def __init__(
        self,
        base_dir: str = "api/sources/animecix",
        data_dir: str = "data",
        workers: int = 2,
    ):
        self.base_dir = base_dir
        self.anime_dir = os.path.join(base_dir, "anime")
        self.animes_json_path = os.path.join(base_dir, "animes.json")
        self.metadata_path = os.path.join(base_dir, "metadata.json")
        self.latest_episodes_path = os.path.join(base_dir, "latest_episodes.json")
        self.calendar_path = os.path.join(base_dir, "calendar.json")
        self.version_path = os.path.join(base_dir, "version.json")

        self.data_dir = data_dir
        self.catalog_cache_path = os.path.join(data_dir, "animecix_catalog.json")
        self.checkpoint_path = os.path.join(data_dir, "animecix_processed_ids.json")
        self.workers = workers

        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.anime_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)

        self.provider = AnimecixProvider()

        # Thread-safe kilitler
        self._meta_lock = threading.Lock()
        self._checkpoint_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._stats_lock = threading.Lock()

        # Checkpoint ve metadata yükle
        self.processed_ids = self._load_checkpoint()
        self.metadata = self._load_metadata()
        self.fansubs = self.metadata.setdefault("fansubs", {})
        self.genres = self.metadata.setdefault("genres", {})

        # Fansub name -> id hızlı haritası
        self.fansub_name_to_id = {v["name"].lower(): int(k) for k, v in self.fansubs.items() if isinstance(v, dict) and "name" in v}
        self.max_fansub_id = max([int(k) for k in self.fansubs.keys()], default=0)

        # Genre name -> id hızlı haritası
        self.genre_name_to_id = {v.lower(): int(k) for k, v in self.genres.items() if isinstance(v, str)}
        self.max_genre_id = max([int(k) for k in self.genres.keys()], default=0)

        # İstatistikler
        self.stats = {
            "total_catalog": 0,
            "already_processed": len(self.processed_ids),
            "successfully_scraped": 0,
            "movies_scraped": 0,
            "series_scraped": 0,
            "total_episodes": 0,
            "total_videos": 0,
            "failed_titles": 0,
        }

    # ─────────────────────────────────────────────────────────────
    # CHECKPOINT & METADATA YÖNETİMİ
    # ─────────────────────────────────────────────────────────────

    def _load_checkpoint(self) -> set[int]:
        disk_ids = set()
        if os.path.exists(self.anime_dir):
            for fn in os.listdir(self.anime_dir):
                if fn.endswith(".json"):
                    try:
                        disk_ids.add(int(fn.replace(".json", "")))
                    except ValueError:
                        pass

        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    file_ids = set(int(x) for x in data)
                    return file_ids & disk_ids
            except Exception as e:
                logger.warning(f"Checkpoint okunamadı: {e}")
        return disk_ids

    def _save_checkpoint(self):
        with self._checkpoint_lock:
            try:
                with open(self.checkpoint_path, "w", encoding="utf-8") as f:
                    json.dump(sorted(list(self.processed_ids)), f)
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
                    json.dump(self.metadata, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"metadata.json kaydedilemedi: {e}")

    def get_or_create_fansub_id(self, fansub_name: str | None) -> int | None:
        """Fansub adını metadata sözlüğüne kaydeder ve ID döner."""
        if not fansub_name:
            return None
        clean_name = fansub_name.strip()
        if not clean_name or clean_name.isdigit():
            return None

        clean_lower = clean_name.lower()
        with self._meta_lock:
            if clean_lower in self.fansub_name_to_id:
                return self.fansub_name_to_id[clean_lower]

            self.max_fansub_id += 1
            new_id = self.max_fansub_id
            self.fansub_name_to_id[clean_lower] = new_id
            self.fansubs[str(new_id)] = {"name": clean_name, "url": ""}
            return new_id

    def get_or_create_genre_id(self, genre_name: str | None) -> int | None:
        """Tür adını metadata sözlüğüne kaydeder ve ID döner."""
        if not genre_name:
            return None
        clean_name = genre_name.strip()
        if not clean_name:
            return None

        clean_lower = clean_name.lower()
        with self._meta_lock:
            if clean_lower in self.genre_name_to_id:
                return self.genre_name_to_id[clean_lower]

            self.max_genre_id += 1
            new_id = self.max_genre_id
            self.genre_name_to_id[clean_lower] = new_id
            self.genres[str(new_id)] = clean_name
            return new_id

    # ─────────────────────────────────────────────────────────────
    # KATALOG ÇEKİMİ
    # ─────────────────────────────────────────────────────────────

    def fetch_catalog(self, force_refresh: bool = False) -> list[dict]:
        """AnimeciX'teki tüm başlıkları (/secure/titles) çeker veya önbellekten okur."""
        if not force_refresh and os.path.exists(self.catalog_cache_path):
            try:
                with open(self.catalog_cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"📂 AnimeciX kataloğu önbellekten yüklendi ({len(data)} başlık).")
                self.stats["total_catalog"] = len(data)
                return data
            except Exception as e:
                logger.warning(f"Katalog önbelleği okunamadı: {e}")

        logger.info("📡 AnimeciX kataloğu indiriliyor (tüm sayfalar taranıyor)...")
        all_titles = []
        page = 1
        per_page = 100

        while True:
            res = self.provider._get("/secure/titles", params={"page": page, "perPage": per_page})
            if not isinstance(res, dict):
                logger.error(f"Katalog çekiminde hata oluştu (sayfa {page}).")
                break

            pagination = res.get("pagination", {})
            titles = pagination.get("data", [])
            if not titles:
                break

            all_titles.extend(titles)
            cur_p = pagination.get("current_page", page)
            last_p = pagination.get("last_page", page)
            logger.info(f"  Sayfa {cur_p}/{last_p} indirildi ({len(all_titles)} başlık)...")

            if cur_p >= last_p:
                break
            page += 1

        logger.info(f"✅ Toplam {len(all_titles)} başlık başarıyla çekildi.")
        self.stats["total_catalog"] = len(all_titles)

        try:
            with open(self.catalog_cache_path, "w", encoding="utf-8") as f:
                json.dump(all_titles, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Katalog önbelleğe kaydedildi: {self.catalog_cache_path}")
        except Exception as e:
            logger.warning(f"Katalog diske yazılamadı: {e}")

        return all_titles

    # ─────────────────────────────────────────────────────────────
    # TEK ANİME DETAY KAZIMA & AYRIŞTIRMA
    # ─────────────────────────────────────────────────────────────

    def scrape_title(self, title_id: int, catalog_item: dict | None = None) -> tuple[dict | None, dict | None]:
        """
        Belirtilen başlığı (/secure/titles/{id}) çeker, film veya dizi yapısına
        göre organize ederek api/sources/animecix/anime/{id}.json dosyasını yazar.

        Dönüş:
            (anime_detail_obj, catalog_vitrin_obj)
        """
        data = self.provider._get(f"/secure/titles/{title_id}")
        if not isinstance(data, dict):
            return None, None

        t = data.get("title")
        if not t or not isinstance(t, dict):
            return None, None

        is_series = bool(t.get("is_series", t.get("type") == "series"))

        # Tür ID'lerini ayıkla ve metadata'ya kaydet
        raw_genres = t.get("genres", [])
        genre_ids = []
        for g in raw_genres:
            if isinstance(g, int):
                if g not in genre_ids:
                    genre_ids.append(g)
                continue
            elif isinstance(g, dict):
                g_name = g.get("display_name") or g.get("name")
            elif isinstance(g, str):
                g_name = g
            else:
                g_name = None

            if g_name:
                gid = self.get_or_create_genre_id(g_name)
                if gid and gid not in genre_ids:
                    genre_ids.append(gid)

        # Temel Künye
        anime_obj = {
            "id": t.get("id", title_id),
            "name": t.get("name"),
            "name_english": t.get("name_english"),
            "name_romanji": t.get("name_romanji"),
            "original_title": t.get("original_title"),
            "type": "series" if is_series else "movie",
            "title_type": t.get("title_type"),
            "year": t.get("year"),
            "release_date": t.get("release_date"),
            "description": t.get("description"),
            "poster": t.get("poster"),
            "backdrop": t.get("backdrop"),
            "rating": t.get("tmdb_vote_average"),
            "vote_count": t.get("tmdb_vote_count"),
            "genres": genre_ids,
            "tmdb_id": t.get("tmdb_id"),
            "mal_id": t.get("mal_id"),
            "anilist_id": t.get("anilist_id"),
            "imdb_id": t.get("imdb_id"),
        }

        total_vids_count = 0
        total_eps_count = 0

        if not is_series:
            # ────────────────── FILM MODU ──────────────────
            anime_obj["season_count"] = 0
            videos = []
            for v in t.get("videos", []):
                url = v.get("url")
                if not url:
                    continue
                raw_player = v.get("name")
                player = self.provider.normalize_player(raw_player, url)
                extra = v.get("extra")
                fansub_name = self.provider.extract_fansub_name(extra)
                fansub_id = self.get_or_create_fansub_id(fansub_name)

                videos.append({
                    "id": v.get("id"),
                    "player": player,
                    "url": url,
                    "extra": extra,
                    "fansub": fansub_name,
                    "fansub_id": fansub_id,
                    "quality": v.get("quality"),
                    "language": v.get("language", "tr"),
                })

            anime_obj["videos"] = videos
            total_vids_count = len(videos)

        else:
            # ────────────────── DIZI / SEZON MODU ──────────────────
            raw_seasons = t.get("seasons", [])
            built_seasons = []

            for s_info in raw_seasons:
                s_num = s_info.get("number")
                if s_num is None:
                    continue

                # Sezon bölümlerini ve videolarını çek
                page = 1
                all_eps = []
                all_vids = []

                while True:
                    s_data = self.provider._get(
                        f"/secure/titles/{title_id}",
                        params={"seasonNumber": s_num, "page": page, "perPage": 100},
                    )
                    if not isinstance(s_data, dict):
                        break

                    s_title = s_data.get("title", {})
                    s_season = s_title.get("season", {})
                    ep_pag = s_season.get("episodePagination", {})
                    data_list = ep_pag.get("data", [])
                    all_eps.extend(data_list)

                    if page == 1:
                        all_vids = s_title.get("videos", [])

                    cur_page = ep_pag.get("current_page", page)
                    last_page = ep_pag.get("last_page", page)
                    if cur_page >= last_page or not data_list:
                        break
                    page += 1

                # Videoları grupla
                vids_by_ep_id = defaultdict(list)
                vids_by_ep_num = defaultdict(list)

                for v in all_vids:
                    url = v.get("url")
                    if not url:
                        continue
                    raw_player = v.get("name")
                    player = self.provider.normalize_player(raw_player, url)
                    extra = v.get("extra")
                    fansub_name = self.provider.extract_fansub_name(extra)
                    fansub_id = self.get_or_create_fansub_id(fansub_name)

                    v_item = {
                        "id": v.get("id"),
                        "player": player,
                        "url": url,
                        "extra": extra,
                        "fansub": fansub_name,
                        "fansub_id": fansub_id,
                        "quality": v.get("quality"),
                        "language": v.get("language", "tr"),
                    }

                    if v.get("episode_id"):
                        vids_by_ep_id[v["episode_id"]].append(v_item)
                    if v.get("episode_num") is not None:
                        vids_by_ep_num[v["episode_num"]].append(v_item)

                # Bölümleri inşa et
                built_episodes = []
                for ep in all_eps:
                    ep_id = ep.get("id")
                    ep_num = ep.get("episode_number")

                    ep_vids = vids_by_ep_id.get(ep_id) or vids_by_ep_num.get(ep_num) or []
                    built_episodes.append({
                        "id": ep_id,
                        "episode_number": ep_num,
                        "name": ep.get("name"),
                        "description": ep.get("description"),
                        "release_date": ep.get("release_date"),
                        "poster": ep.get("poster"),
                        "videos": ep_vids,
                    })

                total_eps_count += len(built_episodes)
                total_vids_count += sum(len(ep["videos"]) for ep in built_episodes)

                built_seasons.append({
                    "id": s_info.get("id"),
                    "season_number": s_num,
                    "name": s_info.get("name"),
                    "episode_count": len(built_episodes),
                    "episodes": built_episodes,
                })

            anime_obj["season_count"] = len(built_seasons)
            anime_obj["seasons"] = built_seasons

        # Dosyayı atomik olarak diske kaydet (Ctrl+C durumunda bozuk dosya oluşmaz)
        temp_file_path = os.path.join(self.anime_dir, f"{title_id}.tmp")
        file_path = os.path.join(self.anime_dir, f"{title_id}.json")
        try:
            with open(temp_file_path, "w", encoding="utf-8") as f:
                json.dump(anime_obj, f, ensure_ascii=False, indent=2)
            os.replace(temp_file_path, file_path)
        except Exception as e:
            logger.error(f"Anime dosyası yazılamadı ({title_id}): {e}")
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except Exception:
                    pass
            return None, None

        # Vitrin katalog nesnesi (hafif)
        vitrin_item = {
            "id": anime_obj["id"],
            "name": anime_obj["name"],
            "name_english": anime_obj["name_english"],
            "name_romanji": anime_obj["name_romanji"],
            "type": anime_obj["type"],
            "year": anime_obj["year"],
            "poster": anime_obj["poster"],
            "backdrop": anime_obj["backdrop"],
            "rating": anime_obj["rating"],
            "season_count": anime_obj.get("season_count", 0),
            "genres": anime_obj["genres"],
            "tmdb_id": anime_obj.get("tmdb_id"),
            "mal_id": anime_obj.get("mal_id"),
            "anilist_id": anime_obj.get("anilist_id"),
        }

        # İstatistikleri güncelle
        with self._stats_lock:
            self.stats["successfully_scraped"] += 1
            if is_series:
                self.stats["series_scraped"] += 1
            else:
                self.stats["movies_scraped"] += 1
            self.stats["total_episodes"] += total_eps_count
            self.stats["total_videos"] += total_vids_count

        return anime_obj, vitrin_item

    # ─────────────────────────────────────────────────────────────
    # TAM ARŞİV ÇEKİMİ (BATCH / PARALLEL)
    # ─────────────────────────────────────────────────────────────

    def scrape_all(self, limit: int | None = None, resume: bool = True, force_catalog: bool = False):
        """Katalogdaki tüm başlıkları iş parçacığı havuzuyla çeker."""
        global _shutdown_requested

        logger.info("=" * 65)
        logger.info("🚀 AnimeciX Arşiv Çekim Motoru Başlatılıyor...")
        logger.info("=" * 65)

        catalog = self.fetch_catalog(force_refresh=force_catalog)
        if not catalog:
            logger.error("Katalog boş, işlem sonlandırılıyor.")
            return

        # Zaten işlenmişleri filtrele
        if resume:
            candidates = [t for t in catalog if t.get("id") not in self.processed_ids]
            logger.info(f"📂 Toplam: {len(catalog)} | Daha önce işlenen: {len(self.processed_ids)} | Kalan: {len(candidates)}")
        else:
            candidates = catalog
            self.processed_ids.clear()

        if limit:
            candidates = candidates[:limit]
            logger.info(f"🔬 Test Limiti devrede: Yalnızca ilk {limit} başlık işlenecek.")

        if not candidates:
            logger.info("✅ Tüm başlıklar zaten indirilmiş ve güncel!")
            self.build_animes_index()
            self.save_metadata()
            self.update_latest_episodes()
            self.update_calendar()
            self.update_versions()
            return

        start_time = time.time()
        completed_count = 0
        total_candidates = len(candidates)
        vitrin_entries = []

        logger.info(f"⚙️  {self.workers} iş parçacığı (worker) ile indirme başlatılıyor...")

        def _worker_task(item: dict):
            if _shutdown_requested:
                return None
            t_id = item.get("id")
            if not t_id:
                return None
            detail, vitrin = self.scrape_title(t_id, catalog_item=item)
            return t_id, vitrin

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_id = {executor.submit(_worker_task, item): item.get("id") for item in candidates}

            for future in as_completed(future_to_id):
                if _shutdown_requested:
                    logger.warning("⏹️  Durdurma talebi algılandı, yeni görevler bekleniyor...")
                    break

                t_id = future_to_id[future]
                try:
                    result = future.result()
                    if result:
                        scraped_id, vitrin_item = result
                        if scraped_id:
                            with self._checkpoint_lock:
                                self.processed_ids.add(scraped_id)
                            if vitrin_item:
                                vitrin_entries.append(vitrin_item)
                    else:
                        with self._stats_lock:
                            self.stats["failed_titles"] += 1
                except Exception as e:
                    logger.error(f"Hata oluştu ({t_id}): {e}")
                    with self._stats_lock:
                        self.stats["failed_titles"] += 1

                completed_count += 1
                if completed_count % 10 == 0 or completed_count == total_candidates:
                    elapsed = time.time() - start_time
                    rate = completed_count / elapsed if elapsed > 0 else 0
                    remaining = (total_candidates - completed_count) / rate if rate > 0 else 0
                    logger.info(
                        f"📊 İlerleme: {completed_count}/{total_candidates} "
                        f"({completed_count / total_candidates * 100:.1f}%) — "
                        f"Hız: {rate:.1f} anime/sn — Tahmini Kalan: {remaining / 60:.1f} dk"
                    )

                # Her 50 anime başlığında bir checkpoint ve metadata'yı diske yaz
                if completed_count % 50 == 0:
                    self._save_checkpoint()
                    self.save_metadata()

        # Çıkış / Bitiş İşlemleri
        self._save_checkpoint()
        self.save_metadata()

        logger.info("📑 animes.json vitrin indeksi yeniden oluşturuluyor...")
        self.build_animes_index()

        logger.info("📅 Son bölümler ve yayın takvimi güncelleniyor...")
        self.update_latest_episodes()
        self.update_calendar()

        logger.info("🔒 versiyon hash'leri güncelleniyor...")
        self.update_versions()

        elapsed_total = time.time() - start_time
        logger.info("=" * 65)
        logger.info("🏁 İŞLEM TAMAMLANDI")
        logger.info(f"⏱️  Geçen Süre: {elapsed_total / 60:.1f} dakika")
        logger.info(f"✅ İndirilen Başlık: {self.stats['successfully_scraped']} (Dizi: {self.stats['series_scraped']}, Film: {self.stats['movies_scraped']})")
        logger.info(f"🎬 Toplam Bölüm: {self.stats['total_episodes']} | Toplam Video: {self.stats['total_videos']}")
        logger.info(f"👥 Kayıtlı Fansub Sayısı: {len(self.fansubs)}")
        logger.info("=" * 65)

    # ─────────────────────────────────────────────────────────────
    # VİTRİN İNDEKSİ & YARDIMCI DOSYALAR
    # ─────────────────────────────────────────────────────────────

    def build_animes_index(self) -> int:
        """api/sources/animecix/anime/*.json dosyalarını tarayarak animes.json dosyasını oluşturur."""
        if not os.path.exists(self.anime_dir):
            return 0

        index = []
        for fn in sorted(os.listdir(self.anime_dir)):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(self.anime_dir, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                index.append({
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "name_english": data.get("name_english"),
                    "name_romanji": data.get("name_romanji"),
                    "type": data.get("type"),
                    "year": data.get("year"),
                    "poster": data.get("poster"),
                    "backdrop": data.get("backdrop"),
                    "rating": data.get("rating"),
                    "season_count": data.get("season_count", 0),
                    "total_episodes": sum(len(s.get("episodes", [])) for s in data.get("seasons", [])),
                    "genres": data.get("genres", []),
                    "tmdb_id": data.get("tmdb_id"),
                    "mal_id": data.get("mal_id"),
                    "anilist_id": data.get("anilist_id"),
                })
            except Exception as e:
                logger.warning(f"Anime dosyası okunamadı ({fn}): {e}")

        # İsme veya ID'ye göre sırala
        index.sort(key=lambda x: x.get("id") or 0)

        with self._index_lock:
            try:
                with open(self.animes_json_path, "w", encoding="utf-8") as f:
                    json.dump(index, f, ensure_ascii=False, indent=2)
                logger.info(f"✅ animes.json oluşturuldu ({len(index)} başlık).")
            except Exception as e:
                logger.error(f"animes.json kaydedilemedi: {e}")

        return len(index)

    def update_latest_episodes(self, max_pages: int = 8) -> list[dict]:
        """AnimeciX son eklenen bölümlerini çekip latest_episodes.json dosyasına kaydeder."""
        all_latest = []
        for p in range(1, max_pages + 1):
            eps = self.provider.fetch_last_episodes(page=p)
            if not eps:
                break
            for ep in eps:
                all_latest.append({
                    "_id": ep.get("_id"),
                    "title_id": ep.get("title_id"),
                    "title_name": ep.get("title_name"),
                    "season_number": ep.get("season_number"),
                    "episode_number": ep.get("episode_number"),
                    "poster": ep.get("title_poster"),
                    "release_date": ep.get("release_date"),
                })

        if all_latest:
            try:
                with open(self.latest_episodes_path, "w", encoding="utf-8") as f:
                    json.dump(all_latest, f, ensure_ascii=False, indent=2)
                logger.info(f"✅ latest_episodes.json güncellendi ({len(all_latest)} kayıt).")
            except Exception as e:
                logger.error(f"latest_episodes.json yazılamadı: {e}")

        return all_latest

    def update_calendar(self) -> list[dict]:
        """AnimeciX haftalık yayın takvimini çeker ve calendar.json dosyasına kaydeder."""
        cal_data = self.provider.fetch_calendar()
        formatted_cal = []

        if isinstance(cal_data, list):
            for day_item in cal_data:
                day_eps = []
                for ep in day_item.get("episodes", []):
                    title_info = ep.get("title", {})
                    day_eps.append({
                        "title_id": ep.get("title_id") or title_info.get("id"),
                        "title_name": title_info.get("name") or ep.get("name"),
                        "season_number": ep.get("season_number"),
                        "episode_number": ep.get("episode_number"),
                        "poster": ep.get("poster") or title_info.get("poster"),
                        "release_date": ep.get("release_date"),
                    })

                formatted_cal.append({
                    "date": day_item.get("date"),
                    "day": day_item.get("day"),
                    "episodes": day_eps,
                })

        if formatted_cal:
            try:
                with open(self.calendar_path, "w", encoding="utf-8") as f:
                    json.dump(formatted_cal, f, ensure_ascii=False, indent=2)
                logger.info(f"✅ calendar.json güncellendi ({len(formatted_cal)} gün).")
            except Exception as e:
                logger.error(f"calendar.json yazılamadı: {e}")

        return formatted_cal

    @staticmethod
    def _compute_hash(filepath: str) -> str | None:
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()[:8]
        except Exception:
            return None

    def update_versions(self):
        """Dosyaların MD5 hash'ini hesaplayarak version.json dosyasını günceller."""
        now_str = datetime.now().strftime("%Y%m%d_%H%M")
        old_version = {}
        if os.path.exists(self.version_path):
            try:
                with open(self.version_path, "r", encoding="utf-8") as f:
                    old_version = json.load(f)
            except Exception:
                old_version = {}

        tracked = {
            "animes": self.animes_json_path,
            "metadata": self.metadata_path,
            "latest_episodes": self.latest_episodes_path,
            "calendar": self.calendar_path,
        }

        new_version = {}
        for key, fp in tracked.items():
            h = self._compute_hash(fp)
            if not h:
                continue
            old_h = old_version.get(key, {}).get("hash")
            if h != old_h:
                new_version[key] = {"last_updated": now_str, "hash": h}
            else:
                new_version[key] = {
                    "last_updated": old_version.get(key, {}).get("last_updated", now_str),
                    "hash": h,
                }

        try:
            with open(self.version_path, "w", encoding="utf-8") as f:
                json.dump(new_version, f, ensure_ascii=False, indent=2)
            logger.info("✅ version.json güncellendi.")
        except Exception as e:
            logger.error(f"version.json yazılamadı: {e}")

    # ─────────────────────────────────────────────────────────────
    # DELTA GÜNCELLEME (GÜNLÜK / SAATLİK SENKRONİZASYON)
    # ─────────────────────────────────────────────────────────────

    def delta_sync(self, pages: int = 3):
        """
        AnimeciX üzerindeki değişiklikleri çok yönlü kontrol eder:
            1. Son eklenen bölümler (/secure/last-episodes)
            2. Haftalık yayın takvimi (/secure/calendar)
            3. "Gelecek Animeler" ve "Son Çıkan Animeler" vitrinleri (/secure/homepage/lists-guests)
            4. Siteye yeni eklenen son katalog başlıkları (/secure/titles)
        Tespit edilen yeni/güncellenen tüm animeleri paralel iş parçacıklarıyla günceller.
        """
        logger.info("🔄 Kapsamlı Delta Senkronizasyon Başlatılıyor...")
        affected_title_ids = set()

        # 1. Son Eklenen Bölümlerden Yeni Başlıkları Topla
        logger.info(f"  📡 Son bölümler taranıyor ({pages} sayfa)...")
        known_episode_ids = set()
        if os.path.exists(self.latest_episodes_path):
            try:
                with open(self.latest_episodes_path, "r", encoding="utf-8") as f:
                    for it in json.load(f):
                        if isinstance(it, dict) and it.get("_id"):
                            known_episode_ids.add(it["_id"])
            except Exception:
                pass

        last_eps = []
        for p in range(1, pages + 1):
            extra_eps = self.provider.fetch_last_episodes(page=p)
            if extra_eps:
                last_eps.extend(extra_eps)

        for ep in last_eps:
            ep_id = ep.get("_id")
            t_id = ep.get("title_id")
            if ep_id and ep_id not in known_episode_ids and t_id:
                affected_title_ids.add(t_id)

        # 2. Haftalık Yayın Takviminden Başlıkları Topla
        logger.info("  📅 Yayın takvimi taranıyor...")
        cal_data = self.provider.fetch_calendar()
        if isinstance(cal_data, list):
            for day_item in cal_data:
                for ep in day_item.get("episodes", []):
                    t_info = ep.get("title", {})
                    t_id = ep.get("title_id") or t_info.get("id")
                    if t_id:
                        # Yerelde yoksa veya devam eden anime ise listeye ekle
                        affected_title_ids.add(t_id)

        # 3. Anasayfa Listelerini Tara ("Gelecek Animeler" ve "Son Çıkan Animeler")
        logger.info("  🌟 Anasayfa vitrin listeleri taranıyor...")
        homepage_lists = self.provider.fetch_homepage_lists()
        for lst in homepage_lists:
            list_name = lst.get("name", "")
            if list_name in ("Gelecek Animeler", "Son Çıkan Animeler", "Sezonun İncileri"):
                for it in lst.get("items", []):
                    t_id = it.get("id")
                    if t_id:
                        # Yerelde dosyası yoksa mutlaka çek
                        file_path = os.path.join(self.anime_dir, f"{t_id}.json")
                        if not os.path.exists(file_path):
                            affected_title_ids.add(t_id)

        # 4. En Son Eklenen Katalog Başlıkları
        logger.info("  📑 En yeni katalog başlıkları kontrol ediliyor...")
        recent_titles_res = self.provider._get("/secure/titles", params={"page": 1, "perPage": 50})
        if isinstance(recent_titles_res, dict):
            for item in recent_titles_res.get("pagination", {}).get("data", []):
                t_id = item.get("id")
                if t_id:
                    file_path = os.path.join(self.anime_dir, f"{t_id}.json")
                    if not os.path.exists(file_path):
                        affected_title_ids.add(t_id)

        logger.info(f"🔎 Toplam {len(affected_title_ids)} anime başlığı güncellenmek/çekilmek üzere tespit edildi.")
        if not affected_title_ids:
            logger.info("Değişiklik tespit edilmedi.")
            return

        # Paralel Olarak Çek
        logger.info(f"⚙️  {self.workers} iş parçacığı ile güncellemeler indiriliyor...")
        updated_count = 0

        def _update_task(t_id: int):
            if _shutdown_requested:
                return None
            anime_obj, vitrin_item = self.scrape_title(t_id)
            return t_id if anime_obj else None

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_id = {executor.submit(_update_task, t_id): t_id for t_id in affected_title_ids}
            for future in as_completed(future_to_id):
                if _shutdown_requested:
                    break
                try:
                    scraped_id = future.result()
                    if scraped_id:
                        updated_count += 1
                        with self._checkpoint_lock:
                            self.processed_ids.add(scraped_id)
                except Exception as e:
                    logger.error(f"Güncelleme hatası ({future_to_id[future]}): {e}")

        self._save_checkpoint()
        self.save_metadata()

        if updated_count > 0:
            logger.info(f"✅ Toplam {updated_count} anime başarıyla güncellendi/kaydedildi.")
            self.build_animes_index()
            self.update_latest_episodes()
            self.update_calendar()
            self.update_versions()
        else:
            logger.info("Hiçbir anime güncellenemedi.")

    # ─────────────────────────────────────────────────────────────
    # AKILLI SENKRONİZASYON (TÜRKANİME TARZI CATALOG & BÖLÜM FARKI)
    # ─────────────────────────────────────────────────────────────

    def smart_sync(self, force_catalog: bool = False, limit: int | None = None):
        """
        Türkanime mantığındaki gibi akıllı tam tarama ve senkronizasyon:
        1. AnimeciX kataloğunu çeker (/secure/titles, 32 sayfa, ~15 saniye).
        2. Yerel veritabanındaki tüm anime dosyalarını (api/sources/animecix/anime/*.json) indeksler.
        3. Karşılaştırır:
           - Bizde hiç olmayan yeni animeler -> İndirilir
           - Bizde olup da bölüm sayısı (episode_count) veya sezon sayısı artmış olanlar -> İndirilir
           - /secure/last-episodes (son bölümler) üzerinden yeni video/bölüm gelenler -> İndirilir
           - Hiç değişmemiş olan binlerce anime -> Doğrudan atlanır (0 HTTP isteği)
        4. Tespit edilenleri paralel iş parçacıklarıyla çeker.
        5. animes.json, latest_episodes.json, calendar.json ve version.json günceller.
        """
        global _shutdown_requested
        logger.info("=" * 65)
        logger.info("🧠 AnimeciX Akıllı Senkronizasyon (Smart Sync) Başlatılıyor...")
        logger.info("=" * 65)

        # 1. Kataloğu al
        catalog = self.fetch_catalog(force_refresh=force_catalog)
        if not catalog:
            logger.error("Katalog boş, işlem durduruldu.")
            return

        # 2. Yerel anime durumunu animes.json ve diskten anında indeksle (<0.01sn)
        logger.info("📂 Yerel anime arşivi kontrol ediliyor...")
        local_stats = {}
        if os.path.exists(self.animes_json_path):
            try:
                with open(self.animes_json_path, "r", encoding="utf-8") as f:
                    for a in json.load(f):
                        if isinstance(a, dict) and a.get("id"):
                            local_stats[a["id"]] = {
                                "season_count": a.get("season_count", 0),
                                "total_episodes": a.get("total_episodes", 0),
                            }
            except Exception:
                pass

        # animes.json yoksa veya eksikse diskteki dosyalardan tamamla
        for fn in os.listdir(self.anime_dir):
            if fn.endswith(".json"):
                try:
                    t_id = int(fn.replace(".json", ""))
                    if t_id not in local_stats:
                        local_stats[t_id] = {"season_count": 0, "total_episodes": 0}
                except ValueError:
                    pass

        logger.info(f"✅ {len(local_stats)} yerel anime mevcut.")

        to_download_candidates = []
        candidate_ids = set()
        new_anime_count = 0
        overflow_count = 0

        # 3. Bizde hiç olmayanları VEYA katalogda bölüm/sezon sayısı artanları tespit et
        # (Bu adım, last-episodes akışından düşmüş olsa bile tüm toplu bölüm yüklemelerini yakalar!)
        for item in catalog:
            t_id = item.get("id")
            if not t_id:
                continue
            if t_id not in local_stats:
                to_download_candidates.append(item)
                candidate_ids.add(t_id)
                new_anime_count += 1
            else:
                loc = local_stats[t_id]
                cat_season_count = item.get("season_count") or len(item.get("seasons", []))
                cat_seasons = item.get("seasons", [])
                cat_total_eps = item.get("episode_count") or sum(s.get("episode_count", 0) for s in cat_seasons)
                # Eğer sitedeki bölüm veya sezon sayısı bizdekinden fazlaysa:
                if cat_season_count > loc["season_count"] or (cat_total_eps and cat_total_eps > loc["total_episodes"]):
                    to_download_candidates.append(item)
                    candidate_ids.add(t_id)
                    overflow_count += 1

        # 4. Son Eklenen Bölümler Kontrolü (/secure/last-episodes vs yerel latest_episodes.json)
        # Önceki çalıştırmada kaydedilen _id'leri yükle
        known_episode_ids = set()
        if os.path.exists(self.latest_episodes_path):
            try:
                with open(self.latest_episodes_path, "r", encoding="utf-8") as f:
                    old_eps = json.load(f)
                    for ep in old_eps:
                        if isinstance(ep, dict) and ep.get("_id"):
                            known_episode_ids.add(ep["_id"])
            except Exception as e:
                logger.warning(f"latest_episodes.json okunamadı: {e}")

        logger.info(f"📋 Yerelde kayıtlı bilinen son bölüm sayısı: {len(known_episode_ids)}")
        logger.info("📡 Canlı son bölüm/video akışı taranıyor...")

        new_episodes_count = 0
        new_episode_title_ids = set()

        # En yeni 8 sayfayı (80 yayın) kronolojik kontrol et
        for p in range(1, 9):
            last_eps_page = self.provider.fetch_last_episodes(page=p)
            if not last_eps_page:
                break

            page_has_new = False
            for ep in last_eps_page:
                ep_id = ep.get("_id")
                ep_tid = ep.get("title_id")

                # Eğer bu yayın kaydı yereldeki latest_episodes.json içinde YOKSA -> Yeni bölümdür!
                if ep_id and ep_id not in known_episode_ids:
                    new_episodes_count += 1
                    page_has_new = True
                    if ep_tid and ep_tid not in candidate_ids:
                        new_episode_title_ids.add(ep_tid)
                        to_download_candidates.append({"id": ep_tid})
                        candidate_ids.add(ep_tid)

            # Akış en yeniden eskiye sıralı olduğundan, bu sayfada hiç yeni kayıt yoksa
            # ve yerel hafızamız varsa daha eski sayfalara bakmaya gerek yoktur.
            if not page_has_new and len(known_episode_ids) > 0:
                break

        logger.info(
            f"📊 Tarama Özeti: {new_anime_count} Yeni Anime | "
            f"{overflow_count} Bölümü/Sezonu Artan Anime | "
            f"{new_episodes_count} Yeni Bölüm/Video Yayını ({len(new_episode_title_ids)} Hedef Anime) | "
            f"{len(local_stats)} Mevcut Anime"
        )

        if limit:
            to_download_candidates = to_download_candidates[:limit]
            logger.info(f"🔬 Limit devrede: {len(to_download_candidates)} başlık işlenecek.")

        if not to_download_candidates:
            logger.info("✅ Hiçbir yeni anime veya yeni bölüm tespit edilmedi. Veritabanı tamamen güncel!")
            self.update_calendar()
            self.update_versions()
            return

        # 5. Paralel İndir
        logger.info(f"⚙️  {self.workers} iş parçacığı ile {len(to_download_candidates)} anime indiriliyor/güncelleniyor...")
        start_time = time.time()
        success_count = 0

        def _sync_worker(item: dict):
            if _shutdown_requested:
                return None
            t_id = item.get("id")
            if not t_id:
                return None
            anime_obj, vitrin = self.scrape_title(t_id, catalog_item=item)
            return t_id if anime_obj else None

        total_sync = len(to_download_candidates)
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_id = {executor.submit(_sync_worker, item): item.get("id") for item in to_download_candidates}
            for future in as_completed(future_to_id):
                if _shutdown_requested:
                    logger.warning("⏹️  Durdurma talebi algılandı...")
                    break
                try:
                    res_id = future.result()
                    if res_id:
                        success_count += 1
                        with self._checkpoint_lock:
                            self.processed_ids.add(res_id)
                except Exception as e:
                    logger.error(f"Hata ({future_to_id[future]}): {e}")

                if success_count > 0 and (success_count % 20 == 0 or success_count == total_sync):
                    elapsed = time.time() - start_time
                    rate = success_count / elapsed if elapsed > 0 else 0
                    remaining = (total_sync - success_count) / rate if rate > 0 else 0
                    logger.info(
                        f"📊 İlerleme: {success_count}/{total_sync} "
                        f"({success_count / total_sync * 100:.1f}%) — "
                        f"Hız: {rate:.1f} anime/sn — Kalan: {remaining / 60:.1f} dk"
                    )
                    self._save_checkpoint()
                    self.save_metadata()

        self._save_checkpoint()
        self.save_metadata()

        logger.info(f"✅ {success_count} anime başarıyla güncellendi/kaydedildi.")
        self.build_animes_index()
        self.update_latest_episodes()
        self.update_calendar()
        self.update_versions()

        elapsed = time.time() - start_time
        logger.info("=" * 65)
        logger.info(f"🏁 Akıllı Senkronizasyon Tamamlandı ({elapsed:.1f}sn)")
        logger.info("=" * 65)




def main():
    signal.signal(signal.SIGINT, _handle_signal)

    parser = argparse.ArgumentParser(description="AnimeciX Arşiv Çekim ve Yönetim Motoru")
    parser.add_argument("--full", action="store_true", help="Tüm AnimeciX arşivini baştan/kaldığı yerden çeker.")
    parser.add_argument("--delta", action="store_true", help="Yalnızca son eklenen bölümleri tarayıp delta güncelleme yapar.")
    parser.add_argument("--limit", type=int, default=None, help="İşlenecek maksimum başlık adedi (Test için).")
    parser.add_argument("--workers", type=int, default=2, help="Paralel iş parçacığı adedi (Varsayılan: 2).")
    parser.add_argument("--force-catalog", action="store_true", help="Kataloğu önbellekten okumak yerine yeniden indirir.")
    parser.add_argument("--build-index", action="store_true", help="Mevcut JSON dosyalarından animes.json vitrinini yeniden kurar.")

    args = parser.parse_args()

    scraper = AnimecixScraper(workers=args.workers)

    if args.build_index:
        scraper.build_animes_index()
        scraper.save_metadata()
        scraper.update_latest_episodes()
        scraper.update_calendar()
        scraper.update_versions()
    elif args.delta:
        scraper.delta_sync()
    elif args.full or args.limit:
        scraper.scrape_all(limit=args.limit, force_catalog=args.force_catalog)
    else:
        # Varsayılan argümansız çağrıda yardım göster
        parser.print_help()


if __name__ == "__main__":
    main()
