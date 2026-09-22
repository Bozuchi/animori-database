import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_animecix_structure():
    base_dir = "api/sources/animecix"
    assert os.path.exists(base_dir), "api/sources/animecix must exist"

    # Check animes.json
    animes_path = os.path.join(base_dir, "animes.json")
    assert os.path.exists(animes_path), "animes.json must exist"
    with open(animes_path, "r", encoding="utf-8") as f:
        animes = json.load(f)
    assert isinstance(animes, list), "animes.json must be a list"
    assert len(animes) > 0, "animes.json must not be empty"
    assert all(isinstance(g, int) for g in animes[0].get("genres", [])), "animes.json genres must be integer IDs"
    print(f"animes.json verified: {len(animes)} items (genres as IDs verified)")

    # Check metadata.json
    metadata_path = os.path.join(base_dir, "metadata.json")
    assert os.path.exists(metadata_path), "metadata.json must exist"
    with open(metadata_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    assert "fansubs" in meta, "metadata.json must contain fansubs"
    assert "genres" in meta, "metadata.json must contain genres"
    print(f"metadata.json verified: {len(meta['fansubs'])} fansubs, {len(meta['genres'])} genres")

    # Check anime directory
    anime_dir = os.path.join(base_dir, "anime")
    assert os.path.exists(anime_dir), "anime directory must exist"
    files = [f for f in os.listdir(anime_dir) if f.endswith(".json")]
    assert len(files) > 0, "anime directory must have json files"
    print(f"anime directory verified: {len(files)} anime detail files")

    # Verify a sample detail file
    sample_file = os.path.join(anime_dir, files[0])
    with open(sample_file, "r", encoding="utf-8") as f:
        sample_data = json.load(f)
    
    assert "id" in sample_data
    assert "name" in sample_data
    assert "type" in sample_data
    assert all(isinstance(g, int) for g in sample_data.get("genres", [])), "detail anime genres must be integer IDs"
    if sample_data["type"] == "series":
        assert "seasons" in sample_data, "series must have seasons"
        assert isinstance(sample_data["seasons"], list)
    else:
        assert "videos" in sample_data, "movie must have videos"
        assert isinstance(sample_data["videos"], list)
    print(f"Sample anime verified: {sample_data.get('name')} (Type: {sample_data.get('type')}, Genres: {sample_data.get('genres')})")

    # Check latest_episodes.json
    latest_path = os.path.join(base_dir, "latest_episodes.json")
    assert os.path.exists(latest_path), "latest_episodes.json must exist"
    with open(latest_path, "r", encoding="utf-8") as f:
        latest = json.load(f)
    assert isinstance(latest, list)
    print(f"latest_episodes.json verified: {len(latest)} records")

    # Check calendar.json
    calendar_path = os.path.join(base_dir, "calendar.json")
    assert os.path.exists(calendar_path), "calendar.json must exist"
    with open(calendar_path, "r", encoding="utf-8") as f:
        calendar = json.load(f)
    assert isinstance(calendar, list)
    print(f"calendar.json verified: {len(calendar)} days")

    # Check version.json
    version_path = os.path.join(base_dir, "version.json")
    assert os.path.exists(version_path), "version.json must exist"
    with open(version_path, "r", encoding="utf-8") as f:
        version = json.load(f)
    assert "animes" in version
    print(f"version.json verified: {list(version.keys())}")

    print("\nALL ANIMECIX STRUCTURE & SCHEMA TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_animecix_structure()
