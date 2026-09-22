"""
test_anizm.py — Anizm Kaynak ve Şema Bütünlük Test Paketi
"""

import os
import sys

# Proje ana dizinini import yoluna ekle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import unittest

from anizm_provider import AnizmProvider


class TestAnizmProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = AnizmProvider()

    def test_01_normalize_player(self):
        self.assertEqual(self.provider.normalize_player("Aincrad (Reklamsız)"), "AINCRAD")
        self.assertEqual(self.provider.normalize_player("Sistenn2"), "SISTENN")
        self.assertEqual(self.provider.normalize_player("Odnoklassniki"), "ODNO")
        self.assertEqual(self.provider.normalize_player("Sibnet"), "SIBNET")
        self.assertEqual(self.provider.normalize_player("Vidmoly"), "VIDMOLY")
        self.assertEqual(self.provider.normalize_player("Tau Video"), "TAU")
        self.assertEqual(self.provider.normalize_player("Google Drive"), "GDRIVE")

    def test_02_fetch_catalog(self):
        catalog = self.provider.fetch_catalog()
        self.assertIsInstance(catalog, list)
        self.assertGreater(len(catalog), 4000)
        sample = catalog[0]
        self.assertIn("slug", sample)
        self.assertIn("name", sample)
        self.assertIn("url", sample)

    def test_03_fetch_latest_episodes(self):
        latest = self.provider.fetch_latest_episodes(page=1)
        self.assertIsInstance(latest, list)
        self.assertGreater(len(latest), 0)
        sample = latest[0]
        self.assertIn("_id", sample)
        self.assertIn("episode_slug", sample)
        self.assertIn("anime_slug", sample)
        self.assertIn("url", sample)

    def test_04_fetch_anime_detail(self):
        detail = self.provider.fetch_anime_detail("another")
        self.assertIsNotNone(detail)
        self.assertEqual(detail.get("slug"), "another")
        self.assertEqual(detail.get("name"), "Another")
        self.assertIsInstance(detail.get("episodes"), list)
        self.assertEqual(len(detail["episodes"]), 12)

    def test_05_fetch_episode_videos(self):
        videos = self.provider.fetch_episode_videos("another-1-bolum-izle")
        self.assertIsInstance(videos, list)
        self.assertGreater(len(videos), 0)
        sample = videos[0]
        self.assertIn("player", sample)
        self.assertIn("url", sample)
        self.assertIn("fansub", sample)


class TestAnizmFiles(unittest.TestCase):
    BASE_DIR = "api/sources/anizm"

    def test_06_files_exist_and_valid(self):
        animes_path = os.path.join(self.BASE_DIR, "animes.json")
        metadata_path = os.path.join(self.BASE_DIR, "metadata.json")
        latest_path = os.path.join(self.BASE_DIR, "latest_episodes.json")
        version_path = os.path.join(self.BASE_DIR, "version.json")
        anime_dir = os.path.join(self.BASE_DIR, "anime")

        for fp in [animes_path, metadata_path, latest_path, version_path]:
            if os.path.exists(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.assertIsNotNone(data)

        if os.path.exists(animes_path):
            with open(animes_path, "r", encoding="utf-8") as f:
                animes_data = json.load(f)
                if animes_data:
                    self.assertTrue(all(isinstance(g, int) for g in animes_data[0].get("genres", [])))

        if os.path.exists(anime_dir):
            files = [f for f in os.listdir(anime_dir) if f.endswith(".json")]
            for fn in files:
                with open(os.path.join(anime_dir, fn), "r", encoding="utf-8") as f:
                    anime_data = json.load(f)
                    self.assertIn("slug", anime_data)
                    self.assertIn("name", anime_data)
                    self.assertIn("episodes", anime_data)
                    self.assertTrue(all(isinstance(g, int) for g in anime_data.get("genres", [])))
                    self.assertTrue(all(isinstance(t, int) for t in anime_data.get("themes", [])))


if __name__ == "__main__":
    unittest.main()
