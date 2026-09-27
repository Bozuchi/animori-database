"""
fetch_anilist_posters.py — AniList Poster URL Güncelleyici

api/sources/turkanime altındaki animeler için AniList GraphQL API üzerinden
yüksek çözünürlüklü poster URL'lerini (coverImage.extraLarge / large) çeker:
  1. anime/{id}.json dosyalarında 'anilist' nesnesi altına 'poster_url' ekler.
  2. animes.json dosyasında 'image_url' alanını bu AniList poster URL'i ile günceller.
  3. anilist_id'si bulunmayan animelerin mevcut image_url değerine dokunmaz.

Kullanım:
    python fetch_anilist_posters.py --test            # 3 örnek anime üzerinde simülasyon/test
    python fetch_anilist_posters.py --dry-run         # Gerçek dosya yazımı yapmadan çalıştırır
    python fetch_anilist_posters.py --limit 10        # İlk 10 animeyi işler
    python fetch_anilist_posters.py                   # Tüm kataloğu işler (eksikleri günceller)
    python fetch_anilist_posters.py --force           # Zaten poster_url olanları da yeniden çeker
"""

import os
import sys
import json
import time
import signal
import argparse
from datetime import datetime
import requests

from logger import setup_logger

logger = setup_logger("PosterSync")

# Sabitler
API_URL = "https://graphql.anilist.co"
BATCH_SIZE = 50
REQUEST_TIMEOUT = 15
RATE_LIMIT_DELAY = 0.8
RETRY_MAX = 3
RETRY_BACKOFF = 10

BASE_DIR = os.path.join("api", "sources", "turkanime")
ANIMES_JSON_PATH = os.path.join(BASE_DIR, "animes.json")
ANIME_DIR = os.path.join(BASE_DIR, "anime")

GRAPHQL_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      coverImage {
        extraLarge
        large
        medium
      }
    }
  }
}
"""

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    """Ctrl+C yakalandığında mevcut batch'ten sonra güvenli durdurmayı sağlar."""
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("İkinci Ctrl+C algılandı, zorla sonlandırılıyor...")
        os._exit(1)
    _shutdown_requested = True
    logger.warning("⏹️  Durdurma isteği alındı! Mevcut batch tamamlandıktan sonra animes.json kaydedilip çıkılacak...")


class AnilistPosterFetcher:
    def __init__(self, dry_run: bool = False, force: bool = False, batch_size: int = BATCH_SIZE):
        self.dry_run = dry_run
        self.force = force
        self.batch_size = batch_size
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._last_request_time = 0

    def _wait_rate_limit(self):
        """İstekler arası minimum bekleme süresini uygular."""
        elapsed = time.time() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)

    def fetch_batch_posters(self, anilist_ids: list[int]) -> dict[int, str]:
        """
        Verilen AniList ID'leri için GraphQL Page sorgusu gönderir ve
        {anilist_id: poster_url} haritası döndürür.
        """
        payload = {
            "query": GRAPHQL_QUERY,
            "variables": {"ids": anilist_ids}
        }

        for attempt in range(RETRY_MAX):
            self._wait_rate_limit()

            try:
                self._last_request_time = time.time()
                response = self.session.post(
                    API_URL,
                    json=payload,
                    timeout=REQUEST_TIMEOUT
                )

                # Rate-limit kontrolü (X-RateLimit-Remaining)
                remaining = response.headers.get("X-RateLimit-Remaining")
                if remaining is not None and remaining.isdigit():
                    rem_int = int(remaining)
                    if rem_int < 10:
                        logger.warning(f"⚠️ Rate limit uyarısı! Kalan istek hakkı: {rem_int}. 5 saniye bekleniyor...")
                        time.sleep(5)

                if response.status_code == 200:
                    data = response.json()
                    media_list = data.get("data", {}).get("Page", {}).get("media", [])
                    result = {}
                    for item in media_list:
                        m_id = item.get("id")
                        cov = item.get("coverImage") or {}
                        # extraLarge en yüksek çözünürlüktür, yoksa large veya medium
                        poster = cov.get("extraLarge") or cov.get("large") or cov.get("medium")
                        if m_id and poster:
                            result[m_id] = poster
                    return result

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait_time = int(retry_after) + 1 if retry_after and retry_after.isdigit() else RETRY_BACKOFF * (attempt + 1)
                    logger.warning(f"⏳ Rate limit aşıldı (HTTP 429). {wait_time} saniye bekleniyor... (Deneme {attempt + 1}/{RETRY_MAX})")
                    time.sleep(wait_time)
                    continue

                if response.status_code in (500, 502, 503, 504):
                    wait_time = RETRY_BACKOFF * (attempt + 1)
                    logger.warning(f"⚠️ Sunucu hatası ({response.status_code}). {wait_time} saniye bekleniyor...")
                    time.sleep(wait_time)
                    continue

                logger.error(f"❌ AniList API hatası: HTTP {response.status_code} — {response.text[:200]}")
                return {}

            except requests.exceptions.Timeout:
                wait_time = RETRY_BACKOFF * (attempt + 1)
                logger.warning(f"⏳ İstek zaman aşımı. {wait_time} saniye bekleniyor...")
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ Bağlantı hatası: {e}")
                return {}

        logger.error(f"❌ {len(anilist_ids)} animelik batch {RETRY_MAX} denemede de yanıt vermedi.")
        return {}

    def get_anime_filepath(self, anime_entry: dict) -> str | None:
        """Anime için dosya yolunu döner (mal_id.json veya slug.json)."""
        mal_id = anime_entry.get("mal_id")
        slug = anime_entry.get("slug")

        if mal_id is not None:
            p = os.path.join(ANIME_DIR, f"{mal_id}.json")
            if os.path.exists(p):
                return p

        if slug:
            p = os.path.join(ANIME_DIR, f"{slug}.json")
            if os.path.exists(p):
                return p

        return None


def run_test():
    """İlk 3 anime üzerinde test yapar ve dosyalara dokunmadan önce çıktıyı doğrular."""
    logger.info("=" * 65)
    logger.info("🧪 AniList Poster Güncelleyici — TEST MODU")
    logger.info("=" * 65)

    if not os.path.exists(ANIMES_JSON_PATH):
        logger.error(f"{ANIMES_JSON_PATH} bulunamadı!")
        return False

    with open(ANIMES_JSON_PATH, "r", encoding="utf-8") as f:
        animes = json.load(f)

    # Test için anilist_id'si olan ilk 3 animeyi ve 1 adet anilist_id'si olmayan animeyi seç
    test_with_al = [a for a in animes if a.get("anilist_id") is not None][:3]
    test_without_al = [a for a in animes if a.get("anilist_id") is None][:1]

    fetcher = AnilistPosterFetcher(dry_run=True)
    anilist_ids = [a["anilist_id"] for a in test_with_al]

    logger.info(f"Test için sorgulanacak AniList ID'leri: {anilist_ids}")
    posters = fetcher.fetch_batch_posters(anilist_ids)
    logger.info(f"Gelen poster yanıt sayısı: {len(posters)}/{len(anilist_ids)}")

    for a in test_with_al:
        al_id = a.get("anilist_id")
        poster = posters.get(al_id)
        filepath = fetcher.get_anime_filepath(a)
        
        logger.info("-" * 50)
        logger.info(f"Anime: {a.get('title')} (MAL: {a.get('mal_id')}, AniList: {al_id})")
        logger.info(f"  Mevcut MAL image_url  : {a.get('image_url')}")
        logger.info(f"  Yeni AniList poster_url: {poster}")
        logger.info(f"  Detay Dosyası          : {filepath}")

        if not poster:
            logger.error("❌ Poster URL alınamadı!")
            return False

        if not filepath or not os.path.exists(filepath):
            logger.error(f"❌ Detay dosyası bulunamadı: {filepath}")
            return False

        with open(filepath, "r", encoding="utf-8") as f:
            detail = json.load(f)

        existing_anilist = detail.get("anilist") or {}
        logger.info(f"  Detaydaki anilist nesnesi: {existing_anilist}")

    if test_without_al:
        no_al = test_without_al[0]
        logger.info("-" * 50)
        logger.info(f"AniList ID'si olmayan örnek anime: {no_al.get('title')}")
        logger.info(f"  anilist_id: {no_al.get('anilist_id')}")
        logger.info(f"  image_url : {no_al.get('image_url')} (Dokunulmayacak - korundu)")

    logger.info("=" * 65)
    logger.info("✅ Test başarıyla tamamlandı. API ve dosya eşleşmeleri doğrulanmıştır.")
    logger.info("=" * 65)
    return True


def main():
    global _shutdown_requested
    signal.signal(signal.SIGINT, _handle_shutdown)

    parser = argparse.ArgumentParser(description="AniList üzerinden anime poster URL'lerini çeker ve günceller.")
    parser.add_argument("--test", action="store_true", help="İlk 3 anime üzerinde test yapar ve simüle eder.")
    parser.add_argument("--dry-run", action="store_true", help="Dosyalara yazmadan simüle eder.")
    parser.add_argument("--limit", type=int, default=None, help="İşlenecek maksimum anime sayısı.")
    parser.add_argument("--force", action="store_true", help="Zaten poster_url'i olan animeleri de yeniden çeker.")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Batch boyutu (varsayılan: 50).")
    args = parser.parse_args()

    if args.test:
        success = run_test()
        sys.exit(0 if success else 1)

    start_time = datetime.now()

    logger.info("=" * 65)
    logger.info("  🎨 AniList Poster URL Senkronizasyonu")
    logger.info("=" * 65)
    if args.dry_run:
        logger.info("⚠️  DRY-RUN MODU: Dosyalara yazılmayacak.")
    if args.force:
        logger.info("⚠️  FORCE MODU: Mevcut poster_url'ler de yeniden sorgulanacak.")
    if args.limit:
        logger.info(f"⚠️  LIMIT: Yalnızca {args.limit} anime işlenecek.")

    # 1. animes.json'ı oku
    if not os.path.exists(ANIMES_JSON_PATH):
        logger.error(f"❌ {ANIMES_JSON_PATH} bulunamadı!")
        sys.exit(1)

    with open(ANIMES_JSON_PATH, "r", encoding="utf-8") as f:
        animes = json.load(f)

    total_all = len(animes)
    fetcher = AnilistPosterFetcher(dry_run=args.dry_run, force=args.force, batch_size=args.batch_size)

    # 2. İşlenecek animeleri belirle
    # anilist_id'si olanlar arasından seçim yap
    targets = []
    skipped_no_id = 0
    skipped_already_has = 0

    for a in animes:
        al_id = a.get("anilist_id")
        if al_id is None:
            skipped_no_id += 1
            continue

        filepath = fetcher.get_anime_filepath(a)
        if not filepath:
            logger.warning(f"Detay dosyası bulunamadı: MAL={a.get('mal_id')}, Slug={a.get('slug')}")
            continue

        # Force değilse, dosyanın içine bakıp poster_url var mı kontrol edebiliriz
        if not args.force:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    detail = json.load(f)
                    if detail.get("anilist", {}).get("poster_url"):
                        skipped_already_has += 1
                        continue
            except Exception:
                pass

        targets.append({
            "entry": a,
            "anilist_id": al_id,
            "filepath": filepath,
        })

    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    total_targets = len(targets)
    logger.info(
        f"📊 Toplam: {total_all} | "
        f"İşlenecek: {total_targets} | "
        f"AniList ID Yok (atlandı): {skipped_no_id} | "
        f"Zaten Poster Var: {skipped_already_has}"
    )

    if total_targets == 0:
        logger.info("✅ Güncellenecek anime bulunamadı. Tüm posterler güncel!")
        sys.exit(0)

    # animes.json içindeki elemanları anilist_id üzerinden hızlı güncellemek için dict haritası
    animes_map = {a.get("anilist_id"): a for a in animes if a.get("anilist_id") is not None}

    updated_detail_count = 0
    updated_index_count = 0
    not_found_count = 0

    # 3. Batch sorguları ve dosya güncellemeleri
    batch_size = args.batch_size
    for i in range(0, total_targets, batch_size):
        if _shutdown_requested:
            logger.warning("🛑 Kullanıcı tarafından durduruldu. Değişiklikler kaydediliyor...")
            break

        batch = targets[i:i + batch_size]
        batch_ids = [item["anilist_id"] for item in batch]
        current_range = f"{i + 1}-{min(i + batch_size, total_targets)}"
        logger.info(f"📡 Batch [{current_range}/{total_targets}] AniList'e soruluyor ({len(batch_ids)} anime)...")

        posters = fetcher.fetch_batch_posters(batch_ids)

        # Batch sonuçlarını hem detay dosyasına hem indeks objesine uygula
        for item in batch:
            al_id = item["anilist_id"]
            poster_url = posters.get(al_id)
            filepath = item["filepath"]
            anime_entry = item["entry"]

            if not poster_url:
                not_found_count += 1
                logger.warning(f"  ⚠️ Poster bulunamadı: {anime_entry.get('title')} (AniList ID: {al_id})")
                continue

            # a) Detay dosyasını güncelle
            if not args.dry_run:
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        detail = json.load(f)
                    
                    if "anilist" not in detail or not isinstance(detail["anilist"], dict):
                        detail["anilist"] = {"id": al_id}
                    
                    detail["anilist"]["poster_url"] = poster_url

                    with open(filepath, "w", encoding="utf-8") as f:
                        json.dump(detail, f, ensure_ascii=False, indent=2)

                    updated_detail_count += 1
                except Exception as e:
                    logger.error(f"  ❌ Detay dosyası yazılamadı ({filepath}): {e}")
            else:
                updated_detail_count += 1

            # b) animes.json objesini güncelle
            if al_id in animes_map:
                animes_map[al_id]["image_url"] = poster_url
                updated_index_count += 1

        # Her 10 batch'te bir veya büyük adımlarda ara kayıt (güvenlik için)
        if not args.dry_run and (i + batch_size) % 500 == 0:
            try:
                with open(ANIMES_JSON_PATH, "w", encoding="utf-8") as f:
                    json.dump(animes, f, ensure_ascii=False, indent=2)
                logger.info("💾 Ara kayıt: animes.json güncellendi.")
            except Exception as e:
                logger.error(f"Ara kayıt hatası: {e}")

    # 4. animes.json'ı nihai olarak diske kaydet
    if not args.dry_run and updated_index_count > 0:
        logger.info("💾 animes.json diske kaydediliyor...")
        try:
            with open(ANIMES_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(animes, f, ensure_ascii=False, indent=2)
            logger.info("✅ animes.json başarıyla kaydedildi.")

            # version.json güncelle
            from storage_manager import StorageManager
            storage = StorageManager(base_dir=BASE_DIR)
            storage.update_versions()
            logger.info("✅ version.json güncellendi.")
        except Exception as e:
            logger.error(f"❌ animes.json kaydedilemedi: {e}")

    # 5. Özet raporu
    elapsed = datetime.now() - start_time
    total_seconds = int(elapsed.total_seconds())
    minutes, seconds = divmod(total_seconds, 60)

    logger.info("=" * 65)
    logger.info("  📊 İŞLEM ÖZETİ")
    logger.info("=" * 65)
    logger.info(f"  Hedeflenen Anime        : {total_targets}")
    logger.info(f"  Güncellenen Detay Dosya : {updated_detail_count}")
    logger.info(f"  Güncellenen animes.json : {updated_index_count}")
    logger.info(f"  AniList'te Poster Yok   : {not_found_count}")
    logger.info(f"  Atlanan (AniList ID yok): {skipped_no_id}")
    logger.info(f"  Toplam Geçen Süre       : {minutes}dk {seconds}sn")
    if args.dry_run:
        logger.info("  ⚠️  DRY-RUN: Diske hiçbir dosya yazılmadı.")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
