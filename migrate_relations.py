"""
migrate_relations.py — relations entries yapısını düzleştirme

Mevcut api/anime/*.json dosyalarındaki relations formatını:
    Eski:  "entries": [{"mal_id": 20}, {"mal_id": 34566}]
    Yeni:  "entries": [20, 34566]
şeklinde günceller.

Güvenlik:
    - Sadece dönüştürme gereken dosyalara dokunur
    - relations alanı yoksa veya zaten düz int dizisiyse atlar
    - Her dosyayı önce okur, bellekte dönüştürür, sonra yazar (atomik değil ama güvenli)
    - Dry-run modu ile önce ne yapılacağını gösterir

Kullanım:
    python migrate_relations.py              (dry-run — sadece rapor)
    python migrate_relations.py --apply      (gerçek güncelleme)
"""

import os
import sys
import json
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ANIME_DIR = os.path.join("api", "anime")


def needs_migration(relations: list) -> bool:
    """relations listesindeki entries'lerden herhangi biri dict mi kontrol eder."""
    for rel in relations:
        for entry in rel.get("entries", []):
            if isinstance(entry, dict):
                return True
    return False


def migrate_relations(relations: list) -> list:
    """entries içindeki {"mal_id": N} dict'lerini düz N integer'a çevirir."""
    migrated = []
    for rel in relations:
        new_entries = []
        for entry in rel.get("entries", []):
            if isinstance(entry, dict) and "mal_id" in entry:
                new_entries.append(entry["mal_id"])
            elif isinstance(entry, int):
                new_entries.append(entry)
            # Beklenmeyen format → olduğu gibi bırak
            else:
                new_entries.append(entry)

        migrated.append({
            "relation": rel.get("relation"),
            "entries": new_entries,
        })
    return migrated


def main():
    apply = "--apply" in sys.argv

    if not apply:
        print("═" * 60)
        print("  DRY-RUN modu — hiçbir dosya değiştirilmeyecek.")
        print("  Gerçek güncelleme için:  python migrate_relations.py --apply")
        print("═" * 60)
    else:
        print("═" * 60)
        print("  APPLY modu — dosyalar güncellenecek.")
        print("═" * 60)

    if not os.path.isdir(ANIME_DIR):
        print(f"\n❌ Klasör bulunamadı: {ANIME_DIR}")
        sys.exit(1)

    files = sorted(f for f in os.listdir(ANIME_DIR) if f.endswith(".json"))
    print(f"\n📂 {len(files)} JSON dosyası bulundu.\n")

    migrated_count = 0
    skipped_count = 0
    error_count = 0

    for filename in files:
        filepath = os.path.join(ANIME_DIR, filename)

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  ❌ {filename} — okunamadı: {e}")
            error_count += 1
            continue

        jikan = data.get("jikan")
        if not jikan or not isinstance(jikan, dict):
            skipped_count += 1
            continue

        relations = jikan.get("relations")
        if not relations or not isinstance(relations, list):
            skipped_count += 1
            continue

        if not needs_migration(relations):
            skipped_count += 1
            continue

        # Dönüştürme gerekiyor
        new_relations = migrate_relations(relations)

        if apply:
            jikan["relations"] = new_relations
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  ✅ {filename} — güncellendi.")
        else:
            # Dry-run: eski ve yeni formatı yan yana göster
            old_sample = relations[0]["entries"][:3]
            new_sample = new_relations[0]["entries"][:3]
            print(f"  🔄 {filename} — {len(relations)} relation grubu dönüştürülecek")
            print(f"       Örnek: {old_sample} → {new_sample}")

        migrated_count += 1

    # Özet
    print(f"\n{'═' * 60}")
    print(f"  📊 Sonuç")
    print(f"{'═' * 60}")
    print(f"  Güncellenen  : {migrated_count}")
    print(f"  Atlanan      : {skipped_count}")
    print(f"  Hata         : {error_count}")
    print(f"  Toplam       : {len(files)}")

    if not apply and migrated_count > 0:
        print(f"\n  ➡️  Uygulamak için:  python migrate_relations.py --apply")


if __name__ == "__main__":
    main()
