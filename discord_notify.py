"""
discord_notify.py — Discord Webhook Bildirim Modülü

Her çalıştırma sonunda Discord'a özet rapor gönderir.
- Başarılı çalıştırmalarda yeşil embed
- Hatalı çalıştırmalarda kırmızı embed
- errors.log'daki hatalar ayrı bölümde vurgulanır
- Beklenmedik çökme durumunda özel kırmızı uyarı

Kullanım:
    from discord_notify import send_report
    send_report(stats={...}, elapsed="2s 34dk", crash_error=None)

Webhook URL'si DISCORD_WEBHOOK_URL ortam değişkeninden okunur.
Tanımlı değilse bildirim sessizce atlanır (çalıştırmayı bozmaz).
"""

import os
import json
import requests
from datetime import datetime

from logger import setup_logger

logger = setup_logger("Discord")

# Discord embed renk kodları
COLOR_SUCCESS = 0x2ECC71   # Yeşil
COLOR_WARNING = 0xF39C12   # Turuncu
COLOR_ERROR = 0xE74C3C     # Kırmızı


def _read_errors_log(path: str = "errors.log") -> list[str]:
    """errors.log dosyasını okur ve hata satırlarını döndürür."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        return lines
    except Exception as e:
        logger.warning(f"errors.log okunamadı: {e}")
        return []


def _build_embed(stats: dict, elapsed: str, error_lines: list[str], crash_error: str | None = None) -> dict:
    """Discord embed mesajını oluşturur."""
    
    has_errors = len(error_lines) > 0 or crash_error is not None
    
    # ── Renk seçimi ──
    if crash_error:
        color = COLOR_ERROR
        title = "🎌 Anime Database — ❌ Çalıştırma Çöktü!"
    elif has_errors:
        color = COLOR_WARNING
        title = "🎌 Anime Database — ⚠️ Hatalarla Tamamlandı"
    else:
        color = COLOR_SUCCESS
        title = "🎌 Anime Database — ✅ Başarıyla Tamamlandı"

    fields = []

    # ── İşlem Özeti ──
    summary_lines = []
    stat_labels = {
        "toplam": "Toplam Anime",
        "kara_listede": "Kara Listede",
        "turkanime_guncellenen": "Türkanime Güncellenen",
        "ozet_cekilen": "Özet Çekilen",
        "jikan_basarili": "Jikan Başarılı",
        "jikan_atlanan": "Jikan Atlandı",
        "jikan_basarisiz": "Jikan Başarısız",
        "anilist_basarili": "AniList Başarılı",
        "anilist_atlanan": "AniList Atlandı",
        "anilist_basarisiz": "AniList Başarısız",
        "bolum_taranan": "Bölüm Taranan",
        "bolum_atlanan": "Bölüm Atlandı",
        "bolum_bos": "Bölüm Boş",
        "bolum_jikan_null": "Bölüm Atlandı (no Jikan)",
        "index_toplam": "İndeks Toplam",
    }

    for key, label in stat_labels.items():
        value = stats.get(key)
        if value is not None:
            summary_lines.append(f"**{label}:** `{value}`")

    if summary_lines:
        fields.append({
            "name": "📊 İşlem Özeti",
            "value": "\n".join(summary_lines),
            "inline": False
        })

    # ── Süre ──
    if elapsed:
        fields.append({
            "name": "⏱️ Toplam Süre",
            "value": elapsed,
            "inline": True
        })

    # ── Çökme hatası ──
    if crash_error:
        # Discord embed field değeri max 1024 karakter
        crash_text = crash_error[:1000]
        fields.append({
            "name": "💥 Çökme Hatası",
            "value": f"```\n{crash_text}\n```",
            "inline": False
        })

    # ── errors.log hataları ──
    if error_lines:
        # En fazla 10 hata göster, gerisini "ve X hata daha..." ile özetle
        max_display = 10
        displayed = error_lines[:max_display]
        
        error_text_parts = []
        for line in displayed:
            # Zaman damgasından sonraki mesajı al
            # Format: "2026-07-07 00:37:18 [MAL] ERROR — mesaj"
            parts = line.split(" — ", 1)
            msg = parts[1] if len(parts) > 1 else line
            error_text_parts.append(f"• {msg}")
        
        if len(error_lines) > max_display:
            error_text_parts.append(f"\n*...ve {len(error_lines) - max_display} hata daha*")
        
        error_text = "\n".join(error_text_parts)
        # Discord field value max 1024 karakter
        if len(error_text) > 1020:
            error_text = error_text[:1017] + "..."

        fields.append({
            "name": f"🚨 Hatalar ({len(error_lines)} adet)",
            "value": error_text,
            "inline": False
        })

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "timestamp": datetime.utcnow().isoformat(),
        "footer": {
            "text": "Anime Database Updater"
        }
    }

    return embed


def send_report(
    stats: dict,
    elapsed: str = "",
    crash_error: str | None = None,
    error_log_path: str = "errors.log",
):
    """
    Discord'a çalıştırma raporu gönderir.

    Args:
        stats: İşlem istatistikleri sözlüğü.
        elapsed: Geçen süre metni (örn: "2s 34dk").
        crash_error: Beklenmedik çökme hatası metni (varsa).
        error_log_path: errors.log dosya yolu.
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    
    if not webhook_url:
        logger.info("DISCORD_WEBHOOK_URL tanımlı değil, bildirim atlanıyor.")
        return

    error_lines = _read_errors_log(error_log_path)
    embed = _build_embed(stats, elapsed, error_lines, crash_error)

    payload = {
        "embeds": [embed]
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"}
        )

        if response.status_code in (200, 204):
            logger.info("Discord bildirimi başarıyla gönderildi.")
        else:
            logger.warning(
                f"Discord bildirimi gönderilemedi. "
                f"HTTP {response.status_code}: {response.text[:200]}"
            )

    except requests.exceptions.Timeout:
        logger.warning("Discord bildirimi zaman aşımına uğradı.")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Discord bildirimi gönderilemedi: {e}")
    except Exception as e:
        logger.warning(f"Discord bildirim hatası: {e}")
