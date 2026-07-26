"""
storage_manager.py — Dosya ve Klasör Yöneticisi

Çekilen anime verilerini statik JSON dosyaları olarak organize eder:
    - api/anime/{mal_id}.json  → Her anime için detaylı veri (Jikan'da yoksa {slug}.json)
    - api/animes.json        → Hafifletilmiş indeks (vitrin için)
    - api/metadata.json      → Ortak metadata (türler, stüdyolar ve fansublar)
    - api/slug_map.json      → Hızlı arama için slug -> dosya adı haritası
    - api/version.json       → Son güncelleme bilgisi
    - api/latest_episodes.json → Son eklenen bölümlerin listesi (en yeniden eskiye, max 100)
"""

import os
import json
import hashlib
from datetime import datetime
from logger import setup_logger


class StorageManager:
    """Statik JSON API dosyalarını yöneten sınıf."""

    # latest_episodes.json kapasite limiti
    LATEST_EPISODES_LIMIT = 100

    def __init__(self, base_dir: str = "api"):
        self.base_dir = base_dir
        self.anime_dir = os.path.join(base_dir, "anime")
        self.slug_map_path = os.path.join(base_dir, "slug_map.json")
        self.metadata_path = os.path.join(base_dir, "metadata.json")
        self.latest_episodes_path = os.path.join(base_dir, "latest_episodes.json")
        self.logger = setup_logger("Storage")

        # Klasörleri oluştur
        os.makedirs(self.anime_dir, exist_ok=True)
        
        # Hızlı arama için slug -> dosya_adi haritası
        self.slug_to_file = {}
        self._load_slug_map()
        self.metadata = self._load_metadata()

        # Bu oturumda yeni eklenen bölümleri biriktiren liste
        self._newly_added_episodes: list[dict] = []

    def _load_metadata(self) -> dict:
        """metadata.json dosyasını yükler."""
        if os.path.exists(self.metadata_path):
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "genres" not in data:
                        data["genres"] = {}
                    if "studios" not in data:
                        data["studios"] = {}
                    if "fansubs" not in data:
                        data["fansubs"] = {}
                    return data
            except Exception as e:
                self.logger.warning(f"metadata.json okunamadı: {e}")
        return {"genres": {}, "studios": {}, "fansubs": {}}

    def save_metadata(self):
        """metadata.json dosyasını diske kaydeder."""
        try:
            with open(self.metadata_path, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, ensure_ascii=False, indent=4)
            self.logger.info("💾 metadata.json güncellendi.")
        except Exception as e:
            self.logger.error(f"metadata.json kaydedilemedi: {e}")

    def _load_slug_map(self):
        """
        slug -> filename eşleşmesini yükler.
        Önce slug_map.json'dan okumayı dener (hızlı).
        Dosya yoksa api/anime/*.json'ları tarayarak yeniden oluşturur.
        """
        # Önce slug_map.json'dan yüklemeyi dene (hızlı)
        if os.path.exists(self.slug_map_path):
            try:
                with open(self.slug_map_path, "r", encoding="utf-8") as f:
                    self.slug_to_file = json.load(f)
                self.logger.info(f"✅ slug_map.json yüklendi. ({len(self.slug_to_file)} anime)")
                return
            except Exception as e:
                self.logger.warning(f"slug_map.json parse hatası, tam taramaya geçiliyor: {e}")

        # slug_map.json yoksa veya bozuksa, tüm dosyaları tarayarak oluştur
        self.logger.info("⏳ slug_map.json bulunamadı, anime dosyaları taranıyor...")
        if not os.path.exists(self.anime_dir):
            return

        for filename in os.listdir(self.anime_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(self.anime_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        slug = data.get("turkanime", {}).get("slug") or data.get("slug")
                        if slug:
                            self.slug_to_file[slug] = filename
                except Exception as e:
                    self.logger.warning(f"Anime dosyası okunamadı ({filename}): {e}")
                    continue

        # Tarama sonucunu kaydet (bir sonraki çalıştırmada hızlı yüklensin)
        self._save_slug_map()
        self.logger.info(f"✅ slug_map.json oluşturuldu. ({len(self.slug_to_file)} anime)")

    def _save_slug_map(self):
        """slug_to_file haritasını slug_map.json'a kaydeder."""
        try:
            with open(self.slug_map_path, "w", encoding="utf-8") as f:
                json.dump(self.slug_to_file, f, ensure_ascii=False)
        except Exception as e:
            self.logger.warning(f"slug_map.json kaydedilemedi: {e}")

    def save_slug_map(self):
        """slug_to_file haritasını diske kaydeder (dış kullanım için)."""
        self._save_slug_map()

    def save_anime_detail(self, slug: str, turkanime_data: dict, jikan_data: dict | None = None, anilist_data: dict | None = None):
        """
        Tek bir anime için birleştirilmiş detay dosyası oluşturur.

        Yapı:
            {
                "turkanime": { "slug": "naruto-shippuuden", "isim": "Naruto Shippuuden", "puan": "9.50", "bolum_durumu": "500/500", "ozet": "..." },
                "jikan": { "mal_id": 1735, "image_url": "...", ... } veya null,
                "anilist": { "id": 1735, "banner_image": "https://..." } veya null
            }

        Args:
            slug: Anime slug'ı (dosya adı olarak kullanılır).
            turkanime_data: Türkanime'den gelen veriler.
            jikan_data: Jikan'dan gelen zenginleştirilmiş veriler (veya None).
            anilist_data: AniList'ten gelen veriler (veya None — mevcut dosyadaki anilist korunur).
        """
        # Orijinal dict'i mutasyona uğratma — slug eklenmiş kopya oluştur
        turkanime_obj = {**turkanime_data, "slug": slug}
        
        detail = {
            "turkanime": turkanime_obj,
            "jikan": jikan_data,
        }

        # AniList verisi varsa ekle; None ise mevcut dosyadaki anilist korunur (for döngüsü ile)
        if anilist_data is not None:
            detail["anilist"] = anilist_data

        # Mevcut dosyada 'episodes' (veya başka ek alanlar) varsa koru.
        # save_anime_detail sadece turkanime/jikan verisini günceller;
        # daha önce save_episodes ile yazılmış bölüm listesini silmemeli.
        existing = self.load_anime_detail(slug)
        if existing:
            for key, value in existing.items():
                if key not in detail:
                    detail[key] = value

        if "anilist" not in detail:
            detail["anilist"] = None

        # Dosya ismi Jikan'da varsa mal_id, yoksa slug olur
        file_basename = slug
        if jikan_data and jikan_data.get("mal_id"):
            file_basename = str(jikan_data["mal_id"])
            
        filename = f"{file_basename}.json"
        filepath = os.path.join(self.anime_dir, filename)
        
        # Eğer önceden başka bir isimle (örneğin slug.json) kaydedildiyse ve
        # şimdi isim mal_id.json olarak değiştiyse, eskisini sil
        old_filename = self.slug_to_file.get(slug)
        if old_filename and old_filename != filename:
            old_filepath = os.path.join(self.anime_dir, old_filename)
            if os.path.exists(old_filepath):
                os.remove(old_filepath)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(detail, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            self.logger.error(f"Anime detay dosyası kaydedilemedi ({slug}): {e}")
            return
            
        # Hafızadaki haritayı güncelle
        self.slug_to_file[slug] = filename

    def load_anime_detail(self, slug: str) -> dict | None:
        """
        Mevcut bir anime detay dosyasını okur.

        Args:
            slug: Anime slug'ı.

        Returns:
            dict: Anime verisi veya None (dosya yoksa).
        """
        filename = self.slug_to_file.get(slug)
        if not filename:
            return None
            
        filepath = os.path.join(self.anime_dir, filename)
        if not os.path.exists(filepath):
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self.logger.warning(f"Dosya okunamadı: {filepath} — {e}")
            return None

    def save_episodes(self, slug: str, episodes: list[dict], mal_id: int | None = None):
        """
        Mevcut anime JSON dosyasına 'episodes' dizisini ekler/günceller.
        Mevcut turkanime ve jikan verilerini korur.
        Yeni eklenen bölümleri _newly_added_episodes listesine biriktirir.

        Args:
            slug: Anime slug'ı.
            episodes: Bölüm verileri listesi.
            mal_id: MyAnimeList anime ID'si (latest_episodes için).
        """
        existing = self.load_anime_detail(slug)
        if existing is None:
            self.logger.warning(f"Episodes kaydedilemedi, anime dosyası bulunamadı: {slug}")
            return

        # ── Yeni bölümleri tespit et (latest_episodes için) ──
        # Her anime için sadece en yüksek episode_number'ı tut
        old_titles = set()
        if existing.get("episodes"):
            for ep in existing["episodes"]:
                old_titles.add(ep.get("turkanime_title"))

        best_new_ep = None
        for ep in episodes:
            title = ep.get("turkanime_title")
            if title and title not in old_titles and ep.get("added_date"):
                best_new_ep = {
                    "mal_id": mal_id,
                    "title": title,
                    "episode_number": ep.get("episode_number"),
                    "added_date": ep.get("added_date"),
                }

        if best_new_ep is not None:
            self._newly_added_episodes.append(best_new_ep)

        existing["episodes"] = episodes

        filename = self.slug_to_file.get(slug)
        if filename:
            filepath = os.path.join(self.anime_dir, filename)
            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
            except (IOError, OSError) as e:
                self.logger.error(f"Episodes kaydedilemedi ({slug}): {e}")

    def build_index(self) -> int:
        """
        Tüm anime detay dosyalarını tarayarak kapsamlı indeks oluşturur.

        animes.json içeriği:
            [
                {
                    "mal_id": 1735,
                    "title": "Naruto: Shippuuden",
                    "title_english": "Naruto: Shippuden",
                    "slug": "naruto-shippuuden",
                    "image_url": "https://cdn.myanimelist.net/...",
                    "type": "TV",
                    "status": "Finished Airing",
                    "year": 2007,
                    "season": "spring",
                    "score": 8.25,
                    "popularity": 8,
                    "genres": [1, 27],
                    "themes": [17],
                    "demographics": [27],
                    "studios": [37]
                },
                ...
            ]

        Returns:
            int: Toplam anime sayısı.
        """
        index = []

        for filename in sorted(os.listdir(self.anime_dir)):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(self.anime_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            turkanime = data.get("turkanime", {})
            jikan = data.get("jikan") or {}
            anilist = data.get("anilist") or {}
            
            slug_val = turkanime.get("slug") or data.get("slug")
            
            # mal_id'yi belirle (jikan'da yoksa fallback olarak slug kullan)
            mal_id_val = jikan.get("mal_id") if jikan.get("mal_id") else None
            
            # İlişkili listeleri filtrele (zaten jikan'da integer array olarak geliyorlar)
            genres = jikan.get("genres", [])
            themes = jikan.get("themes", [])
            demographics = jikan.get("demographics", [])
            studios = jikan.get("studios", [])

            index.append({
                "mal_id": mal_id_val,
                "anilist_id": anilist.get("id"),
                "title": jikan.get("title") or turkanime.get("isim", ""),
                "title_english": jikan.get("title_english"),
                "slug": slug_val,
                "image_url": jikan.get("image_url"),
                "type": jikan.get("type"),
                "status": jikan.get("status"),
                "year": jikan.get("year"),
                "season": jikan.get("season"),
                "score": jikan.get("score"),
                "popularity": jikan.get("popularity"),
                "genres": genres,
                "themes": themes,
                "demographics": demographics,
                "studios": studios
            })

        # İndeks dosyasını yaz
        index_path = os.path.join(self.base_dir, "animes.json")
        try:
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            self.logger.error(f"animes.json kaydedilemedi: {e}")
            return 0

        self.logger.info(f"✅ animes.json oluşturuldu. ({len(index)} anime)")
        return len(index)

    @staticmethod
    def _compute_file_hash(filepath: str) -> str | None:
        """
        Dosyanın içeriğinin MD5 hash'inin ilk 8 karakterini döndürür.
        Değişiklik tespiti amaçlıdır, kriptografik güvenlik gerektirmez.

        Args:
            filepath: Hash'i hesaplanacak dosyanın yolu.

        Returns:
            str: 8 karakterlik hash veya None (dosya yoksa).
        """
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()[:8]
        except (IOError, OSError):
            return None

    def update_versions(self):
        """
        version.json dosyasını hash tabanlı değişiklik tespiti ile günceller.

        Takip edilen dosyalar:
            - animes.json     → İndeks
            - metadata.json   → Ortak metadata (türler, stüdyolar, fansublar)
            - latest_episodes.json → Son eklenen bölümler

        Mantık:
            - Her dosyanın MD5 hash'i hesaplanır (ilk 8 karakter)
            - Hash değişmişse → last_updated güncellenir
            - Hash aynıysa → eski last_updated korunur
            - Sonuç version.json'a yazılır

        Yapı:
            {
                "animes": {
                    "last_updated": "20260719_0337",
                    "hash": "a1b2c3d4"
                },
                "metadata": {
                    "last_updated": "20260719_0337",
                    "hash": "e5f6g7h8"
                },
                "latest_episodes": {
                    "last_updated": "20260719_0337",
                    "hash": "i9j0k1l2"
                }
            }
        """
        version_path = os.path.join(self.base_dir, "version.json")
        now_str = datetime.now().strftime("%Y%m%d_%H%M")

        # Mevcut version.json'ı oku (varsa)
        old_version = {}
        if os.path.exists(version_path):
            try:
                with open(version_path, "r", encoding="utf-8") as f:
                    old_version = json.load(f)
            except (json.JSONDecodeError, IOError):
                old_version = {}

        # Takip edilecek dosyalar ve ek alanları
        tracked_files = {
            "animes": os.path.join(self.base_dir, "animes.json"),
            "metadata": self.metadata_path,
            "latest_episodes": self.latest_episodes_path,
        }

        new_version = {}
        changed_keys = []

        for key, filepath in tracked_files.items():
            new_hash = self._compute_file_hash(filepath)
            old_entry = old_version.get(key, {})
            old_hash = old_entry.get("hash")

            if new_hash is None:
                # Dosya henüz oluşmamış → atla
                continue

            if new_hash != old_hash:
                # İçerik değişmiş → last_updated güncelle
                entry = {"last_updated": now_str, "hash": new_hash}
                changed_keys.append(key)
            else:
                # İçerik aynı → eski last_updated'i koru
                entry = {"last_updated": old_entry.get("last_updated", now_str), "hash": old_hash}

            new_version[key] = entry

        # Diske yaz
        try:
            with open(version_path, "w", encoding="utf-8") as f:
                json.dump(new_version, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            self.logger.error(f"version.json kaydedilemedi: {e}")
            return

        if changed_keys:
            self.logger.info(f"✅ version.json güncellendi. (değişen: {', '.join(changed_keys)})")
        else:
            self.logger.info("ℹ️  Hiçbir dosya değişmedi, version.json korundu.")

    def update_latest_episodes(self):
        """
        Bu oturumda yeni eklenen bölümleri latest_episodes.json dosyasına yazar.

        Mantık:
            1. Mevcut latest_episodes.json dosyasını oku (varsa)
            2. Bu oturumda biriktirilen yeni bölümleri (_newly_added_episodes) ekle
            3. added_date'e göre en yeniden eskiye doğru sırala
            4. LATEST_EPISODES_LIMIT ile sınırlandır (varsayılan 100)
            5. Dosyayı diske yaz

        latest_episodes.json formatı:
            [
                {
                    "mal_id": 1735,
                    "title": "Naruto Shippuuden 12. Bölüm",
                    "episode_number": 12,
                    "added_date": "2026-07-17T02:15:00"
                },
                ...
            ]
        """
        if not self._newly_added_episodes:
            self.logger.info("ℹ️  Yeni bölüm eklenmedi, latest_episodes.json güncellenmedi.")
            return

        # 1. Mevcut dosyayı oku
        existing_latest = []
        if os.path.exists(self.latest_episodes_path):
            try:
                with open(self.latest_episodes_path, "r", encoding="utf-8") as f:
                    existing_latest = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                self.logger.warning(f"latest_episodes.json okunamadı, sıfırdan oluşturulacak: {e}")
                existing_latest = []

        # 2. Yeni bölümleri ekle
        merged = self._newly_added_episodes + existing_latest

        # 3. added_date'e göre en yeniden eskiye sırala
        merged.sort(key=lambda ep: ep.get("added_date", ""), reverse=True)

        # 4. Her anime için sadece en son eklenen bölümü tut (mal_id bazında tekil)
        seen_mal_ids = set()
        unique_merged = []
        for ep in merged:
            mid = ep.get("mal_id")
            if mid is not None and mid in seen_mal_ids:
                continue
            if mid is not None:
                seen_mal_ids.add(mid)
            unique_merged.append(ep)

        # 5. Kapasite limiti uygula
        unique_merged = unique_merged[:self.LATEST_EPISODES_LIMIT]

        # 6. Diske yaz
        try:
            with open(self.latest_episodes_path, "w", encoding="utf-8") as f:
                json.dump(unique_merged, f, ensure_ascii=False, indent=2)
            self.logger.info(
                f"✅ latest_episodes.json güncellendi. "
                f"(+{len(self._newly_added_episodes)} anime, toplam {len(unique_merged)} kayıt)"
            )
        except (IOError, OSError) as e:
            self.logger.error(f"latest_episodes.json kaydedilemedi: {e}")

        # Biriktirme listesini temizle
        self._newly_added_episodes.clear()
