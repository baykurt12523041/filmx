"""Asenkron dosya indirme modülü.

aiohttp tabanlı, OOP ve modüler yapıda indirme yöneticisi.
Yerel FastAPI sunucusundan çözümlenen (örn. /movie-detail -> iframe)
URL'lerin diske indirilmesi için kullanılır.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

__all__ = ["DownloadManager", "DownloadError"]


class DownloadError(RuntimeError):
    """İndirme sırasında oluşan hatalar için genel istisna."""


class DownloadManager:
    """Asenkron dosya indirici.

    Örnek:
        manager = DownloadManager(timeout=60)
        await manager.download("https://example.com/video.mp4", "downloads/video.mp4")
    """

    def __init__(
        self,
        timeout: int = 60,
        chunk_size: int = 1024 * 64,
        log_every_percent: float = 5.0,
    ) -> None:
        """İndirme yöneticisini yapılandır.

        Args:
            timeout: Toplam istek zamanaşımı (saniye).
            chunk_size: Tek seferde okunacak bayt miktarı.
            log_every_percent: Yüzde kaç ilerlemede bir log basılacağı.
        """
        if timeout <= 0:
            raise ValueError("timeout pozitif olmalıdır.")
        if chunk_size <= 0:
            raise ValueError("chunk_size pozitif olmalıdır.")

        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._chunk_size = chunk_size
        self._log_every_percent = log_every_percent

    @staticmethod
    def _format_size(num_bytes: int | None) -> str:
        """Bayt değerini okunabilir formata çevir."""
        if num_bytes is None:
            return "bilinmiyor"
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} TB"

    async def download(self, url: str, output_path: str | Path) -> Path:
        """Verilen URL'yi yerel diske asenkron olarak indir.

        Args:
            url: İndirilecek dosyanın URL'i.
            output_path: Dosyanın kaydedileceği yerel yol.

        Returns:
            Kaydedilen dosyanın mutlak yolu.

        Raises:
            ValueError: URL boşsa.
            DownloadError: Ağ, zamanaşımı veya dosya yazma hatası durumunda.
        """
        if not url or not url.strip():
            raise ValueError("url boş olamaz.")

        destination = Path(output_path).expanduser().resolve()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DownloadError(f"Hedef klasör oluşturulamadı: {destination.parent}") from exc

        logger.info("İndirme başlıyor: %s -> %s", url, destination)

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url) as response:
                    response.raise_for_status()

                    total_size: int | None = None
                    content_length = response.headers.get("Content-Length")
                    if content_length and content_length.isdigit():
                        total_size = int(content_length)

                    downloaded = 0
                    start_time = time.monotonic()
                    next_log_threshold = self._log_every_percent

                    try:
                        with destination.open("wb") as file_handle:
                            async for chunk in response.content.iter_chunked(self._chunk_size):
                                if not chunk:
                                    continue
                                file_handle.write(chunk)
                                downloaded += len(chunk)
                                next_log_threshold = self._log_progress(
                                    downloaded=downloaded,
                                    total_size=total_size,
                                    start_time=start_time,
                                    next_log_threshold=next_log_threshold,
                                )
                    except OSError as exc:
                        raise DownloadError(f"Dosya yazılamadı: {destination}") from exc

                    elapsed = time.monotonic() - start_time
                    logger.info(
                        "İndirme tamamlandı: %s (%s, %.1fs)",
                        destination,
                        self._format_size(downloaded),
                        elapsed,
                    )
                    return destination

        except aiohttp.ClientResponseError as exc:
            msg = f"HTTP hatası ({exc.status}): {url}"
            logger.error(msg)
            raise DownloadError(msg) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            # aiohttp zamanaşımları asyncio.TimeoutError olarak da yüzeye çıkabilir.
            msg = f"Bağlantı/zamanaşımı hatası: {url} ({exc})"
            logger.error(msg)
            raise DownloadError(msg) from exc
        except DownloadError:
            raise
        except Exception as exc:  # Beklenmeyen hataları sar, çağrıcı tek tip yakalasın.
            msg = f"Beklenmeyen indirme hatası: {url} ({exc})"
            logger.exception(msg)
            raise DownloadError(msg) from exc

    def _log_progress(
        self,
        *,
        downloaded: int,
        total_size: int | None,
        start_time: float,
        next_log_threshold: float,
    ) -> float:
        """İlerlemeyi konsola logla, sonraki log eşiğini döndür."""
        elapsed = max(time.monotonic() - start_time, 1e-6)
        speed = downloaded / elapsed

        if total_size:
            percentage = (downloaded / total_size) * 100.0
            if percentage >= next_log_threshold or downloaded >= total_size:
                logger.info(
                    "[%%%6.2f] %s / %s (%.2f MB/s)",
                    min(percentage, 100.0),
                    self._format_size(downloaded),
                    self._format_size(total_size),
                    speed / (1024 * 1024),
                )
                return next_log_threshold + self._log_every_percent
            return next_log_threshold

        # Toplam boyut bilinmiyorsa her ~1 MB'da bir logla.
        if downloaded // (1024 * 1024) != int(getattr(self, "_last_logged_mb", -1)):
            logger.info(
                "İndiriliyor: %s (toplam boyut bilinmiyor, %.2f MB/s)",
                self._format_size(downloaded),
                speed / (1024 * 1024),
            )
        return next_log_threshold
