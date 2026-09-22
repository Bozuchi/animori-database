"""
anizm_scraper.py — Anizm (anizm.net) Veri Çekme ve Yönetim Motoru

Anizm platformundaki tüm anime arşivini (~4.845 başlık) ayrık mimariye uygun
olarak api/sources/anizm/ dizinine indirir ve günceller.

Özellikler:
    - Tam Çekim (--full): Tüm kataloğu çoklu iş parçacığıyla (ThreadPoolExecutor) çeker.
    - Akıllı Senkronizasyon (smart_sync): Bizde olmayan başlıkları ve yeni bölümleri çeker.
    - Checkpoint & Kesintisiz Devam: data/anizm_processed_slugs.json ile kaldığı yerden devam eder.
    - Atomik Dosya Yazma: Geçici dosya (.tmp) yazıp os.replace ile kaydeder.
    - Otomatik Versiyonlama: MD5 hash tabanlı version.json oluşturur.
    - Ayrık Veri Modeli:
        - api/sources/anizm/anime/{slug}.json
        - api/sources/anizm/animes.json
        - api/sources/anizm/metadata.json
        - api/sources/anizm/latest_episodes.json
        - api/sources/anizm/calendar.json
        - api/sources/anizm/version.json
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
from concurrent.futures import ThreadPoolExecutor, as_completed

from anizm_provider import AnizmProvider
from logger import setup_logger

logger = setup_logger("AnizmScraper")

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("İkinci sinyal alındı, zorla durduruluyor...")
        os._exit(1)
    _shutdown_requested = True
    logger.warning("⏹️  Durdurma sinyali alındı! Mevcut iş parçacıkları tamamlandıktan sonra güvenle çıkılacak...")


class AnizmScraper:
    def __init__(
        self,
        base_dir: str = "api/sources/anizm",
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
        self.catalog_cache_path = os.path.join(data_dir, "anizm_catalog.json")
        self.checkpoint_path = os.path.join(data_dir, "anizm_processed_slugs.json")
        self.workers = workers

        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.anime_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)

        self.provider = AnizmProvider()

        self._meta_lock = threading.Lock()
        self._checkpoint_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._stats_lock = threading.Lock()

        self.processed_slugs = self._load_checkpoint()
        self.metadata = self._load_metadata()
        self.fansubs = self.metadata.setdefault("fansubs", {})
        self.genres = self.metadata.setdefault("genres", {})
        self.themes = self.metadata.setdefault("themes", {})

        self.fansub_name_to_id = {v["name"].lower(): int(k) for k, v in self.fansubs.items() if isinstance(v, dict) and "name" in v}
        self.max_fansub_id = max([int(k) for k in self.fansubs.keys()], default=0)

        self.genre_name_to_id = {v.lower(): int(k) for k, v in self.genres.items() if isinstance(v, str)}
        self.max_genre_id = max([int(k) for k in self.genres.keys()], default=0)

        self.theme_name_to_id = {v.lower(): int(k) for k, v in self.themes.items() if isinstance(v, str)}
        self.max_theme_id = max([int(k) for k in self.themes.keys()], default=0)

        self.stats = {
            "total_catalog": 0,
            "already_processed": len(self.processed_slugs),
            "successfully_scraped": 0,
            "total_episodes": 0,
            "total_videos": 0,
            "failed_titles": 0,
        }

    # ─────────────────────────────────────────────────────────────
    # CHECKPOINT & METADATA YÖNETİMİ
    # ─────────────────────────────────────────────────────────────

    def _load_checkpoint(self) -> set[str]:
        disk_slugs = set()
        if os.path.exists(self.anime_dir):
            disk_slugs = {fn.replace(".json", "") for fn in os.listdir(self.anime_dir) if fn.endswith(".json")}

        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    file_slugs = set(str(x) for x in data)
                    # Yalnızca diskte gerçekten var olanları geçerli kabul et
                    return file_slugs & disk_slugs
            except Exception as e:
                logger.warning(f"Checkpoint okunamadı: {e}")
        return disk_slugs

    def _save_checkpoint(self):
        with self._checkpoint_lock:
            try:
                with open(self.checkpoint_path, "w", encoding="utf-8") as f:
                    json.dump(sorted(list(self.processed_slugs)), f, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Checkpoint kaydedilemedi: {e}")

    def _load_metadata(self) -> dict:
        if os.path.exists(self.metadata_path):
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"metadata.json okunamadı: {e}")
        return {"genres": {}, "themes": {}, "fansubs": {}}

    def save_metadata(self):
        with self._meta_lock:
            try:
                with open(self.metadata_path, "w", encoding="utf-8") as f:
                    json.dump(self.metadata, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"metadata.json kaydedilemedi: {e}")

    def get_or_create_fansub_id(self, fansub_name: str | None) -> int | None:
        if not fansub_name:
            return None
        clean_name = fansub_name.strip()
        if not clean_name:
            return None

        key = clean_name.lower()
        with self._meta_lock:
            if key in self.fansub_name_to_id:
                return self.fansub_name_to_id[key]

            self.max_fansub_id += 1
            new_id = self.max_fansub_id
            self.fansubs[str(new_id)] = {"name": clean_name}
            self.fansub_name_to_id[key] = new_id
            return new_id

    def get_or_create_genre_id(self, genre_name: str | None) -> int | None:
        if not genre_name:
            return None
        clean_name = genre_name.strip()
        if not clean_name:
            return None

        key = clean_name.lower()
        with self._meta_lock:
            if key in self.genre_name_to_id:
                return self.genre_name_to_id[key]

            self.max_genre_id += 1
            new_id = self.max_genre_id
            self.genres[str(new_id)] = clean_name
            self.genre_name_to_id[key] = new_id
            return new_id

    def get_or_create_theme_id(self, theme_name: str | None) -> int | None:
        if not theme_name:
            return None
        clean_name = theme_name.strip()
        if not clean_name:
            return None

        key = clean_name.lower()
        with self._meta_lock:
            if key in self.theme_name_to_id:
                return self.theme_name_to_id[key]

            self.max_theme_id += 1
            new_id = self.max_theme_id
            self.themes[str(new_id)] = clean_name
            self.theme_name_to_id[key] = new_id
            return new_id

    # ─────────────────────────────────────────────────────────────
    # KATALOG YÖNETİMİ
    # ─────────────────────────────────────────────────────────────

    def fetch_catalog(self, force_refresh: bool = False) -> list[dict]:
        if not force_refresh and os.path.exists(self.catalog_cache_path):
            try:
                with open(self.catalog_cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, list) and len(cached) > 0:
                    logger.info(f"📂 Anizm kataloğu önbellekten yüklendi ({len(cached)} başlık).")
                    return cached
            except Exception as e:
                logger.warning(f"Katalog önbelleği okunamadı: {e}")

        catalog = self.provider.fetch_catalog()
        if catalog:
            try:
                with open(self.catalog_cache_path, "w", encoding="utf-8") as f:
                    json.dump(catalog, f, ensure_ascii=False, indent=2)
                logger.info(f"💾 Anizm kataloğu önbelleğe kaydedildi ({len(catalog)} başlık).")
            except Exception as e:
                logger.warning(f"Katalog önbelleğe yazılamadı: {e}")

        return catalog

    # ─────────────────────────────────────────────────────────────
    # TEKİL ANİME ÇEKİMİ (SCRAPE TITLE)
    # ─────────────────────────────────────────────────────────────

    def scrape_title(self, slug: str, catalog_item: dict | None = None) -> tuple[dict | None, dict | None]:
        """
        Belirtilen anime slug'ının tüm bölümlerini ve videolarını çeker.
        Dosyayı api/sources/anizm/anime/{slug}.json olarak atomik kaydeder.
        """
        detail = self.provider.fetch_anime_detail(slug)
        if not detail:
            logger.warning(f"Anime detayı alınamadı: {slug}")
            return None, None

        anime_name = detail.get("name") or (catalog_item.get("name") if catalog_item else slug.replace("-", " ").title())
        logger.info(f"🎬 [{slug}] {anime_name} işleniyor ({detail.get('episode_count', 0)} bölüm)...")

        # Tür ve Tema ID'lerini bağla
        raw_genres = detail.get("genres", [])
        raw_themes = detail.get("themes", [])

        genre_ids = []
        for g in raw_genres:
            if isinstance(g, int):
                if g not in genre_ids:
                    genre_ids.append(g)
            elif isinstance(g, str):
                gid = self.get_or_create_genre_id(g)
                if gid and gid not in genre_ids:
                    genre_ids.append(gid)

        theme_ids = []
        for th in raw_themes:
            if isinstance(th, int):
                if th not in theme_ids:
                    theme_ids.append(th)
            elif isinstance(th, str):
                tid = self.get_or_create_theme_id(th)
                if tid and tid not in theme_ids:
                    theme_ids.append(tid)

        episodes = detail.get("episodes", [])
        built_episodes = []
        total_vids = 0

        for ep in episodes:
            if _shutdown_requested:
                logger.warning(f"⏹️  Kapatma isteği nedeniyle '{slug}' eksik kaydedilmedi (temiz çıkış yapılıyor).")
                return None, None

            ep_slug = ep.get("episode_slug")
            ep_num = ep.get("episode_number")

            # Bölüm videolarını çöz
            videos = self.provider.fetch_episode_videos(ep_slug)
            built_vids = []

            for v in videos:
                fansub_name = v.get("fansub")
                fansub_id = self.get_or_create_fansub_id(fansub_name)

                built_vids.append({
                    "id": v.get("id"),
                    "player": v.get("player", "DIGER"),
                    "url": v.get("url"),
                    "fansub": fansub_name,
                    "fansub_id": fansub_id,
                    "translator": v.get("translator"),
                })

            total_vids += len(built_vids)
            built_episodes.append({
                "episode_number": ep_num,
                "name": f"{ep_num}. Bölüm",
                "episode_slug": ep_slug,
                "url": ep.get("url"),
                "videos": built_vids,
            })

        anime_obj = {
            "slug": slug,
            "name": anime_name,
            "original_name": detail.get("original_name"),
            "studio": detail.get("studio"),
            "genres": genre_ids,
            "themes": theme_ids,
            "description": detail.get("description"),
            "poster": detail.get("poster"),
            "episode_count": len(built_episodes),
            "episodes": built_episodes,
        }

        # Atomik dosya kaydı
        temp_file = os.path.join(self.anime_dir, f"{slug}.tmp")
        final_file = os.path.join(self.anime_dir, f"{slug}.json")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(anime_obj, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, final_file)
        except Exception as e:
            logger.error(f"Anime dosyası kaydedilemedi ({slug}): {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass
            return None, None

        # Vitrin (animes.json) için hafif nesne
        vitrin_item = {
            "slug": slug,
            "name": anime_name,
            "original_name": detail.get("original_name"),
            "studio": detail.get("studio"),
            "poster": detail.get("poster"),
            "episode_count": len(built_episodes),
            "genres": genre_ids,
        }

        with self._stats_lock:
            self.stats["successfully_scraped"] += 1
            self.stats["total_episodes"] += len(built_episodes)
            self.stats["total_videos"] += total_vids

        return anime_obj, vitrin_item

    # ─────────────────────────────────────────────────────────────
    # VİTRİN & İNDEKS OLUŞTURMA
    # ─────────────────────────────────────────────────────────────

    def build_animes_index(self) -> int:
        """api/sources/anizm/anime/*.json dosyalarından animes.json vitrinini oluşturur."""
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
                    "slug": data.get("slug"),
                    "name": data.get("name"),
                    "original_name": data.get("original_name"),
                    "studio": data.get("studio"),
                    "poster": data.get("poster"),
                    "episode_count": data.get("episode_count", len(data.get("episodes", []))),
                    "total_episodes": len(data.get("episodes", [])),
                    "genres": data.get("genres", []),
                })
            except Exception as e:
                logger.warning(f"Anime dosyası okunamadı ({fn}): {e}")

        index.sort(key=lambda x: (x.get("name") or "").lower())

        with self._index_lock:
            try:
                with open(self.animes_json_path, "w", encoding="utf-8") as f:
                    json.dump(index, f, ensure_ascii=False, indent=2)
                logger.info(f"✅ animes.json oluşturuldu ({len(index)} başlık).")
            except Exception as e:
                logger.error(f"animes.json kaydedilemedi: {e}")

        return len(index)

    def update_latest_episodes(self, max_pages: int = 5) -> list[dict]:
        """Anizm son eklenen bölümlerini çekip latest_episodes.json dosyasına kaydeder."""
        all_latest = []
        for p in range(1, max_pages + 1):
            eps = self.provider.fetch_latest_episodes(page=p)
            if not eps:
                break
            all_latest.extend(eps)

        if all_latest:
            try:
                with open(self.latest_episodes_path, "w", encoding="utf-8") as f:
                    json.dump(all_latest, f, ensure_ascii=False, indent=2)
                logger.info(f"✅ latest_episodes.json güncellendi ({len(all_latest)} kayıt).")
            except Exception as e:
                logger.error(f"latest_episodes.json yazılamadı: {e}")

        return all_latest

    def update_calendar(self) -> list[dict]:
        """Anizm haftalık yayın takvimini çeker ve calendar.json dosyasına kaydeder."""
        cal = self.provider.fetch_calendar()
        if cal:
            try:
                with open(self.calendar_path, "w", encoding="utf-8") as f:
                    json.dump(cal, f, ensure_ascii=False, indent=2)
                logger.info(f"✅ calendar.json güncellendi ({len(cal)} gün).")
            except Exception as e:
                logger.error(f"calendar.json yazılamadı: {e}")
        return cal

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
    # DELTA SENKRONİZASYON (SON EKLENEN BÖLÜMLER)
    # ─────────────────────────────────────────────────────────────

    def delta_sync(self, pages: int = 3):
        """
        Yalnızca son eklenen bölümler akışını ve takvimi tarayarak etkilenen
        animeleri ve vitrini günceller (Hızlı CRON / GitHub Actions modu).
        """
        global _shutdown_requested
        logger.info("=" * 65)
        logger.info(f"⚡ Anizm Delta Senkronizasyon Başlatılıyor (Son {pages} Sayfa)...")
        logger.info("=" * 65)

        affected_slugs = set()

        # 1. Son eklenen bölümleri tara
        logger.info(f"  📺 Son eklenen bölümler taranıyor ({pages} sayfa)...")
        for p in range(1, pages + 1):
            eps = self.provider.fetch_latest_episodes(page=p)
            for ep in eps:
                slug = ep.get("anime_slug")
                if slug:
                    affected_slugs.add(slug)

        # 2. Takvimi tara
        logger.info("  📅 Haftalık yayın takvimi kontrol ediliyor...")
        cal = self.provider.fetch_calendar()
        for day in cal:
            for item in day.get("items", []):
                slug = item.get("slug")
                if slug:
                    # Yerelde dosyası yoksa veya devam ediyorsa ekle
                    file_path = os.path.join(self.anime_dir, f"{slug}.json")
                    if not os.path.exists(file_path):
                        affected_slugs.add(slug)

        logger.info(f"🔎 Toplam {len(affected_slugs)} anime başlığı etkilendi.")
        if not affected_slugs:
            logger.info("Değişiklik tespit edilmedi.")
            return

        updated_count = 0

        def _update_task(slug: str):
            if _shutdown_requested:
                return None
            detail, vitrin = self.scrape_title(slug)
            return slug if detail else None

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_slug = {executor.submit(_update_task, s): s for s in affected_slugs}
            for future in as_completed(future_to_slug):
                if _shutdown_requested:
                    break
                try:
                    res = future.result()
                    if res:
                        updated_count += 1
                        with self._checkpoint_lock:
                            self.processed_slugs.add(res)
                except Exception as e:
                    logger.error(f"Delta güncelleme hatası ({future_to_slug[future]}): {e}")

        self._save_checkpoint()
        self.save_metadata()

        if updated_count > 0:
            logger.info(f"✅ Toplam {updated_count} anime başarıyla güncellendi.")
            self.build_animes_index()
            self.update_latest_episodes()
            self.update_calendar()
            self.update_versions()
        else:
            logger.info("Hiçbir anime güncellenemedi.")

    # ─────────────────────────────────────────────────────────────
    # AKILLI DİFERANSİYEL SENKRONİZASYON (SMART SYNC)
    # ─────────────────────────────────────────────────────────────

    def smart_sync(self, force_catalog: bool = False, limit: int | None = None):
        """
        Anizm için akıllı diferansiyel tarama:
        1. Sitemaptan kataloğu çeker (~4.845 başlık).
        2. Yerel veritabanında hiç olmayan yeni anime slug'larını tespit eder.
        3. Son eklenen bölümler akışındaki _id'leri yerel latest_episodes.json ile karşılaştırır.
           Yeni bir bölüm gelmişse sadece o animenin slug'ını güncelleme kuyruğuna ekler.
        4. Yeni içerik yoksa 0 istek atarak anında çıkar.
        """
        global _shutdown_requested
        logger.info("=" * 65)
        logger.info("🧠 Anizm Akıllı Senkronizasyon (Smart Sync) Başlatılıyor...")
        logger.info("=" * 65)

        # 1. Kataloğu al
        catalog = self.fetch_catalog(force_refresh=force_catalog)
        if not catalog:
            logger.error("Katalog boş, işlem durduruldu.")
            return

        # 2. Yerel anime slug'larını indeksle
        logger.info("📂 Yerel anime arşivi taranıyor...")
        local_slugs = set()
        for fn in os.listdir(self.anime_dir):
            if fn.endswith(".json"):
                local_slugs.add(fn.replace(".json", ""))

        logger.info(f"✅ {len(local_slugs)} yerel anime mevcut.")

        to_download_candidates = []
        candidate_slugs = set()
        new_anime_count = 0

        # 3. Bizde hiç olmayan yeni animeleri tespit et
        for item in catalog:
            slug = item.get("slug")
            if not slug:
                continue
            if slug not in local_slugs:
                to_download_candidates.append(item)
                candidate_slugs.add(slug)
                new_anime_count += 1

        # 4. Son Eklenen Bölümler Kontrolü (latest_episodes.json farkı)
        known_ep_ids = set()
        if os.path.exists(self.latest_episodes_path):
            try:
                with open(self.latest_episodes_path, "r", encoding="utf-8") as f:
                    for ep in json.load(f):
                        if isinstance(ep, dict) and ep.get("_id"):
                            known_ep_ids.add(ep["_id"])
            except Exception as e:
                logger.warning(f"latest_episodes.json okunamadı: {e}")

        logger.info(f"📋 Yerelde kayıtlı bilinen son bölüm sayısı: {len(known_ep_ids)}")
        logger.info("📡 Canlı son bölüm akışı taranıyor...")

        new_episodes_count = 0
        new_episode_target_slugs = set()

        for p in range(1, 6):
            last_eps = self.provider.fetch_latest_episodes(page=p)
            if not last_eps:
                break

            page_has_new = False
            for ep in last_eps:
                ep_id = ep.get("_id")
                anime_slug = ep.get("anime_slug")

                if ep_id and ep_id not in known_ep_ids:
                    new_episodes_count += 1
                    page_has_new = True
                    if anime_slug and anime_slug not in candidate_slugs:
                        new_episode_target_slugs.add(anime_slug)
                        to_download_candidates.append({"slug": anime_slug, "name": ep.get("title")})
                        candidate_slugs.add(anime_slug)

            if not page_has_new and len(known_ep_ids) > 0:
                break

        logger.info(
            f"📊 Tarama Özeti: {new_anime_count} Yeni Anime | "
            f"{new_episodes_count} Yeni Bölüm Yayını ({len(new_episode_target_slugs)} Hedef Anime) | "
            f"{len(local_slugs)} Mevcut Anime"
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
        logger.info(f"⚙️  {self.workers} iş parçacığı ile {len(to_download_candidates)} anime indiriliyor...")
        start_time = time.time()
        success_count = 0
        total_sync = len(to_download_candidates)

        def _sync_worker(item: dict):
            if _shutdown_requested:
                return None
            slug = item.get("slug")
            if not slug:
                return None
            anime_obj, vitrin = self.scrape_title(slug, catalog_item=item)
            return slug if anime_obj else None

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_slug = {executor.submit(_sync_worker, item): item.get("slug") for item in to_download_candidates}
            for future in as_completed(future_to_slug):
                if _shutdown_requested:
                    logger.warning("⏹️  Durdurma talebi algılandı...")
                    break
                try:
                    res_slug = future.result()
                    if res_slug:
                        success_count += 1
                        with self._checkpoint_lock:
                            self.processed_slugs.add(res_slug)
                except Exception as e:
                    logger.error(f"Hata ({future_to_slug[future]}): {e}")

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

    # ─────────────────────────────────────────────────────────────
    # TAM ARŞİV ÇEKİMİ (FULL SCRAPE)
    # ─────────────────────────────────────────────────────────────

    def scrape_all(self, limit: int | None = None, force_catalog: bool = False):
        """Tüm Anizm arşivini baştan veya checkpointten devam ederek çeker."""
        global _shutdown_requested
        logger.info("=" * 65)
        logger.info("🚀 Anizm Tam Arşiv Çekimi Başlatılıyor...")
        logger.info("=" * 65)

        catalog = self.fetch_catalog(force_refresh=force_catalog)
        self.stats["total_catalog"] = len(catalog)

        candidates = [item for item in catalog if item.get("slug") and item.get("slug") not in self.processed_slugs]
        if limit:
            candidates = candidates[:limit]

        logger.info(f"📋 Toplam: {len(catalog)} | Daha Önce Çekilen: {len(self.processed_slugs)} | Kalan Çekilecek: {len(candidates)}")
        if not candidates:
            logger.info("Tüm başlıklar zaten çekilmiş.")
            self.build_animes_index()
            self.save_metadata()
            self.update_latest_episodes()
            self.update_calendar()
            self.update_versions()
            return

        start_time = time.time()
        completed_count = 0
        total_candidates = len(candidates)

        def _worker_task(item: dict):
            if _shutdown_requested:
                return None
            slug = item.get("slug")
            detail, vitrin = self.scrape_title(slug, catalog_item=item)
            return slug if detail else None

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_slug = {executor.submit(_worker_task, item): item.get("slug") for item in candidates}
            for future in as_completed(future_to_slug):
                if _shutdown_requested:
                    logger.warning("⏹️  Durdurma talebi algılandı...")
                    break
                try:
                    res_slug = future.result()
                    if res_slug:
                        with self._checkpoint_lock:
                            self.processed_slugs.add(res_slug)
                except Exception as e:
                    logger.error(f"Hata ({future_to_slug[future]}): {e}")

                completed_count += 1
                if completed_count % 20 == 0 or completed_count == total_candidates:
                    elapsed = time.time() - start_time
                    rate = completed_count / elapsed if elapsed > 0 else 0
                    remaining = (total_candidates - completed_count) / rate if rate > 0 else 0
                    logger.info(
                        f"📊 İlerleme: {completed_count}/{total_candidates} "
                        f"({completed_count / total_candidates * 100:.1f}%) — "
                        f"Hız: {rate:.1f} anime/sn — Kalan: {remaining / 60:.1f} dk"
                    )
                    self._save_checkpoint()
                    self.save_metadata()

        self._save_checkpoint()
        self.save_metadata()
        self.build_animes_index()
        self.update_latest_episodes()
        self.update_calendar()
        self.update_versions()


def main():
    signal.signal(signal.SIGINT, _handle_signal)

    parser = argparse.ArgumentParser(description="Anizm Arşiv Çekim ve Yönetim Motoru")
    parser.add_argument("--full", action="store_true", help="Tüm Anizm arşivini çeker.")
    parser.add_argument("--limit", type=int, default=None, help="İşlenecek maksimum başlık adedi.")
    parser.add_argument("--workers", type=int, default=2, help="Paralel iş parçacığı adedi (Varsayılan: 2).")
    parser.add_argument("--force-catalog", action="store_true", help="Kataloğu sitemaptan yeniden indirir.")
    parser.add_argument("--build-index", action="store_true", help="animes.json vitrinini yeniden kurar.")
    parser.add_argument("--max-hours", type=float, default=None, help="Maksimum çalışma süresi (Saat).")

    args = parser.parse_args()
    timer = None

    if args.max_hours and args.max_hours > 0:
        def _timeout():
            global _shutdown_requested
            logger.warning(f"⏰ Maksimum çalışma süresi ({args.max_hours} saat) doldu. Güvenli kapanış başlatılıyor...")
            _shutdown_requested = True

        timer = threading.Timer(args.max_hours * 3600, _timeout)
        timer.daemon = True
        timer.start()

    scraper = AnizmScraper(workers=args.workers)

    try:
        if args.build_index:
            scraper.build_animes_index()
            scraper.save_metadata()
            scraper.update_latest_episodes()
            scraper.update_calendar()
            scraper.update_versions()
        elif args.full or args.limit:
            scraper.scrape_all(limit=args.limit, force_catalog=args.force_catalog)
        else:
            scraper.smart_sync(force_catalog=args.force_catalog)
    finally:
        if timer:
            timer.cancel()


if __name__ == "__main__":
    main()
