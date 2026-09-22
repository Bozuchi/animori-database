import json
from episode_scraper import EpisodeScraper
from jikan_api import JikanEnricher

def main():
    print("=" * 50)
    print(" 🎬 Episode Scraper & Çakışma Çözücü Test Aracı")
    print("=" * 50)

    # Kullanıcıdan gerekli test verilerini al
    slug = input("\n🔗 Anime Slug (örn: shingeki-no-kyojin): ").strip()
    if not slug:
        print("❌ Slug boş bırakılamaz. Çıkılıyor...")
        return

    anime_name = input("📝 Anime Ana İsmi (örn: Shingeki no Kyojin): ").strip()
    mal_id_input = input("🆔 Jikan MAL ID (Test etmek istemiyorsanız boş bırakın): ").strip()
    mal_id = int(mal_id_input) if mal_id_input.isdigit() else None

    print("\n⏳ Modüller yükleniyor...")
    ep_scraper = EpisodeScraper()
    jikan = JikanEnricher()

    print(f"\n🔍 '{anime_name}' ({slug}) için bölümler taranıyor...")
    print("-" * 50)

    # Scrape işlemini başlat (Sıfırdan çekiyormuşuz gibi existing_episodes boş veriliyor)
    episodes = ep_scraper.scrape_episodes(
        slug=slug,
        mal_id=mal_id,
        jikan=jikan,
        existing_episodes=[],
        anime_name=anime_name
    )

    print("-" * 50)
    print("\n✅ İşlem Tamamlandı! Örnek Çıktı:\n")
    
    # Tüm JSON'ı basmak terminali çok doldurabilir, sadece sonuçları güzelce yazdıralım
    print(json.dumps(episodes, indent=2, ensure_ascii=False))

    # Kısa bir özet rapor
    toplam_bolum = len(episodes)
    eslesen_bolum = sum(1 for ep in episodes if ep.get("jikan_mal_id") is not None)
    eslesmeyen_bolum = toplam_bolum - eslesen_bolum
    
    print("\n" + "=" * 50)
    print(" 📊 TEST ÖZETİ")
    print("=" * 50)
    print(f"Toplam Çekilen Bölüm : {toplam_bolum}")
    print(f"Jikan İle Eşleşen    : {eslesen_bolum} (Asıl bölümler)")
    print(f"Eşleşmeyen / Atlanan : {eslesmeyen_bolum} (OVA, Movie veya çakışmada elenenler)")
    print("=" * 50)

if __name__ == "__main__":
    main()