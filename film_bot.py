"""Tek dosyalik film botu — API sunucusu gerektirmez.

Film sitesini dogrudan tarar (arama, son filmler, detay) ve Telegram'da
hem DM'de hem yayin kanalinda paylasir. Pella gibi tek dosya calistiran
hostlarda kosmak icin tasarlandi: yaninda sadece requirements.txt gerekir.

Komutlar (DM):
    /start  - Karsilama
    /ara    - <film adi> arama, butonlu sonuc listesi
    /paylas - (yonetici) yeni filmleri hemen kanala at

Otomatik: PUBLISH_INTERVAL_MIN dakikada bir /latest taranir, yeni filmler
kanala poster + bilgi + kaynak butonuyla atilir. poster kaydi POSTED_FILE
dosyasinda tutulur (tekrar onlenir).

Ortam degiskenleri (.env desteklenir):
    TELEGRAM_BOT_TOKEN  - zorunlu
    CHANNEL_ID          - yayin kanali (@kanaladi veya -100...); bossa otomatik yayin kapali
    ADMIN_IDS           - /paylas yetkili ID'ler (virgullu)
    PUBLISH_INTERVAL_MIN- otomatik tarama dakika (varsayilan 60)
    POSTED_FILE         - paylasilan slug dosyasi (varsayilan posted.json)
    WATCH_BASE_URL      - opsiyonel: herkese acik api.py adresi. Doluysa izleme
                          butonlari {WATCH_BASE_URL}/embed?url=... proxy'sine gider
                          (guvenilir yol). Bossa direkt kaynak linki kullanilir.

Not: kaynak siteler korumali oynatici (embed) kullanir; bot dogrudan video
dosyasi indirmez/gondermez. Guvenilir izleme icin WATCH_BASE_URL sarttir.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Ayarlar
# ----------------------------------------------------------------------
BASE = "https://www.fullhdfilmizlesene.now"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": BASE + "/",
}
MAX_RESULTS_SHOWN = 10

__all__ = ["FilmBotService", "main"]


# ----------------------------------------------------------------------
# Site tarama katmani (api.py ile ayni mantik, tek dosyada)
# ----------------------------------------------------------------------
async def _fetch_html(path_or_url: str) -> str:
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    async with AsyncSession(impersonate="chrome120") as s:
        r = await s.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Siteye erisilemedi ({r.status_code}): {url}")
        return r.text


def _pick_img(img: Any) -> str | None:
    if not img:
        return None
    for attr in ("data-src", "data-srcset", "src", "srcset"):
        v = img.get(attr)
        if v and not v.startswith("data:image"):
            return v.split(",")[0].strip().split(" ")[0]
    return None


def _parse_cards(html: str) -> list[dict[str, Any]]:
    """Liste sayfalarindaki filmleri coz: baslik, afis, detay linki."""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict[str, Any]] = []
    for li in soup.select("li.film"):
        a = li.select_one("a.tt")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"/film/([^/]+)/?", href)
        if not m:
            continue
        slug = m.group(1)
        title_el = li.select_one(".film-title")
        title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True).replace(" izle", "")
        out.append({
            "title": title,
            "poster": _pick_img(li.select_one("img")),
            "url": f"{BASE}/film/{slug}/",
            "slug": slug,
        })
    return out


def _decode_scx_value(enc: str) -> str:
    """Sitedeki JS karsiligi: atob(rtt(enc)) -> gercek iframe URL."""
    try:
        rot = codecs.encode(enc.strip(), "rot_13")
        rot += "=" * (-len(rot) % 4)
        return base64.b64decode(rot).decode("utf-8", errors="ignore")
    except Exception:
        return enc


def _parse_sources(html: str) -> dict:
    m = re.search(r"var\s+scx\s*=\s*(\{.+?\});", html)
    if not m:
        return {}
    try:
        scx = json.loads(m.group(1))
    except Exception:
        return {}
    out: dict = {}
    for key, val in scx.items():
        try:
            label = base64.b64decode(val.get("tt", "")).decode("utf-8", errors="ignore")
        except Exception:
            label = key
        sx = val.get("sx", {}) or {}
        parts: dict = {}
        for kind in ("t", "p"):
            raw = sx.get(kind, [])
            items = enumerate(raw) if isinstance(raw, list) else (raw.items() if isinstance(raw, dict) else [])
            for k, enc in items:
                if isinstance(enc, str) and enc:
                    parts[f"{kind}:{k}"] = _decode_scx_value(enc)
        out[key] = {"label": label, "parts": parts}
    return out


async def site_search(query: str) -> list[dict[str, Any]]:
    q = re.sub(r"\s+", "+", query.strip())
    html = await _fetch_html(f"/arama/{quote(q, safe='+')}")
    return _parse_cards(html)


async def site_latest(page: int = 1) -> list[dict[str, Any]]:
    path = "/" if page == 1 else f"/yeni-filmler/{page}"
    return _parse_cards(await _fetch_html(path))


def _detail_path(slug_or_url: str) -> str:
    if slug_or_url.startswith(BASE):
        return slug_or_url[len(BASE):]
    if slug_or_url.startswith("http"):
        raise ValueError("Bu bot sadece fullhdfilmizlesene.now linklerini destekler")
    if slug_or_url.startswith("/film/"):
        return slug_or_url
    return f"/film/{slug_or_url.strip('/')}/"


async def site_movie_detail(slug_or_url: str) -> dict[str, Any]:
    path = _detail_path(slug_or_url)
    html = await _fetch_html(path)
    soup = BeautifulSoup(html, "lxml")

    h1 = soup.select_one("header.single-bas h1 a") or soup.select_one("h1 a")
    title = h1.get_text(strip=True) if h1 else None
    ozet = soup.select_one(".ozet-ic")
    description = ozet.get_text(" ", strip=True) if ozet else None
    imdb_el = soup.select_one(".imdb-ic span")
    imdb = imdb_el.get_text(strip=True) if imdb_el else None

    categories: list[str] = []
    for li in soup.select(".film-info li"):
        k_el = li.select_one(".dt")
        if k_el and k_el.get_text(strip=True) in ("Tür", "Kategori"):
            categories += [a.get_text(strip=True) for a in li.select("a")]

    poster = _pick_img(soup.select_one("section.detay-sol img"))
    sources = _parse_sources(html)
    iframe = None
    for src in sources.values():
        if src["parts"]:
            iframe = next(iter(src["parts"].values()))
            break

    return {
        "title": title,
        "slug": path.strip("/").split("/")[-1],
        "url": BASE + path,
        "description": description,
        "imdb": imdb,
        "categories": categories,
        "poster": poster,
        "iframe": iframe,
    }


# ----------------------------------------------------------------------
# Bot servisi
# ----------------------------------------------------------------------
class FilmBotService:
    """Siteyi dogrudan tarayan, tek dosyalik Telegram botu."""

    def __init__(
        self,
        token: str | None = None,
        channel_id: str | None = None,
        admin_ids: set[int] | str | None = None,
        publish_interval_min: int = 60,
        posted_file: str | Path = "posted.json",
        watch_base_url: str | None = None,
    ) -> None:
        resolved_token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        if not resolved_token:
            raise ValueError("TELEGRAM_BOT_TOKEN bulunamadi.")
        self._token = resolved_token
        self._channel_id = (channel_id if channel_id is not None else os.getenv("CHANNEL_ID", "")).strip()
        if isinstance(admin_ids, str) or admin_ids is None:
            raw = admin_ids if admin_ids is not None else os.getenv("ADMIN_IDS", "")
            self._admin_ids = {int(x) for x in raw.split(",") if x.strip().isdigit()}
        else:
            self._admin_ids = set(admin_ids)
        env_interval = os.getenv("PUBLISH_INTERVAL_MIN", "")
        self._publish_interval_min = publish_interval_min if not env_interval.strip().isdigit() else int(env_interval)
        posted_default = os.getenv("POSTED_FILE", "posted.json")
        self._posted_file = Path(posted_file if posted_file != "posted.json" else posted_default).expanduser()
        watch_default = os.getenv("WATCH_BASE_URL", "").strip()
        self._watch_base_url = (watch_base_url if watch_base_url is not None else watch_default).rstrip("/")

    def _watch_url(self, iframe_url: str) -> str:
        """Izleme linki: proxy varsa guvenilir yol, yoksa direkt link."""
        if self._watch_base_url:
            return f"{self._watch_base_url}/embed?url={quote(iframe_url, safe='')}"
        return iframe_url

    def _is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._admin_ids

    # -- formatlayicilar ------------------------------------------------
    @staticmethod
    def _format_search_results(query: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return f"🔍 *{query}* için sonuç bulunamadı."
        lines = [f"🔍 *{query}* — {len(results)} sonuç:\n"]
        for i, item in enumerate(results[:MAX_RESULTS_SHOWN], start=1):
            lines.append(f"{i}. *{item.get('title', 'Bilinmiyor')}*\n   `ID: {item.get('slug', '-')}`")
        lines.append("\nDetay için aşağıdaki butonlara dokunun.")
        return "\n".join(lines)

    @staticmethod
    def _format_movie_detail(detail: dict[str, Any]) -> str:
        title = detail.get("title", "Bilinmiyor")
        imdb = detail.get("imdb") or "-"
        categories = ", ".join(detail.get("categories", []) or []) or "-"
        desc = (detail.get("description") or "")[:600]
        text = f"🎬 *{title}*\n⭐ IMDB: {imdb}\n🏷️ Tür: {categories}\n\n{desc}"
        if detail.get("iframe"):
            text += "\n\n▶️ Kaynağı açmak için aşağıdaki butona dokunun."
        else:
            text += "\n\n⚠️ Bu içerik için oynatılabilir kaynak bulunamadı."
        return text

    def _build_detail_post(self, detail: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup | None]:
        text = self._format_movie_detail(detail)
        keyboard = None
        if detail.get("iframe"):
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("▶️ İzle", url=self._watch_url(str(detail["iframe"])))]]
            )
        return text, keyboard

    def _build_search_keyboard(self, results: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
        buttons: list[list[InlineKeyboardButton]] = []
        for item in results:
            slug = str(item.get("slug") or "").strip()
            title = str(item.get("title", slug))[:40]
            if slug:
                buttons.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"detail:{slug[:50]}")])
        return InlineKeyboardMarkup(buttons) if buttons else None

    # -- kanal yayini ---------------------------------------------------
    def _load_posted(self) -> set[str]:
        try:
            data = json.loads(self._posted_file.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except (OSError, ValueError):
            return set()

    def _save_posted(self, posted: set[str]) -> None:
        try:
            self._posted_file.write_text(json.dumps(sorted(posted), ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("posted dosyası yazılamadı.", exc_info=True)

    async def publish_latest(self, bot: Any, max_pages: int = 2) -> dict[str, int]:
        """Yeni filmleri kanala postala. Returns {"posted": n, "skipped": m}."""
        if not self._channel_id:
            raise ValueError("CHANNEL_ID ayarlı değil.")
        posted = self._load_posted()
        new_items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            try:
                cards = await site_latest(page)
            except Exception:
                logger.exception("publish: sayfa %s alınamadı", page)
                break
            for item in cards:
                slug = str(item.get("slug") or "").strip()
                if not slug or slug in posted or slug in seen:
                    continue
                seen.add(slug)
                new_items.append(item)

        sent = 0
        for item in new_items:
            slug = str(item.get("slug"))
            try:
                detail = await site_movie_detail(slug)
            except Exception:
                logger.exception("publish: detay alınamadı %s", slug)
                continue
            text, keyboard = self._build_detail_post(detail)
            try:
                if detail.get("poster"):
                    await bot.send_photo(
                        chat_id=self._channel_id, photo=detail["poster"],
                        caption=text[:1024], parse_mode="Markdown", reply_markup=keyboard,
                    )
                else:
                    await bot.send_message(
                        chat_id=self._channel_id, text=text[:4000],
                        parse_mode="Markdown", reply_markup=keyboard,
                    )
                posted.add(slug)
                sent += 1
            except Exception:
                logger.exception("publish: kanala gönderilemedi %s", slug)
            await asyncio.sleep(1)  # flood koruması
        self._save_posted(posted)
        return {"posted": sent, "skipped": len(seen) - sent}

    async def _publisher_loop(self, app: Application) -> None:
        await asyncio.sleep(10)
        while True:
            if self._channel_id:
                try:
                    result = await self.publish_latest(app.bot)
                    if result["posted"]:
                        logger.info("Otomatik paylaşım: %s film.", result["posted"])
                except Exception:
                    logger.exception("Otomatik paylaşım hatası")
            await asyncio.sleep(max(self._publish_interval_min, 1) * 60)

    # -- handler'lar ----------------------------------------------------
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message is not None
        await update.message.reply_text(
            "👋 Merhaba! Film botuna hoş geldin.\n\n"
            "• `/ara <film adı>` — arama yapar\n"
            "• Yeni filmler otomatik olarak kanalda paylaşılır\n\n"
            "Örnek: `/ara supergirl`",
            parse_mode="Markdown",
        )

    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message is not None
        query = " ".join(context.args or []).strip()
        if not query:
            await update.message.reply_text("Kullanım: `/ara <film adı>`", parse_mode="Markdown")
            return
        await update.message.reply_text(f"🔎 `{query}` aranıyor...", parse_mode="Markdown")
        try:
            results = await site_search(query)
        except Exception:
            logger.exception("Arama hatası")
            await update.message.reply_text("❌ Siteye erişilemedi, sonra tekrar dene.")
            return
        text = self._format_search_results(query, results)
        keyboard = self._build_search_keyboard(results[:MAX_RESULTS_SHOWN])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    async def publish_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """(Yönetici) /paylas -> yeni filmleri hemen kanala at."""
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
            await update.message.reply_text(f"❌ Paylaşım hatası: `{exc}`", parse_mode="Markdown")
            return
        await update.message.reply_text(f"✅ {result['posted']} film kanala paylaşıldı.")

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            detail = await site_movie_detail(slug)
        except Exception:
            logger.exception("Detay hatası")
            await query.edit_message_text("❌ Detay alınamadı.")
            return
        text, keyboard = self._build_detail_post(detail)
        try:
            if detail.get("poster") and update.effective_chat:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id, photo=detail["poster"],
                    caption=text[:1024], parse_mode="Markdown", reply_markup=keyboard,
                )
                return
        except Exception:
            logger.warning("Poster gönderilemedi.", exc_info=True)
        await query.edit_message_text(text[:4000], parse_mode="Markdown", reply_markup=keyboard)

    # -- kurulum ---------------------------------------------------------
    def build_app(self) -> Application:
        service = self

        async def _post_init(app: Application) -> None:
            app.create_task(service._publisher_loop(app))

        app = Application.builder().token(self._token).post_init(_post_init).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CommandHandler("ara", self.search_command))
        app.add_handler(CommandHandler("paylas", self.publish_command))
        app.add_handler(CallbackQueryHandler(self.button_callback))
        return app

    def run_polling(self) -> None:
        app = self.build_app()
        logger.info("Film botu başlatılıyor (kanal: %s)...", self._channel_id or "kapalı")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    try:
        service = FilmBotService()
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    service.run_polling()


if __name__ == "__main__":
    main()
