#!/usr/bin/env python3
"""
api/anime klasöründeki json dosyalarını tarar ve verilen fansub_id'yi
içeren dosyaları (ve hangi bölümlerde geçtiğini) listeler.

Kullanım:
    python fansub_ara.py <fansub_id> [--dir api/anime]

Örnek:
    python fansub_ara.py 119
    python fansub_ara.py 188 --dir ./api/anime
"""

import argparse
import json
import sys
from pathlib import Path


def dosyada_fansub_var_mi(dosya_yolu: Path, fansub_id: int):
    """
    Verilen json dosyasını okur, episodes -> videos içinde
    fansub_id'yi arar. Bulursa eşleşen bölüm numaralarının
    listesini döner, bulamazsa None döner.
    """
    try:
        with open(dosya_yolu, "r", encoding="utf-8") as f:
            veri = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"UYARI: {dosya_yolu} okunamadı: {e}", file=sys.stderr)
        return None

    eslesen_bolumler = []

    for bolum in veri.get("episodes", []):
        videolar = bolum.get("videos", []) or []
        for video in videolar:
            if video.get("fansub_id") == fansub_id:
                eslesen_bolumler.append(bolum.get("episode_number"))
                break  # bu bölümü bir kere sayalım yeter

    return eslesen_bolumler if eslesen_bolumler else None


def main():
    parser = argparse.ArgumentParser(description="Fansub id'sine göre anime json dosyalarını ara.")
    parser.add_argument("fansub_id", type=int, help="Aranacak fansub_id (örn: 119)")
    parser.add_argument(
        "--dir", "-d",
        default="api/anime",
        help="Json dosyalarının bulunduğu klasör (varsayılan: api/anime)"
    )
    args = parser.parse_args()

    klasor = Path(args.dir)
    if not klasor.is_dir():
        print(f"HATA: '{klasor}' bir klasör değil ya da bulunamadı.", file=sys.stderr)
        sys.exit(1)

    json_dosyalari = sorted(klasor.glob("*.json"))
    if not json_dosyalari:
        print(f"'{klasor}' içinde hiç .json dosyası bulunamadı.")
        sys.exit(0)

    sonuclar = []
    for dosya in json_dosyalari:
        eslesen_bolumler = dosyada_fansub_var_mi(dosya, args.fansub_id)
        if eslesen_bolumler:
            sonuclar.append((dosya, eslesen_bolumler))

    if not sonuclar:
        print(f"fansub_id={args.fansub_id} içeren dosya bulunamadı.")
        return

    print(f"fansub_id={args.fansub_id} bulunan dosyalar ({len(sonuclar)} adet):\n")
    for dosya, bolumler in sonuclar:
        bolum_str = ", ".join(str(b) for b in bolumler)
        print(f"- {dosya}  (bölümler: {bolum_str})")


if __name__ == "__main__":
    main()