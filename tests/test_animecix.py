import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from animecix_provider import AnimecixProvider


sys.stdout.reconfigure(encoding="utf-8")

def main():
    p = AnimecixProvider()
    print("Testing AnimecixProvider...")

    # 1. Last episodes
    last_eps = p.fetch_last_episodes(page=1)
    print(f"1. fetch_last_episodes: {len(last_eps)} items")
    assert len(last_eps) > 0, "last_episodes should return items"
    sample_ep = last_eps[0]
    title_name = sample_ep.get("title_name")
    s_num = sample_ep.get("season_number")
    e_num = sample_ep.get("episode_number")
    print(f"   Sample: {title_name} S{s_num}E{e_num}")

    # 2. Calendar
    cal = p.fetch_calendar()
    print(f"2. fetch_calendar: {len(cal)} days")
    assert len(cal) > 0, "calendar should return days"

    # 3. Homepage lists
    lists = p.fetch_homepage_lists()
    print(f"3. fetch_homepage_lists: {len(lists)} lists")
    assert len(lists) > 0, "homepage lists should return data"

    # 4. Title detail (using title_id from last_episodes)
    sample_title_id = sample_ep.get("title_id")
    detail = p.fetch_title_detail(sample_title_id)
    assert detail is not None, "fetch_title_detail should return data"
    print(f"4. fetch_title_detail({sample_title_id}): {detail.get('name')}")
    print(f"   TMDB ID: {detail.get('tmdb_id')}, MAL ID: {detail.get('mal_id')}")

    # 5. Episode videos
    videos = p.fetch_episode_videos(sample_title_id, s_num or 1, e_num or 1)
    print(f"5. fetch_episode_videos: {len(videos)} videos")

    # 6. Search
    results = p.search("Naruto", limit=2)
    print(f"6. search('Naruto'): {len(results)} results -> {[r.get('name') for r in results]}")

    # 7. Normalization tests
    assert p.normalize_player("Sibnet", "https://video.sibnet.ru/shell.php?videoid=123") == "SIBNET"
    assert p.normalize_player("Tau Video", "https://tau-video.xyz/embed/abc") == "TAU"
    assert p.normalize_player("VipPlayer", "https://vidmoly.me/embed-123.html") == "VIDMOLY"
    print("7. normalize_player tests passed")

    # 8. Fansub extraction tests
    assert p.extract_fansub_name("Çevirmen: Napryzon Encoder: Hakuryuu") == "Napryzon"
    assert p.extract_fansub_name("Ecthelion / https://www.planetdp.org/...") == "Ecthelion"
    assert p.extract_fansub_name("1") is None
    print("8. extract_fansub_name tests passed")

    print("\nALL ANIMEICX PROVIDER TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
