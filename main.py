"""
Media Search Backend v3
نشر على Railway/Render — بدون Webhook أو Scheduler
المزامنة يدوية بضغطة زر
"""

import base64, hashlib, json, logging, os, re, sqlite3
from datetime import datetime
from typing import Optional

import dropbox, uvicorn
from dropbox.files import FileMetadata
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("media")

# ── إعدادات من متغيرات البيئة (Railway/Render) ──────────────────────────────
DB_PATH = os.getenv("DB_PATH", "media_library.db")
PORT    = int(os.getenv("PORT", 8000))

# التوكن يمكن حفظه كمتغير بيئة بدلاً من DB (اختياري، أكثر أماناً)
ENV_TOKEN   = os.getenv("DROPBOX_TOKEN", "")
ENV_FOLDERS = os.getenv("DROPBOX_FOLDERS", "")  # مجلدات مفصولة بفاصلة: /Photos,/Videos


# ── مرادفات البحث ─────────────────────────────────────────────────────────────
SYNONYM_GROUPS = [
    {"جوال","هاتف","تلفون","موبايل","ايفون","آيفون","سامسونج","phone","mobile","iphone","smartphone","android"},
    {"كمبيوتر","حاسوب","لابتوب","جهاز","laptop","computer","pc","mac","notebook","macbook"},
    {"تابلت","آيباد","ipad","tablet","لوح"},
    {"شاشة","monitor","screen","display","تلفزيون","تلفاز","tv"},
    {"سماعة","headphone","earphone","airpods","سماعات","earbuds"},
    {"كاميرا","camera","تصوير","عدسة","lens","dslr"},
    {"شخص","رجل","human","person","people","man","male"},
    {"امرأة","بنت","woman","girl","female","lady"},
    {"طفل","ولد","أطفال","child","kid","baby","infant","children"},
    {"مجموعة","فريق","group","team","crowd"},
    {"بحر","شاطئ","ساحل","محيط","sea","ocean","beach","coast","shore","waves"},
    {"جبل","تل","mountain","hill","peak","قمة","summit"},
    {"صحراء","رمال","desert","sand","dunes","كثبان"},
    {"غابة","أشجار","حديقة","park","forest","trees","woods","garden","nature","طبيعة"},
    {"مدينة","شارع","city","town","urban","street","road","building","عمارة"},
    {"سماء","غيوم","sky","clouds","sunrise","sunset","شروق","غروب","dawn","dusk"},
    {"نهر","بحيرة","river","lake","waterfall","شلال","water","ماء"},
    {"طعام","أكل","وجبة","food","meal","dish","cuisine","مطبخ","restaurant","مطعم"},
    {"قهوة","كافيه","coffee","espresso","latte","cappuccino","cafe"},
    {"شاي","tea","مشروب","drink","beverage"},
    {"حلويات","كيك","cake","dessert","sweet","chocolate","شوكولاتة","pastry"},
    {"اجتماع","meeting","conference","مؤتمر","لقاء","presentation","عرض"},
    {"مكتب","عمل","office","work","business","desk","workplace","corporate"},
    {"سيارة","عربية","car","vehicle","automobile","مركبة","auto","truck","شاحنة"},
    {"طيارة","طائرة","airplane","plane","flight","مطار","airport","aviation"},
    {"سفينة","قارب","ship","boat","yacht","بحرية","sailing"},
    {"رياضة","تمرين","sport","fitness","exercise","gym","جيم","workout"},
    {"كرة","football","soccer","basketball","tennis","كرة قدم","كرة سلة"},
    {"أحمر","red","قرمزي","crimson","scarlet"},
    {"أزرق","blue","كحلي","navy","سماوي","azure"},
    {"أخضر","green","زيتي","olive","emerald"},
    {"أصفر","yellow","ذهبي","gold","golden"},
    {"أبيض","white","فاتح","light","cream"},
    {"أسود","black","داكن","dark","charcoal"},
    {"فيديو","مقطع","video","clip","reel","movie","film","footage"},
    {"صورة","photo","image","pic","img","photograph","portrait"},
    {"لوغو","شعار","logo","brand","icon","identity"},
    {"بانر","إعلان","banner","ad","advertisement","poster","flyer"},
    {"عرس","زفاف","wedding","marriage","حفل","bride","groom","عروس"},
    {"عيد","ميلاد","birthday","celebration","party","احتفال","anniversary"},
    {"رياض","الرياض","riyadh","ksa","saudi","سعودية"},
    {"جدة","jeddah","makkah","مكة","medina","المدينة"},
    {"دبي","dubai","uae","الإمارات","emirates"},
]

def _build_idx():
    idx = {}
    for grp in SYNONYM_GROUPS:
        for term in grp:
            t = term.lower()
            idx.setdefault(t, set()).update(g.lower() for g in grp)
    return idx

_IDX = _build_idx()

def expand(q: str) -> list[str]:
    q = q.lower().strip()
    terms = {q}
    for key, syns in _IDX.items():
        if q == key or q in key or key in q:
            terms.update(syns)
    return list(terms)


# ── قاعدة البيانات ────────────────────────────────────────────────────────────
def init_db():
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS media_items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            dropbox_path  TEXT UNIQUE NOT NULL,
            name          TEXT NOT NULL,
            name_clean    TEXT NOT NULL,
            file_type     TEXT NOT NULL,
            extension     TEXT,
            size          INTEGER,
            modified      TEXT,
            dropbox_link  TEXT,
            thumbnail_b64 TEXT,
            folder_path   TEXT,
            content_hash  TEXT,
            indexed_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_nc ON media_items(name_clean COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_ft ON media_items(file_type);
        CREATE INDEX IF NOT EXISTS idx_fp ON media_items(folder_path);

        CREATE TABLE IF NOT EXISTS app_config (
            k TEXT PRIMARY KEY,
            v TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            synced_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            items_added   INTEGER DEFAULT 0,
            items_updated INTEGER DEFAULT 0,
            items_removed INTEGER DEFAULT 0,
            duration_sec  REAL,
            status        TEXT,
            message       TEXT
        );
    """)
    # إذا وُجد توكن/مجلدات في متغيرات البيئة، اكتبها في DB
    if ENV_TOKEN:
        c.execute("INSERT OR REPLACE INTO app_config(k,v) VALUES('token',?)", (ENV_TOKEN,))
    if ENV_FOLDERS:
        folders = [f.strip() for f in ENV_FOLDERS.split(",") if f.strip()]
        c.execute("INSERT OR REPLACE INTO app_config(k,v) VALUES('folders',?)",
                  (json.dumps(folders),))
    c.commit(); c.close()
    log.info("✅ DB ready: %s", DB_PATH)

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def cfg_get(k: str) -> Optional[str]:
    c = db()
    r = c.execute("SELECT v FROM app_config WHERE k=?", (k,)).fetchone()
    c.close()
    return r["v"] if r else None

def cfg_set(k: str, v: str):
    c = db()
    c.execute("INSERT OR REPLACE INTO app_config(k,v) VALUES(?,?)", (k, v))
    c.commit(); c.close()


# ── Dropbox Helpers ───────────────────────────────────────────────────────────
IMG = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.tiff','.heic','.avif','.svg'}
VID = {'.mp4','.mov','.avi','.mkv','.webm','.m4v','.wmv','.flv','.3gp','.mts'}

def classify(name):
    e = os.path.splitext(name)[1].lower()
    if e in IMG: return "image", e
    if e in VID: return "video", e
    return None, e

def clean(name):
    n = os.path.splitext(name)[0]
    return re.sub(r'\s+',' ', re.sub(r'[_\-\.]+',' ', n)).strip()

def get_link(dbx, path):
    try:
        links = dbx.sharing_list_shared_links(path=path, direct_only=True).links
        if links:
            return links[0].url.replace("?dl=0","?raw=1")
        s = dropbox.sharing.SharedLinkSettings(
            requested_visibility=dropbox.sharing.RequestedVisibility.public)
        return dbx.sharing_create_shared_link_with_settings(path, s).url.replace("?dl=0","?raw=1")
    except Exception as e:
        log.warning("link %s: %s", path, e); return None

def get_thumb(dbx, path, ftype):
    if ftype != "image": return None
    try:
        _, r = dbx.files_get_thumbnail_v2(
            dropbox.files.PathOrLink.path(path),
            format=dropbox.files.ThumbnailFormat.jpeg,
            size=dropbox.files.ThumbnailSize.w256h256,
            mode=dropbox.files.ThumbnailMode.fitone_bestfit)
        return "data:image/jpeg;base64," + base64.b64encode(r.content).decode()
    except: return None


# ── المزامنة ──────────────────────────────────────────────────────────────────
_running = False

def do_sync(token: str, folders: list[str]):
    global _running
    if _running: return {"skipped": True}
    _running = True
    t0 = datetime.utcnow()
    stats = dict(added=0, updated=0, removed=0)
    conn = db()
    try:
        dbx = dropbox.Dropbox(token)
        dbx.users_get_current_account()

        old = {r["dropbox_path"]: r["content_hash"]
               for r in conn.execute("SELECT dropbox_path,content_hash FROM media_items")}
        seen = set()

        for folder in folders:
            log.info("📂 %s", folder)
            try:
                res = dbx.files_list_folder(folder, recursive=True)
                while True:
                    for e in res.entries:
                        if not isinstance(e, FileMetadata): continue
                        ft, ext = classify(e.name)
                        if not ft: continue
                        seen.add(e.path_lower)
                        if old.get(e.path_lower) == e.content_hash: continue
                        link  = get_link(dbx, e.path_lower)
                        thumb = get_thumb(dbx, e.path_lower, ft)
                        nc    = clean(e.name)
                        fp    = os.path.dirname(e.path_lower)
                        c = conn.cursor()
                        if e.path_lower not in old:
                            c.execute("""INSERT INTO media_items
                                (dropbox_path,name,name_clean,file_type,extension,
                                 size,modified,dropbox_link,thumbnail_b64,folder_path,content_hash)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                                (e.path_lower,e.name,nc,ft,ext,e.size,
                                 str(e.server_modified),link,thumb,fp,e.content_hash))
                            stats["added"] += 1
                        else:
                            c.execute("""UPDATE media_items SET
                                name=?,name_clean=?,file_type=?,extension=?,size=?,modified=?,
                                dropbox_link=?,thumbnail_b64=?,folder_path=?,content_hash=?,
                                indexed_at=CURRENT_TIMESTAMP WHERE dropbox_path=?""",
                                (e.name,nc,ft,ext,e.size,str(e.server_modified),
                                 link,thumb,fp,e.content_hash,e.path_lower))
                            stats["updated"] += 1
                    if not res.has_more: break
                    res = dbx.files_list_folder_continue(res.cursor)
            except Exception as ex:
                log.error("folder %s: %s", folder, ex)

        deleted = set(old) - seen
        if deleted:
            conn.executemany("DELETE FROM media_items WHERE dropbox_path=?",
                             [(p,) for p in deleted])
            stats["removed"] = len(deleted)

        dur = (datetime.utcnow()-t0).total_seconds()
        conn.execute("""INSERT INTO sync_log
            (items_added,items_updated,items_removed,duration_sec,status,message)
            VALUES(?,?,?,?,'success','مزامنة ناجحة')""",
            (stats["added"],stats["updated"],stats["removed"],dur))
        conn.execute("INSERT OR REPLACE INTO app_config(k,v) VALUES('last_sync',?)",
                     (datetime.utcnow().isoformat(),))
        conn.commit()
        log.info("✅ %.1fs +%d ~%d -%d", dur, stats["added"],stats["updated"],stats["removed"])
        return {**stats, "duration_sec": round(dur,1)}
    except Exception as ex:
        conn.execute("INSERT INTO sync_log(status,message) VALUES('error',?)", (str(ex),))
        conn.commit(); raise
    finally:
        conn.close(); _running = False


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="Media Search", version="3.0")
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def startup(): init_db()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status":"ok","version":"3.0","sync_running":_running}

@app.get("/api/config")
def get_config():
    token   = cfg_get("token") or ""
    folders = json.loads(cfg_get("folders") or "[]")
    ls      = cfg_get("last_sync")
    ah      = cfg_get("auto_sync_hours") or "6"
    return {
        "configured": bool(token and folders),
        "has_token":  bool(token),
        "folders":    folders,
        "last_sync":  ls,
        "auto_sync_hours": int(ah),
    }

@app.post("/api/config")
def save_config(body: dict):
    token   = body.get("token","").strip()
    folders = body.get("folders",[])
    if not token: raise HTTPException(400,"Access Token مطلوب")
    if not folders: raise HTTPException(400,"أضف مجلداً واحداً على الأقل")
    try:
        dbx = dropbox.Dropbox(token)
        acc = dbx.users_get_current_account()
        log.info("Connected: %s", acc.email)
    except Exception as e:
        raise HTTPException(400, f"توكن غير صحيح: {e}")
    cfg_set("token", token)
    cfg_set("folders", json.dumps(folders))
    return {"success": True}

@app.post("/api/sync")
def trigger_sync(background_tasks: BackgroundTasks):
    token   = cfg_get("token")
    folders = json.loads(cfg_get("folders") or "[]")
    if not token: raise HTTPException(400,"يرجى إعداد Dropbox أولاً")
    if _running:  return {"message":"مزامنة جارية بالفعل"}
    background_tasks.add_task(do_sync, token, folders)
    return {"success":True,"message":"بدأت المزامنة"}

@app.get("/api/sync/status")
def sync_status():
    conn = db()
    s = conn.execute("""SELECT COUNT(*) total,
        SUM(file_type='image') images, SUM(file_type='video') videos,
        COUNT(DISTINCT folder_path) folders, SUM(size) total_bytes
        FROM media_items""").fetchone()
    last_ok  = conn.execute(
        "SELECT * FROM sync_log WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
    last_err = conn.execute(
        "SELECT * FROM sync_log WHERE status='error'   ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return {
        "running":      _running,
        "total":        s["total"] or 0,
        "images":       s["images"] or 0,
        "videos":       s["videos"] or 0,
        "folders":      s["folders"] or 0,
        "total_bytes":  s["total_bytes"] or 0,
        "last_sync":    cfg_get("last_sync"),
        "last_success": dict(last_ok)  if last_ok  else None,
        "last_error":   dict(last_err) if last_err else None,
    }

@app.get("/api/search")
def search(
    q:      str           = Query(...),
    type:   Optional[str] = Query(None),
    limit:  int           = Query(60, le=200),
    offset: int           = Query(0),
):
    q = q.strip()
    if not q: raise HTTPException(400,"كلمة البحث مطلوبة")
    terms = expand(q)
    conds, params = [], []
    for t in terms:
        conds.append("(LOWER(name_clean) LIKE ? OR LOWER(name) LIKE ?)")
        params += [f"%{t}%", f"%{t}%"]
    where = "(" + " OR ".join(conds) + ")"
    if type in ("image","video"):
        where += " AND file_type=?"; params.append(type)
    conn = db()
    rows  = conn.execute(
        f"SELECT id,dropbox_path,name,name_clean,file_type,extension,"
        f"size,modified,dropbox_link,thumbnail_b64,folder_path "
        f"FROM media_items WHERE {where} ORDER BY modified DESC LIMIT ? OFFSET ?",
        params+[limit, offset]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) c FROM media_items WHERE {where}", params).fetchone()["c"]
    conn.close()
    return {
        "query":      q,
        "terms_used": terms[:8],
        "total":      total,
        "offset":     offset,
        "results": [{**dict(r), "is_video": r["file_type"]=="video"} for r in rows],
    }

@app.get("/api/stats")
def stats():
    conn = db()
    r = conn.execute("""SELECT COUNT(*) total,
        SUM(file_type='image') images, SUM(file_type='video') videos,
        COUNT(DISTINCT folder_path) folders FROM media_items""").fetchone()
    conn.close()
    return dict(r)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
