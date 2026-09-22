"""
migrate_anilist.py — AniList Verisi Toplu Ekleme Scripti

Mevcut tüm anime dosyalarına AniList verisini toplu olarak ekler.
GraphQL alias batch sorguları ile tek istekte 20 anime sorgulayarak hızlı çalışır.

Kullanım:
    python migrate_anilist.py              # Sadece eksik olanları ekle
    python migrate_anilist.py --force      # Hepsini yeniden sorgula
    python migrate_anilist.py --dry-run    # Kuru çalıştırma (yazmaz)

Güvenlik:
    - Ctrl+C ile güvenli kapatma (mevcut batch tamamlanır, yapılan değişiklikler korunur)
    - AniList API rate limit'e uygun çalışır (batch'ler arası 1.5s + backoff)
    - --dry-run ile dosyalara dokunmadan test edilebilir
"""

import os
import sys
import json
import signal
import argparse
from datetime import datetime

from logger import setup_logger
from anilist_client import AnilistClient
from storage_manager import StorageManager


logger = setup_logger("MigrateAnilist")

# Graceful shutdown (Ctrl+C desteği)
_shutdown_requested = False


def _handle_shutdown(signum, frame):
    """Ctrl+C sinyalini yakalar ve güvenli kapatma bayrağını ayarlar."""
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("İkinci Ctrl+C algılandı, zorla kapatılıyor...")
        os._exit(1)
    _shutdown_requested = True
    logger.warning("⏹️  Kapatma isteği alındı! Mevcut batch tamamlandıktan sonra güvenli şekilde kapanacak...")


def main():
    global _shutdown_requested
    signal.signal(signal.SIGINT, _handle_shutdown)

    parser = argparse.ArgumentParser(
        description="Mevcut anime dosyalarına toplu AniList verisi ekler."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Zaten AniList verisi olan animeleri de yeniden sorgula.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Gerçek dosya yazımı yapmadan test eder.",
    )
    args = parser.parse_args()

    start_time = datetime.now()

    logger.info("=" * 60)
    logger.info("  🔗 AniList Veri Migration Scripti")
    logger.info("=" * 60)
    if args.force:
        logger.info("⚠️  --force modu: Tüm animeler yeniden sorgulanacak.")
    if args.dry_run:
        logger.info("⚠️  --dry-run modu: Dosyalara yazılmayacak.")

    storage = StorageManager()
    anilist = AnilistClient()

    # ─────────────────────────────────────────────
    # 1. Tüm anime dosyalarını tara ve işlenecek olanları belirle
    # ─────────────────────────────────────────────
    anime_files = []
    skipped_no_jikan = 0
    skipped_anilist = 0

    for filename in sorted(os.listdir(storage.anime_dir)):
        if not filename.endswith(".json"):
            continue

        filepath = os.path.join(storage.anime_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Dosya okunamadı ({filename}): {e}")
            continue

        jikan = data.get("jikan")
        if not jikan or not jikan.get("mal_id"):
            # Jikan verisi olmayan animeleri atla (MAL ID gerekli)
            skipped_no_jikan += 1
            continue

        # --force yoksa, zaten anilist verisi olanları atla
        if not args.force and data.get("anilist") is not None:
            skipped_anilist += 1
            continue

        anime_files.append({
            "filename": filename,
            "filepath": filepath,
            "mal_id": jikan["mal_id"],
            "name": jikan.get("title") or data.get("turkanime", {}).get("isim", filename),
            "data": data,
        })

    toplam = len(anime_files)
    logger.info(
        f"📊 İşlenecek: {toplam} anime | "
        f"Jikan yok: {skipped_no_jikan} | "
        f"AniList mevcut: {skipped_anilist}"
    )

    if toplam == 0:
        logger.info("✅ Tüm animelerde AniList verisi zaten mevcut!")
        return

    # ─────────────────────────────────────────────
    # 2. MAL ID'leri topla ve batch'ler halinde sorgula
    # ─────────────────────────────────────────────
    mal_ids = [af["mal_id"] for af in anime_files]

    basarili = 0
    basarisiz = 0
    islenen = 0

    for i in range(0, len(mal_ids), anilist.BATCH_SIZE):
        if _shutdown_requested:
            logger.warning("🛑 Kullanıcı tarafından durduruldu.")
            break

        batch_mal_ids = mal_ids[i:i + anilist.BATCH_SIZE]
        batch_end = min(i + anilist.BATCH_SIZE, len(mal_ids))

        logger.info(
            f"📡 Batch [{i + 1}-{batch_end}/{toplam}] sorgulanıyor... "
            f"({len(batch_mal_ids)} anime)"
        )

        batch_results = anilist.fetch_batch(batch_mal_ids)

        # Sonuçları dosyalara yaz
        for j, mal_id in enumerate(batch_mal_ids):
            idx = i + j
            af = anime_files[idx]
            result = batch_results.get(mal_id)
            islenen += 1

            if result is not None:
                basarili += 1
                logger.info(
                    f"  ✅ {af['name']} — anilist_id: {result['id']}"
                    + (f", banner: var" if result.get("banner_image") else ", banner: yok")
                )

                if not args.dry_run:
                    af["data"]["anilist"] = result
                    try:
                        with open(af["filepath"], "w", encoding="utf-8") as f:
                            json.dump(af["data"], f, ensure_ascii=False, indent=2)
                    except (IOError, OSError) as e:
                        logger.error(f"  ❌ Dosya yazılamadı ({af['filename']}): {e}")
            else:
                basarisiz += 1
                logger.warning(
                    f"  ⚠️  {af['name']} (mal_id: {mal_id}) — AniList'te bulunamadı."
                )

    # ─────────────────────────────────────────────
    # 3. İndeksi yeniden oluştur
    # ─────────────────────────────────────────────
    if not args.dry_run and basarili > 0:
        logger.info("📦 İndeks yeniden oluşturuluyor...")
        total_in_index = storage.build_index()
        storage.update_versions()
        logger.info(f"✅ animes.json güncellendi. ({total_in_index} anime)")

    # ─────────────────────────────────────────────
    # 4. Özet
    # ─────────────────────────────────────────────
    elapsed = datetime.now() - start_time
    total_seconds = int(elapsed.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    elapsed_parts = []
    if hours > 0:
        elapsed_parts.append(f"{hours}s")
    if minutes > 0:
        elapsed_parts.append(f"{minutes}dk")
    elapsed_parts.append(f"{seconds}sn")
    elapsed_str = " ".join(elapsed_parts)

    logger.info("=" * 60)
    logger.info("  📊 MİGRASYON ÖZETİ")
    logger.info("=" * 60)
    logger.info(f"  İşlenen           : {islenen}/{toplam}")
    logger.info(f"  Başarılı          : {basarili}")
    logger.info(f"  Başarısız/Yok     : {basarisiz}")
    logger.info(f"  Jikan Yok (atl.)  : {skipped_no_jikan}")
    logger.info(f"  AniList Mevcut    : {skipped_anilist}")
    logger.info(f"  Toplam Süre       : {elapsed_str}")
    if args.dry_run:
        logger.info("  ⚠️  DRY-RUN: Hiçbir dosya yazılmadı!")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
