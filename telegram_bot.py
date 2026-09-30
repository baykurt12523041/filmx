"""Telegram bot servisi — arama + bildirim + izleme linki.

Rol: bot dosya indirmez. Yerel FastAPI'de arama yapar, detay + poster
gösterir ve "Tarayıcıda İzle" butonuyla kullanıcıyı API'nin /embed
sayfasına yönlendirir.

Komutlar:
    /start       - Karşılama mesajı
    /ara <sorgu> - GET {API_BASE_URL}/search?query=<sorgu>

Akış:
    /ara -> sonuç listesi (butonlu) -> Detay butonu -> /movie-detail ->
    poster + bilgi + "Tarayıcıda İzle" URL butonu.

Ortam değişkenleri:
    TELEGRAM_BOT_TOKEN - zorunlu
    API_BASE_URL       - botun API'ye iç ağdan erişimi (varsayılan http://127.0.0.1:8000)
    WATCH_BASE_URL     - kullanıcının tarayıcısının açacağı herkese açık adres
                         (varsayılan API_BASE_URL; sunucuda mutlaka public URL verin)
    CHANNEL_ID         - yayın kanalı (@kanaladi veya -100... ID); boşsa otomatik paylaşım kapalı
    ADMIN_IDS          - /paylas kullanabilecek Telegram kullanıcı ID'leri (virgüllü)
    PUBLISH_INTERVAL_MIN - otomatik tarama aralığı dakika (varsayılan 60)
    POSTED_FILE        - paylaşılan slug'ların tutulduğu dosya (varsayılan posted.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger(__name__)

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WATCH_BASE_URL = os.getenv("WATCH_BASE_URL", API_BASE_URL).rstrip("/")
MAX_RESULTS_SHOWN = 10
DEFAULT_POSTED_FILE = Path(os.getenv("POSTED_FILE", "posted.json"))
DEFAULT_PUBLISH_INTERVAL_MIN = int(os.getenv("PUBLISH_INTERVAL_MIN", "60") or 60)

__all__ = ["TelegramBotService", "main"]


class TelegramBotService:
    """FastAPI tabanlı film arama + izleme-link botu (indirme yapmaz)."""

    def __init__(
        self,
        token: str | None = None,
        api_base_url: str = API_BASE_URL,
        watch_base_url: str = WATCH_BASE_URL,
        channel_id: str | None = None,
        admin_ids: set[int] | str | None = None,
        publish_interval_min: int = DEFAULT_PUBLISH_INTERVAL_MIN,
        posted_file: str | Path = DEFAULT_POSTED_FILE,
        request_timeout: int = 30,
    ) -> None:
        """Bot servisini yapılandır.

        Args:
            token: Telegram bot tokeni. None ise TELEGRAM_BOT_TOKEN env'den okunur.
            api_base_url: FastAPI'nin iç ağ adresi (örn. http://127.0.0.1:8000).
            watch_base_url: Kullanıcının tarayıcısının açacağı public adres.
            channel_id: Yayın kanalı. None ise CHANNEL_ID env'den okunur.
            admin_ids: /paylas yetkisi olan kullanıcı ID'leri (set veya "1,2,3").
            publish_interval_min: Otomatik kanal taraması (dakika).
            posted_file: Paylaşılan slug'ların tutulduğu dosya.
            request_timeout: FastAPI istekleri için zamanaşımı (sn).

        Raises:
            ValueError: Token bulunamazsa.
        """
        resolved_token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        if not resolved_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN bulunamadı. "
                "Ortam değişkenini ayarlayın veya TelegramBotService(token=...) verin."
            )

        self._token = resolved_token
        self._api_base_url = api_base_url.rstrip("/")
        self._watch_base_url = watch_base_url.rstrip("/")
        self._channel_id = (channel_id if channel_id is not None else os.getenv("CHANNEL_ID", "")).strip()
        if isinstance(admin_ids, str) or admin_ids is None:
            raw = admin_ids if admin_ids is not None else os.getenv("ADMIN_IDS", "")
            self._admin_ids = {int(x) for x in raw.split(",") if x.strip().isdigit()}
        else:
            self._admin_ids = set(admin_ids)
        self._publish_interval_min = publish_interval_min
        self._posted_file = Path(posted_file).expanduser()
        self._request_timeout = aiohttp.ClientTimeout(total=request_timeout)

    def _is_admin(self, user_id: int | None) -> bool:
        """Kullanıcının /paylas yetkisi var mı."""
        return user_id is not None and user_id in self._admin_ids

    # ------------------------------------------------------------------
    # FastAPI istemcisi
    # ------------------------------------------------------------------
    async def _fetch_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """FastAPI'den JSON al, hata yönetimiyle."""
        url = f"{self._api_base_url}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self._request_timeout) as session:
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    data: dict[str, Any] = await response.json()
                    return data
        except aiohttp.ClientResponseError as exc:
            logger.error("API HTTP hatası %s %s: %s", url, params, exc.status)
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.error("API bağlantı/zamanaşımı hatası %s: %s", url, exc)
            raise

    async def search_movies(self, query: str) -> list[dict[str, Any]]:
        """GET /search?query=... -> sonuç listesi."""
        data = await self._fetch_json("/search", {"query": query})
        results = data.get("results", [])
        return results if isinstance(results, list) else []

    async def fetch_movie_detail(self, slug_or_url: str) -> dict[str, Any]:
        """GET /movie-detail?url=... -> film detayı (iframe dahil)."""
        return await self._fetch_json("/movie-detail", {"url": slug_or_url})

    def build_watch_url(self, iframe_url: str) -> str:
        """Kullanıcının tarayıcısında açılacak izleme linki."""
        return f"{self._watch_base_url}/embed?url={quote(iframe_url, safe='')}"

    # ------------------------------------------------------------------
    # Mesaj formatlayıcılar
    # ------------------------------------------------------------------
    @staticmethod
    def _format_search_results(query: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return f"🔍 *{query}* için sonuç bulunamadı."
        lines = [f"🔍 *{query}* — {len(results)} sonuç (ilk {min(len(results), MAX_RESULTS_SHOWN)} gösteriliyor):\n"]
        for i, item in enumerate(results[:MAX_RESULTS_SHOWN], start=1):
            title = item.get("title", "Bilinmiyor")
            slug = item.get("slug", "-")
            lines.append(f"{i}. *{title}*\n   `ID: {slug}`")
        lines.append("\nDetay için aşağıdaki butonlara dokunun.")
        return "\n".join(lines)

    @staticmethod
    def _format_movie_detail(detail: dict[str, Any]) -> str:
        title = detail.get("title", "Bilinmiyor")
        imdb = detail.get("imdb") or "-"
        categories = ", ".join(detail.get("categories", []) or []) or "-"
        desc = (detail.get("description") or "")[:600]
        text = (
            f"🎬 *{title}*\n"
            f"⭐ IMDB: {imdb}\n"
            f"🏷️ Tür: {categories}\n\n"
            f"{desc}"
        )
        if detail.get("iframe"):
            text += "\n\n▶️ İzlemek için aşağıdaki butona dokunun."
        else:
            text += "\n\n⚠️ Bu içerik için oynatılabilir kaynak bulunamadı."
        return text

    def _build_detail_post(self, detail: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup | None]:
        """Detay sözlüğünden (metin + izleme butonu) üret. Kanal ve DM'de ortak kullanılır."""
        text = self._format_movie_detail(detail)
        keyboard = None
        iframe = detail.get("iframe")
        if iframe:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("▶️ Tarayıcıda İzle", url=self.build_watch_url(str(iframe)))]]
            )
        return text, keyboard

    # ------------------------------------------------------------------
    # Kanal yayını
    # ------------------------------------------------------------------
    def _load_posted(self) -> set[str]:
        """Daha önce kanala atılan slug'ları oku."""
        try:
            data = json.loads(self._posted_file.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except (OSError, ValueError):
            return set()

    def _save_posted(self, posted: set[str]) -> None:
        """Paylaşılan slug'ları diske yaz (tekrar post önlenir)."""
        try:
            self._posted_file.write_text(json.dumps(sorted(posted), ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("posted dosyası yazılamadı.", exc_info=True)

    async def publish_latest(self, bot: Any, max_pages: int = 2) -> dict[str, int]:
        """Kanal akışı: /latest'ten yeni filmleri kanala postala.

        Args:
            bot: send_photo / send_message bilen Telegram bot nesnesi.
            max_pages: Taranacak /latest sayfa sayısı.

        Returns:
            {"posted": <gönderilen>, "skipped": <atlanan>}.
        """
        if not self._channel_id:
            raise ValueError("CHANNEL_ID ayarlı değil.")
        posted = self._load_posted()

        new_items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            try:
                data = await self._fetch_json("/latest", {"page": page})
            except Exception:
                logger.exception("publish: /latest sayfa %s alınamadı", page)
                break
            for item in data.get("results", []) or []:
                slug = str(item.get("slug") or "").strip()
                if not slug or slug in posted or slug in seen:
                    continue
                seen.add(slug)
                new_items.append(item)

        sent = 0
        for item in new_items:
            slug = str(item.get("slug"))
            try:
                detail = await self.fetch_movie_detail(slug)
            except Exception:
                logger.exception("publish: detay alınamadı %s", slug)
                continue
            text, keyboard = self._build_detail_post(detail)
            try:
                poster = detail.get("poster")
                if poster:
                    await bot.send_photo(
                        chat_id=self._channel_id,
                        photo=poster,
                        caption=text[:1024],
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                else:
                    await bot.send_message(
                        chat_id=self._channel_id,
                        text=text[:4000],
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                posted.add(slug)
                sent += 1
            except Exception:
                logger.exception("publish: kanala gönderilemedi %s", slug)
            await asyncio.sleep(1)  # flood limiti koruması
        self._save_posted(posted)
        return {"posted": sent, "skipped": len(seen) - sent}

    async def _publisher_loop(self, app: Application) -> None:
        """Arka plan: belirli aralıklarla yeni filmleri kanala at."""
        await asyncio.sleep(10)  # bot tam açılmadan post atma
        while True:
            if self._channel_id:
                try:
                    result = await self.publish_latest(app.bot)
                    if result["posted"]:
                        logger.info("Otomatik paylaşım: %s film kanala atıldı.", result["posted"])
                except Exception:
                    logger.exception("Otomatik paylaşım hatası")
            await asyncio.sleep(max(self._publish_interval_min, 1) * 60)

    # ------------------------------------------------------------------
    # Handler'lar
    # ------------------------------------------------------------------
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Karşılama mesajı."""
        assert update.message is not None
        await update.message.reply_text(
            "👋 Merhaba! Film arama botuna hoş geldin.\n\n"
            "Kullanım:\n"
            "• `/ara <film adı>` — arama yapar\n"
            "• Sonuçtaki butonla detay + poster + izleme linki alırsın\n"
            "• Yeni filmler otomatik olarak kanalda paylaşılır\n\n"
            "Not: bot dosya indirmez, izleme tarayıcıda açılır.\n\n"
            "Örnek: `/ara supergirl`",
            parse_mode="Markdown",
        )

    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/ara <query> -> FastAPI /search'e GET at, sonuçları butonlu listele."""
        assert update.message is not None
        query = " ".join(context.args or []).strip()
        if not query:
            await update.message.reply_text("Kullanım: `/ara <film adı>`\nÖrnek: `/ara supergirl`", parse_mode="Markdown")
            return

        await update.message.reply_text(f"🔎 `{query}` aranıyor...", parse_mode="Markdown")
        try:
            results = await self.search_movies(query)
        except Exception:
            logger.exception("Arama hatası")
            await update.message.reply_text("❌ Sunucuya erişilemedi. API çalışıyor mu?")
            return

        context.user_data["last_query"] = query
        text = self._format_search_results(query, results)
        keyboard = self._build_search_keyboard(results[:MAX_RESULTS_SHOWN])
        if keyboard:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await update.message.reply_text(text, parse_mode="Markdown")

    def _build_search_keyboard(self, results: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
        buttons: list[list[InlineKeyboardButton]] = []
        for item in results:
            slug = str(item.get("slug") or item.get("url") or "").strip()
            title = str(item.get("title", slug))[:40]
            if not slug:
                continue
            # callback_data 64 bayt sınırına takılmaması için slug'ı kısalt.
            buttons.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"detail:{slug[:50]}")])
        return InlineKeyboardMarkup(buttons) if buttons else None

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Inline butonlar: 'detail:<slug>' -> detay + poster + izleme linki."""
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        data = query.data or ""

        if not data.startswith("detail:"):
            await query.edit_message_text("❓ Bilinmeyen işlem.")
            return

        slug = data.split(":", 1)[1]
        await query.edit_message_text(f"📄 Detay alınıyor: `{slug}`...", parse_mode="Markdown")
        try:
            detail = await self.fetch_movie_detail(slug)
        except Exception:
            logger.exception("Detay hatası")
            await query.edit_message_text("❌ Detay alınamadı. Sunucu loglarını kontrol edin.")
            return

        text, keyboard = self._build_detail_post(detail)
        # Poster varsa fotoğrafla, yoksa metinle göster.
        try:
            poster = detail.get("poster")
            if poster and update.effective_chat:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=poster,
                    caption=text[:1024],
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
                return
        except Exception:
            logger.warning("Poster gönderilemedi, metinle devam ediliyor.", exc_info=True)
        await query.edit_message_text(text[:4000], parse_mode="Markdown", reply_markup=keyboard)

    async def publish_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """(Yönetici) /paylas -> yeni filmleri hemen kanala postala."""
        assert update.message is not None
        user_id = update.effective_user.id if update.effective_user else None
        if not self._is_admin(user_id):
            await update.message.reply_text("⛔ Bu komut yalnızca yöneticilere açık.")
            return
        if not self._channel_id:
            await update.message.reply_text("⛔ CHANNEL_ID ayarlı değil.")
            return
        await update.message.reply_text("📢 Yeni filmler taranıyor...")
        try:
            result = await self.publish_latest(context.bot)
        except Exception as exc:
            logger.exception("Manuel paylaşım hatası")
            await update.message.reply_text(f"❌ Paylaşım hatası: `{exc}`", parse_mode="Markdown")
            return
        await update.message.reply_text(
            f"✅ {result['posted']} film kanala paylaşıldı. ({result['skipped']} zaten vardı)"
        )

    # ------------------------------------------------------------------
    # Uygulama kurulumu / çalıştırma
    # ------------------------------------------------------------------
    def build_app(self) -> Application:
        """Telegram Application nesnesini handler'larla kur."""
        service = self

        async def _post_init(app: Application) -> None:
            # Bot açıldıktan sonra otomatik kanal yayını döngüsünü başlat.
            app.create_task(service._publisher_loop(app))

        app = Application.builder().token(self._token).post_init(_post_init).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CommandHandler("ara", self.search_command))
        app.add_handler(CommandHandler("paylas", self.publish_command))
        app.add_handler(CallbackQueryHandler(self.button_callback))
        return app

    def run_polling(self) -> None:
        """Botu long-polling modunda çalıştır (bloklar)."""
        app = self.build_app()
        logger.info(
            "Telegram bot başlatılıyor (API: %s, izleme: %s)...",
            self._api_base_url,
            self._watch_base_url,
        )
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    """Ortamdan token alıp botu başlatır."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    # .env desteği opsiyonel (python-dotenv kuruluysa).
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    try:
        service = TelegramBotService()
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    service.run_polling()


if __name__ == "__main__":
    main()
