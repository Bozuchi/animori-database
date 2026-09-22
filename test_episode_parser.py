"""
test_episode_parser.py — episode_scraper.py'deki bölüm numarası
ayrıştırma mantığını interaktif olarak test etmek için script.

episode_scraper.py'ye bağımlılık yok (requests, turkanime_api, logger
gerektirmez) — sadece extract_episode_number() içindeki regex mantığı
buraya birebir kopyalandı.
"""

import re

# ─────────────────────────────────────────────
# episode_scraper.py'den birebir kopyalanan regex'ler
# ─────────────────────────────────────────────

# Geçersiz bölüm formatlarını yakalar (tire, virgüllü veya noktalı buçuklu sayılar)
# Örn: "12-13. Bölüm", "13,5. Bölüm", "5.5. Bölüm" → eşleştirme YAPILMAZ (None döner)
INVALID_EPISODE_PATTERN = re.compile(r'(\d+[-–]\d+|\d+[.,،]\d+)\.\s*[Bb]ölüm')

# Geçerli bölüm formatı — tam sayı + ". Bölüm"
# Örn: "Naruto 12. Bölüm" → episode_number = 12
VALID_EPISODE_PATTERN = re.compile(r'(\d+)\.\s*[Bb]ölüm')


def extract_episode_number(title: str) -> tuple[int | None, str]:
    """
    episode_scraper.py'deki extract_episode_number() ile birebir aynı mantık.
    Ek olarak, None dönme SEBEBİNİ de döndürür (test amaçlı).

    Returns:
        (episode_number, reason) — episode_number int veya None,
        reason ise None dönme nedenini açıklayan string.
    """
    if INVALID_EPISODE_PATTERN.search(title):
        return None, "GEÇERSİZ FORMAT: Tire ('12-13. Bölüm') ya da buçuklu/virgüllü sayı ('5.5. Bölüm', '13,5. Bölüm') tespit edildi."

    match = VALID_EPISODE_PATTERN.search(title)
    if match:
        return int(match.group(1)), ""

    return None, "BİLİNMEYEN FORMAT: Başlıkta '{sayı}. Bölüm' kalıbı bulunamadı."


def main():
    print("=" * 60)
    print("Bölüm Ayrıştırma Test Aracı")
    print("=" * 60)
    print("Bir bölüm başlığı gir, sonucu göreyim.")
    print("Çıkmak için: q / exit / quit\n")

    while True:
        try:
            title = input(">> Bölüm ismi: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nÇıkılıyor.")
            break

        if title.lower() in ("q", "quit", "exit"):
            print("Çıkılıyor.")
            break

        if not title:
            continue

        ep_number, reason = extract_episode_number(title)

        if ep_number is not None:
            print(f"   ✅ Sonuç: {ep_number}\n")
        else:
            print(f"   ❌ Sonuç: None")
            print(f"      Neden: {reason}\n")


if __name__ == "__main__":
    main()