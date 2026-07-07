"""
episode_scraper.py — Bölüm & Video Kazıyıcı

Türkanime API kütüphanesini kullanarak animelerin bölümlerini ve
her bölümdeki dış kaynak video (iframe) linklerini çeker.
Jikan API'den gelen bölüm detaylarıyla (filler/recap) eşleştirir.

Özellikler:
    - Sadece onaylanmış iframe kaynakları (SIBNET, MP4UPLOAD vb.) çekilir (whitelist)
    - Regex tabanlı akıllı bölüm numarası eşleştirmesi
    - Çakışma tespiti ve metin benzerliği ile akıllı çözümleme
    - Delta güncelleme desteği (main.py tarafından kontrol edilir)
"""

import re
import time
from difflib import SequenceMatcher

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
    """Türkanime bölüm/video çekici, çakışma çözücü ve Jikan eşleştirme motoru."""

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
            print(
                f"[Episodes]   ⚔️  Çakışma çözüldü (Bölüm {ep_num}): "
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
                print(f"[Episodes]   📖 Jikan'dan {len(jikan_episodes)} bölüm bilgisi alındı.")

        # 3. Eşleştirme, çakışma çözümleme ve birleştirme
        return self.build_episode_list(turkanime_episodes, jikan_episodes, anime_name)
