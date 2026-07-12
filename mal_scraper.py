"""
mal_scraper.py — MyAnimeList HTML Scraper (Jikan API Yerine)

Jikan API bağımlılığını ortadan kaldırarak doğrudan myanimelist.net HTML
sayfalarından anime detaylarını ve bölüm (episode) bilgilerini scrape eder.

JikanEnricher ile birebir aynı public arayüze sahiptir — main.py ve
storage_manager.py içinde sadece import satırını değiştirmek yeterlidir.

Endpoints (HTML scrape):
    - Detay:  https://myanimelist.net/anime/{mal_id}/x
    - Bölüm:  https://myanimelist.net/anime/{mal_id}/x/episode?offset={n}
"""

import requests
import time
import logging
import re
import json
import os

from bs4 import BeautifulSoup


MAL_BASE_URL = "https://myanimelist.net"
REQUEST_TIMEOUT = 15       # saniye
RATE_LIMIT_DELAY = 1       # her istek arasındaki bekleme süresi (saniye)
RETRY_MAX = 3              # 429/403 hatası için maksimum tekrar deneme
RETRY_BACKOFF = 5          # 429/403 hatası sonrası bekleme süresi (saniye, katlanan)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class JikanEnricher:
    """
    MyAnimeList HTML scraper — JikanEnricher ile birebir aynı arayüz.

    Sınıf adı 'JikanEnricher' olarak korunmuştur; böylece main.py ve
    episode_scraper.py içinde sadece import satırı değişir, sınıf referansları
    aynen çalışır.
    """

    def __init__(self, error_log_path: str = "errors.log", mappings_path: str = "manual_mappings.json"):
        self.error_log_path = error_log_path
        self._setup_error_logger()
        self.manual_mappings = self._load_manual_mappings(mappings_path)
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

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
            print(f"[MAL] ⚠️  manual_mappings.json okunamadı: {e}")
            return {}

    def _setup_error_logger(self):
        """Hata kayıtları için dosya logger'ları oluşturur."""
        self.error_logger = logging.getLogger("mal_scraper_errors")
        self.error_logger.setLevel(logging.ERROR)

        if not self.error_logger.handlers:
            handler = logging.FileHandler(self.error_log_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s — %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            self.error_logger.addHandler(handler)

    def _log_error(self, message: str):
        """Konsola yazdırır ve errors.log dosyasına kaydeder."""
        print(f"[MAL] ❌ {message}")
        self.error_logger.error(message)

    def _request_with_retry(self, url: str) -> str | None:
        """
        HTTP GET isteği yapar. 429/403 hatalarında backoff ile tekrar dener.

        Returns:
            str: HTML yanıt metni veya None (hata durumunda).
        """
        for attempt in range(RETRY_MAX):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)

                if response.status_code == 200:
                    return response.text

                if response.status_code in (429, 403):
                    wait_time = RETRY_BACKOFF * (attempt + 1)
                    print(
                        f"[MAL] ⏳ HTTP {response.status_code}! "
                        f"{wait_time}s bekleniyor... (Deneme {attempt + 1}/{RETRY_MAX})"
                    )
                    time.sleep(wait_time)
                    continue

                # Diğer HTTP hataları
                print(f"[MAL] ⚠️  HTTP {response.status_code}: {url}")
                return None

            except requests.exceptions.Timeout:
                print(
                    f"[MAL] ⏱️  Timeout hatası "
                    f"(Deneme {attempt + 1}/{RETRY_MAX}): {url}"
                )
                time.sleep(3)

            except requests.exceptions.RequestException as e:
                print(
                    f"[MAL] ⚠️  Bağlantı hatası "
                    f"(Deneme {attempt + 1}/{RETRY_MAX}): {e}"
                )
                time.sleep(3)

        return None

    # ─────────────────────────────────────────────
    # Yardımcı HTML Parse Fonksiyonları
    # ─────────────────────────────────────────────

    @staticmethod
    def _get_info_value(soup: BeautifulSoup, label: str) -> str | None:
        """
        Sol sidebar'daki "Information" bölümünden etiket değerini çeker.
        Örn: label="Type" → "TV", label="Status" → "Finished Airing"
        """
        span = soup.find("span", class_="dark_text", string=re.compile(rf"^\s*{re.escape(label)}\s*:?\s*$"))
        if not span:
            return None

        parent = span.parent
        if not parent:
            return None

        # Eğer link varsa, linkin metnini döndür
        link = parent.find("a")
        if link and label not in ("Aired",):
            return link.get_text(strip=True)

        # Link yoksa span'ı çıkarıp kalan metni al
        text = parent.get_text(strip=True)
        # "Label:" kısmını kaldır
        text = re.sub(rf"^\s*{re.escape(label)}\s*:\s*", "", text)
        return text if text else None

    @staticmethod
    def _extract_ids_from_links(soup: BeautifulSoup, label: str, url_pattern: str) -> list[int]:
        """
        Belirli bir etiketin altındaki linklerin href'lerinden MAL ID'lerini çıkarır.
        
        Args:
            soup: BeautifulSoup nesnesi.
            label: Aranacak etiket (örn: "Genres", "Studios").
            url_pattern: Regex deseni (href'ten ID çıkarmak için).
        
        Returns:
            list[int]: MAL ID listesi.
        """
        span = soup.find("span", class_="dark_text", string=re.compile(rf"^\s*{re.escape(label)}\s*:?\s*$"))
        if not span:
            return []

        parent = span.parent
        if not parent:
            return []

        ids = []
        for link in parent.find_all("a", href=True):
            match = re.search(url_pattern, link["href"])
            if match:
                try:
                    ids.append(int(match.group(1)))
                except (ValueError, IndexError):
                    continue
        return ids

    @staticmethod
    def _parse_aired(aired_text: str | None) -> dict:
        """
        MAL "Aired" alanını ISO 8601 benzeri from/to formatına çevirir.
        
        Giriş örnekleri:
            "Feb 15, 2007 to Mar 23, 2017"
            "Apr 3, 2022 to ?"
            "Oct 7, 2023"
        
        Returns:
            dict: {"from": "2007-02-15T00:00:00+00:00", "to": "2017-03-23T00:00:00+00:00"}
        """
        result = {"from": None, "to": None}
        if not aired_text:
            return result

        parts = [p.strip() for p in aired_text.split(" to ")]

        for i, part in enumerate(parts[:2]):
            if part == "?" or not part:
                continue
            try:
                from datetime import datetime
                # MAL formatı: "Feb 15, 2007" veya "2007" gibi
                for fmt in ("%b %d, %Y", "%b, %Y", "%Y"):
                    try:
                        dt = datetime.strptime(part, fmt)
                        key = "from" if i == 0 else "to"
                        result[key] = dt.strftime("%Y-%m-%dT00:00:00+00:00")
                        break
                    except ValueError:
                        continue
            except Exception:
                continue

        return result

    @staticmethod
    def _parse_popularity(text: str | None) -> int | None:
        """
        Popularity sıralama metninden sayıyı çıkarır.
        Örn: "#15" → 15
        """
        if not text:
            return None
        match = re.search(r"#(\d+)", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _parse_score(soup: BeautifulSoup) -> float | None:
        """
        Skor değerini çeker.
        <span class="score-label score-8" itemprop="ratingValue">8.29</span>
        """
        score_el = soup.find("span", attrs={"itemprop": "ratingValue"})
        if score_el:
            try:
                val = float(score_el.get_text(strip=True))
                return val if val > 0 else None
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _parse_trailer_id(soup: BeautifulSoup) -> str | None:
        """
        Trailer YouTube video ID'sini çeker.
        <a class="iframe js-fancybox-video video-unit promotion"
           href="https://www.youtube-nocookie.com/embed/1dy2zPPrKD0?...">
        """
        promo = soup.find("a", class_=re.compile(r"js-fancybox-video"))
        if promo and promo.get("href"):
            match = re.search(r"/embed/([^?&]+)", promo["href"])
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _parse_relations(soup: BeautifulSoup) -> list[dict]:
        """
        İlişkili anime/manga girişlerini parse eder.
        
        Hem yeni tile (entries-tile) hem eski tablo (entries-table) formatlarını
        destekler.
        
        Returns:
            list[dict]: [{"relation": "Prequel", "entries": [{"mal_id": 20}]}, ...]
        """
        relations = []
        seen_relations = {}  # relation_type -> {"relation": ..., "entries": [...]}

        related_div = soup.find("div", class_="related-entries")
        if not related_div:
            return []

        # ── Tile formatı (div.entries-tile > div.entry) ──
        entries_tile = related_div.find("div", class_="entries-tile")
        if entries_tile:
            for entry in entries_tile.find_all("div", class_="entry"):
                relation_div = entry.find("div", class_="relation")
                title_div = entry.find("div", class_="title")

                if not relation_div or not title_div:
                    continue

                relation_text = relation_div.get_text(strip=True)
                # "Prequel (TV)" → "Prequel" — parantezi kaldır
                relation_type = re.sub(r"\s*\(.*?\)\s*$", "", relation_text).strip()

                link = title_div.find("a", href=True)
                if not link:
                    continue

                # /anime/20/Naruto → mal_id=20 veya /manga/11/Naruto → mal_id=11
                mal_match = re.search(r"/(?:anime|manga)/(\d+)/", link["href"])
                if not mal_match:
                    continue

                mal_id = int(mal_match.group(1))

                if relation_type not in seen_relations:
                    seen_relations[relation_type] = {
                        "relation": relation_type,
                        "entries": []
                    }
                seen_relations[relation_type]["entries"].append(mal_id)

        # ── Tablo formatı (table.entries-table > tr > td > ul > li > a) ──
        entries_table = related_div.find("table", class_="entries-table")
        if entries_table:
            for row in entries_table.find_all("tr"):
                # İlk td: ilişki türü (Side Story, Spin-Off, Other vb.)
                rel_td = row.find("td", class_="ar")
                entries_td = row.find("td", attrs={"width": "100%"})

                if not rel_td or not entries_td:
                    continue

                relation_type = rel_td.get_text(strip=True).rstrip(":")

                for link in entries_td.find_all("a", href=True):
                    mal_match = re.search(r"/(?:anime|manga)/(\d+)/", link["href"])
                    if not mal_match:
                        continue

                    mal_id = int(mal_match.group(1))

                    if relation_type not in seen_relations:
                        seen_relations[relation_type] = {
                            "relation": relation_type,
                            "entries": []
                        }
                    seen_relations[relation_type]["entries"].append(mal_id)

        return list(seen_relations.values())

    @staticmethod
    def _parse_year_season(soup: BeautifulSoup) -> tuple[int | None, str | None]:
        """
        "Premiered" alanından yıl ve sezon bilgisini çeker.
        Örn: "Winter 2007" → (2007, "winter")
        
        Premiered yoksa "Aired" alanından sadece yılı çıkarmaya çalışır.
        """
        span = soup.find("span", class_="dark_text", string=re.compile(r"^\s*Premiered\s*:?\s*$"))
        if span:
            parent = span.parent
            if parent:
                link = parent.find("a")
                if link:
                    text = link.get_text(strip=True)
                    # "Winter 2007"
                    match = re.match(r"(\w+)\s+(\d{4})", text)
                    if match:
                        season = match.group(1).lower()
                        year = int(match.group(2))
                        return year, season

        # Premiered yoksa Aired'den yılı çıkar
        aired_span = soup.find("span", class_="dark_text", string=re.compile(r"^\s*Aired\s*:?\s*$"))
        if aired_span and aired_span.parent:
            aired_text = aired_span.parent.get_text(strip=True)
            match = re.search(r"(\d{4})", aired_text)
            if match:
                return int(match.group(1)), None

        return None, None

    # ─────────────────────────────────────────────
    # Public API — JikanEnricher ile Aynı Arayüz
    # ─────────────────────────────────────────────

    def get_full_details(self, mal_id: int) -> dict | None:
        """
        mal_id ile MAL anime detay sayfasını scrape ederek gerekli alanları çıkarır.

        Args:
            mal_id: MyAnimeList anime ID'si.

        Returns:
            dict: Filtrelenmiş anime detayları veya None.
        """
        url = f"{MAL_BASE_URL}/anime/{mal_id}/x"
        html = self._request_with_retry(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")

        # ── Başlık ──
        title_el = soup.find("h1", class_="title-name")
        title = title_el.get_text(strip=True) if title_el else None

        if not title:
            # Sayfa geçersiz/boş olabilir
            return None

        # ── İngilizce başlık ──
        # js-alternative-titles div'i içindeki "English:" etiketi
        title_english = None
        alt_titles_div = soup.find("div", class_="js-alternative-titles")
        if alt_titles_div:
            eng_span = alt_titles_div.find("span", class_="dark_text", string=re.compile(r"^\s*English\s*:?\s*$"))
            if eng_span and eng_span.parent:
                eng_text = eng_span.parent.get_text(strip=True)
                title_english = re.sub(r"^\s*English\s*:\s*", "", eng_text).strip() or None

        # ── Poster resmi ──
        image_url = None
        img_el = soup.find("img", attrs={"itemprop": "image"})
        if img_el:
            image_url = img_el.get("data-src") or img_el.get("src")

        # ── Trailer ID ──
        trailer_id = self._parse_trailer_id(soup)

        # ── Temel bilgiler (Information bölümü) ──
        anime_type = self._get_info_value(soup, "Type")
        source = self._get_info_value(soup, "Source")
        status = self._get_info_value(soup, "Status")
        duration = self._get_info_value(soup, "Duration")
        rating = self._get_info_value(soup, "Rating")

        # ── Aired ──
        aired_text = self._get_info_value(soup, "Aired")
        aired = self._parse_aired(aired_text)

        # ── Year & Season ──
        year, season = self._parse_year_season(soup)

        # ── Airing (yayında mı?) ──
        airing = False
        if status:
            airing = "Currently Airing" in status

        # ── Score ──
        score = self._parse_score(soup)

        # ── Popularity ──
        popularity_text = self._get_info_value(soup, "Popularity")
        popularity = self._parse_popularity(popularity_text)

        # ── Synopsis ──
        synopsis = None
        synopsis_el = soup.find("p", attrs={"itemprop": "description"})
        if synopsis_el:
            # <br/> etiketlerini \n'ye dönüştür
            for br in synopsis_el.find_all("br"):
                br.replace_with("\n")
            synopsis = synopsis_el.get_text(strip=True)
            # "[Written by MAL Rewrite]" gibi etiketleri kaldırma — Jikan da olduğu gibi bırakır

        # ── Genres, Themes, Demographics, Studios (ID olarak) ──
        genres = self._extract_ids_from_links(soup, "Genres", r"/anime/genre/(\d+)/")
        # "Genre" (tekil) da kontrol et — MAL bazen tekil kullanır
        if not genres:
            genres = self._extract_ids_from_links(soup, "Genre", r"/anime/genre/(\d+)/")

        themes = self._extract_ids_from_links(soup, "Themes", r"/anime/genre/(\d+)/")
        if not themes:
            themes = self._extract_ids_from_links(soup, "Theme", r"/anime/genre/(\d+)/")

        demographics = self._extract_ids_from_links(soup, "Demographics", r"/anime/genre/(\d+)/")
        if not demographics:
            demographics = self._extract_ids_from_links(soup, "Demographic", r"/anime/genre/(\d+)/")

        studios = self._extract_ids_from_links(soup, "Studios", r"/anime/producer/(\d+)/")
        if not studios:
            studios = self._extract_ids_from_links(soup, "Studio", r"/anime/producer/(\d+)/")

        # ── Relations ──
        relations = self._parse_relations(soup)

        return {
            "mal_id": mal_id,
            "image_url": image_url,
            "trailer_id": trailer_id,
            "title": title,
            "title_english": title_english,
            "type": anime_type,
            "source": source,
            "status": status,
            "year": year,
            "season": season,
            "airing": airing,
            "aired": aired,
            "duration": duration,
            "rating": rating,
            "score": score,
            "popularity": popularity,
            "synopsis": synopsis,
            "genres": genres,
            "themes": themes,
            "demographics": demographics,
            "studios": studios,
            "relations": relations,
        }

    def fetch_episodes(self, mal_id: int) -> dict[int, dict]:
        """
        MAL bölüm listesi sayfalarını sayfalayarak tüm bölüm bilgilerini çeker.

        Pagination: offset=0, 100, 200, ... ile sayfa başına 100 bölüm.
        Sayfada hiç tr.episode-list-data satırı yoksa tarama durur.

        Args:
            mal_id: MyAnimeList anime ID'si.

        Returns:
            dict: {bölüm_numarası: {"filler": bool, "recap": bool}, ...}
                  Boş dict hata durumunda.
        """
        episodes = {}
        offset = 0

        while True:
            url = f"{MAL_BASE_URL}/anime/{mal_id}/x/episode?offset={offset}"
            html = self._request_with_retry(url)

            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")
            rows = soup.find_all("tr", class_="episode-list-data")

            if not rows:
                # Boş sayfa — tarama bitti
                break

            for row in rows:
                # ── Bölüm numarası ──
                # İlk td genellikle bölüm numarasını içerir
                ep_number = None
                number_td = row.find("td", class_="episode-number")
                if number_td:
                    num_text = number_td.get_text(strip=True)
                    try:
                        ep_number = int(num_text)
                    except (ValueError, TypeError):
                        continue
                else:
                    # Alternatif: satırdaki ilk td
                    first_td = row.find("td")
                    if first_td:
                        num_text = first_td.get_text(strip=True)
                        try:
                            ep_number = int(num_text)
                        except (ValueError, TypeError):
                            continue

                if ep_number is None:
                    continue

                # ── Filler / Recap tespiti ──
                filler = False
                recap = False

                title_td = row.find("td", class_="episode-title")
                if title_td:
                    type_spans = title_td.find_all("span", class_=re.compile(r"icon-episode-type-bg"))
                    for span in type_spans:
                        span_text = span.get_text(strip=True).lower()
                        if "filler" in span_text:
                            filler = True
                        if "recap" in span_text:
                            recap = True

                episodes[ep_number] = {
                    "filler": filler,
                    "recap": recap,
                }

            offset += 100
            time.sleep(RATE_LIMIT_DELAY)

        return episodes

    def enrich(self, mal_id: int, slug: str = "", name: str = "") -> dict | None:
        """
        MAL ID ile anime detaylarını çeken birleşik metod.

        Adımlar:
            0. Manuel eşleştirme dosyasını kontrol et (öncelikli)
            1. mal_id ile detay sayfasından verileri scrape et
            2. Hata varsa errors.log'a kaydet, None döndür

        Args:
            mal_id: Türkanime'nin dış bağlantılar sekmesinden çekilen MAL ID.
            slug: Anime slug'ı (manuel eşleştirme kontrolü için).
            name: Anime ismi (loglama için).

        Returns:
            dict: Zenginleştirilmiş anime verileri veya None.
        """
        # Adım 0: Manuel eşleştirme kontrolü (öncelikli)
        manual_mal_id = self.manual_mappings.get(slug)
        if manual_mal_id:
            print(f"[MAL] 📌 Manuel eşleştirme kullanılıyor: {name} -> mal_id: {manual_mal_id}")
            time.sleep(RATE_LIMIT_DELAY)
            details = self.get_full_details(manual_mal_id)
            if details is None:
                self._log_error(f"Manuel eşleştirme detayları alınamadı: {name} (mal_id: {manual_mal_id})")
            return details

        # Adım 1: Detay çekimi (mal_id doğrudan Türkanime'den geliyor)
        if mal_id is None:
            self._log_error(f"MAL ID bulunamadı: {name}")
            return None

        time.sleep(RATE_LIMIT_DELAY)
        details = self.get_full_details(mal_id)

        if details is None:
            self._log_error(f"Detaylar alınamadı: {name} (mal_id: {mal_id})")
            return None

        return details
