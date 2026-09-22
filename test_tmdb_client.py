"""
test_tmdb_client.py — TMDBClient Birim Testleri
"""

import unittest
from tmdb_client import TMDBClient


class TestTMDBClient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TMDBClient()

    def test_tv_details(self):
        """TV dizisi detaylarını ve sezon özetlerini test eder (Demon Slayer - 85937)."""
        tv = self.client.get_details(85937, is_movie=False)
        self.assertIsNotNone(tv)
        self.assertEqual(tv["id"], 85937)
        self.assertEqual(tv["type"], "tv")
        self.assertTrue(bool(tv["name"]))
        self.assertTrue(bool(tv["poster_path"]))
        self.assertTrue(bool(tv["backdrop_path"]))
        self.assertGreaterEqual(len(tv.get("seasons_summary", [])), 4)

    def test_season_details(self):
        """Sezon bölümlerinin ve başlıklarının çekilmesini test eder."""
        s1 = self.client.get_season_details(85937, 1)
        self.assertIsNotNone(s1)
        self.assertEqual(s1["season_number"], 1)
        self.assertEqual(s1["episode_count"], 26)
        self.assertEqual(len(s1["episodes"]), 26)
        self.assertEqual(s1["episodes"][0]["episode_number"], 1)
        self.assertTrue(bool(s1["episodes"][0]["name"]))

    def test_movie_details(self):
        """Film detaylarını test eder (Your Name - 372058)."""
        movie = self.client.get_details(372058, is_movie=True)
        self.assertIsNotNone(movie)
        self.assertEqual(movie["id"], 372058)
        self.assertEqual(movie["type"], "movie")
        self.assertTrue(bool(movie["name"]))
        self.assertGreater(movie.get("runtime", 0), 90)

    def test_search(self):
        """Arama fonksiyonunu test eder."""
        results = self.client.search("Frieren", is_movie=False)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["type"], "tv")
        self.assertIn("frieren", results[0]["name"].lower() + " " + results[0]["original_name"].lower())


if __name__ == "__main__":
    unittest.main()
