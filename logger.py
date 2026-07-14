"""
logger.py — Merkezi Loglama Modülü

Tüm modüllerin kullanacağı standart logging yapılandırması.
- Konsola (stdout) formatlı çıktı (INFO ve üstü)
- errors.log dosyasına sadece ERROR ve üstü
- Her çalıştırmada errors.log sıfırlanır (run-based)

Kullanım:
    from logger import setup_logger
    logger = setup_logger("ModülAdı")
    logger.info("Bilgi mesajı")
    logger.error("Hata mesajı", exc_info=True)  # Stack trace ile
"""

import logging
import sys

_initialized = False


def setup_logger(name: str, error_log_path: str = "errors.log") -> logging.Logger:
    """
    Modül bazlı logger oluşturur.

    İlk çağrıda errors.log sıfırlanır (mode="w"),
    sonraki çağrılarda append modda açılır (mode="a").

    Args:
        name: Logger adı (örn: "Scraper", "MAL", "Episodes", "Storage").
        error_log_path: Hata log dosyasının yolu.

    Returns:
        logging.Logger: Yapılandırılmış logger nesnesi.
    """
    global _initialized
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    # ── Konsol handler (INFO ve üstü) ──
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(console)

    # ── Dosya handler (ERROR ve üstü) ──
    # İlk logger'ı oluşturan modül dosyayı sıfırlar (mode="w"),
    # sonrakiler append eder (mode="a") → aynı çalıştırmadaki
    # farklı modüllerin hataları tek dosyada toplanır.
    mode = "w" if not _initialized else "a"
    _initialized = True

    file_handler = logging.FileHandler(error_log_path, mode=mode, encoding="utf-8")
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(file_handler)

    return logger
