"""
storage_manager.py — Dosya ve Klasör Yöneticisi

Çekilen anime verilerini statik JSON dosyaları olarak organize eder:
    - api/anime/{slug}.json  → Her anime için detaylı veri
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

        # Klasörleri oluştur
        os.makedirs(self.anime_dir, exist_ok=True)

    def save_anime_detail(self, slug: str, turkanime_data: dict, jikan_data: dict | None = None):
        """
        Tek bir anime için birleştirilmiş detay dosyası oluşturur.

        Yapı:
            {
                "slug": "naruto-shippuuden",
                "turkanime": { isim, puan, bolum_durumu, ozet },
                "jikan": { mal_id, image_url, ... } veya null
            }

        Args:
            slug: Anime slug'ı (dosya adı olarak kullanılır).
            turkanime_data: Türkanime'den gelen veriler.
            jikan_data: Jikan'dan gelen zenginleştirilmiş veriler (veya None).
        """
        detail = {
            "slug": slug,
            "turkanime": turkanime_data,
            "jikan": jikan_data,
        }

        filepath = os.path.join(self.anime_dir, f"{slug}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)

    def load_anime_detail(self, slug: str) -> dict | None:
        """
        Mevcut bir anime detay dosyasını okur.

        Args:
            slug: Anime slug'ı.

        Returns:
            dict: Anime verisi veya None (dosya yoksa).
        """
        filepath = os.path.join(self.anime_dir, f"{slug}.json")
        if not os.path.exists(filepath):
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Storage] ⚠️  Dosya okunamadı: {filepath} — {e}")
            return None

    def build_index(self) -> int:
        """
        Tüm anime detay dosyalarını tarayarak hafifletilmiş indeks oluşturur.

        animes.json içeriği:
            [
                {
                    "slug": "naruto-shippuuden",
                    "isim": "Naruto Shippuuden",
                    "image_url": "https://cdn.myanimelist.net/...",  (Jikan'dan)
                    "puan": "9.50"
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

            # Kapak resmi: Jikan image_url kullanılıyor
            image_url = None
            jikan = data.get("jikan")
            if jikan and jikan.get("image_url"):
                image_url = jikan["image_url"]

            turkanime = data.get("turkanime", {})

            index.append({
                "slug": data.get("slug"),
                "isim": turkanime.get("isim", ""),
                "image_url": image_url,
                "puan": turkanime.get("puan", "0.00"),
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
