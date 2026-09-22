"""
analyze_fribb_coverage.py — Fribb Mapping & Veritabanı Eşleşme Analiz Betiği

Mevcut api/anime/*.json dosyalarını tarayarak Fribb/anime-lists dataseti
üzerinden TMDB eşleşmelerini kontrol eder:
- Eşleşen animeleri ve hangi TMDB ID, Tip ve Sezon ile eşleştiğini belirler.
- Eşleşmeyen animeleri, bölüm ve video sayılarıyla raporlar.
- Birden fazla sezonun tek bir TMDB ID altında nasıl gruplandığını listeler.
- Null episode_number içeren bölümleri ve sayılarını tespit eder.
- Sonuçları data/fribb_coverage_report.json dosyasına kaydeder.
"""

import os
import json
import time
from concurrent.futures import ThreadPoolExecutor
from fribb_mapper import FribbMapper


def process_anime_file(filepath: str, mapper: FribbMapper) -> dict:
    filename = os.path.basename(filepath)
    name_no_ext = filename[:-5]
    
    with open(filepath, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception as e:
            return {
                "filename": filename,
                "error": f"JSON parse error: {e}",
                "matched": False
            }

    # MAL ID tespiti: Dosya adı sayı ise doğrudan o, değilse jikan verisinden
    mal_id = int(name_no_ext) if name_no_ext.isdigit() else None
    if not mal_id and data.get("jikan"):
        mal_id = data["jikan"].get("mal_id")

    turkanime_info = data.get("turkanime") or {}
    jikan_info = data.get("jikan") or {}
    anilist_info = data.get("anilist") or {}

    title = turkanime_info.get("isim") or jikan_info.get("title") or name_no_ext
    slug = turkanime_info.get("slug") or ""
    
    episodes = data.get("episodes") or []
    episode_count = len(episodes)
    video_count = sum(len(ep.get("videos") or []) for ep in episodes)

    # Null episode_number tespiti
    null_episodes = []
    for idx, ep in enumerate(episodes):
        if ep.get("episode_number") is None:
            null_episodes.append({
                "index": idx + 1,
                "title": ep.get("turkanime_title") or f"Episode {idx + 1}",
                "video_count": len(ep.get("videos") or [])
            })

    # Fribb eşleşme sorgusu
    tmdb_info = mapper.get_tmdb_info_by_mal(mal_id) if mal_id else None

    item_report = {
        "filename": filename,
        "mal_id": mal_id,
        "title": title,
        "slug": slug,
        "episode_count": episode_count,
        "video_count": video_count,
        "has_null_episodes": len(null_episodes) > 0,
        "null_episode_count": len(null_episodes),
        "null_episodes_sample": null_episodes[:3] if null_episodes else []
    }

    if tmdb_info:
        item_report["matched"] = True
        item_report["tmdb_id"] = tmdb_info["tmdb_id"]
        item_report["tmdb_type"] = tmdb_info["type"]
        item_report["season_number"] = tmdb_info["season_number"]
        item_report["entry_type"] = tmdb_info.get("entry_type", "TV")
    else:
        item_report["matched"] = False
        item_report["tmdb_id"] = None
        item_report["tmdb_type"] = None
        item_report["season_number"] = None
        item_report["anilist_id"] = anilist_info.get("anilist_id")

    return item_report


def run_analysis(
    anime_dir: str = "api/anime",
    output_report_path: str = "data/fribb_coverage_report.json"
):
    print("=" * 60)
    print("🚀 Fribb / TMDB Veritabanı Kapsama Analizi Başlatılıyor...")
    print("=" * 60)

    t0 = time.time()
    mapper = FribbMapper()

    files = [
        os.path.join(anime_dir, f)
        for f in os.listdir(anime_dir)
        if f.endswith(".json")
    ]
    total_files = len(files)
    print(f"📁 Taranacak toplam anime dosyası: {total_files}")

    print("⚡ Çok iş parçacıklı dosya okuma ve eşleştirme çalışıyor...")
    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(lambda p: process_anime_file(p, mapper), files))

    matched_list = [r for r in results if r.get("matched")]
    unmatched_list = [r for r in results if not r.get("matched")]

    total_episodes = sum(r.get("episode_count", 0) for r in results)
    total_videos = sum(r.get("video_count", 0) for r in results)

    matched_episodes = sum(r.get("episode_count", 0) for r in matched_list)
    matched_videos = sum(r.get("video_count", 0) for r in matched_list)

    unmatched_episodes = sum(r.get("episode_count", 0) for r in unmatched_list)
    unmatched_videos = sum(r.get("video_count", 0) for r in unmatched_list)

    # Null episode_number istatistikleri
    files_with_null = [r for r in results if r.get("has_null_episodes")]
    total_null_eps = sum(r.get("null_episode_count", 0) for r in results)

    # TMDB ID grupları (birden fazla sezon içeren seriler)
    tmdb_groups: dict[int, dict] = {}
    for r in matched_list:
        tid = r["tmdb_id"]
        if tid not in tmdb_groups:
            tmdb_groups[tid] = {
                "tmdb_id": tid,
                "tmdb_type": r["tmdb_type"],
                "seasons": {}
            }
        s_num = r["season_number"]
        tmdb_groups[tid]["seasons"][s_num] = {
            "mal_id": r["mal_id"],
            "title": r["title"],
            "filename": r["filename"],
            "episode_count": r["episode_count"],
            "video_count": r["video_count"]
        }

    # Rapor nesnesi
    report = {
        "summary": {
            "total_anime_files": total_files,
            "matched_anime_count": len(matched_list),
            "matched_percentage": round(len(matched_list) / total_files * 100, 2) if total_files else 0,
            "unmatched_anime_count": len(unmatched_list),
            "unmatched_percentage": round(len(unmatched_list) / total_files * 100, 2) if total_files else 0,
            "unique_tmdb_series_count": len(tmdb_groups),
            "multi_season_tmdb_series_count": sum(1 for g in tmdb_groups.values() if len(g["seasons"]) > 1),
            "total_episodes": total_episodes,
            "matched_episodes": matched_episodes,
            "unmatched_episodes": unmatched_episodes,
            "total_videos": total_videos,
            "matched_videos": matched_videos,
            "unmatched_videos": unmatched_videos,
            "files_with_null_episodes": len(files_with_null),
            "total_null_episodes": total_null_eps,
            "elapsed_seconds": round(time.time() - t0, 2)
        },
        "tmdb_merged_groups_sample": [
            g for g in tmdb_groups.values() if len(g["seasons"]) > 1
        ][:15],
        "unmatched_animes": unmatched_list,
        "matched_animes_sample": matched_list[:30]
    }

    os.makedirs(os.path.dirname(output_report_path) or ".", exist_ok=True)
    with open(output_report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print("\n" + "=" * 60)
    print("📊 ANALİZ TAMAMLANDI - ÖZET RAPOR")
    print("=" * 60)
    print(f"Toplam Anime Dosyası       : {total_files}")
    print(f"✅ TMDB ile Eşleşen        : {len(matched_list)} (%{report['summary']['matched_percentage']})")
    print(f"❌ Eşleşmeyen              : {len(unmatched_list)} (%{report['summary']['unmatched_percentage']})")
    print(f"🎯 Tekil TMDB Serisi       : {len(tmdb_groups)} adet")
    print(f"🔗 Çok Sezonlu Birleşen    : {report['summary']['multi_season_tmdb_series_count']} seri")
    print("-" * 60)
    print(f"Toplam Video Linki         : {total_videos:,}")
    print(f"✅ Korunan Video Linki     : {matched_videos:,} (%{round(matched_videos/total_videos*100, 2) if total_videos else 0})")
    print(f"⚠️ Eşleşmeyen Video Linki   : {unmatched_videos:,} (%{round(unmatched_videos/total_videos*100, 2) if total_videos else 0})")
    print("-" * 60)
    print(f"Null Episode İçeren Dosya  : {len(files_with_null)}")
    print(f"Toplam Null Episode Sayısı : {total_null_eps}")
    print(f"⏱️ Geçen Süre              : {elapsed:.2f} saniye")
    print(f"💾 Detaylı Rapor Kaydedildi: {output_report_path}")
    print("=" * 60)

    if unmatched_list:
        print("\n🔍 Eşleşmeyen İlk 10 Anime Örneği:")
        for u in unmatched_list[:10]:
            print(f"  - [{u['filename']}] {u['title']} ({u['episode_count']} Bölüm, {u['video_count']} Video)")

    return report


if __name__ == "__main__":
    run_analysis()
