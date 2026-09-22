"""
animecix_provider.py — AnimeciX API Sağlayıcısı

AnimeciX (animecix.tv) platformundan anime, bölüm, yayın takvimi ve
video akışlarını çekmek için geliştirilmiş kök sağlayıcı sınıfı.

Özellikler:
    - Son çıkan bölümleri çekme (fetch_last_episodes)
    - Haftalık yayın takvimini çekme (fetch_calendar)
    - Anasayfa listelerini çekme (fetch_homepage_lists)
    - Anime başlık detayı (fetch_title_detail) — doğrudan tmdb_id, mal_id, anilist_id döner
    - Bölüm video oynatıcılarını çekme (fetch_episode_videos)
    - Anime arama (search)
    - Oynatıcı adı normalizasyonu (normalize_player)
    - Fansub adı ayrıştırma (extract_fansub_name)
    - TauVideo çözücü (resolve_tau_video)
"""

import base64
import os
import re
import time
import urllib.parse
from typing import Any
from Crypto.Cipher import AES
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from logger import setup_logger

logger = setup_logger("AnimecixProvider")

# AnimeciX API imza anahtarı (X-E-H)
XEH_KEY = b"i4C7R2fXGocdYgFLzCbDlsJjukf8G58b"


# Fansub rol regex'i (çevirmen, redaktör, encoder vb. etiketleri temizlemek için)
FANSUB_ROLE_REGEX = re.compile(
    r"(çeviri\s*(?:falan|filan|vb|vs|ve)?|çeviri\s*[&/+]?\s*redakte|çeviri|çeviren|çevirmen|çevirar|redakt[öo]r|redakte|redaksiyon"
    r"|edit[öo]r|encode[r]?|enkode|kodlama|upload[er]?|y[üu]kleyen|kontrol|qc|dizgi|timing"
    r"|zamanlama|karaoke|[şs]ark[ıi]|logo|tasar[ıi]m)\s*[:\-–]\s*",
    re.IGNORECASE,
)

# Bilinen video oynatıcı domain eşleştirmeleri
KNOWN_PLAYER_DOMAINS = [
    ("tau-video.xyz", "TAU"),
    ("sibnet.ru", "SIBNET"),
    ("vidmoly", "VIDMOLY"),
    ("drive.google.com", "GDRIVE"),
    ("mega.nz", "MEGA"),
    ("sendvid.com", "SENDVID"),
    ("uqload", "UQLOAD"),
    ("upvid", "UPVID"),
    ("streamango", "STREAMANGO"),
    ("mp4upload", "MP4UPLOAD"),
    ("mail.ru", "MAILRU"),
    ("my.mail.ru", "MAILRU"),
    ("vk.com", "VK"),
    ("ok.ru", "ODNO"),
    ("odnoklassniki", "ODNO"),
    ("dailymotion.com", "DAILYMOTION"),
    ("dai.ly", "DAILYMOTION"),
    ("youtube.com", "YOUTUBE"),
    ("youtu.be", "YOUTUBE"),
    ("dood", "DOODSTREAM"),
    ("fembed", "FEMBED"),
]



class AnimecixProvider:
    """AnimeciX REST API istemcisi ve veri sağlayıcısı."""

    BASE_URL = "https://animecix.tv"
    TAU_BASE_URL = "https://tau-video.xyz"
    DEFAULT_TIMEOUT = 15

    def __init__(self, base_url: str | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout

        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "tr,en;q=0.9",
            "Referer": f"{self.base_url}/",
        })

    @staticmethod
    def generate_xeh(query_str: str) -> str:
        """
        AnimeciX X-E-H imza algoritması.
        plaintext = "{version}" + query_string
        AES-GCM (128-bit tag, 12-byte random IV) ile şifrelenir.
        Dönüş: base64(ciphertext||tag) + "." + base64(iv)
        """
        plaintext = f"{{version}}{query_str}".encode("utf-8")
        iv = os.urandom(12)
        cipher = AES.new(XEH_KEY, AES.MODE_GCM, nonce=iv)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        ct_tag_b64 = base64.b64encode(ciphertext + tag).decode("utf-8")
        iv_b64 = base64.b64encode(iv).decode("utf-8")
        return f"{ct_tag_b64}.{iv_b64}"

    def _get(self, endpoint: str, params: dict | None = None, max_retries: int = 5) -> dict | list | None:
        """API'ye GET isteği atar ve JSON çıktısını döner. HTTP 429 durumunda otomatik backoff uygular."""
        url = f"{self.base_url}{endpoint}" if endpoint.startswith("/") else f"{self.base_url}/{endpoint}"
        query_str = urllib.parse.urlencode(params) if params else ""
        xeh_header = self.generate_xeh(query_str)

        headers = {
            "X-E-H": xeh_header,
        }

        # Kibar istek aralığı (Cloudflare / API rate limit koruması)
        time.sleep(0.25)

        backoff_delays = [10, 20, 40, 60, 90]

        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 404:
                    logger.debug(f"404 Not Found: {url}")
                    return None
                elif resp.status_code == 429:
                    wait_time = backoff_delays[min(attempt, len(backoff_delays) - 1)]
                    logger.warning(f"⏳ HTTP 429 Rate Limit ({url}). {wait_time}sn bekleniyor (Deneme {attempt + 1}/{max_retries})...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.warning(f"HTTP {resp.status_code} ({url}): {resp.text[:200]}")
                    return None
            except requests.RequestException as e:
                logger.error(f"İstek hatası ({url}): {e}")
                time.sleep(3)
                continue

        return None

    # ─────────────────────────────────────────────────────────────
    # FEED & YAYIN KONTROL UÇLARI
    # ─────────────────────────────────────────────────────────────

    def fetch_last_episodes(self, page: int = 1) -> list[dict]:
        """
        Son eklenen bölümleri kronolojik sırayla (en yeniden eskiye) çeker.

        Dönen her kayıt yapısı:
            - _id: str
            - title_id: int (AnimeciX title ID)
            - season_number: int
            - episode_number: int
            - title_name: str
            - title_poster: str
            - release_date: str
            - videos: list[dict] (Eklenen video nesneleri)
        """
        data = self._get("/secure/last-episodes", params={"page": page})
        if isinstance(data, dict):
            return data.get("data", [])
        return []

    def fetch_calendar(self) -> list[dict]:
        """
        Haftalık yayın takvimini çeker.
        7 günlük gün listesi döner; her günün 'episodes' listesinde
        yayınlanan bölümler ve her bölümün içinde tam 'title' künyesi
        (tmdb_id, mal_id vb.) yer alır.
        """
        data = self._get("/secure/calendar")
        if isinstance(data, dict):
            return data.get("data", [])
        return []

    def fetch_homepage_lists(self) -> list[dict]:
        """
        Anasayfada yer alan vitrin listelerini döner.
        Örnek listeler:
            - 'Son Çıkan Animeler' (40 anime)
            - 'Sezonun İncileri' (10 anime)
            - 'Çok Oy Alan Movieler' (10 film)
            - 'Gelecek Animeler'
        """
        data = self._get("/secure/homepage/lists-guests")
        if isinstance(data, dict):
            return data.get("lists", [])
        return []

    # ─────────────────────────────────────────────────────────────
    # DETAY & BÖLÜM VİDEOLARI
    # ─────────────────────────────────────────────────────────────

    def fetch_title_detail(self, title_id: int, season_number: int = 1) -> dict | None:
        """
        Verilen AnimeciX başlığının künye detayını döner.

        Dönen 'title' nesnesindeki kritik alanlar:
            - id: AnimeciX ID (int)
            - tmdb_id: TMDB ID (int)
            - mal_id: MyAnimeList ID (int)
            - anilist_id: AniList ID (int)
            - imdb_id: IMDB ID (str)
            - name: Başlık adı
            - original_title: Orijinal Japonca adı
            - name_english: İngilizce adı
            - name_romanji: Romaji adı
            - type / is_series: 'series' (TV) veya 'movie' (Film)
            - season_count: Sezon adedi
            - seasons: Sezon listesi [{id, number, episode_count, ...}]
        """
        data = self._get(f"/secure/titles/{title_id}", params={"seasonNumber": season_number})
        if isinstance(data, dict):
            return data.get("title")
        return None

    def fetch_episode_videos(self, title_id: int, season_num: int = 1, episode_num: int = 1) -> list[dict]:
        """
        Belirtilen sezon ve bölüm için tüm video oynatıcı alternatiflerini çeker.
        Pagination varsa tüm sayfaları birleştirir.

        Dönen her video nesnesi:
            - name: Oynatıcı adı (ör. 'Sibnet', 'Tau Video', 'VipPlayer')
            - url: Embed/oynatıcı adresi
            - extra: Fansub / çevirmen metni (ör. 'Çevirmen: Napryzon Encoder: Hakuryuu')
            - quality: Kalite bilgisi
            - language: Dil bilgisi ('tr')
        """
        all_videos = []
        page = 1

        while True:
            params = {
                "titleId": title_id,
                "season": season_num,
                "episode": episode_num,
                "page": page,
            }
            data = self._get("/secure/videos", params=params)
            if not isinstance(data, dict):
                break

            pagination = data.get("pagination", {})
            videos = pagination.get("data", [])
            all_videos.extend(videos)

            current_page = pagination.get("current_page", 1)
            last_page = pagination.get("last_page", 1)

            if current_page >= last_page or not videos:
                break
            page += 1

        return all_videos

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """
        AnimeciX arşivinde başlık araması yapar.

        Dönen her sonuç:
            - id: AnimeciX ID
            - name: İsim
            - name_english: İngilizce isim
            - name_romanji: Romaji isim
            - type / title_type: 'anime', 'series', 'movie'
            - year: Yayın yılı
            - tmdb_vote_average: Puan
        """
        encoded_query = urllib.parse.quote(query.strip())
        data = self._get(f"/secure/search/{encoded_query}", params={"limit": limit})
        if isinstance(data, dict):
            return data.get("results", [])
        return []

    # ─────────────────────────────────────────────────────────────
    # YARDIMCI METOTLAR (NORMALİZASYON VE AYRIŞTIRMA)
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_player(player_name: str | None, url: str | None) -> str:
        """
        Oynatıcı adını veya URL domain'ini veritabanı standartlarına uygun
        büyük harfli kısa ada dönüştürür (ör. 'SIBNET', 'VIDMOLY', 'TAU', 'GDRIVE').
        """
        url_lower = (url or "").lower()
        for domain, standard_name in KNOWN_PLAYER_DOMAINS:
            if domain in url_lower:
                return standard_name

        name_lower = (player_name or "").lower().strip()
        if "sibnet" in name_lower:
            return "SIBNET"
        if "vidmoly" in name_lower or "vipplayer" in name_lower:
            return "VIDMOLY"
        if "tau" in name_lower:
            return "TAU"
        if "gdrive" in name_lower or "google" in name_lower:
            return "GDRIVE"
        if "mega" in name_lower:
            return "MEGA"
        if "sendvid" in name_lower:
            return "SENDVID"
        if "uqload" in name_lower:
            return "UQLOAD"
        if "upvid" in name_lower:
            return "UPVID"
        if "mango" in name_lower:
            return "STREAMANGO"
        if "mp4upload" in name_lower:
            return "MP4UPLOAD"
        if "dailymotion" in name_lower:
            return "DAILYMOTION"
        if "odno" in name_lower or "odnoklassniki" in name_lower:
            return "ODNO"


        # Bilinmeyen oynatıcılar için temizlenmiş büyük harf
        clean_name = re.sub(r"[^A-Za-z0-9_]", "", (player_name or "BILINMEYEN")).upper()
        return clean_name or "DIGER"

    @staticmethod
    def extract_fansub_name(extra_text: str | None) -> str | None:
        """
        Video nesnesinin 'extra' alanından okunaklı fansub / çevirmen adını çıkarır.
        (Max 28 karakter; geçersiz/sayısal/boş değerler için None döner).
        """
        if not extra_text:
            return None

        # URL'leri, Discord ve Telegram davetlerini temizle
        s = re.sub(
            r"(https?://\S+|www\.\S+|discord(?:app)?\.(?:gg|com)/\S+|t\.me/\S+)",
            " ",
            extra_text,
            flags=re.IGNORECASE,
        )
        s = re.sub(r"\b(?:dc|discord|telegram|tg)\s*:?\s*$", " ", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+", " ", s).strip()

        if not s or s.isdigit():
            return None

        # Rol etiketli krediler (Çevirmen: X, Encoder: Y)
        matches = list(FANSUB_ROLE_REGEX.finditer(s))
        if matches:
            pairs = []
            for i, m in enumerate(matches):
                role = m.group(1).lower()
                val_start = m.end()
                val_end = matches[i + 1].start() if i + 1 < len(matches) else len(s)
                val = s[val_start:val_end].strip(", /-\u2013|. ")
                if val and not val.isdigit():
                    pairs.append((role, val))

            # Tercihen çevirmen rolünü seç
            for r, v in pairs:
                if r.startswith("çevir"):
                    return v[:28].strip()
            if pairs:
                return pairs[0][1][:28].strip()

        # Ayraçlarla ayrılmış liste (ör. "Quizzy - Prenses" -> "Quizzy")
        parts = re.split(r"\s*[|/]\s*|\s+[-–]\s+|\s*&\s*", s)
        first_valid = [p.strip() for p in parts if p.strip() and not p.strip().isdigit()]
        if first_valid:
            return first_valid[0][:28].strip()

        return None

    def resolve_tau_video(self, tau_embed_or_key: str) -> list[dict]:
        """
        TauVideo embed linkinden veya video anahtarından doğrudan video akışlarını (MP4/HLS) çözer.

        Args:
            tau_embed_or_key: 'https://tau-video.xyz/embed/64d1925428edaf68b3c26ae9' veya '64d1925428edaf68b3c26ae9'

        Returns:
            list[dict]: [{'label': '1080p', 'url': 'https://...'}, ...]
        """
        key = tau_embed_or_key.rstrip("/").split("/")[-1]
        api_url = f"{self.TAU_BASE_URL}/api/video/{key}"

        try:
            resp = self.session.get(api_url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("urls", [])
        except Exception as e:
            logger.debug(f"TauVideo akış çözme hatası ({key}): {e}")

        return []
