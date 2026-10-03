# -*- coding: utf-8 -*-
"""
Веб-приложение «Разрешение под экран» (тема 29)

FastAPI-сервер: отдаёт веб-интерфейс и REST-API для загрузки изображений,
предпросмотра «до/после», пакетной обработки и скачивания результата

"""
from __future__ import annotations

import io
import os
import re
import base64
import shutil
import zipfile
import secrets
from dataclasses import asdict

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import processing as P

#  Пути и хранилище сессий
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATASETS_DIR = os.path.join(os.path.dirname(BASE_DIR), "datasets")
# Рабочая директория сессий (в контейнере — /tmp, перезаписывается ENV)
WORK_DIR = os.environ.get("RESFIT_WORKDIR", "/tmp/resfit_sessions")
os.makedirs(WORK_DIR, exist_ok=True)

MAX_FILES = 200
MAX_FILE_MB = 40

# Каждой вкладке браузера своя сессия
SESSIONS: dict[str, dict] = {}

app = FastAPI(title="Разрешение под экран", version="1.0")


def _safe_name(name: str) -> str:
    """Безопасное имя файла (для хранения на диске)"""
    name = os.path.basename(name or "image")
    name = re.sub(r"[^\w.\-() ]+", "_", name, flags=re.UNICODE).strip()
    return name or "image"


def _session_dir(sid: str) -> str:
    return os.path.join(WORK_DIR, sid)


def _new_session() -> str:
    sid = secrets.token_hex(8)
    src = os.path.join(_session_dir(sid), "src")
    out = os.path.join(_session_dir(sid), "out")
    os.makedirs(src, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    SESSIONS[sid] = {"files": []}
    return sid


def _get_session(sid: str) -> dict:
    if not sid or sid not in SESSIONS:
        raise HTTPException(status_code=404, detail="Сессия не найдена. Загрузите изображения заново")
    return SESSIONS[sid]


def _register_file(sid: str, stored_path: str, display_name: str) -> dict | None:
    """Проанализировать изображение и добавить его в список файлов сессии"""
    try:
        im = P.open_image(stored_path)
        w, h = im.width, im.height
        fmt = (im.format or "").upper()
    except Exception:
        os.remove(stored_path)
        return None
    scale, nw, nh = P.compute_fit(w, h)
    fid = secrets.token_hex(6)
    entry = {
        "id": fid, "name": display_name, "path": stored_path,
        "w": w, "h": h, "bytes": os.path.getsize(stored_path), "format": fmt,
        "fit_scale": round(scale, 3),
    }
    SESSIONS[sid]["files"].append(entry)
    return entry


def _public_file(e: dict) -> dict:
    """Убрать из записи внутренний путь перед отправкой клиенту"""
    return {k: v for k, v in e.items() if k != "path"}


def _find_file(sess: dict, fid: str) -> dict:
    for e in sess["files"]:
        if e["id"] == fid:
            return e
    raise HTTPException(status_code=404, detail="Файл не найден в сессии")


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


#  Веб-страница
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
def health():
    return {"status": "ok"}


#  API: загрузка
@app.post("/api/upload")
async def upload(sid: str = Form(None), files: list[UploadFile] = File(...)):
    """Загрузить файлы"""
    if not sid or sid not in SESSIONS:
        sid = _new_session()
    src_dir = os.path.join(_session_dir(sid), "src")
    added = []
    for uf in files:
        ext = os.path.splitext(uf.filename or "")[1].lower()
        if ext not in P.ALLOWED_EXT:
            continue
        if len(SESSIONS[sid]["files"]) >= MAX_FILES:
            break
        raw = await uf.read()
        if len(raw) > MAX_FILE_MB * 1024 * 1024:
            continue
        stored = os.path.join(src_dir, f"{secrets.token_hex(6)}_{_safe_name(uf.filename)}")
        with open(stored, "wb") as w:
            w.write(raw)
        entry = _register_file(sid, stored, os.path.basename(uf.filename or "image"))
        if entry:
            added.append(_public_file(entry))
    if not added:
        raise HTTPException(status_code=400, detail="Не найдено подходящих изображений (jpg, png, webp, bmp, gif, tiff)")
    return {"sid": sid, "added": added, "files": [_public_file(e) for e in SESSIONS[sid]["files"]]}


@app.post("/api/samples")
def load_samples(sid: str = Form(None)):
    """Загрузить в сессию встроенный тестовый датасет (6 изображений)"""
    if not sid or sid not in SESSIONS:
        sid = _new_session()
    src_dir = os.path.join(_session_dir(sid), "src")
    added = []
    if not os.path.isdir(DATASETS_DIR):
        raise HTTPException(status_code=500, detail="Папка datasets не найдена в образе")
    for fname in sorted(os.listdir(DATASETS_DIR)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in P.ALLOWED_EXT:
            continue
        stored = os.path.join(src_dir, f"{secrets.token_hex(6)}_{_safe_name(fname)}")
        shutil.copyfile(os.path.join(DATASETS_DIR, fname), stored)
        entry = _register_file(sid, stored, fname)
        if entry:
            added.append(_public_file(entry))
    return {"sid": sid, "added": added, "files": [_public_file(e) for e in SESSIONS[sid]["files"]]}


@app.post("/api/reset")
def reset(sid: str = Form(...)):
    """Очистить сессию (удалить загруженные и результаты)"""
    if sid in SESSIONS:
        shutil.rmtree(_session_dir(sid), ignore_errors=True)
        del SESSIONS[sid]
    return {"ok": True}


#  API: миниатюра
@app.get("/api/thumb")
def thumb(sid: str, id: str):
    sess = _get_session(sid)
    e = _find_file(sess, id)
    im = P.open_image(e["path"]).convert("RGB")
    im.thumbnail((360, 360), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=80, method=4)
    return Response(content=buf.getvalue(), media_type="image/webp")


#  API: предпросмотр «до/после»
@app.post("/api/preview")
def preview(payload: dict = Body(...)):
    sid = payload.get("sid")
    fid = payload.get("id")
    sess = _get_session(sid)
    e = _find_file(sess, fid)
    s = P.Settings.from_dict(payload.get("settings", {}))

    src = P.open_image(e["path"]).convert("RGB")
    scale, nw, nh = P.compute_fit(src.width, src.height)

    before = src.copy()
    before.thumbnail((960, 960), Image.LANCZOS)
    bbuf = io.BytesIO(); before.save(bbuf, format="WEBP", quality=82, method=4)
    before_url = _data_url(bbuf.getvalue(), "image/webp")

    rejected = scale >= s.reject_threshold
    result = {
        "id": fid, "name": e["name"], "src_w": src.width, "src_h": src.height,
        "scale": round(scale, 3), "rejected": rejected,
        "reject_threshold": s.reject_threshold, "before": before_url,
    }
    if rejected:
        result["after"] = None
        result["reason"] = (f"требуется увеличение ×{scale:.2f} ≥ порога {s.reject_threshold:.2f} - "
                            f"файл был бы отбракован")
        return result

    frame, scale, (nw, nh) = P.render_frame(src, s)
    after_bytes = P.preview_bytes(frame, s, max_w=960)
    mime = "image/jpeg" if s.out_format == "jpeg" else "image/webp"
    result["after"] = _data_url(after_bytes, mime)
    result["reason"] = ""
    result["placed"] = {"w": nw, "h": nh}
    return result


#  API: пакетная обработка
@app.post("/api/process")
def process(payload: dict = Body(...)):
    sid = payload.get("sid")
    sess = _get_session(sid)
    if not sess["files"]:
        raise HTTPException(status_code=400, detail="Нет загруженных изображений")
    s = P.Settings.from_dict(payload.get("settings", {}))

    out_dir = os.path.join(_session_dir(sid), "out")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    results = [P.process_file(e["path"], e["name"], out_dir, s) for e in sess["files"]]
    summary = P.summarize(results, s)
    P.write_reports(out_dir, results, summary)

    return {"summary": summary, "files": [asdict(r) for r in results]}


#  API: скачивание результата (zip)
@app.get("/api/download")
def download(sid: str):
    _get_session(sid)
    out_dir = os.path.join(_session_dir(sid), "out")
    if not os.path.isdir(out_dir) or not os.listdir(out_dir):
        raise HTTPException(status_code=400, detail="Сначала выполните обработку")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fname in sorted(os.listdir(out_dir)):
            z.write(os.path.join(out_dir, fname), arcname=fname)
    buf.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="resolution_fit_result.zip"'}
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


@app.get("/api/report", response_class=HTMLResponse)
def report(sid: str):
    """Показать читаемый HTML-отчёт последней обработки"""
    _get_session(sid)
    path = os.path.join(_session_dir(sid), "out", "report.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail="Сначала выполните обработку")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
