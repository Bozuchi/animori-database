"""
tmdb_client.py — The Movie Database (TMDB) API İstemcisi

TMDB API v3 üzerinden TV dizileri ve filmler için yüksek kaliteli
meta verileri, afiş/arka plan görsellerini, sezonları ve bölüm detaylarını çeker.

Türkçe (tr-TR) verileri önceliklendirir, eksik alanlarda İngilizce (en-US)
fallback uygulayarak eksiksiz bir içerik sağlar.
"""

import os
import time
import requests
import threading
from dotenv import load_dotenv
from logger import setup_logger

_logger = setup_logger("TMDBClient")

# Base URL
TMDB_BASE_URL = "https://api.themoviedb.org/3"


class TMDBClient:
    """TMDB API v3/v4 ile iletişim kuran istemci sınıfı."""

    def __init__(self, api_key: str | None = None, read_token: str | None = None):
        self.logger = _logger
        
        # .env dosyasını yükle
        load_dotenv()

        self.api_key = api_key or os.getenv("TMDB_API_KEY")
        self.read_token = read_token or os.getenv("TMDB_READ_TOKEN")

        if not self.api_key and not self.read_token:
            self.logger.warning("TMDB_API_KEY veya TMDB_READ_TOKEN bulunamadı! Lütfen .env dosyasını kontrol edin.")

        self.session = requests.Session()
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "Animori-Database/2.0"
        }
        if self.read_token:
            self.headers["Authorization"] = f"Bearer {self.read_token}"

        # Rate limiting (istekler arası min bekleme: 50ms)
        self.min_interval = 0.05
        self._last_request_time = 0.0
        self._lock = threading.Lock()

    def _rate_limit(self):
        """İstekler arasında kısa bir bekleme uygulayarak TMDB limitlerini korur."""
        with self._lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_request_time = time.time()

    def _request(self, endpoint: str, params: dict | None = None, max_retries: int = 3) -> dict | None:
        """TMDB API'sine GET isteği atar, 429 ve bağlantı hatalarını yönetir."""
        self._rate_limit()

        url = f"{TMDB_BASE_URL}{endpoint}"
        query_params = dict(params or {})

        # Bearer token yoksa api_key parametresi ekle
        if not self.read_token and self.api_key:
            query_params["api_key"] = self.api_key

        retry_count = 0
        backoff = 1.0

        while retry_count < max_retries:
            try:
                response = self.session.get(url, headers=self.headers, params=query_params, timeout=10)
                
                # 404 Bulunamadı
                if response.status_code == 404:
                    return None

                # 429 Rate Limit
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 2))
                    self.logger.warning(f"TMDB Rate Limit (429)! {retry_after} sn bekleniyor...")
                    time.sleep(retry_after)
                    retry_count += 1
                    continue

                response.raise_for_status()
                return response.json()

            except (requests.RequestException, Exception) as e:
                retry_count += 1
                if retry_count >= max_retries:
                    self.logger.error(f"TMDB API Hatası ({endpoint}): {e}")
                    return None
                time.sleep(backoff)
                backoff *= 2.0

        return None

    def get_details(self, tmdb_id: int, is_movie: bool = False) -> dict | None:
        """
        TV Dizisi veya Film için detaylı bilgileri çeker.
        Başlık doğrudan İngilizce (en-US) olarak alınır.
        Özet (overview) için Türkçe varsa Türkçe, yoksa İngilizce fallback kullanılır.
        """
        media_type = "movie" if is_movie else "tv"
        endpoint = f"/{media_type}/{tmdb_id}"

        # 1. İngilizce ana veri isteği (external_ids ve translations ile tek istekte)
        en_data = self._request(endpoint, {
            "language": "en-US",
            "append_to_response": "external_ids,translations"
        })
        if not en_data:
            return None

        # Başlık: Doğrudan İngilizce (yoksa orijinal başlık)
        name = (en_data.get("title" if is_movie else "name") or "").strip()
        original_name = en_data.get("original_title" if is_movie else "original_name", "")
        if not name:
            name = original_name

        # Özet: translations içinde Türkçe (tr) ara; yoksa İngilizce özet al
        overview = (en_data.get("overview") or "").strip()
        translations = en_data.get("translations", {}).get("translations", [])
        tr_trans = next((t for t in translations if t.get("iso_639_1") == "tr"), None)
        if tr_trans:
            tr_ov = (tr_trans.get("data", {}).get("overview") or "").strip()
            if tr_ov:
                overview = tr_ov

        first_air_date = en_data.get("release_date" if is_movie else "first_air_date")

        # Türler: [16, 10759] gibi ID listesi
        genre_ids = [g["id"] for g in en_data.get("genres", []) if "id" in g]

        # Harici ID'ler (IMDb vb.)
        ext_ids = en_data.get("external_ids", {}) or {}
        external_ids = {}
        if ext_ids.get("imdb_id"):
            external_ids["imdb_id"] = ext_ids["imdb_id"]
        if ext_ids.get("tvdb_id"):
            external_ids["tvdb_id"] = ext_ids["tvdb_id"]

        result = {
            "id": tmdb_id,
            "type": media_type,
            "name": name,
            "original_name": original_name,
            "overview": overview,
            "poster_path": en_data.get("poster_path"),
            "backdrop_path": en_data.get("backdrop_path"),
            "first_air_date": first_air_date,
            "status": en_data.get("status", "Ended"),
            "vote_average": round(float(en_data.get("vote_average", 0.0)), 2),
            "vote_count": int(en_data.get("vote_count", 0)),
            "genres": genre_ids,
            "original_language": en_data.get("original_language", "ja"),
            "external_ids": external_ids
        }

        if is_movie:
            result["runtime"] = en_data.get("runtime", 0)
        else:
            # Sezon özet listesi (Sezon 0 genelde Special'dır, 1 ve üstünü al)
            raw_seasons = en_data.get("seasons", [])
            valid_seasons = []
            for s in raw_seasons:
                s_num = s.get("season_number", 0)
                if s_num > 0:
                    valid_seasons.append({
                        "season_number": s_num,
                        "name": s.get("name") or f"Season {s_num}",
                        "overview": s.get("overview") or "",
                        "poster_path": s.get("poster_path"),
                        "air_date": s.get("air_date"),
                        "episode_count": s.get("episode_count", 0)
                    })
            result["seasons_summary"] = sorted(valid_seasons, key=lambda x: x["season_number"])

        return result

    def get_season_details(self, tmdb_id: int, season_number: int) -> dict | None:
        """
        TV Dizisi için belirtilen sezonun tüm bölümlerini çeker.
        Bölüm adları doğrudan İngilizce olarak alınır; özetler Türkçe (varsa) ile tamamlanır.
        """
        endpoint = f"/tv/{tmdb_id}/season/{season_number}"

        # İngilizce ana veri (translations ile birlikte)
        en_data = self._request(endpoint, {
            "language": "en-US",
            "append_to_response": "translations"
        })
        if not en_data:
            return None

        # Sezon özeti için Türkçe çeviri var mı?
        season_overview = (en_data.get("overview") or "").strip()
        season_translations = en_data.get("translations", {}).get("translations", [])
        tr_season_trans = next((t for t in season_translations if t.get("iso_639_1") == "tr"), None)
        if tr_season_trans:
            tr_so = (tr_season_trans.get("data", {}).get("overview") or "").strip()
            if tr_so:
                season_overview = tr_so

        # Türkçe bölüm özetleri için tr-TR verisini de al
        tr_data = self._request(endpoint, {"language": "tr-TR"})
        tr_episodes_map = {}
        if tr_data and "episodes" in tr_data:
            for ep in tr_data["episodes"]:
                tr_episodes_map[ep.get("episode_number")] = ep

        episodes = []
        for ep in en_data.get("episodes", []):
            ep_num = ep.get("episode_number")
            tr_ep = tr_episodes_map.get(ep_num, {})

            # Başlık: Doğrudan İngilizce
            name = (ep.get("name") or "").strip()
            if not name:
                name = f"Episode {ep_num}"

            # Özet: Türkçe varsa al, yoksa İngilizce
            tr_overview = (tr_ep.get("overview") or "").strip()
            en_overview = (ep.get("overview") or "").strip()
            overview = tr_overview if tr_overview else en_overview

            episodes.append({
                "episode_number": ep_num,
                "name": name,
                "overview": overview,
                "still_path": ep.get("still_path") or tr_ep.get("still_path"),
                "air_date": ep.get("air_date") or tr_ep.get("air_date")
            })

        season_name = (en_data.get("name") or f"Season {season_number}").strip()

        return {
            "season_number": season_number,
            "name": season_name,
            "overview": season_overview,
            "poster_path": en_data.get("poster_path"),
            "air_date": en_data.get("air_date"),
            "episode_count": len(episodes),
            "episodes": episodes
        }

    def get_episode_group_seasons(self, tmdb_id: int) -> list[dict] | None:
        """
        Anime serilerinde TMDB'deki 'Seasons' bölüm gruplarını (type 6) tespit eder.
        Örn: Jujutsu Kaisen gibi serilerde tek sezona yığılmış bölümleri
        resmi yayın sezonlarına (Season 1: 1-24, Season 2: 1-23) böler.
        """
        groups_res = self._request(f"/tv/{tmdb_id}/episode_groups")
        if not groups_res or "results" not in groups_res:
            return None

        # Seasons veya type 6 olan grubu ara
        target_group_id = None
        for grp in groups_res["results"]:
            if grp.get("name", "").strip().lower() == "seasons" or grp.get("type") == 6:
                target_group_id = grp.get("id")
                break

        if not target_group_id:
            return None

        group_data = self._request(f"/tv/episode_group/{target_group_id}")
        if not group_data or "groups" not in group_data:
            return None

        seasons = []
        raw_groups = [g for g in group_data["groups"] if g.get("order", 0) > 0]
        if not raw_groups:
            raw_groups = group_data["groups"]

        for grp in sorted(raw_groups, key=lambda x: x.get("order", 1)):
            s_order = grp.get("order", 1)
            grp_name = grp.get("name") or f"Season {s_order}"

            episodes = []
            for idx, ep in enumerate(grp.get("episodes", [])):
                rel_ep_num = idx + 1
                abs_ep_num = ep.get("episode_number")
                episodes.append({
                    "episode_number": rel_ep_num,
                    "absolute_number": abs_ep_num if abs_ep_num != rel_ep_num else None,
                    "name": ep.get("name") or f"Episode {rel_ep_num}",
                    "overview": ep.get("overview") or "",
                    "still_path": ep.get("still_path"),
                    "air_date": ep.get("air_date")
                })

            seasons.append({
                "season_number": s_order,
                "name": grp_name,
                "overview": "",
                "poster_path": None,
                "air_date": episodes[0]["air_date"] if episodes else None,
                "episode_count": len(episodes),
                "episodes": episodes
            })

        return seasons if seasons else None

    def search(self, query: str, is_movie: bool = False, limit: int = 10) -> list[dict]:
        """Başlık araması yapar. Tanınabilir başlıklar için varsayılan olarak global/İngilizce sonuçları çeker."""
        endpoint = "/search/movie" if is_movie else "/search/tv"
        data = self._request(endpoint, {"query": query, "language": "en-US"})
        if not data or "results" not in data:
            return []

        results = []
        for item in data["results"][:limit]:
            name = item.get("title" if is_movie else "name")
            orig_name = item.get("original_title" if is_movie else "original_name")
            date = item.get("release_date" if is_movie else "first_air_date")
            year = int(date.split("-")[0]) if date and "-" in date else None

            results.append({
                "id": item.get("id"),
                "type": "movie" if is_movie else "tv",
                "name": name,
                "original_name": orig_name,
                "poster_path": item.get("poster_path"),
                "backdrop_path": item.get("backdrop_path"),
                "year": year,
                "vote_average": item.get("vote_average", 0.0)
            })

        return results
