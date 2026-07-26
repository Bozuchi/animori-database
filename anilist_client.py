"""
anilist_client.py — AniList GraphQL API Client

AniList API üzerinden anime verilerini çeker:
    - id           → AniList benzersiz anime kimliği
    - bannerImage  → Geniş format kapak görseli URL'si

Kullanım:
    Pipeline (tekli sorgu):
        client = AnilistClient()
        result = client.fetch(mal_id=52991)
        # {"id": 154587, "banner_image": "https://..."}

    Migration (toplu sorgu — GraphQL alias batch):
        results = client.fetch_batch([1, 5, 21, 52991])
        # {1: {"id": 1, "banner_image": ...}, 5: None, ...}

Rate Limit:
    - AniList API: 90 istek/dakika
    - İstekler arası minimum 1 saniye bekleme
    - HTTP 429 durumunda Retry-After header'ı kontrol edilir
    - Üstel geri çekilme (exponential backoff) uygulanır
"""

import time
import requests
from logger import setup_logger


class AnilistClient:
    """AniList GraphQL API ile anime verilerini çeken client."""

    API_URL = "https://graphql.anilist.co"

    # Rate limit sabitleri
    RATE_LIMIT_DELAY = 1.0    # İstekler arası minimum bekleme (saniye)
    RETRY_MAX = 3             # Maksimum deneme sayısı
    RETRY_BACKOFF = 10        # Üstel geri çekilme çarpanı (saniye)
    REQUEST_TIMEOUT = 15      # HTTP istek zaman aşımı (saniye)

    # Batch sorgu sabitleri
    BATCH_SIZE = 20           # Tek istekte sorgulanacak maksimum anime sayısı

    # Tek anime için GraphQL sorgusu (variables kullanır)
    SINGLE_QUERY = """
    query ($malId: Int) {
      Media(idMal: $malId, type: ANIME) {
        id
        title {
          romaji
        }
        bannerImage
      }
    }
    """

    def __init__(self):
        self.logger = setup_logger("Anilist")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._last_request_time = 0

    def _wait_rate_limit(self):
        """İstekler arası minimum bekleme süresini uygular."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - elapsed)

    def _request_with_retry(self, payload: dict) -> dict | None:
        """
        GraphQL isteğini retry ve backoff mekanizmasıyla gönderir.

        Args:
            payload: GraphQL query ve variables içeren dict.

        Returns:
            dict: API yanıtının JSON'ı veya None (başarısızlık durumunda).
        """
        for attempt in range(self.RETRY_MAX):
            self._wait_rate_limit()

            try:
                self._last_request_time = time.time()
                response = self.session.post(
                    self.API_URL,
                    json=payload,
                    timeout=self.REQUEST_TIMEOUT,
                )

                if response.status_code == 200:
                    return response.json()

                # AniList, sorgu sonuçlarının tamamı/bir kısmı null olduğunda 404 döner
                # ama yanıt body'sinde geçerli GraphQL data içerebilir
                if response.status_code == 404:
                    try:
                        body = response.json()
                        if "data" in body:
                            return body
                    except (ValueError, KeyError):
                        pass
                    return None

                if response.status_code == 429:
                    # Rate limit aşıldı
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        wait_time = int(retry_after) + 1
                    else:
                        wait_time = self.RETRY_BACKOFF * (attempt + 1)
                    self.logger.warning(
                        f"Rate limit aşıldı (429). {wait_time}s bekleniyor... "
                        f"(deneme {attempt + 1}/{self.RETRY_MAX})"
                    )
                    time.sleep(wait_time)
                    continue

                if response.status_code in (500, 502, 503):
                    wait_time = self.RETRY_BACKOFF * (attempt + 1)
                    self.logger.warning(
                        f"Sunucu hatası ({response.status_code}). {wait_time}s bekleniyor... "
                        f"(deneme {attempt + 1}/{self.RETRY_MAX})"
                    )
                    time.sleep(wait_time)
                    continue

                # Diğer HTTP hataları → tekrar denemeye değmez
                self.logger.error(
                    f"AniList API hatası: HTTP {response.status_code} — {response.text[:200]}"
                )
                return None

            except requests.exceptions.Timeout:
                wait_time = self.RETRY_BACKOFF * (attempt + 1)
                self.logger.warning(
                    f"İstek zaman aşımı. {wait_time}s bekleniyor... "
                    f"(deneme {attempt + 1}/{self.RETRY_MAX})"
                )
                time.sleep(wait_time)
                continue

            except requests.exceptions.RequestException as e:
                self.logger.error(f"AniList bağlantı hatası: {e}")
                return None

        self.logger.error(f"AniList API {self.RETRY_MAX} denemede de başarısız oldu.")
        return None

    @staticmethod
    def _parse_media(media: dict | None) -> dict | None:
        """
        AniList Media yanıtını iç formata dönüştürür.

        Args:
            media: AniList API'den dönen Media objesi.

        Returns:
            dict: {"id": int, "banner_image": str|None} veya None.
        """
        if not media:
            return None

        return {
            "id": media.get("id"),
            "banner_image": media.get("bannerImage"),
        }

    def fetch(self, mal_id: int) -> dict | None:
        """
        Tek bir anime için AniList verisini çeker.
        Pipeline akışında (main.py) kullanılır.

        Args:
            mal_id: MyAnimeList anime ID'si.

        Returns:
            dict: {"id": int, "banner_image": str|None} veya None (hata durumunda).
        """
        payload = {
            "query": self.SINGLE_QUERY,
            "variables": {"malId": mal_id},
        }

        result = self._request_with_retry(payload)
        if not result:
            return None

        # GraphQL hata kontrolü
        if "errors" in result:
            errors = result["errors"]
            self.logger.warning(
                f"AniList GraphQL hatası (mal_id={mal_id}): {errors[0].get('message', '')}"
            )
            return None

        media = result.get("data", {}).get("Media")
        return self._parse_media(media)

    def fetch_batch(self, mal_ids: list[int]) -> dict[int, dict | None]:
        """
        Birden fazla anime için toplu AniList sorgusu yapar.
        GraphQL alias mekanizmasını kullanarak tek istekte BATCH_SIZE kadar anime sorgular.
        Migration scripti (migrate_anilist.py) tarafından kullanılır.

        AniList batch sorgusunda bir anime bulunamadığında tüm sonuçları null'a
        çevirebilir. Bu durumda null dönen animeler tekli sorgu ile yeniden denenir.

        Örnek sorgu:
            query {
              a1: Media(idMal: 1, type: ANIME) { id title { romaji } bannerImage }
              a21: Media(idMal: 21, type: ANIME) { id title { romaji } bannerImage }
            }

        Args:
            mal_ids: MyAnimeList anime ID'lerinin listesi.

        Returns:
            dict: {mal_id: {"id": int, "banner_image": str|None} veya None} mapping'i.
        """
        results = {}

        # mal_ids'i BATCH_SIZE'lık gruplara böl
        for i in range(0, len(mal_ids), self.BATCH_SIZE):
            batch = mal_ids[i:i + self.BATCH_SIZE]

            # GraphQL alias sorgusu oluştur
            fields = "id title { romaji } bannerImage"
            query_parts = []
            for mal_id in batch:
                alias = f"a{mal_id}"
                query_parts.append(
                    f"  {alias}: Media(idMal: {mal_id}, type: ANIME) {{ {fields} }}"
                )

            query = "query {\n" + "\n".join(query_parts) + "\n}"
            payload = {"query": query}

            response = self._request_with_retry(payload)

            if not response:
                # Tüm batch başarısız → hepsini None olarak işaretle
                for mal_id in batch:
                    results[mal_id] = None
                continue

            has_errors = "errors" in response
            data = response.get("data", {})

            # Batch sonuçlarını parse et
            null_mal_ids = []
            for mal_id in batch:
                alias = f"a{mal_id}"
                media = data.get(alias)
                parsed = self._parse_media(media)
                results[mal_id] = parsed

                # Hata varken null dönen animeleri tekli retry için işaretle
                if parsed is None and has_errors:
                    null_mal_ids.append(mal_id)

            # ── Tekli retry: batch hatası tüm sonuçları null'a çevirebilir ──
            # AniList, batch'te bir anime bulunamadığında diğer sonuçları da
            # null'a çevirir. Bu yüzden null dönen animeleri tekli sorgu ile
            # yeniden deniyoruz.
            if null_mal_ids:
                self.logger.info(
                    f"Batch hatasi nedeniyle {len(null_mal_ids)} anime "
                    f"tekli sorguyla yeniden deneniyor..."
                )
                for mal_id in null_mal_ids:
                    result = self.fetch(mal_id)
                    results[mal_id] = result

        return results
