"""
episode_scraper.py — Bölüm & Video Kazıyıcı

Türkanime API kütüphanesini kullanarak animelerin bölümlerini ve
her bölümdeki dış kaynak video (iframe) linklerini çeker.
Jikan API'den gelen bölüm detaylarıyla (filler/recap) eşleştirir.

Özellikler:
    - Sadece onaylanmış iframe kaynakları (SIBNET, MP4UPLOAD vb.) çekilir (whitelist)
    - Regex tabanlı akıllı bölüm numarası eşleştirmesi
    - Delta güncelleme desteği (main.py tarafından kontrol edilir)
"""

import re
import time

from turkanime_api import Anime as TurkanimeAnime

# ─────────────────────────────────────────────
# Sabitler
# ─────────────────────────────────────────────

# İzin verilen dış kaynak video sağlayıcıları (whitelist)
# Sadece bu listedeki player'ların iframe URL'leri çekilecektir.
ALLOWED_PLAYERS = {"SIBNET", "MAIL", "FILEMOON", "MP4UPLOAD", "UQLOAD", "SENDVID", "BYS", "GDRIVE"}

# İstekler arası bekleme süresi (saniye)
TURKANIME_DELAY = 0.1

# Regex: Geçersiz bölüm formatlarını yakalar (tire veya virgüllü sayılar)
# Örn: "12-13. Bölüm", "13,5. Bölüm" → eşleştirme YAPILMAZ
INVALID_EPISODE_PATTERN = re.compile(r'(\d+[-–]\d+|\d+[,،]\d+)\.\s*[Bb]ölüm')

# Regex: Geçerli bölüm formatı — tam sayı + ". Bölüm"
# Örn: "Naruto 12. Bölüm" → episode_number = 12
VALID_EPISODE_PATTERN = re.compile(r'(\d+)\.\s*[Bb]ölüm')


class EpisodeScraper:
    """Türkanime bölüm/video çekici ve Jikan eşleştirme motoru."""

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
            print(f"[Episodes] ⚠️  Bölüm listesi alınamadı ({slug}): {e}")
            return []

        if not bolumler:
            return []

        print(f"[Episodes]   📋 {len(bolumler)} bölüm bulundu. (Mevcut: {len(existing_episodes_map)})")

        episodes = []
        for idx, bolum in enumerate(bolumler, 1):
            # ── Akıllı Delta Kontrolü ──
            if bolum.title in existing_episodes_map:
                # Bu bölüm daha önce çekilmiş, HTTP isteği yapmadan doğrudan kopyala
                episodes.append({
                    "turkanime_title": bolum.title,
                    "videos": existing_episodes_map[bolum.title].get("videos", [])
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
                print(f"[Episodes]   ⚠️  Videolar alınamadı ({bolum.slug}): {e}")
                episodes.append(ep_data)
                continue

            for video in videos:
                # Sadece izin verilen player'ları kabul et (whitelist)
                if video.player not in ALLOWED_PLAYERS:
                    continue

                try:
                    video_url = video.url  # Lazy: HTTP + AES decryption
                except Exception:
                    video_url = None

                if video_url:
                    ep_data["videos"].append({
                        "fansub": video.fansub,
                        "player": video.player,
                        "url": video_url,
                    })
                    
                time.sleep(TURKANIME_DELAY)

            episodes.append(ep_data)

            # Her 10 bölümde bir ilerleme bildir
            if idx % 10 == 0:
                print(f"[Episodes]   ⏳ {idx}/{len(bolumler)} bölüm işlendi...")

            time.sleep(TURKANIME_DELAY)

        return episodes

    def build_episode_list(
        self,
        turkanime_episodes: list[dict],
        jikan_episodes: dict[int, dict],
    ) -> list[dict]:
        """
        Türkanime bölümlerini Jikan verileriyle eşleştirerek nihai listeyi oluşturur.

        Eşleştirme kuralları:
            - Başlıkta "{sayı}. Bölüm" formatı varsa → eşleştir
            - Tire veya virgüllü sayı varsa → eşleştirme YAPMA
            - Eşleşen bölümlere Jikan'dan filler/recap bilgisi eklenir
            - Eşleşmeyen bölümlerde jikan_mal_id, filler, recap = null

        Args:
            turkanime_episodes: fetch_turkanime_episodes() çıktısı.
            jikan_episodes: JikanEnricher.fetch_episodes() çıktısı.

        Returns:
            list[dict]: Nihai episodes dizisi (JSON'a yazılacak format).
        """
        result = []

        for ep in turkanime_episodes:
            title = ep["turkanime_title"]
            ep_number = self.extract_episode_number(title)

            entry = {
                "turkanime_title": title,
                "episode_number": ep_number,
                "videos": ep["videos"],
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
        existing_episodes: list[dict] = None
    ) -> list[dict]:
        """
        Tüm adımları birleştiren ana metod.

        Adımlar:
            1. Türkanime'den bölüm + video çek (mevcutları atlayarak)
            2. mal_id varsa Jikan'dan bölüm detaylarını çek
            3. Eşleştirmeyi yap ve nihai listeyi döndür

        Args:
            slug: Anime slug'ı.
            mal_id: MyAnimeList anime ID'si (veya None).
            jikan: JikanEnricher instance'ı (fetch_episodes metodu için).
            existing_episodes: Mevcut bölümler listesi (delta için).

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
                print(f"[Episodes]   📖 Jikan'dan {len(jikan_episodes)} bölüm bilgisi alındı.")

        # 3. Eşleştirme ve birleştirme
        return self.build_episode_list(turkanime_episodes, jikan_episodes)
