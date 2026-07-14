"""
episode_scraper.py — Bölüm & Video Kazıyıcı

Türkanime API kütüphanesini kullanarak animelerin bölümlerini ve
her bölümdeki dış kaynak video (iframe) linklerini çeker.
Jikan API'den gelen bölüm detaylarıyla (filler/recap) eşleştirir.
AniSkip API'den OP/ED atlama sürelerini alır.

Özellikler:
    - Sadece onaylanmış iframe kaynakları (SIBNET, MP4UPLOAD vb.) çekilir (whitelist)
    - Regex tabanlı akıllı bölüm numarası eşleştirmesi
    - Çakışma tespiti ve metin benzerliği ile akıllı çözümleme
    - AniSkip API ile OP/ED skip süreleri entegrasyonu
    - Delta güncelleme desteği (main.py tarafından kontrol edilir)
"""

import re
import time
from difflib import SequenceMatcher

import requests
from logger import setup_logger

_logger = setup_logger("Episodes")

from turkanime_api import Anime as TurkanimeAnime

# ─────────────────────────────────────────────
# Sabitler
# ─────────────────────────────────────────────

# İzin verilen dış kaynak video sağlayıcıları (whitelist)
# Sadece bu listedeki player'ların iframe URL'leri çekilecektir.
ALLOWED_PLAYERS = {"SIBNET", "MAIL", "FILEMOON", "MP4UPLOAD", "UQLOAD", "SENDVID", "BYS", "GDRIVE"}

# İstekler arası bekleme süresi (saniye)
TURKANIME_DELAY = 0.1

# AniSkip API base URL’i (OP/ED atlama süreleri)
ANISKIP_BASE_URL = "https://api.aniskip.com/v2/skip-times"

# Regex: Geçersiz bölüm formatlarını yakalar (tire veya virgüllü sayılar)
# Örn: "12-13. Bölüm", "13,5. Bölüm" → eşleştirme YAPILMAZ
INVALID_EPISODE_PATTERN = re.compile(r'(\d+[-–]\d+|\d+[,،]\d+)\.\s*[Bb]ölüm')

# Regex: Geçerli bölüm formatı — tam sayı + ". Bölüm"
# Örn: "Naruto 12. Bölüm" → episode_number = 12
VALID_EPISODE_PATTERN = re.compile(r'(\d+)\.\s*[Bb]ölüm')


class EpisodeScraper:
    """Türkanime bölüm/video çekici, çakışma çözücü, Jikan eşleştirme ve AniSkip motoru."""

    def __init__(self, metadata: dict = None):
        self.metadata = metadata if metadata is not None else {"genres": {}, "studios": {}, "fansubs": {}}
        self.logger = _logger

    def _get_or_create_fansub_id(self, name: str) -> int:
        """Fansub ismini metadata'da sorgular, yoksa yeni ID ile ekler."""
        if not name:
            name = "Bilinmeyen"
        name_clean = name.strip()
        name_lower = name_clean.lower()
        
        # Büyük/küçük harf duyarsız arama (hem dict hem düz string formatını destekler)
        for fs_id_str, fs_data in self.metadata.get("fansubs", {}).items():
            if isinstance(fs_data, dict):
                fs_name = fs_data.get("name", "")
            else:
                fs_name = str(fs_data)
                
            if fs_name.lower() == name_lower:
                return int(fs_id_str)
        
        # Mevcut en büyük ID'yi bul ve +1 ekle
        existing_ids = [int(k) for k in self.metadata.get("fansubs", {}).keys() if k.isdigit()]
        new_id = max(existing_ids) + 1 if existing_ids else 1
        
        if "fansubs" not in self.metadata:
            self.metadata["fansubs"] = {}
            
        self.metadata["fansubs"][str(new_id)] = {
            "name": name_clean,
            "url": ""
        }
        self.logger.info(f"🆕 Yeni fansub tanımlandı: {name_clean} (ID: {new_id})")
        return new_id

    @staticmethod
    def fetch_skip_times(mal_id: int, ep_number: int) -> dict | None:
        """
        AniSkip API'den bir bölümün OP (Opening) ve ED (Ending)
        atlama sürelerini çeker.

        Args:
            mal_id: MyAnimeList anime ID'si.
            ep_number: Bölüm numarası.

        Returns:
            dict: {"op": {"start": float, "end": float}, "ed": {...}} veya None.
        """
        url = f"{ANISKIP_BASE_URL}/{mal_id}/{ep_number}"
        params = {"types": ["op", "ed"], "episodeLength": 0}

        max_retries = 3
        retry_delay = 2
        data = None

        for attempt in range(max_retries):
            try:
                resp = requests.get(url, params=params, timeout=10)
                
                # 429 Rate Limit Kontrolü
                if resp.status_code == 429:
                    _logger.warning(f"AniSkip Rate Limit aşıldı (Deneme {attempt + 1}/{max_retries}). {retry_delay}s bekleniyor...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                    
                resp.raise_for_status()
                data = resp.json()
                break
                
            except requests.exceptions.Timeout:
                _logger.warning(f"AniSkip Timeout hatası (Deneme {attempt + 1}/{max_retries}).")
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            except requests.exceptions.RequestException as e:
                # 404 (Bulunamadı) durumu gayet normaldir, atlanabilir.
                if getattr(e.response, 'status_code', None) == 404:
                    return None
                _logger.warning(f"AniSkip API Bağlantı Hatası (mal_id: {mal_id}, ep: {ep_number}): {e}")
                return None
            except Exception as e:
                _logger.warning(f"AniSkip Beklenmeyen Hata: {e}")
                return None

        if not data or not data.get("found") or not data.get("results"):
            return None

        skip_times = {}
        for item in data["results"]:
            skip_type = item.get("skipType")  # "op" veya "ed"
            interval = item.get("interval", {})
            if skip_type in ("op", "ed") and interval:
                skip_times[skip_type] = {
                    "start": interval.get("startTime"),
                    "end": interval.get("endTime"),
                }

        return skip_times if skip_times else None

    @staticmethod
    def _title_similarity(title: str, anime_name: str) -> float:
        """
        Bölüm başlığı ile anime ana ismi arasındaki metin benzerliğini hesaplar.
        SequenceMatcher kullanarak 0.0 (hiç benzemiyor) ile 1.0 (aynı) arası değer döner.

        Karşılaştırma büyük/küçük harf duyarsızdır.

        Args:
            title: Türkanime bölüm başlığı.
            anime_name: Animenin ana ismi.

        Returns:
            float: 0.0–1.0 arası benzerlik skoru.
        """
        return SequenceMatcher(None, title.lower(), anime_name.lower()).ratio()

    @staticmethod
    def _resolve_conflicts(
        ep_numbers: dict[int, list[int]],
        titles: list[str],
        anime_name: str,
    ) -> set[int]:
        """
        Aynı bölüm numarasına sahip birden fazla içerik arasındaki çakışmaları çözer.

        Çözüm mantığı:
            - Her çakışan grup için, başlıkları anime ana ismiyle karşılaştırır.
            - En yüksek benzerlik skoruna sahip bölüm "kazanan" olur.
            - Diğerleri "kaybeden" olarak işaretlenir → Jikan eşleştirme dışı kalır.

        Örnek:
            Anime ismi: "Shingeki no Kyojin"
            Çakışanlar: ["Shingeki no Kyojin 4. Bölüm", "Shingeki no Kyojin OVA 4. Bölüm"]
            Kazanan: "Shingeki no Kyojin 4. Bölüm" (daha yüksek skor)

        Args:
            ep_numbers: {bölüm_numarası: [indeks_listesi]} haritalama.
            titles: Tüm bölüm başlıklarının listesi.
            anime_name: Animenin ana ismi.

        Returns:
            set[int]: Eşleştirme dışı bırakılacak indekslerin kümesi.
        """
        excluded_indices = set()

        for ep_num, indices in ep_numbers.items():
            if len(indices) <= 1:
                continue  # Çakışma yok

            # Her çakışan bölüm için benzerlik skoru hesapla
            scored = []
            for idx in indices:
                score = SequenceMatcher(
                    None, titles[idx].lower(), anime_name.lower()
                ).ratio()
                scored.append((idx, score))

            # En yüksek skora sahip olanı kazanan olarak seç
            scored.sort(key=lambda x: x[1], reverse=True)
            winner_idx = scored[0][0]

            # Kaybedenleri eşleştirme dışı bırak
            for idx, score in scored:
                if idx != winner_idx:
                    excluded_indices.add(idx)

            # Bilgilendirme logu
            winner_title = titles[winner_idx]
            loser_titles = [titles[idx] for idx, _ in scored if idx != winner_idx]
            _logger.info(
                f"⚔️  Çakışma çözüldü (Bölüm {ep_num}): "
                f"Kazanan='{winner_title}' | Kaybeden(ler)={loser_titles}"
            )

        return excluded_indices

    @staticmethod
    def extract_episode_number(title: str) -> int | None:
        """
        Türkanime bölüm başlığından bölüm numarasını çıkarır.

        Geçerli örnekler:
            "Naruto 12. Bölüm"           → 12
            "Shingeki no Kyojin 1. Bölüm" → 1

        Geçersiz (None döner):
            "Naruto 12-13. Bölüm"         → None (tire — birden fazla bölüm)
            "Shingeki no Kyojin 13,5. Bölüm" → None (buçuk bölüm)
            "Naruto OVA"                  → None (bölüm formatı yok)

        Args:
            title: Türkanime bölüm başlığı.

        Returns:
            int: Bölüm numarası veya None.
        """
        if INVALID_EPISODE_PATTERN.search(title):
            return None

        match = VALID_EPISODE_PATTERN.search(title)
        return int(match.group(1)) if match else None

    def fetch_turkanime_episodes(self, slug: str, existing_episodes_map: dict[str, dict] = None) -> list[dict]:
        """
        turkanime_api kütüphanesini kullanarak bir animenin tüm bölümlerini
        ve her bölümdeki dış kaynak videolarını çeker. Eksik bölümleri akıllıca tarar.

        Akış:
            1. TurkanimeAnime(slug, parse_fansubs=True) oluştur
            2. anime.bolumler ile tüm bölüm objelerini al
            3. Her bölüm için kontrol et:
               - Eğer existing_episodes_map içinde varsa → videolarını çekme, eskisini kullan
               - Yoksa → bolum.videos ile tüm videoları al, filtrele (whitelist), URL'leri çöz

        Args:
            slug: Anime slug'ı (örn: "naruto")
            existing_episodes_map: Önceden çekilmiş {başlık: episode_dict} haritası

        Returns:
            list[dict]: Her bölüm için veri listesi. Boş liste hata durumunda.
        """
        existing_episodes_map = existing_episodes_map or {}

        try:
            anime_obj = TurkanimeAnime(slug, parse_fansubs=True)
            bolumler = anime_obj.bolumler
        except Exception as e:
            self.logger.warning(f"Bölüm listesi alınamadı ({slug}): {e}")
            return []

        if not bolumler:
            return []

        self.logger.info(f"📋 {len(bolumler)} bölüm bulundu. (Mevcut: {len(existing_episodes_map)})")

        episodes = []
        for idx, bolum in enumerate(bolumler, 1):
            # ── Akıllı Delta Kontrolü ──
            if bolum.title in existing_episodes_map:
                # Bu bölüm daha önce çekilmiş, HTTP isteği yapmadan doğrudan kopyala
                existing_ep = existing_episodes_map[bolum.title]
                episodes.append({
                    "turkanime_title": bolum.title,
                    "videos": existing_ep.get("videos", []),
                    "skip_times": existing_ep.get("skip_times"),
                })
                continue

            # ── Yeni Bölüm Çekimi ──
            ep_data = {
                "turkanime_title": bolum.title,
                "videos": [],
            }

            try:
                videos = bolum.videos
            except Exception as e:
                self.logger.warning(f"Videolar alınamadı ({bolum.slug}): {e}")
                episodes.append(ep_data)
                continue

            for video in videos:
                # Sadece izin verilen player'ları kabul et (whitelist)
                if video.player not in ALLOWED_PLAYERS:
                    continue

                try:
                    video_url = video.url  # Lazy: HTTP + AES decryption
                except Exception as e:
                    self.logger.warning(f"Video URL çözümlenemedi ({video.player}): {e}")
                    video_url = None

                if video_url:
                    fansub_id = self._get_or_create_fansub_id(video.fansub)
                    ep_data["videos"].append({
                        "fansub_id": fansub_id,
                        "player": video.player,
                        "url": video_url,
                    })
                    
                time.sleep(TURKANIME_DELAY)

            episodes.append(ep_data)

            # Her 10 bölümde bir ilerleme bildir
            if idx % 10 == 0:
                self.logger.info(f"⏳ {idx}/{len(bolumler)} bölüm işlendi...")

            time.sleep(TURKANIME_DELAY)

        return episodes

    def build_episode_list(
        self,
        turkanime_episodes: list[dict],
        jikan_episodes: dict[int, dict],
        anime_name: str = "",
    ) -> list[dict]:
        """
        Türkanime bölümlerini Jikan verileriyle eşleştirerek nihai listeyi oluşturur.
        Çakışan bölüm numaralarını metin benzerliği ile çözer.

        Eşleştirme kuralları:
            - Başlıkta "{sayı}. Bölüm" formatı varsa → eşleştir
            - Tire veya virgüllü sayı varsa → eşleştirme YAPMA
            - Aynı numaraya sahip birden fazla bölüm varsa → çakışma tespiti
            - Çakışmalarda anime ismine en benzeyen başlık kazanır
            - Kaybeden bölümler eşleştirme dışı bırakılır
            - Eşleşen bölümlere Jikan'dan filler/recap bilgisi eklenir
            - Eşleşmeyen bölümlerde jikan_mal_id, filler, recap = null

        Args:
            turkanime_episodes: fetch_turkanime_episodes() çıktısı.
            jikan_episodes: JikanEnricher.fetch_episodes() çıktısı.
            anime_name: Animenin ana ismi (çakışma çözümlemesi için).

        Returns:
            list[dict]: Nihai episodes dizisi (JSON'a yazılacak format).
        """
        # ── Adım 1: Tüm bölüm numaralarını çıkar ──
        titles = [ep["turkanime_title"] for ep in turkanime_episodes]
        ep_numbers_list = [self.extract_episode_number(t) for t in titles]

        # ── Adım 2: Çakışma haritası oluştur ──
        # {bölüm_numarası: [bu numaraya sahip bölümlerin indeksleri]}
        ep_num_to_indices: dict[int, list[int]] = {}
        for i, ep_num in enumerate(ep_numbers_list):
            if ep_num is not None:
                ep_num_to_indices.setdefault(ep_num, []).append(i)

        # ── Adım 3: Çakışmaları çöz ──
        excluded_indices = self._resolve_conflicts(
            ep_num_to_indices, titles, anime_name
        ) if anime_name else set()

        # ── Adım 4: Nihai listeyi oluştur ──
        result = []
        for i, ep in enumerate(turkanime_episodes):
            title = ep["turkanime_title"]
            ep_number = ep_numbers_list[i]

            # Çakışma kaybedeni ise → eşleştirme dışı bırak
            if i in excluded_indices:
                ep_number = None

            entry = {
                "turkanime_title": title,
                "episode_number": ep_number,
                "videos": ep["videos"],
                "skip_times": ep.get("skip_times"),
            }

            # Eşleştirme: ep_number varsa ve Jikan verisinde bu numara mevcutsa
            if ep_number is not None and ep_number in jikan_episodes:
                jikan_ep = jikan_episodes[ep_number]
                entry["jikan_mal_id"] = ep_number
                entry["filler"] = jikan_ep.get("filler", False)
                entry["recap"] = jikan_ep.get("recap", False)
            else:
                entry["jikan_mal_id"] = None
                entry["filler"] = None
                entry["recap"] = None

            result.append(entry)

        return result

    def scrape_episodes(
        self, 
        slug: str, 
        mal_id: int | None, 
        jikan, 
        existing_episodes: list[dict] = None,
        anime_name: str = "",
    ) -> list[dict]:
        """
        Tüm adımları birleştiren ana metod.

        Adımlar:
            1. Türkanime'den bölüm + video çek (mevcutları atlayarak)
            2. mal_id varsa Jikan'dan bölüm detaylarını çek
            3. Çakışma tespiti + metin benzerliği ile çözümleme
            4. Eşleştirmeyi yap ve nihai listeyi döndür
            5. Yeni bölümler için AniSkip'ten OP/ED sürelerini çek

        Args:
            slug: Anime slug'ı.
            mal_id: MyAnimeList anime ID'si (veya None).
            jikan: JikanEnricher instance'ı (fetch_episodes metodu için).
            existing_episodes: Mevcut bölümler listesi (delta için).
            anime_name: Animenin ana ismi (çakışma çözümlemesi için).

        Returns:
            list[dict]: Nihai episodes dizisi. Boş liste hata durumunda.
        """
        existing_episodes = existing_episodes or []
        existing_episodes_map = {
            ep["turkanime_title"]: ep 
            for ep in existing_episodes 
            if "turkanime_title" in ep
        }

        # 1. Türkanime bölümleri (eksik videoları çeker, mevcutları korur)
        turkanime_episodes = self.fetch_turkanime_episodes(slug, existing_episodes_map)
        if not turkanime_episodes:
            return []

        # 2. Jikan bölüm detayları (mal_id varsa)
        jikan_episodes = {}
        if mal_id is not None:
            jikan_episodes = jikan.fetch_episodes(mal_id)
            if jikan_episodes:
                self.logger.info(f"📖 Jikan'dan {len(jikan_episodes)} bölüm bilgisi alındı.")

        # 3. Eşleştirme, çakışma çözümleme ve birleştirme
        episodes = self.build_episode_list(turkanime_episodes, jikan_episodes, anime_name)

        # 4. Yeni bölümler için AniSkip'ten OP/ED atlama sürelerini çek
        if mal_id is not None:
            aniskip_count = 0
            for ep in episodes:
                # skip_times zaten varsa (mevcut bölüm) → atla
                if ep.get("skip_times") is not None:
                    continue

                ep_number = ep.get("episode_number")
                if ep_number is None:
                    continue

                skip_times = self.fetch_skip_times(mal_id, ep_number)
                ep["skip_times"] = skip_times
                if skip_times:
                    aniskip_count += 1
                time.sleep(0.1)

            if aniskip_count:
                self.logger.info(f"⏭️  AniSkip'ten {aniskip_count} bölüm için OP/ED süresi alındı.")

        return episodes
