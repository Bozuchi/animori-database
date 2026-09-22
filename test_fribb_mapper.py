"""
test_fribb_mapper.py — FribbMapper Birim Testleri
"""

import unittest
from fribb_mapper import FribbMapper


class TestFribbMapper(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mapper = FribbMapper()

    def test_tv_series_mapping(self):
        """Kimetsu no Yaiba sezonlarının MAL ve AniList eşleşmelerini test eder."""
        # Sezon 1 (Unwavering Resolve)
        self.assertEqual(self.mapper.get_mal_id(85937, 1), 38000)
        self.assertEqual(self.mapper.get_anilist_id(85937, 1), 101922)

        # Sezon 2 (Mugen Train TV)
        self.assertEqual(self.mapper.get_mal_id(85937, 2), 49926)

        # Sezon 3 (Entertainment District)
        self.assertEqual(self.mapper.get_mal_id(85937, 3), 47778)

        # Sezon 4 (Swordsmith Village)
        self.assertEqual(self.mapper.get_mal_id(85937, 4), 51019)

        # Sezon 5 (Hashira Training)
        self.assertEqual(self.mapper.get_mal_id(85937, 5), 55701)

    def test_jujutsu_kaisen_mapping(self):
        """Jujutsu Kaisen sezonlarının MAL eşleşmelerini test eder."""
        self.assertEqual(self.mapper.get_mal_id(95479, 1), 40748)
        self.assertEqual(self.mapper.get_mal_id(95479, 2), 51009)

    def test_movie_mapping(self):
        """Kimi no Na wa (Your Name) film eşleşmesini test eder."""
        # TMDB: 372058 -> MAL: 32281
        self.assertEqual(self.mapper.get_mal_id(372058, is_movie=True), 32281)
        self.assertEqual(self.mapper.get_anilist_id(372058, is_movie=True), 21519)

    def test_reverse_mal_to_tmdb(self):
        """MAL ID'den TMDB bilgilerinin ters çözümlemesini test eder."""
        kny = self.mapper.get_tmdb_info_by_mal(38000)
        self.assertIsNotNone(kny)
        self.assertEqual(kny["tmdb_id"], 85937)
        self.assertEqual(kny["type"], "tv")
        self.assertEqual(kny["season_number"], 1)

        jjk2 = self.mapper.get_tmdb_info_by_mal(51009)
        self.assertIsNotNone(jjk2)
        self.assertEqual(jjk2["tmdb_id"], 95479)
        self.assertEqual(jjk2["season_number"], 2)

        movie = self.mapper.get_tmdb_info_by_mal(32281)
        self.assertIsNotNone(movie)
        self.assertEqual(movie["tmdb_id"], 372058)
        self.assertEqual(movie["type"], "movie")


if __name__ == "__main__":
    unittest.main()
