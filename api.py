"""FullHDFilmizlesene API - FastAPI + Uvicorn yapisi korunarak ozellestirildi."""
import asyncio
import base64
import codecs
import json
import re
import unicodedata
import uuid
from urllib.parse import quote

from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

BASE = "https://www.fullhdfilmizlesene.now"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": BASE + "/",
}

app = FastAPI(title="FullHDFilmizlesene API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _fetch_html(path_or_url: str) -> str:
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    async with AsyncSession(impersonate="chrome120") as s:
        r = await s.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"Siteye erisilemedi: {url}")
        return r.text


def _pick_img(img) -> str | None:
    if not img:
        return None
    for attr in ("data-src", "data-srcset", "src", "srcset"):
        v = img.get(attr)
        if v and not v.startswith("data:image"):
            return v.split(",")[0].strip().split(" ")[0]
    return None


def _parse_cards(html: str):
    """Liste sayfalarindaki filmleri coz: baslik, afis, detay linki."""
    soup = BeautifulSoup(html, "lxml")
    out = []
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
    out = {}
    for key, val in scx.items():
        try:
            label = base64.b64decode(val.get("tt", "")).decode("utf-8", errors="ignore")
        except Exception:
            label = key
        sx = val.get("sx", {}) or {}
        parts = {}
        for kind in ("t", "p"):
            raw = sx.get(kind, [])
            items = enumerate(raw) if isinstance(raw, list) else (raw.items() if isinstance(raw, dict) else [])
            for k, enc in items:
                if isinstance(enc, str) and enc:
                    parts[f"{kind}:{k}"] = _decode_scx_value(enc)
        out[key] = {"label": label, "parts": parts}
    return out





@app.get("/", response_class=HTMLResponse)
async def root():
    return """<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Film API</title>
<style>
:root{--bg:#0b0f1a;--panel:#141b2e;--card:#182136;--line:#263252;--txt:#eaf0ff;--mut:#93a0c0;--acc:#5b8cff;--acc2:#9d6bff;--ok:#37d67a;--err:#ff5d6c}
*{box-sizing:border-box}body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;background:radial-gradient(1000px 500px at 80% -10%,#223063 0%,transparent 60%),radial-gradient(900px 500px at 10% 0%,#3b1f5e 0%,transparent 55%),var(--bg);color:var(--txt);min-height:100vh}
header{position:sticky;top:0;z-index:5;backdrop-filter:blur(12px);background:rgba(11,15,26,.75);border-bottom:1px solid var(--line);padding:14px 22px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.brand{font-size:22px;font-weight:800;background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;background-clip:text;color:transparent}
.search{margin-left:auto;display:flex;gap:8px}
input{background:#0e1526;border:1px solid var(--line);color:var(--txt);padding:10px 14px;border-radius:12px;min-width:240px;outline:none}
input:focus{border-color:var(--acc)}
button{background:linear-gradient(135deg,var(--acc),var(--acc2));border:none;color:#fff;padding:10px 18px;border-radius:12px;font-weight:700;cursor:pointer;transition:transform .12s,box-shadow .12s}
button:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(91,140,255,.35)}
button.ghost{background:#1b2540;border:1px solid var(--line)}
main{max-width:1200px;margin:0 auto;padding:22px}
.toolbar{display:flex;gap:10px;align-items:center;margin:6px 0 14px}
#pg,#stat{color:var(--mut);font-size:13px}
#log{background:#0e1526;border:1px solid var(--line);border-radius:12px;padding:10px 14px;font-size:12px;color:var(--mut);white-space:pre-wrap;max-height:110px;overflow:auto;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;cursor:pointer;transition:transform .15s,box-shadow .15s,border-color .15s}
.card:hover{transform:translateY(-4px);border-color:var(--acc);box-shadow:0 12px 30px rgba(0,0,0,.45)}
.poster{position:relative;aspect-ratio:2/3;background:#0e1526;overflow:hidden}
.poster img{width:100%;height:100%;object-fit:cover;display:block}
.shade{position:absolute;inset:0;background:linear-gradient(180deg,transparent 55%,rgba(0,0,0,.75))}
.t{padding:10px 12px;font-size:13px;font-weight:600;min-height:52px}
.detail{display:flex;gap:16px;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:16px;margin:0 0 16px}
.detail.hidden{display:none}
.detail img{width:130px;border-radius:12px;object-fit:cover}
.badges{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.badge{background:#1b2540;border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:12px;color:var(--mut)}
.badge.hot{color:#ffd166;border-color:#5a4a1f}
.desc{color:var(--mut);font-size:13px;line-height:1.6;max-height:110px;overflow:auto}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.overlay{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(4,7,15,.7);backdrop-filter:blur(8px);opacity:0;pointer-events:none;transition:opacity .2s}
.overlay.open{opacity:1;pointer-events:auto}
.browser{width:min(960px,100%);background:#101728;border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:0 30px 80px rgba(0,0,0,.6);transform:translateY(12px) scale(.98);transition:transform .2s}
.overlay.open .browser{transform:none}
.bar{display:flex;align-items:center;gap:10px;padding:12px 14px;background:#0c1222;border-bottom:1px solid var(--line)}
.dot{width:12px;height:12px;border-radius:50%;flex-shrink:0}
.url{flex:1;background:#0a0f1e;border:1px solid var(--line);border-radius:999px;padding:6px 14px;font-size:12px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wtitle{font-size:13px;font-weight:700;max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.x{background:#2a1730;border:1px solid #5a2b3a;width:32px;height:32px;border-radius:10px;padding:0;font-size:14px;line-height:1;flex-shrink:0}
.screen{position:relative;background:#000}
.screen::before{content:'';display:block;padding-top:56.25%}
.screen iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.loading{color:var(--mut);padding:20px;text-align:center}
</style>
</head><body>
<header><div class="brand">Film API</div>
<div class="search"><input id="q" value="supergirl" placeholder="Film ara..."><button id="btnSearch">Ara</button></div></header>
<main>
<div class="toolbar"><button id="btnLatest">Son Filmler</button><button id="btnMore" class="ghost">Daha Fazla</button><span id="pg"></span><span id="stat"></span></div>
<div id="log">hazir...</div>
<div id="detail" class="detail hidden"><img id="dPoster" referrerpolicy="no-referrer" alt=""><div><h2 id="dTitle" style="margin:0"></h2><div class="badges" id="dBadges"></div><div class="desc" id="dDesc"></div><div class="row"><button id="btnWatch">▶ İzle</button><button id="btnDownload">⬇ İndir</button><button id="btnServerSave" class="ghost">Sunucuya Kaydet</button></div></div></div>
<div id="results" class="grid"></div>
<div class="overlay" id="ov"><div class="browser"><div class="bar"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span class="wtitle" id="wTitle"></span><button class="x" id="btnOvDownload" title="İndir">⬇</button><button class="x" id="btnClose">X</button></div><div class="screen"><iframe id="player" allowfullscreen allow="fullscreen; autoplay; picture-in-picture"></iframe></div></div></div>
</main>
<script>
var pg=1;
function log(m){var el=document.getElementById('log');el.textContent=m+'  |  '+el.textContent.slice(0,1500);}
function stat(m){document.getElementById('stat').textContent=m;}
function skeleton(n){var b=document.getElementById('results');b.innerHTML='';for(var i=0;i<n;i++){var d=document.createElement('div');d.className='card';d.innerHTML='<div class="poster"><div class="loading">yukleniyor...</div></div><div class="t">...</div>';b.appendChild(d);}}
async function loadLatest(p){pg=p||1;document.getElementById('pg').textContent='sayfa: '+pg;skeleton(6);stat('yukleniyor...');try{var r=await fetch('/latest?page='+pg);if(!r.ok)throw new Error('HTTP '+r.status);var j=await r.json();stat(j.count+' film');log('son filmler sayfa '+pg+': '+j.count);show(j.results||[]);}catch(e){stat('hata');log('HATA /latest: '+e.message);}}
async function moreFilms(){pg++;document.getElementById('pg').textContent='sayfa: '+pg;stat('yukleniyor...');try{var r=await fetch('/latest?page='+pg);if(!r.ok)throw new Error('HTTP '+r.status);var j=await r.json();stat(j.count+' film');log('sayfa '+pg+': '+j.count);appendCards(j.results||[]);}catch(e){pg--;log('HATA: '+e.message);}}
async function doSearch(){var q=document.getElementById('q').value;skeleton(4);stat('araniyor...');try{var r=await fetch('/search?query='+encodeURIComponent(q));if(!r.ok)throw new Error('HTTP '+r.status);var j=await r.json();stat(j.count+' sonuc');log('arama '+q+': '+j.count);show(j.results||[]);}catch(e){stat('hata');log('HATA arama: '+e.message);}}
function show(l){document.getElementById('results').innerHTML='';appendCards(l);}
function appendCards(l){var b=document.getElementById('results');l.forEach(function(f){var d=document.createElement('div');d.className='card';d.innerHTML=`<div class="poster"><img loading="lazy" referrerpolicy="no-referrer" src="${f.poster||''}" onerror="this.remove()"><div class="shade"></div></div><div class="t">${f.title||''}</div>`;d.onclick=function(){play(f);};b.appendChild(d);});}
async function play(f){var u=f.url||f.slug;stat('detay yukleniyor...');try{var r=await fetch('/movie-detail?url='+encodeURIComponent(u));if(!r.ok)throw new Error('HTTP '+r.status);var j=await r.json();log(j.title+' | imdb='+j.imdb);showDetail(j);if(j.iframe)openPlayer(j);stat('hazir');}catch(e){stat('hata');log('HATA detay: '+e.message);}}
var currentIframe=null,currentTitle='';
function openPlayer(j){currentIframe=j.iframe||null;currentTitle=j.title||'';var src='/embed?url='+encodeURIComponent(j.iframe);document.getElementById('player').src=src;document.getElementById('wTitle').textContent=j.title||'';document.getElementById('ov').classList.add('open');document.body.style.overflow='hidden';}
function closePlayer(){document.getElementById('ov').classList.remove('open');document.getElementById('player').src='';document.body.style.overflow='';}
async function downloadBrowser(){if(!currentIframe){log('HATA indirme: once bir film secin');return;}stat('indiriliyor...');try{var u='/download?url='+encodeURIComponent(currentIframe)+'&title='+encodeURIComponent(currentTitle||'film');var r=await fetch(u);if(!r.ok){var j=null;try{j=await r.json();}catch(e){}log('HATA indirme: '+(j&&j.detail?j.detail:'HTTP '+r.status));stat('hata');return;}var b=await r.blob();var a=document.createElement('a');a.href=URL.createObjectURL(b);var cd=r.headers.get('Content-Disposition')||'';var m=cd.match(/filename="([^"]+)"/);a.download=(m&&m[1])||((currentTitle||'film')+'.mp4');document.body.appendChild(a);a.click();a.remove();log('indirme baslatildi: '+a.download);stat('hazir');}catch(e){stat('hata');log('HATA indirme: '+e.message);}}
async function saveToServer(){if(!currentIframe){log('HATA kayit: once bir film secin');return;}stat('sunucuya kaydediliyor...');try{var r=await fetch('/server-download?url='+encodeURIComponent(currentIframe)+'&title='+encodeURIComponent(currentTitle||'film'));if(!r.ok)throw new Error('HTTP '+r.status);var j=await r.json();log('sunucu kayit job: '+j.job_id);pollJob(j.job_id);}catch(e){stat('hata');log('HATA kayit: '+e.message);}}
async function pollJob(id){try{var r=await fetch('/download-status/'+id);var j=await r.json();if(j.status==='done'){stat('kayit tamam');log('kayit tamam: '+j.file);}else if(j.status==='error'){stat('hata');log('HATA kayit: '+j.error);}else{stat('kaydediliyor %'+(j.percent||0));setTimeout(function(){pollJob(id);},1500);}}catch(e){log('HATA durum: '+e.message);}}
function showDetail(j){if(j.iframe){currentIframe=j.iframe;currentTitle=j.title||'';}var box=document.getElementById('detail');box.classList.remove('hidden');document.getElementById('dPoster').src=j.poster||'';document.getElementById('dTitle').textContent=j.title||'';document.getElementById('dDesc').textContent=j.description||'';var bg=document.getElementById('dBadges');bg.innerHTML='';(j.categories||[]).forEach(function(c){var s=document.createElement('span');s.className='badge';s.textContent=c;bg.appendChild(s);});if(j.imdb){var s=document.createElement('span');s.className='badge hot';s.textContent='IMDB '+j.imdb;bg.appendChild(s);}box.scrollIntoView({behavior:'smooth',block:'nearest'});}
document.getElementById('btnLatest').onclick=function(){loadLatest(1);};
document.getElementById('btnMore').onclick=moreFilms;
document.getElementById('btnSearch').onclick=doSearch;
document.getElementById('btnClose').onclick=closePlayer;
document.getElementById('btnDownload').onclick=downloadBrowser;
document.getElementById('btnOvDownload').onclick=downloadBrowser;
document.getElementById('btnServerSave').onclick=saveToServer;
document.getElementById('btnWatch').onclick=function(){if(currentIframe)openPlayer({iframe:currentIframe,title:currentTitle});};
document.getElementById('ov').addEventListener('click',function(e){if(e.target===this)closePlayer();});
document.addEventListener('keydown',function(e){if(e.key==='Escape')closePlayer();});
document.getElementById('q').addEventListener('keydown',function(e){if(e.key==='Enter')doSearch();});
loadLatest(1);
</script></body></html>"""


@app.get("/status")
async def status():
    return {
        "status": "ok",
        "site": BASE,
        "endpoints": ["/latest?page=1", "/search?query=...", "/movie-detail?url=...", "/liste?url=...", "/embed?url=...", "/download?url=...", "/server-download?url=...", "/download-status/{job_id}", "/downloads"],
    }


@app.get("/latest")
async def latest(page: int = Query(1, ge=1, description="Sayfa numarasi")):
    path = "/" if page == 1 else f"/yeni-filmler/{page}"
    html = await _fetch_html(path)
    cards = _parse_cards(html)
    return {"page": page, "count": len(cards), "results": cards}


@app.get("/liste")
async def liste(url: str = Query(..., description="Site ici yol, orn: /filmizle/korku-filmleri veya /yil/2025-filmler-izle")):
    path = url[len(BASE):] if url.startswith(BASE) else url
    if not path.startswith("/"):
        path = "/" + path
    html = await _fetch_html(path)
    cards = _parse_cards(html)
    return {"source_url": BASE + path, "count": len(cards), "results": cards}


@app.get("/search")
async def search(query: str = Query(..., min_length=2, description="Film adi")):
    q = re.sub(r"\s+", "+", query.strip())
    html = await _fetch_html(f"/arama/{quote(q, safe='+')}")
    cards = _parse_cards(html)
    return {"query": query, "count": len(cards), "results": cards}


@app.get("/movie-detail")
async def movie_detail(url: str = Query(..., description="Film detay URL'si veya slug")):
    # slug da kabul et: "supergirl" -> "/film/supergirl/"
    if url.startswith(BASE):
        path = url[len(BASE):]
    elif url.startswith("http"):
        raise HTTPException(status_code=400, detail="Bu API sadece fullhdfilmizlesene.now linklerini destekler")
    elif url.startswith("/film/"):
        path = url
    else:
        path = f"/film/{url.strip('/')}/"

    html = await _fetch_html(path)
    soup = BeautifulSoup(html, "lxml")

    h1 = soup.select_one("header.single-bas h1 a") or soup.select_one("h1 a")
    title = h1.get_text(strip=True) if h1 else None

    ozet = soup.select_one(".ozet-ic")
    description = ozet.get_text(" ", strip=True) if ozet else None

    imdb_el = soup.select_one(".imdb-ic span")
    imdb = imdb_el.get_text(strip=True) if imdb_el else None

    categories = []
    for li in soup.select(".film-info li"):
        k_el = li.select_one(".dt")
        if k_el and k_el.get_text(strip=True) in ("Tür", "Kategori"):
            categories += [a.get_text(strip=True) for a in li.select("a")]

    poster = _pick_img(soup.select_one("section.detay-sol img"))

    sources = _parse_sources(html)
    # ilk uygun iframe'i sec
    iframe = None
    for src in sources.values():
        if src["parts"]:
            iframe = next(iter(src["parts"].values()))
            break

    return {
        "title": title,
        "url": BASE + path,
        "description": description,
        "imdb": imdb,
        "categories": categories,
        "poster": poster,
        "iframe": iframe,
        "sources": sources,
    }


ALLOWED_EMBED_HOSTS = ("rapidvid.net", "rapidvid.org", "imgz.me", "turbo.imgz.me")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Sunucuya-kaydet işleri: job_id -> {status, file, total, done, error}
DOWNLOAD_JOBS: dict = {}


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", (name or "").strip(), flags=re.UNICODE)
    return (cleaned or "indirme")[:80]


@app.get("/embed", response_class=HTMLResponse)
async def embed(url: str = Query(..., description="Player iframe URL")):
    """Rapidvid 403'u asmak icin: backend dogru Referer ile ceker, tarayiciya ayni origin'den sunar."""
    from urllib.parse import urlparse

    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Gecersiz URL")
    host = urlparse(url).netloc
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_EMBED_HOSTS):
        raise HTTPException(status_code=400, detail="Bu host proxy'ye kapali")
    async with AsyncSession(impersonate="chrome120") as s:
        r = await s.get(url, headers={**HEADERS, "Referer": BASE + "/"}, timeout=30)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Player alinamadi")
        html = r.text
    origin = f"{urlparse(url).scheme}://{host}/"
    if "<head" in html:
        html = html.replace("<head", f'<base href="{origin}"><head', 1)
    return HTMLResponse(content=html)


async def _fetch_binary(url: str):
    """Embed/kaynak URL'yi backend Referer ile cek (403'u asmak icin)."""
    async with AsyncSession(impersonate="chrome120") as s:
        r = await s.get(url, headers={**HEADERS, "Referer": BASE + "/"}, timeout=60)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Kaynak alinamadi")
        return r.content, r.headers.get("content-type", "application/octet-stream")


@app.get("/download")
async def download(
    url: str = Query(..., description="Indirilecek kaynak URL (movie-detail -> iframe)"),
    title: str = Query("film", description="Dosya adi icin baslik"),
):
    """Tarayiciya dosya olarak indir: /download?url=...&title=..."""
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Gecersiz URL")
    content, content_type = await _fetch_binary(url)
    if "text/html" in content_type:
        # Embed player sayfasi: video dosyasi degil, tarayicida anlik cozulen korumali yayin.
        raise HTTPException(
            status_code=422,
            detail="Bu kaynak dogrudan indirilemez (oynatici sayfasi, video dosyasi degil). "
            "Izlemek icin player'i kullanin veya tarayicida HLS yakalama eklentisiyle indirin.",
        )
    filename = _safe_filename(title)
    ext = ".mp4" if "video" in content_type or "octet" in content_type else ".html"
    # Header'lar latin-1 disina izin vermez: Türkçe karakterleri ASCII'ye indirge.
    ascii_name = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii") or "film"
    )
    quoted = quote(f"{filename}{ext}")
    return StreamingResponse(
        iter([content]),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}{ext}"; filename*=UTF-8\'\'{quoted}'
        },
    )


async def _server_download_job(job_id: str, url: str, filename: str) -> None:
    """Arka plan: kaynagi downloads/ klasorune indir, ilerlemeyi DOWNLOAD_JOBS'a yaz."""
    job = DOWNLOAD_JOBS[job_id]
    dest = DOWNLOAD_DIR / filename
    try:
        async with AsyncSession(impersonate="chrome120") as s:
            r = await s.get(url, headers={**HEADERS, "Referer": BASE + "/"}, timeout=60, stream=True)
            if r.status_code != 200:
                job.update(status="error", error=f"HTTP {r.status_code}")
                return
            content_type = r.headers.get("content-type", "")
            if "text/html" in content_type:
                job.update(
                    status="error",
                    error="Bu kaynak dogrudan indirilemez (oynatici sayfasi). Player'dan izleyin.",
                )
                return
            total = int(r.headers.get("content-length", 0) or 0)
            job["total"] = total
            done = 0
            with dest.open("wb") as f:
                async for chunk in r.aiter_content(chunk_size=1024 * 64):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    job["done"] = done
        job.update(status="done", file=str(dest.resolve()), done=done)
    except Exception as exc:  # noqa: BLE001 - hata mesaji job'a yansitilir
        try:
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink()
        except OSError:
            pass
        job.update(status="error", error=str(exc))


@app.get("/server-download")
async def server_download(
    url: str = Query(..., description="Indirilecek kaynak URL"),
    title: str = Query("film", description="Dosya adi icin baslik"),
):
    """Sunucu diskine arka planda kaydet, job_id don. Durum: /download-status/{job_id}."""
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Gecersiz URL")
    job_id = uuid.uuid4().hex[:12]
    DOWNLOAD_JOBS[job_id] = {"status": "running", "total": 0, "done": 0, "file": None, "error": None}
    filename = _safe_filename(title) + ".mp4"
    asyncio.create_task(_server_download_job(job_id, url, filename))
    return {"job_id": job_id, "file": filename, "status_url": f"/download-status/{job_id}"}


@app.get("/download-status/{job_id}")
async def download_status(job_id: str):
    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job bulunamadi")
    total, done = job.get("total", 0) or 0, job.get("done", 0) or 0
    percent = round((done / total) * 100, 1) if total else 0
    return {**job, "percent": percent}


@app.get("/downloads")
async def list_downloads():
    files = sorted(
        ({"name": p.name, "size": p.stat().st_size} for p in DOWNLOAD_DIR.glob("*") if p.is_file()),
        key=lambda x: x["name"],
    )
    return {"dir": str(DOWNLOAD_DIR.resolve()), "count": len(files), "files": files}
