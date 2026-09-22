"""
anizm_provider.py — Anizm (anizm.net) API ve Web Sağlayıcı Modülü

Anizm platformundaki anime kataloğunu, haftalık yayın takvimini,
son eklenen bölümleri ve bölüm video oynatıcılarını Cloudflare Turnstile
korumasını curl_cffi (Chrome TLS parmak izi) ile aşarak çeker.

Özellikler:
    - Thread-local session (Çoklu iş parçacığı güvenliği)
    - Sitemap tabanlı tam katalog keşfi (~4.848 anime serisi)
    - AJAX/JSON arama (/searchAnime) ve sayfalama (/episodesContent)
    - Çoklu çevirmen (translator) ve video oynatıcı (player) çözümleme
    - Oynatıcı adı normalizasyonu (Aincrad, Sistenn, Odno, Sibnet, Vidmoly vb.)
"""

import os
import re
import time
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor
from curl_cffi import requests
from logger import setup_logger

logger = setup_logger("AnizmProvider")


class AnizmProvider:
    BASE_URL = "https://anizm.net"
    MIRROR_URL = "https://anizle.co"

    KNOWN_PLAYER_NAMES = {
        "aincrad": "AINCRAD",
        "anizmplayer": "AINCRAD",
        "betaplayer": "BETAPLAYER",
        "puffytr": "BETAPLAYER",
        "sistenn": "SISTENN",
        "sistenn1": "SISTENN",
        "sistenn2": "SISTENN",
        "sistenn3": "SISTENN",
        "odno": "ODNO",
        "odnoklassniki": "ODNO",
        "ok.ru": "ODNO",
        "sibnet": "SIBNET",
        "vidmoly": "VIDMOLY",
        "tau": "TAU",
        "tau video": "TAU",
        "gdrive": "GDRIVE",
        "google": "GDRIVE",
        "drive": "GDRIVE",
        "mega": "MEGA",
        "dailymotion": "DAILYMOTION",
        "youtube": "YOUTUBE",
        "mailru": "MAILRU",
        "mail.ru": "MAILRU",
        "myvi": "MYVI",
        "myviru": "MYVI",
        "mp4upload": "MP4UPLOAD",
        "doodstream": "DOODSTREAM",
        "dood": "DOODSTREAM",
        "fembed": "FEMBED",
        "streamtape": "STREAMTAPE",
        "voe": "VOE",
    }

    def __init__(self, base_url: str | None = None, timeout: int = 15):
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout
        self._local = threading.local()

    def _get_session(self) -> requests.Session:
        """Her iş parçacığı için izole curl_cffi oturumu sağlar (Thread-safety)."""
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session(impersonate="chrome124")
        return self._local.session

    def _request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        max_retries: int = 3,
    ) -> requests.Response | None:
        session = self._get_session()
        target_url = url if url.startswith("http") else f"{self.base_url}{url}"

        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        if headers:
            req_headers.update(headers)

        time.sleep(0.20)  # Kibar istek gecikmesi

        backoff_delays = [5, 10, 20, 35]

        for attempt in range(max_retries):
            try:
                resp = session.request(
                    method=method,
                    url=target_url,
                    params=params,
                    headers=req_headers,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    return resp
                elif resp.status_code == 404:
                    logger.debug(f"404 Not Found: {target_url}")
                    return None
                elif resp.status_code in (429, 503):
                    wait = backoff_delays[min(attempt, len(backoff_delays) - 1)]
                    logger.warning(f"⏳ HTTP {resp.status_code} ({target_url}). {wait}sn bekleniyor...")
                    time.sleep(wait)
                    continue
                else:
                    logger.warning(f"HTTP {resp.status_code} ({target_url})")
                    return None
            except Exception as e:
                logger.error(f"Bağlantı hatası ({target_url}): {e}")
                time.sleep(3)
                continue

        return None

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> requests.Response | None:
        return self._request("GET", url, params=params, headers=headers)

    # ─────────────────────────────────────────────────────────────
    # OYNATICI NORMALİZASYONU
    # ─────────────────────────────────────────────────────────────

    @classmethod
    def normalize_player(cls, raw_name: str | None, url: str | None = None) -> str:
        """Oynatıcı adını ve URL'sini standart büyük harfli koda dönüştürür."""
        # 1. URL tabanlı doğrudan tespit
        if url:
            u_lower = url.lower()
            if "drive.google.com" in u_lower or "google.com/file" in u_lower:
                return "GDRIVE"
            if "ok.ru" in u_lower or "odnoklassniki" in u_lower:
                return "ODNO"
            if "vidmoly" in u_lower:
                return "VIDMOLY"
            if "sibnet.ru" in u_lower or "sibnet" in u_lower:
                return "SIBNET"
            if "dood" in u_lower:
                return "DOODSTREAM"
            if "voe.sx" in u_lower or "voe." in u_lower:
                return "VOE"
            if "sistenn" in u_lower:
                return "SISTENN"
            if "anizmplayer" in u_lower:
                return "AINCRAD"
            if "puffytr" in u_lower or "puffy" in u_lower:
                return "BETAPLAYER"
            if "mail.ru" in u_lower:
                return "MAILRU"
            if "myvi" in u_lower:
                return "MYVI"
            if "mp4upload" in u_lower:
                return "MP4UPLOAD"
            if "streamtape" in u_lower:
                return "STREAMTAPE"
            if "fembed" in u_lower:
                return "FEMBED"

        if not raw_name:
            return "DIGER"
        clean = raw_name.strip().lower()

        # Doğrudan eşleşme
        if clean in cls.KNOWN_PLAYER_NAMES:
            return cls.KNOWN_PLAYER_NAMES[clean]

        # Kısmi eşleşmeler
        for key, code in cls.KNOWN_PLAYER_NAMES.items():
            if key in clean:
                return code

        # Temizle ve büyük harfe çevir
        cleaned = re.sub(r"[^A-Za-z0-9]", "", clean).upper()
        return cleaned or "DIGER"

    # ─────────────────────────────────────────────────────────────
    # KATALOG & ARAMA METOTLARI
    # ─────────────────────────────────────────────────────────────

    def fetch_catalog(self) -> list[dict]:
        """
        Sitenin resmi sitemap XML dosyasından (~4.848 anime) tüm katalog başlıklarını çeker.

        Dönen her eleman:
            {
                "slug": "one-piece",
                "name": "One Piece",
                "url": "https://anizm.net/one-piece",
                "lastmod": "2026-09-19"
            }
        """
        logger.info("📑 Anizm sitemap kataloğu çekiliyor (/sitemap/seriler/0)...")
        resp = self._get("/sitemap/seriler/0")
        if not resp:
            logger.warning("Sitemap alınamadı, anizle.co aynası deneniyor...")
            resp = self._get(f"{self.MIRROR_URL}/sitemap/seriler/0")

        if not resp or resp.status_code != 200:
            logger.error("Anizm kataloğu çekilemedi.")
            return []

        # <sitemap><loc>https://anizm.net/{slug}</loc><lastmod>YYYY-MM-DD</lastmod></sitemap>
        entries = re.findall(
            r"<loc>(https?://[^<]+/([^<]+))</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?",
            resp.text,
        )

        catalog = []
        seen_slugs = set()
        for full_url, slug, lastmod in entries:
            clean_slug = slug.strip("/").strip()
            if not clean_slug or clean_slug in seen_slugs:
                continue
            seen_slugs.add(clean_slug)

            name = clean_slug.replace("-", " ").title()
            catalog.append({
                "slug": clean_slug,
                "name": name,
                "url": full_url,
                "lastmod": lastmod or None,
            })

        logger.info(f"✅ Anizm kataloğundan toplam {len(catalog)} anime serisi çekildi.")
        return catalog

    def search_anime(self, query: str, page: int = 1, limit: int = 20) -> list[dict]:
        """Anizm arama API'sini kullanarak başlık arar."""
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/",
        }
        params = {
            "query": query,
            "page": page,
            "type": "detailed",
            "limit": limit,
        }
        resp = self._get("/searchAnime", params=params, headers=headers)
        if not resp:
            return []
        try:
            data = resp.json()
            return data.get("data", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.warning(f"Arama JSON ayrıştırma hatası: {e}")
            return []

    # ─────────────────────────────────────────────────────────────
    # CANLI YAYIN AKIŞI & TAKVİM
    # ─────────────────────────────────────────────────────────────

    def fetch_latest_episodes(self, page: int = 1) -> list[dict]:
        """
        Anasayfadaki son eklenen bölümler akışını çeker (?sayfa=1..30).
        Her sayfa 18 bölüm barındırır.
        """
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/",
        }
        resp = self._get(f"/?sayfa={page}", headers=headers)
        if not resp:
            return []

        # episodesContent konteynerini bul
        match = re.search(r'id=["\']episodesContent["\'][^>]*>(.*?)</div>\s*<div[^>]*id=["\']paginationLinks', resp.text, re.DOTALL)
        content_html = match.group(1) if match else resp.text

        # Bölüm linklerini ve kartlarını ayıkla
        cards = re.findall(r'<a[^>]+href=["\']([^"\']+-bolum[^"\']*)["\'][^>]*>(.*?)</a>', content_html, re.DOTALL)
        results = []
        seen_slugs = set()

        for ep_url, inner in cards:
            # URL'den slug ve anime slug ayıkla
            full_slug = ep_url.rstrip("/").split("/")[-1]
            if not full_slug or full_slug in seen_slugs:
                continue
            seen_slugs.add(full_slug)

            # Bölüm numarası
            ep_num_match = re.search(r"-(\d+)-bolum", full_slug)
            ep_num = int(ep_num_match.group(1)) if ep_num_match else None

            # Anime slug: "-{sayi}-bolum..." öncesi
            if ep_num_match:
                anime_slug = full_slug[:ep_num_match.start()]
            else:
                anime_slug = re.sub(r"-\d+.*$", "", full_slug)

            # Resim / Poster
            poster_match = re.search(r'src=["\']([^"\']+)["\']', inner)
            poster = poster_match.group(1) if poster_match else None
            if poster and not poster.startswith("http"):
                poster = f"{self.base_url}{poster}"

            # Başlık
            title_match = re.search(r'<[^>]+class=["\'][^"\']*title[^"\']*["\'][^>]*>(.*?)<', inner)
            title = title_match.group(1).strip() if title_match else full_slug.replace("-", " ").title()

            results.append({
                "_id": full_slug,
                "episode_slug": full_slug,
                "anime_slug": anime_slug,
                "title": title,
                "episode_number": ep_num,
                "poster": poster,
                "url": ep_url if ep_url.startswith("http") else f"{self.base_url}/{full_slug}",
            })

        return results

    def fetch_calendar(self) -> list[dict]:
        """Haftalık yayın takvimini (/takvim) çeker."""
        resp = self._get("/takvim")
        if not resp:
            return []

        # Takvim günlerini bul (Pazartesi, Salı, Çarşamba...)
        day_sections = re.findall(
            r'<div[^>]+class=["\'][^"\']*(?:dayContainer|calendarDay|takvimGun)[^"\']*["\'][^>]*>(.*?)</div>\s*(?=<div[^>]+class=["\']|\Z)',
            resp.text,
            re.DOTALL,
        )

        schedule = []
        # Eğer özel class bulunamazsa h2/h3 başlıklarına göre böl
        if not day_sections:
            day_matches = re.split(r'<h[23][^>]*>(Pazartesi|Salı|Çarşamba|Perşembe|Cuma|Cumartesi|Pazar)</h[23]>', resp.text, flags=re.I)
            for i in range(1, len(day_matches), 2):
                day_name = day_matches[i].strip()
                day_content = day_matches[i + 1] if i + 1 < len(day_matches) else ""
                eps = []
                for m in re.finditer(r'href=["\']https?://anizm\.net/([^"\']+)["\'][^>]*>(.*?)</a>', day_content, re.DOTALL):
                    slug = m.group(1)
                    txt = " ".join(re.sub(r'<[^>]+>', ' ', m.group(2)).split())
                    if slug and "-bolum" not in slug and "kategori" not in slug:
                        eps.append({"anime_slug": slug, "title": txt})
                schedule.append({"day": day_name, "animes": eps})

        return schedule

    # ─────────────────────────────────────────────────────────────
    # ANİME DETAY & BÖLÜMLERİ
    # ─────────────────────────────────────────────────────────────

    def fetch_anime_detail(self, slug: str) -> dict | None:
        """
        Belirtilen anime slug'ının detay sayfasını (/slug) çeker ve ayrıştırır.

        Dönen yapı:
            {
                "slug": "death-note",
                "name": "Death Note",
                "original_name": "デスノート",
                "studio": "Madhouse",
                "description": "...",
                "poster": "https://anizm.net/storage/pcovers/4.webp",
                "genres": ["Doğaüstü-Güçler", "Gerilim", "Psikolojik"],
                "themes": ["Psikolojik"],
                "episodes": [
                    {
                        "episode_number": 1,
                        "episode_slug": "death-note-1-bolum-izle",
                        "url": "https://anizm.net/death-note-1-bolum-izle"
                    },
                    ...
                ]
            }
        """
        clean_slug = slug.strip("/").split("/")[-1]
        resp = self._get(f"/{clean_slug}")
        if not resp or resp.status_code != 200:
            return None

        html = resp.text

        # 404 / Bulunamadı kontrolü
        if "404 - Sayfa Bulunamadı" in html or "notfound" in getattr(resp, "url", ""):
            return None

        # Başlık
        title_match = re.search(r'<title>(.*?)\s*(?:izle\s*\|\s*Anizm|\|\s*Anizm)</title>', html, re.I)
        name = title_match.group(1).strip() if title_match else clean_slug.replace("-", " ").title()

        # Poster
        poster_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        poster = poster_match.group(1) if poster_match else None
        if not poster:
            p_img = re.search(r'<img[^>]+src=["\']([^"\']+/storage/pcovers/[^"\']+)["\']', html)
            poster = p_img.group(1) if p_img else None

        # Açıklama
        desc_match = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html)
        description = desc_match.group(1).strip() if desc_match else None

        # Metadata (.dataRow tablosu)
        original_name = None
        studio = None
        genres = []
        themes = []

        rows = re.findall(r'<li[^>]*class=["\'][^"\']*dataRow[^"\']*["\'][^>]*>(.*?)</li>', html, re.DOTALL)
        for row in rows:
            t_match = re.search(r'class=["\']dataTitle["\'][^>]*>(.*?)</span>', row, re.DOTALL)
            v_match = re.search(r'class=["\']dataValue["\'][^>]*>(.*?)</span>', row, re.DOTALL)
            if not t_match or not v_match:
                continue

            title_txt = re.sub(r'<[^>]+>', '', t_match.group(1)).strip().lower()
            val_txt = v_match.group(1)

            if "japonca" in title_txt:
                original_name = re.sub(r'<[^>]+>', '', val_txt).strip()
            elif "stüdyo" in title_txt or "studyo" in title_txt:
                studio = re.sub(r'<[^>]+>', '', val_txt).strip()
            elif "türler" in title_txt or "turler" in title_txt:
                genres = [g.strip() for g in re.findall(r'>([^<]+)</a>', val_txt) if g.strip()]
            elif "temalar" in title_txt:
                themes = [t.strip() for t in re.findall(r'>([^<]+)</a>', val_txt) if t.strip()]

        # Bölüm linkleri
        # Şablon: {slug}-{num}-bolum-izle veya {slug}-{num}-bolum
        ep_pattern = re.compile(
            rf'href=["\'](?:https?://[^"\']+)?/({re.escape(clean_slug)}-(\d+)-bolum[^"\']*)["\']',
            re.I,
        )
        found_eps = {}
        for m in ep_pattern.finditer(html):
            ep_slug = m.group(1)
            ep_num = int(m.group(2))

            # -izle olan versiyonu tercih et
            if ep_num not in found_eps or ep_slug.endswith("-izle"):
                found_eps[ep_num] = {
                    "episode_number": ep_num,
                    "episode_slug": ep_slug,
                    "url": f"{self.base_url}/{ep_slug}",
                }

        sorted_episodes = [found_eps[num] for num in sorted(found_eps.keys())]

        return {
            "slug": clean_slug,
            "name": name,
            "original_name": original_name,
            "studio": studio,
            "genres": genres,
            "themes": themes,
            "description": description,
            "poster": poster,
            "episode_count": len(sorted_episodes),
            "episodes": sorted_episodes,
        }

    # ─────────────────────────────────────────────────────────────
    # BÖLÜM VİDEOLARI & OYNATICILARI (AJAX)
    # ─────────────────────────────────────────────────────────────

    def resolve_player_url(self, video_id: int | str, episode_slug: str | None = None) -> str:
        """
        Anizm'in /player/{video_id} 302 yönlendiricisinden nihai oynatıcı linkini
        (ok.ru, drive.google.com, vidmoly.biz, sistenn, dood, voe vb.) yakalar.
        Yönlendirme bulunamazsa fallback olarak https://anizm.net/player/{video_id} döner.
        """
        player_endpoint = f"{self.base_url}/player/{video_id}"
        clean_slug = episode_slug.strip("/").split("/")[-1] if episode_slug else ""
        referer = f"{self.base_url}/{clean_slug}" if clean_slug else self.base_url
        headers = {
            "Referer": referer,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        session = self._get_session()
        try:
            # 1. Hafif HEAD isteği dene (Payload indirmez, anında döner)
            resp = session.head(
                player_endpoint,
                headers=headers,
                allow_redirects=False,
                verify=False,
                timeout=8,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if location and location.startswith("http"):
                    return location
        except Exception as e:
            logger.debug(f"HEAD link çözme hatası ({video_id}): {e}")

        try:
            # 2. HEAD başarısız olursa GET dene
            resp_get = session.get(
                player_endpoint,
                headers=headers,
                allow_redirects=False,
                verify=False,
                timeout=8,
            )
            if resp_get.status_code in (301, 302, 303, 307, 308):
                location = resp_get.headers.get("Location")
                if location and location.startswith("http"):
                    return location
        except Exception as e:
            logger.debug(f"GET link çözme hatası ({video_id}): {e}")

        return player_endpoint

    def fetch_episode_videos(self, episode_slug: str, resolve_urls: bool = True) -> list[dict]:
        """
        Belirtilen bölüm sayfasındaki (/slug) tüm çevirmen ve video oynatıcılarını çözer.

        Adımlar:
            1. Bölüm sayfasındaki translator butonlarını bulur (data-translatorclick).
            2. /episode/{epId}/translator/{transId} AJAX çağrısıyla video butonlarını alır.
            3. resolve_urls=True ise her videonun 302 yönlendirmesini takip edip nihai
               harici oynatıcı linkini (ok.ru, drive, vidmoly vb.) yakalar.
        """
        clean_ep_slug = episode_slug.strip("/").split("/")[-1]
        resp = self._get(f"/{clean_ep_slug}")
        if not resp:
            return []

        html = resp.text
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/{clean_ep_slug}",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

        # 1. Çevirmenleri bul (<a data-translatorclick ... translator="url">)
        translator_matches = re.finditer(
            r'<a[^>]+data-translatorclick[^>]+translator=["\']([^"\']+)["\'][^>]*>',
            html,
        )

        translators = []
        for m in translator_matches:
            tag_str = m.group(0)
            trans_url = m.group(1)

            # Fansub adı
            fansub_match = re.search(r'data-fansub-name=["\']([^"\']+)["\']', tag_str)
            fansub_name = fansub_match.group(1).strip() if fansub_match else "Anizm"

            translators.append({
                "url": trans_url,
                "fansub": fansub_name,
            })

        if not translators:
            return []

        all_videos = []

        # 2. Her çevirmenin oynatıcı butonlarını al
        for tr in translators:
            t_resp = self._get(tr["url"], headers=headers)
            if not t_resp:
                continue

            try:
                t_data = t_resp.json()
            except Exception:
                continue

            video_html = t_data.get("data", "")
            # <a video="https://anizm.net/video/{id}" data-video-name="Aincrad (Reklamsız)">
            v_buttons = re.finditer(
                r'video=["\']([^"\']+/video/(\d+))["\'][^>]*data-video-name=["\']([^"\']+)["\']',
                video_html,
            )

            for vb in v_buttons:
                video_id = int(vb.group(2))
                raw_player_name = vb.group(3)
                norm_player = self.normalize_player(raw_player_name)
                player_url = f"{self.base_url}/player/{video_id}"

                all_videos.append({
                    "id": video_id,
                    "player": norm_player,
                    "url": player_url,
                    "fansub": tr["fansub"],
                })

        # 3. Yönlendirmeleri çöz (Location başlığından gerçek nihai URL'i yakala)
        if resolve_urls and all_videos:
            def _resolve_task(v_item: dict):
                real_url = self.resolve_player_url(v_item["id"], clean_ep_slug)
                v_item["url"] = real_url
                refined = self.normalize_player(v_item["player"], real_url)
                if refined and refined != "DIGER":
                    v_item["player"] = refined
                return v_item

            with ThreadPoolExecutor(max_workers=min(len(all_videos), 6)) as executor:
                list(executor.map(_resolve_task, all_videos))

        return all_videos
