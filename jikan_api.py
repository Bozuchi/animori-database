"""
jikan_api.py — Jikan API Veri Zenginleştirici

Türkanime'den gelen anime isimlerini Jikan API (MyAnimeList) üzerinde
aratarak zengin detayları çeker. Rate limit koruması ve hata yönetimi
içerir.

Endpoints:
    - Arama:   https://api.jikan.moe/v4/anime?q={isim}
    - Detay:   https://api.jikan.moe/v4/anime/{mal_id}/full
"""

import requests
import time
import logging
import re
import json
import os
from urllib.parse import quote


JIKAN_BASE_URL = "https://api.jikan.moe/v4"
REQUEST_TIMEOUT = 15       # saniye
RATE_LIMIT_DELAY = 1.5     # her istek arasındaki bekleme süresi (saniye)
RETRY_MAX = 3              # 429 hatası için maksimum tekrar deneme
RETRY_BACKOFF = 5          # 429 hatası sonrası bekleme süresi (saniye, katlanan)


class JikanEnricher:
    """Jikan API üzerinden anime verilerini zenginleştiren sınıf."""

    def __init__(self, error_log_path: str = "errors.log", mappings_path: str = "manual_mappings.json"):
        self.error_log_path = error_log_path
        self._setup_error_logger()
        self.manual_mappings = self._load_manual_mappings(mappings_path)

    def _load_manual_mappings(self, path: str) -> dict:
        """Manuel slug -> mal_id eşleştirme dosyasını yükler."""
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # _comment gibi string değerli anahtarları filtrele, sadece int mal_id'leri al
                return {k: v for k, v in data.items() if isinstance(v, int)}
        except Exception as e:
            print(f"[Jikan] ⚠️  manual_mappings.json okunamadı: {e}")
            return {}

    def _setup_error_logger(self):
        """Hata kayıtları için dosya logger'ları oluşturur."""
        # Hata logger'ı
        self.error_logger = logging.getLogger("jikan_errors")
        self.error_logger.setLevel(logging.ERROR)

        if not self.error_logger.handlers:
            handler = logging.FileHandler(self.error_log_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s — %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            self.error_logger.addHandler(handler)

    def _log_error(self, message: str):
        """Konsola yazdırır ve errors.log dosyasına kaydeder."""
        print(f"[Jikan] ❌ {message}")
        self.error_logger.error(message)

    def _request_with_retry(self, url: str) -> dict | None:
        """
        HTTP GET isteği yapar. 429 hatalarında backoff ile tekrar dener.

        Returns:
            dict: JSON yanıtı veya None (hata durumunda).
        """
        for attempt in range(RETRY_MAX):
            try:
                response = requests.get(url, timeout=REQUEST_TIMEOUT)

                if response.status_code == 200:
                    return response.json()

                if response.status_code == 429:
                    wait_time = RETRY_BACKOFF * (attempt + 1)
                    print(
                        f"[Jikan] ⏳ 429 Rate Limit! "
                        f"{wait_time}s bekleniyor... (Deneme {attempt + 1}/{RETRY_MAX})"
                    )
                    time.sleep(wait_time)
                    continue

                # Diğer HTTP hataları
                print(f"[Jikan] ⚠️  HTTP {response.status_code}: {url}")
                return None

            except requests.exceptions.Timeout:
                print(
                    f"[Jikan] ⏱️  Timeout hatası "
                    f"(Deneme {attempt + 1}/{RETRY_MAX}): {url}"
                )
                time.sleep(3)

            except requests.exceptions.RequestException as e:
                print(f"[Jikan] ⚠️  Bağlantı hatası: {e}")
                return None

        return None

    def search_anime(self, name: str) -> int | None:
        """
        Anime ismini Jikan API'de aratır ve ilk eşleşen sonucun mal_id'sini döndürür.

        Args:
            name: Aranacak anime ismi.

        Returns:
            int: mal_id veya None (bulunamadıysa).
        """
        # Baştaki _ karakterleri Jikan/MAL backend'inde SQL wildcard olarak
        # yorumlanıp timeout'a sebep olur — temizle
        clean_name = name.lstrip("_").strip()
        if not clean_name:
            return None

        encoded_name = quote(clean_name)
        url = f"{JIKAN_BASE_URL}/anime?q={encoded_name}&limit=3"

        data = self._request_with_retry(url)
        if data and data.get("data"):
            results = data["data"]
            
            # Alfanümerik karşılaştırma için aranan ismi temizle
            # Tüm noktalama işaretlerini ve boşlukları siler, sadece harf ve rakamları bırakır
            clean_searched = re.sub(r'[^a-z0-9]', '', name.lower())
            
            for result in results:
                # Bu sonucun olası isimlerini topla (ana isim ve ingilizce)
                jikan_titles = []
                if result.get("title"):
                    jikan_titles.append(result["title"])
                if result.get("title_english"):
                    jikan_titles.append(result["title_english"])
                
                for j_title in jikan_titles:
                    if not j_title:
                        continue
                    
                    clean_jikan = re.sub(r'[^a-z0-9]', '', j_title.lower())
                    
                    # Tam eşleşme (noktalamalar ve boşluklar hariç)
                    if clean_searched == clean_jikan:
                        return result.get("mal_id")
            
            # Hiçbir sonuçta eşleşme sağlanamadıysa reddet ve logla
            self._log_error(
                f"İsim uyuşmazlığı: Türkanime='{name}' <-> "
                f"Jikan='{results[0].get('title')}'"
            )
            return None

        return None

    def get_full_details(self, mal_id: int) -> dict | None:
        """
        mal_id ile anime detaylarını çeker ve sadece gerekli alanları filtreler.

        Args:
            mal_id: MyAnimeList anime ID'si.

        Returns:
            dict: Filtrelenmiş anime detayları veya None.
        """
        url = f"{JIKAN_BASE_URL}/anime/{mal_id}/full"

        data = self._request_with_retry(url)
        if not data or not data.get("data"):
            return None

        d = data["data"]

        # Genres: sadece mal_id ve name
        genres = [
            {"mal_id": g["mal_id"], "name": g["name"]}
            for g in d.get("genres", [])
        ]

        # Themes: sadece mal_id ve name
        themes = [
            {"mal_id": t["mal_id"], "name": t["name"]}
            for t in d.get("themes", [])
        ]

        # Demographics: sadece mal_id ve name
        demographics = [
            {"mal_id": dm["mal_id"], "name": dm["name"]}
            for dm in d.get("demographics", [])
        ]

        # Relations: relation ve entry içindeki mal_id'ler
        relations = []
        for rel in d.get("relations", []):
            entries = [{"mal_id": e["mal_id"]} for e in rel.get("entry", [])]
            relations.append({
                "relation": rel.get("relation"),
                "entries": entries,
            })

        # Trailer ID: Sadece embed_url'den çıkarmaya çalış
        trailer = d.get("trailer", {})
        trailer_id = None
        
        if trailer.get("embed_url"):
            # Örnek embed_url: "https://www.youtube-nocookie.com/embed/LHtdKWJdif4?enablejsapi=1&wmode=opaque&autoplay=1"
            match = re.search(r'/embed/([^?]+)', trailer.get("embed_url"))
            if match:
                trailer_id = match.group(1)

        # Aired: sadece from ve to (prop hariç)
        aired_raw = d.get("aired", {})
        aired = {
            "from": aired_raw.get("from"),
            "to": aired_raw.get("to"),
        }

        return {
            "mal_id": d.get("mal_id"),
            "image_url": d.get("images", {}).get("jpg", {}).get("image_url"),
            "trailer_id": trailer_id,
            "title_english": d.get("title_english"),
            "type": d.get("type"),
            "source": d.get("source"),
            "status": d.get("status"),
            "airing": d.get("airing"),
            "aired": aired,
            "duration": d.get("duration"),
            "rating": d.get("rating"),
            "score": d.get("score"),
            "popularity": d.get("popularity"),
            "synopsis": d.get("synopsis"),
            "genres": genres,
            "themes": themes,
            "demographics": demographics,
            "relations": relations,
        }

    def enrich(self, name: str, slug: str = "") -> dict | None:
        """
        Anime ismini aratıp detaylarını çeken birleşik metod.

        Adımlar:
            0. Manuel eşleştirme dosyasını kontrol et
            1. İsimle arama yap -> mal_id bul
            2. mal_id ile /full endpoint'inden detayları çek
            3. Hata varsa errors.log'a kaydet, None döndür

        Args:
            name: Türkanime'den gelen anime ismi.
            slug: Anime slug'ı (manuel eşleştirme kontrolü için).

        Returns:
            dict: Zenginleştirilmiş anime verileri veya None.
        """
        # Adım 0: Manuel eşleştirme kontrolü
        manual_mal_id = self.manual_mappings.get(slug)
        if manual_mal_id:
            print(f"[Jikan] 📌 Manuel eşleştirme kullanılıyor: {name} -> mal_id: {manual_mal_id}")
            time.sleep(RATE_LIMIT_DELAY)
            details = self.get_full_details(manual_mal_id)
            if details is None:
                self._log_error(f"Manuel eşleştirme detayları alınamadı: {name} (mal_id: {manual_mal_id})")
            return details

        # Adım 1: Arama
        time.sleep(RATE_LIMIT_DELAY)
        mal_id = self.search_anime(name)

        if mal_id is None:
            self._log_error(f"Anime bulunamadı: {name}")
            return None

        # Adım 2: Detay çekimi
        time.sleep(RATE_LIMIT_DELAY)
        details = self.get_full_details(mal_id)

        if details is None:
            self._log_error(f"Detaylar alınamadı: {name} (mal_id: {mal_id})")
            return None

        return details
