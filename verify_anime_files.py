"""
verify_anime_files.py — TMDB Anime Dosyaları Bütünlük ve Şema Doğrulayıcısı

Tüm api/anime/*.json dosyalarını ve api/animes.json vitrin indeksini tarar:
1. Dosya bozukluğu / 0 byte / geçersiz JSON kontrolü
2. TMDB Birleşik Şema kontrolü (id, name, type, seasons / videos)
3. Oynatıcı dağılımı ve istatistik raporu
4. animes.json indeksiyle dosya sayısı senkronizasyonu
"""

import os
import sys
import json
import glob
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")


def main():
    anime_dir = os.path.join("api", "anime")
    index_path = os.path.join("api", "animes.json")

    if not os.path.exists(anime_dir):
        print(f"HATA: {anime_dir} klasörü bulunamadı!")
        sys.exit(1)

    files = sorted([f for f in os.listdir(anime_dir) if f.endswith(".json")])
    total_files = len(files)
    print(f"🔍 Toplam {total_files:,} anime dosyası taranıyor...")
    print("=" * 70)

    # Hata havuzları
    corrupt_files = []
    zero_byte_files = []
    schema_errors = []

    # İstatistik sayaçları
    movie_count = 0
    tv_count = 0
    total_seasons = 0
    total_episodes = 0
    total_videos = 0
    unassigned_count = 0

    player_counter = Counter()

    for fname in files:
        fpath = os.path.join(anime_dir, fname)
        size = os.path.getsize(fpath)

        if size == 0:
            zero_byte_files.append(fname)
            continue

        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            corrupt_files.append((fname, str(e)))
            continue

        if not isinstance(data, dict):
            schema_errors.append((fname, "Kök eleman JSON dict değil"))
            continue

        # Temel alanlar
        tid = data.get("id")
        name = data.get("name")
        mtype = data.get("type")

        if tid is None or not name:
            schema_errors.append((fname, "id veya name eksik"))
            continue

        # unassigned_videos kontrolü
        u_vids = data.get("unassigned_videos") or []
        unassigned_count += len(u_vids)
        for u in u_vids:
            p = u.get("player", "UNKNOWN")
            player_counter[p] += 1

        if mtype == "movie":
            movie_count += 1
            vids = data.get("videos") or []
            total_videos += len(vids)
            for v in vids:
                p = v.get("player", "UNKNOWN")
                player_counter[p] += 1

        elif mtype == "tv":
            tv_count += 1
            seasons = data.get("seasons") or []
            total_seasons += len(seasons)
            for s in seasons:
                eps = s.get("episodes") or []
                total_episodes += len(eps)
                for ep in eps:
                    vids = ep.get("videos") or []
                    total_videos += len(vids)
                    for v in vids:
                        p = v.get("player", "UNKNOWN")
                        player_counter[p] += 1
        else:
            schema_errors.append((fname, f"Bilinmeyen tip: {mtype}"))

    # İndeks senkronizasyon kontrolü
    index_count = None
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
                index_count = len(index_data)
        except Exception as e:
            print(f"UYARI: animes.json okunamadı: {e}")

    # Rapor
    print("\n" + "=" * 70)
    print("📊 VERİTABANI DOĞRULAMA VE İSTATİSTİK RAPORU")
    print("=" * 70)
    print(f"Toplam Canlı Dosya           : {total_files:,}")
    if index_count is not None:
        synced = "✅ SENKRONİZE" if index_count == total_files else "⚠️ EŞİTSİZLİK!"
        print(f"animes.json Vitrin İndeksi    : {index_count:,} ({synced})")
    print(f"Dizi (TV) Sayısı             : {tv_count:,}")
    print(f"Film (Movie) Sayısı          : {movie_count:,}")
    print(f"Toplam Sezon Sayısı          : {total_seasons:,}")
    print(f"Toplam Bölüm Sayısı          : {total_episodes:,}")
    print(f"Toplam Aktif Video Sayısı    : {total_videos:,}")
    print(f"Emniyet Havuzu (Unassigned)  : {unassigned_count:,} video")
    print("-" * 70)
    print("🎬 En Çok Kullanılan Oynatıcılar:")
    for p, c in player_counter.most_common(12):
        print(f"   {p:<15}: {c:,} video")
    print("-" * 70)

    has_error = False
    if zero_byte_files:
        print(f"❌ 0-Byte Boş Dosyalar ({len(zero_byte_files)}): {zero_byte_files[:5]}")
        has_error = True
    if corrupt_files:
        print(f"❌ Bozuk JSON Dosyaları ({len(corrupt_files)}): {corrupt_files[:5]}")
        has_error = True
    if schema_errors:
        print(f"❌ Şema Hataları ({len(schema_errors)}): {schema_errors[:5]}")
        has_error = True

    if not has_error:
        print("🎉 TEBRİKLER: Tüm anime dosyaları hatasız, geçerli ve tutarlı!")
        print("=" * 70)
        sys.exit(0)
    else:
        print("⚠️ Bütünlük kontrolü başarısız oldu!")
        print("=" * 70)
        sys.exit(1)


if __name__ == "__main__":
    main()
