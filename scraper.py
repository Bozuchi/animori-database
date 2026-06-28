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


class TurkanimeScraper:
    """Türkanime arşivini tarayarak anime verilerini toplayan kazıyıcı sınıf."""

    def __init__(self):
        self.animeler: dict[str, dict] = {}

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

        toplam_sorgu = 0
        sayfa_sayisi = 1

        while True:
            url = f"/anime-izle?sayfa={sayfa_sayisi}"

            try:
                toplam_sorgu += 1
                html = bypass.fetch(url)
            except Exception as e:
                print(f"[Scraper] ⚠️  Bağlantı Hatası (Sayfa {sayfa_sayisi}): {e}")
                time.sleep(2)
                continue

            # Sayfayı her bir animeyi barındıran "panel" kutucuklarına böl
            bloklar = html.split('<div class="panel panel-visible"')[1:]

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

    def fetch_full_ozet(self, slug: str) -> str:
        """
        Tek bir anime için tam özet bilgisini çeker.

        Arşiv sayfasındaki özet kesik olduğundan ("..." ile biter),
        anime sayfasına gidip tam özeti turkanime_api.Anime üzerinden alır.

        Args:
            slug: Anime slug'ı (örn: "naruto")

        Returns:
            str: Tam özet metni veya boş string (hata durumunda).
        """
        try:
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
            print(f"[Scraper] ⚠️  Tam özet alınamadı ({slug}): {e}")
            return ""
