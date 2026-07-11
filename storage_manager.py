"""
storage_manager.py — Dosya ve Klasör Yöneticisi

Çekilen anime verilerini statik JSON dosyaları olarak organize eder:
    - api/anime/{mal_id}.json  → Her anime için detaylı veri (Jikan'da yoksa {slug}.json)
    - api/animes.json        → Hafifletilmiş indeks (vitrin için)
    - api/version.json       → Son güncelleme bilgisi
"""

import os
import json
from datetime import datetime


class StorageManager:
    """Statik JSON API dosyalarını yöneten sınıf."""

    def __init__(self, base_dir: str = "api"):
        self.base_dir = base_dir
        self.anime_dir = os.path.join(base_dir, "anime")
        self.slug_map_path = os.path.join(base_dir, "slug_map.json")

        # Klasörleri oluştur
        os.makedirs(self.anime_dir, exist_ok=True)
        
        # Hızlı arama için slug -> dosya_adi haritası
        self.slug_to_file = {}
        self._load_slug_map()

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
                print(f"[Storage] ✅ slug_map.json yüklendi. ({len(self.slug_to_file)} anime)")
                return
            except Exception:
                pass

        # slug_map.json yoksa veya bozuksa, tüm dosyaları tarayarak oluştur
        print("[Storage] ⏳ slug_map.json bulunamadı, anime dosyaları taranıyor...")
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
                except Exception:
                    continue

        # Tarama sonucunu kaydet (bir sonraki çalıştırmada hızlı yüklensin)
        self._save_slug_map()
        print(f"[Storage] ✅ slug_map.json oluşturuldu. ({len(self.slug_to_file)} anime)")

    def _save_slug_map(self):
        """slug_to_file haritasını slug_map.json'a kaydeder."""
        try:
            with open(self.slug_map_path, "w", encoding="utf-8") as f:
                json.dump(self.slug_to_file, f, ensure_ascii=False)
        except Exception as e:
            print(f"[Storage] ⚠️  slug_map.json kaydedilemedi: {e}")

    def save_slug_map(self):
        """slug_to_file haritasını diske kaydeder (dış kullanım için)."""
        self._save_slug_map()

    def save_anime_detail(self, slug: str, turkanime_data: dict, jikan_data: dict | None = None):
        """
        Tek bir anime için birleştirilmiş detay dosyası oluşturur.

        Yapı:
            {
                "turkanime": { "slug": "naruto-shippuuden", "isim": "Naruto Shippuuden", "puan": "9.50", "bolum_durumu": "500/500", "ozet": "..." },
                "jikan": { "mal_id": 1735, "image_url": "...", ... } veya null
            }

        Args:
            slug: Anime slug'ı (dosya adı olarak kullanılır).
            turkanime_data: Türkanime'den gelen veriler.
            jikan_data: Jikan'dan gelen zenginleştirilmiş veriler (veya None).
        """
        # Orijinal dict'i mutasyona uğratma — slug eklenmiş kopya oluştur
        turkanime_obj = {**turkanime_data, "slug": slug}
        
        detail = {
            "turkanime": turkanime_obj,
            "jikan": jikan_data,
        }

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

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
            
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
            print(f"[Storage] ⚠️  Dosya okunamadı: {filepath} — {e}")
            return None

    def save_episodes(self, slug: str, episodes: list[dict]):
        """
        Mevcut anime JSON dosyasına 'episodes' dizisini ekler/günceller.
        Mevcut turkanime ve jikan verilerini korur.

        Args:
            slug: Anime slug'ı.
            episodes: Bölüm verileri listesi.
        """
        existing = self.load_anime_detail(slug)
        if existing is None:
            print(f"[Storage] ⚠️  Episodes kaydedilemedi, anime dosyası bulunamadı: {slug}")
            return

        existing["episodes"] = episodes

        filename = self.slug_to_file.get(slug)
        if filename:
            filepath = os.path.join(self.anime_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)

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
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

        print(f"[Storage] ✅ animes.json oluşturuldu. ({len(index)} anime)")
        return len(index)

    def update_version(self, total_anime: int):
        """
        version.json dosyasını günceller.

        Yapı:
            {
                "last_updated": "20260624_0015",
                "total_anime": 5000
            }

        Args:
            total_anime: Toplam anime sayısı.
        """
        version = {
            "last_updated": datetime.now().strftime("%Y%m%d_%H%M"),
            "total_anime": total_anime,
        }

        version_path = os.path.join(self.base_dir, "version.json")
        with open(version_path, "w", encoding="utf-8") as f:
            json.dump(version, f, ensure_ascii=False, indent=2)

        print(f"[Storage] ✅ version.json güncellendi. ({version['last_updated']})")
