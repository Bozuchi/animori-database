"""
scraper.py — Türkanime Arşiv Kazıyıcı

Türkanime sitesindeki tüm anime arşivini sayfa sayfa tarayarak
her anime için temel verileri (slug, isim, puan, bölüm durumu, özet) çeker.

turkanime_api.bypass modülünü kullanarak Cloudflare korumasını aşar.
"""

import turkanime_api.bypass as bypass
from turkanime_api import Anime as TurkanimeAnime
import re
import time
import html
import logging

class TurkanimeScraper:
    """Türkanime arşivini tarayarak anime verilerini toplayan kazıyıcı sınıf."""

    def __init__(self, error_log_path: str = "errors.log"):
        self.animeler: dict[str, dict] = {}
        self._setup_error_logger(error_log_path)

    def _setup_error_logger(self, log_path: str):
        """Hata kayıtları için dosya logger'ı oluşturur."""
        self.error_logger = logging.getLogger("scraper_errors")
        self.error_logger.setLevel(logging.ERROR)

        if not self.error_logger.handlers:
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s — %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            self.error_logger.addHandler(handler)

    def _parse_blok(self, blok: str) -> tuple[str, dict] | None:
        """Tek bir anime panel bloğunu parse ederek (slug, veri) döndürür."""

        # 1. Başlık ve Slug Çekimi
        title_match = re.search(
            r'<div class="panel-title">\s*<a href="[^"]*/anime/([^"]+)"[^>]*title="([^"]+?)(?:\s+izle)?">', 
            blok
        )
        if not title_match:
            return None

        slug = title_match.group(1)
        isim = html.unescape(title_match.group(2).strip())

        # 2. Puan Çekimi
        score_match = re.search(r'<div class="rank-s">([\d.]+)</div>', blok)
        puan = score_match.group(1) if score_match else "0.00"

        # 3. Bölüm Durumu Çekimi
        ep_match = re.search(r'<span class="pull-right">([^<]+)</span>', blok)
        bolum_durumu = ep_match.group(1).strip() if ep_match else ""

        return slug, {
            "isim": isim,
            "puan": puan,
            "bolum_durumu": bolum_durumu,
            "ozet": None,  # None = henüz çekilmedi. Tam özet fetch_full_ozet() ile çekilecek
        }

    def scrape_all(self) -> dict[str, dict]:
        """
        Türkanime arşivinin tüm sayfalarını tarar.

        Returns:
            dict: slug -> {isim, puan, bolum_durumu, ozet} şeklinde sözlük.
        """
        print(f"[Scraper] Hedef Sunucu: {bypass.BASE_URL}")
        print("[Scraper] Türkanime arşivi sayfa sayfa taranıyor...")
        print("[Scraper] Lütfen bitene kadar kapatmayın...\n")

        sayfa_sayisi = 1
        toplam_sorgu = 0

        while True:
            url = f"/anime-izle?sayfa={sayfa_sayisi}"
            
            # Sonsuz döngüyü önlemek için max deneme sayısı
            max_retries = 3
            retry_count = 0
            page_html = None

            while retry_count < max_retries:
                try:
                    toplam_sorgu += 1
                    page_html = bypass.fetch(url)
                    break  # Başarılı olursa döngüden çık
                except Exception as e:
                    retry_count += 1
                    print(f"[Scraper] ⚠️  Bağlantı Hatası (Sayfa {sayfa_sayisi} - Deneme {retry_count}/{max_retries}): {e}")
                    time.sleep(2)
            
            if page_html is None:
                print(f"[Scraper] ❌ Sayfa {sayfa_sayisi} çok fazla hata verdi, atlanıyor...")
                break # Veya duruma göre 'sayfa_sayisi += 1; continue' yapılabilir. Döngüyü kırmak şimdilik daha güvenli.

            # Sayfayı her bir animeyi barındıran "panel" kutucuklarına böl
            bloklar = page_html.split('<div class="panel panel-visible"')[1:]

            if not bloklar:
                break

            yeni_eklenen = 0
            for blok in bloklar:
                sonuc = self._parse_blok(blok)
                if sonuc is None:
                    continue

                slug, veri = sonuc

                # Bu animeyi daha önce eklediysek atla
                if slug in self.animeler:
                    continue

                self.animeler[slug] = veri
                yeni_eklenen += 1

            print(
                f"[Scraper] Sayfa {sayfa_sayisi} tarandı. "
                f"+{yeni_eklenen} anime. (Toplam: {len(self.animeler)})"
            )

            if yeni_eklenen == 0:
                print("[Scraper] Yeni anime bulunamadı, sayfa sonuna ulaşıldı.")
                break

            sayfa_sayisi += 1
            time.sleep(0.2)

        print(
            f"\n[Scraper] ✅ Tarama tamamlandı! "
            f"Toplam {len(self.animeler)} anime bulundu. "
            f"({toplam_sorgu} sayfa sorgusu yapıldı)"
        )
        return self.animeler

    def create_anime_object(self, slug: str):
        """
        TurkanimeAnime objesi oluşturur.

        Aynı anime için birden fazla HTTP isteği yapmamak için bu obje
        fetch_full_ozet ve fetch_mal_id arasında paylaşılabilir.

        Args:
            slug: Anime slug'ı (örn: "naruto")

        Returns:
            TurkanimeAnime: Anime objesi veya None (hata durumunda).
        """
        try:
            return TurkanimeAnime(slug)
        except Exception as e:
            print(f"[Scraper] ⚠️  Anime objesi oluşturulamadı ({slug}): {e}")
            return None

    def fetch_full_ozet(self, slug: str, anime_obj=None) -> str:
        """
        Tek bir anime için tam özet bilgisini çeker.

        Arşiv sayfasındaki özet kesik olduğundan ("..." ile biter),
        anime sayfasına gidip tam özeti turkanime_api.Anime üzerinden alır.

        Args:
            slug: Anime slug'ı (örn: "naruto")
            anime_obj: Önceden oluşturulmuş TurkanimeAnime objesi (opsiyonel).

        Returns:
            str | None: Tam özet metni, boş string (özet yoksa), veya None (hata durumunda).
        """
        try:
            if anime_obj is None:
                anime_obj = TurkanimeAnime(slug)
            ozet = anime_obj.info.get("Özet", "")
            time.sleep(0.2)
            
            if ozet:
                # <br> veya <br /> etiketlerini yeni satıra (\n) çevir
                ozet = re.sub(r'<br\s*/?>', '\n', ozet, flags=re.IGNORECASE)
                # Kalan HTML entity'lerini temizle (&quot; vb.)
                ozet = html.unescape(ozet)
                
            return ozet.strip() if ozet else ""
        except Exception as e:
            error_msg = f"Tam özet alınamadı ({slug}): {e}"
            print(f"[Scraper] ⚠️  {error_msg}")
            self.error_logger.error(f"[Scraper] {error_msg}")
            return None

    def fetch_mal_id(self, slug: str, anime_obj=None) -> int | None:
        """
        Türkanime'nin "Dış Bağlantılar" sekmesinden MyAnimeList ID'sini çeker.

        Adımlar:
            1. TurkanimeAnime(slug) ile anime objesi oluştur → anime_id al
            2. /ajax/disbaglanti&animeId={anime_id} endpoint'ine GET isteği at
            3. Dönen HTML'den regex ile myanimelist.net/anime/{mal_id} linkini çıkar

        Args:
            slug: Anime slug'ı (örn: "naruto")
            anime_obj: Önceden oluşturulmuş TurkanimeAnime objesi (opsiyonel).

        Returns:
            int: MyAnimeList ID'si veya None (link yoksa veya hata durumunda).
        """
        try:
            if anime_obj is None:
                anime_obj = TurkanimeAnime(slug)
            anime_id = anime_obj.anime_id

            if not anime_id:
                print(f"[Scraper] ⚠️  anime_id alınamadı ({slug})")
                return None

            # Dış bağlantılar sekmesini çek
            dis_html = bypass.fetch(f"/ajax/disbaglanti&animeId={anime_id}")
            time.sleep(0.2)

            # MyAnimeList linkinden mal_id'yi ayıkla
            mal_match = re.search(r'myanimelist\.net/anime/(\d+)', dis_html)
            if mal_match:
                return int(mal_match.group(1))

            # MAL linki bulunamadı
            print(f"[Scraper] ⚠️  MAL linki bulunamadı ({slug})")
            return None

        except Exception as e:
            error_msg = f"MAL ID alınamadı ({slug}): {e}"
            print(f"[Scraper] ⚠️  {error_msg}")
            self.error_logger.error(f"[Scraper] {error_msg}")
            return None
