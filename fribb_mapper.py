"""
fribb_mapper.py — Fribb/anime-lists Eşleme Motoru

TMDB, MyAnimeList (MAL), AniList ve diğer platformlar arasındaki
kimlik eşlemelerini yerel önbellek üzerinden yüksek performanslı
sözlük aramaları ile yönetir.

Veri Kaynağı: https://github.com/Fribb/anime-lists
"""

import os
import json
import time
import urllib.request
from logger import setup_logger

_logger = setup_logger("FribbMapper")

# Önbellek dizini ve dosya yolları
CACHE_DIR = "data"
CACHE_FILE = os.path.join(CACHE_DIR, "anime-list-mini.json")
FRIBB_RAW_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-mini.json"

# Önbellek tazeleme süresi (7 gün - saniye cinsinden)
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


class FribbMapper:
    """Fribb anime-lists veri kümesini yöneten ve hızlı çift yönlü ID eşlemesi sağlayan sınıf."""

    def __init__(self, cache_file: str = CACHE_FILE, auto_load: bool = True):
        self.cache_file = cache_file
        self.logger = _logger

        # Arama indeksleri
        self.tmdb_season_to_mal: dict[tuple[int, int], int] = {}
        self.tmdb_season_to_anilist: dict[tuple[int, int], int] = {}
        self.movie_tmdb_to_mal: dict[int, int] = {}
        self.movie_tmdb_to_anilist: dict[int, int] = {}
        
        self.mal_to_tmdb: dict[int, dict] = {}
        self.mal_to_anilist: dict[int, int] = {}
        self.tmdb_to_all_mal_ids: dict[int, set[int]] = {}

        self.loaded = False
        if auto_load:
            self.load()

    def _ensure_cache_exists(self, force: bool = False):
        """Önbellek dosyasının varlığını ve güncelliğini kontrol eder, gerekirse indirir."""
        os.makedirs(os.path.dirname(self.cache_file) or ".", exist_ok=True)

        needs_download = False
        if force or not os.path.exists(self.cache_file):
            needs_download = True
        else:
            file_age = time.time() - os.path.getmtime(self.cache_file)
            if file_age > CACHE_TTL_SECONDS:
                self.logger.info("Fribb önbellek dosyası 7 günden eski, güncelleniyor...")
                needs_download = True

        if needs_download:
            self.logger.info(f"Fribb dataset indiriliyor: {FRIBB_RAW_URL}")
            try:
                req = urllib.request.Request(
                    FRIBB_RAW_URL,
                    headers={"User-Agent": "Animori-Database/2.0"}
                )
                with urllib.request.urlopen(req, timeout=30) as response:
                    data = response.read()
                    with open(self.cache_file, "wb") as f:
                        f.write(data)
                self.logger.info(f"✅ Fribb dataset başarıyla kaydedildi: {self.cache_file} ({len(data)} bayt)")
            except Exception as e:
                if os.path.exists(self.cache_file):
                    self.logger.warning(f"Fribb dataset indirilemedi ({e}), mevcut önbellek kullanılacak.")
                else:
                    raise RuntimeError(f"Fribb dataset indirilemedi ve yerel önbellek bulunamadı: {e}")

    def load(self, force_refresh: bool = False):
        """Dataset'i okur ve bellek içi indeksleri oluşturur."""
        self._ensure_cache_exists(force=force_refresh)

        t0 = time.time()
        self.logger.info(f"Fribb dataset yükleniyor: {self.cache_file}")

        with open(self.cache_file, "r", encoding="utf-8") as f:
            raw_list = json.load(f)

        # İndeksleri sıfırla
        self.tmdb_season_to_mal.clear()
        self.tmdb_season_to_anilist.clear()
        self.movie_tmdb_to_mal.clear()
        self.movie_tmdb_to_anilist.clear()
        self.mal_to_tmdb.clear()
        self.mal_to_anilist.clear()
        self.tmdb_to_all_mal_ids.clear()

        # Kayıtları öncelik puanına göre sıralayalım:
        # 1. tvdb == tmdb olan resmi ana TV sezonları en yüksek önceliğe sahiptir
        # 2. TV / MOVIE tipleri
        # 3. Düşük MAL ID (ana yapım olma ihtimali daha yüksek)
        def sort_key(item):
            season_info = item.get("season", {})
            is_canonical_season = (
                isinstance(season_info, dict)
                and season_info.get("tmdb") == season_info.get("tvdb")
                and season_info.get("tmdb", 0) > 0
            )
            entry_type = item.get("type", "")
            type_score = 1 if entry_type == "TV" else (2 if entry_type == "MOVIE" else 3)
            canonical_score = 0 if is_canonical_season else 1
            mal_id = item.get("mal_id") or 9999999
            return (canonical_score, type_score, mal_id)

        sorted_list = sorted(raw_list, key=sort_key)

        for item in sorted_list:
            mal_id = item.get("mal_id")
            anilist_id = item.get("anilist_id")
            tmdb_info = item.get("themoviedb_id")
            entry_type = item.get("type", "TV")
            season_info = item.get("season", {})

            if mal_id and anilist_id:
                if mal_id not in self.mal_to_anilist:
                    self.mal_to_anilist[mal_id] = anilist_id

            if not tmdb_info or not isinstance(tmdb_info, dict):
                continue

            # 1. TV Series Eşlemesi (themoviedb_id: {'tv': 12345})
            if "tv" in tmdb_info and tmdb_info["tv"]:
                tmdb_id = tmdb_info["tv"]
                season_num = season_info.get("tmdb", 1) if isinstance(season_info, dict) else 1
                key = (tmdb_id, season_num)

                if mal_id:
                    # İlk gelen (en yüksek öncelikli) kaydı koru
                    if key not in self.tmdb_season_to_mal:
                        self.tmdb_season_to_mal[key] = mal_id

                    if tmdb_id not in self.tmdb_to_all_mal_ids:
                        self.tmdb_to_all_mal_ids[tmdb_id] = set()
                    self.tmdb_to_all_mal_ids[tmdb_id].add(mal_id)

                    if mal_id not in self.mal_to_tmdb:
                        self.mal_to_tmdb[mal_id] = {
                            "tmdb_id": tmdb_id,
                            "type": "tv",
                            "season_number": season_num,
                            "entry_type": entry_type
                        }

                if anilist_id and key not in self.tmdb_season_to_anilist:
                    self.tmdb_season_to_anilist[key] = anilist_id

            # 2. Movie Eşlemesi (themoviedb_id: {'movie': [12345]})
            if "movie" in tmdb_info and tmdb_info["movie"]:
                movie_ids = tmdb_info["movie"]
                if isinstance(movie_ids, int):
                    movie_ids = [movie_ids]

                for movie_id in movie_ids:
                    if mal_id:
                        if movie_id not in self.movie_tmdb_to_mal:
                            self.movie_tmdb_to_mal[movie_id] = mal_id

                        if movie_id not in self.tmdb_to_all_mal_ids:
                            self.tmdb_to_all_mal_ids[movie_id] = set()
                        self.tmdb_to_all_mal_ids[movie_id].add(mal_id)

                        if mal_id not in self.mal_to_tmdb:
                            self.mal_to_tmdb[mal_id] = {
                                "tmdb_id": movie_id,
                                "type": "movie",
                                "season_number": 1,
                                "entry_type": entry_type
                            }

                    if anilist_id and movie_id not in self.movie_tmdb_to_anilist:
                        self.movie_tmdb_to_anilist[movie_id] = anilist_id

        self.loaded = True
        elapsed = time.time() - t0
        self.logger.info(
            f"✅ Fribb mapping yüklendi ({elapsed:.2f} sn): "
            f"{len(self.tmdb_season_to_mal)} TV sezonu, "
            f"{len(self.movie_tmdb_to_mal)} Film, "
            f"{len(self.mal_to_tmdb)} MAL eşlemesi."
        )

    def get_mal_id(self, tmdb_id: int, season_number: int = 1, is_movie: bool = False) -> int | None:
        """TMDB ID ve sezon numarasından MyAnimeList ID döndürür."""
        if not self.loaded:
            self.load()

        if is_movie:
            return self.movie_tmdb_to_mal.get(tmdb_id)

        # Önce tam (tmdb_id, season_number) eşleşmesini ara
        mal_id = self.tmdb_season_to_mal.get((tmdb_id, season_number))
        if mal_id:
            return mal_id

        # Film olarak kayıtlı olabilir mi kontrol et
        if season_number == 1 and tmdb_id in self.movie_tmdb_to_mal:
            return self.movie_tmdb_to_mal[tmdb_id]

        return None

    def get_anilist_id(self, tmdb_id: int, season_number: int = 1, is_movie: bool = False) -> int | None:
        """TMDB ID ve sezon numarasından AniList ID döndürür."""
        if not self.loaded:
            self.load()

        if is_movie:
            return self.movie_tmdb_to_anilist.get(tmdb_id)

        anilist_id = self.tmdb_season_to_anilist.get((tmdb_id, season_number))
        if anilist_id:
            return anilist_id

        if season_number == 1 and tmdb_id in self.movie_tmdb_to_anilist:
            return self.movie_tmdb_to_anilist[tmdb_id]

        # MAL üzerinden AniList fallback
        mal_id = self.get_mal_id(tmdb_id, season_number, is_movie)
        if mal_id:
            return self.mal_to_anilist.get(mal_id)

        return None

    def get_tmdb_info_by_mal(self, mal_id: int) -> dict | None:
        """
        MAL ID'den TMDB bilgilerini döndürür.
        Dönen format:
            {"tmdb_id": int, "type": "tv"|"movie", "season_number": int, "entry_type": str}
        """
        if not self.loaded:
            self.load()

        return self.mal_to_tmdb.get(mal_id)

    def get_all_mal_ids(self, tmdb_id: int) -> list[int]:
        """Bir TMDB serisine (tüm sezonlar dahil) ait tüm MAL ID'lerinin listesini döner."""
        if not self.loaded:
            self.load()

        return sorted(list(self.tmdb_to_all_mal_ids.get(tmdb_id, [])))
