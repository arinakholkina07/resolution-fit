# -*- coding: utf-8 -*-
"""
Обработка изображений

Что делает модуль:
  1) вписывает изображение в кадр 1920x1080 с сохранением пропорций
     основного содержимого
  2) заполняет пустые поля кадра одним из способов:
     размытие исходника / сплошной цвет / градиент / зеркальное отражение
  3) выполняет отбраковку: если для укладки в 1920x1080 требуется
     увеличение исходника в >= порог раз (по умолчанию 2.0), то файл
     помечается как «отбракован» и не конвертируется
  4) сохраняет результат с веб-оптимизацией (WebP или JPEG)
  5) формирует отчёт (какие файлы преобразованы, какие
     отбракованы и почему)

"""
from __future__ import annotations

import io
import os
import csv
import json
import html
from dataclasses import dataclass, asdict

from PIL import Image, ImageFilter, ImageOps

TARGET_W, TARGET_H = 1920, 1080

# Форматы изображений, которые считаем допустимыми на входе
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


#  Настройки обработки
@dataclass
class Settings:
    """Параметры обработки"""
    fill_method: str = "blur"
    solid_color: str = "#101418"
    gradient_from: str = "#1b2735"
    gradient_to: str = "#090a0f"
    blur_radius: int = 40    
    reject_threshold: float = 2.0
    out_format: str = "webp"
    quality: int = 82 
    keep_metadata: bool = False

    @staticmethod
    def from_dict(d: dict) -> "Settings":
        base = Settings()
        for k, v in (d or {}).items():
            if hasattr(base, k) and v is not None:
                setattr(base, k, v)
        base.blur_radius = int(base.blur_radius)
        base.reject_threshold = float(base.reject_threshold)
        base.quality = max(1, min(100, int(base.quality)))
        base.keep_metadata = bool(base.keep_metadata)
        return base


@dataclass
class ProcessResult:
    """Строка сводного отчёта по одному файлу"""
    name: str
    status: str
    reason: str = ""
    src_w: int = 0
    src_h: int = 0
    src_bytes: int = 0
    scale: float = 0.0
    out_name: str = ""
    out_w: int = 0
    out_h: int = 0
    out_bytes: int = 0
    saved_percent: float = 0.0


#  Вспомогательные функции
def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """'#rrggbb' -> (r, g, b). Терпимо к отсутствию '#' и к коротким записям"""
    value = (value or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return (16, 20, 24)
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (16, 20, 24)


def open_image(path: str) -> Image.Image:
    """Открыть изображение и учесть ориентацию из EXIF"""
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im


def compute_fit(w: int, h: int, tw: int = TARGET_W, th: int = TARGET_H):
    """Единый коэффициент вписывания с сохранением пропорций (contain)
       Возвращает (scale, new_w, new_h). scale > 1 означает увеличение исходника"""
    scale = min(tw / w, th / h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    return scale, new_w, new_h


#  Построение фона (заполнение пустых полей кадра)
def _cover_resize(im: Image.Image, tw: int, th: int) -> Image.Image:
    """Масштабировать изображение так, чтобы оно заполнило весь кадр без
       пустот, режим "cover"."""
    scale = max(tw / im.width, th / im.height)
    nw, nh = max(tw, round(im.width * scale)), max(th, round(im.height * scale))
    im2 = im.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return im2.crop((left, top, left + tw, top + th))


def bg_blur(src: Image.Image, radius: int, tw: int = TARGET_W, th: int = TARGET_H) -> Image.Image:
    """Фон: растянутая и размытая копия исходника (способ по умолчанию)"""
    bg = _cover_resize(src.convert("RGB"), tw, th)
    bg = bg.filter(ImageFilter.GaussianBlur(max(1, radius)))
    bg = Image.eval(bg, lambda p: int(p * 0.9))
    return bg


def bg_solid(color_rgb: tuple[int, int, int], tw: int = TARGET_W, th: int = TARGET_H) -> Image.Image:
    """Фон: сплошной цвет"""
    return Image.new("RGB", (tw, th), color_rgb)


def bg_gradient(c1: tuple[int, int, int], c2: tuple[int, int, int],
                tw: int = TARGET_W, th: int = TARGET_H) -> Image.Image:
    """Фон: вертикальный линейный градиент"""
    col = Image.new("RGB", (1, th))
    px = col.load()
    denom = max(1, th - 1)
    for y in range(th):
        t = y / denom
        px[0, y] = (
            round(c1[0] + (c2[0] - c1[0]) * t),
            round(c1[1] + (c2[1] - c1[1]) * t),
            round(c1[2] + (c2[2] - c1[2]) * t),
        )
    return col.resize((tw, th))


def bg_mirror(contained: Image.Image, tw: int = TARGET_W, th: int = TARGET_H) -> Image.Image:
    """Фон: зеркальное отражение краёв исходника в пустые поля"""
    cw, ch = contained.size
    bg = Image.new("RGB", (tw, th), (0, 0, 0))
    offx = (tw - cw) // 2
    offy = (th - ch) // 2

    if offy > 0:
        flip_v = contained.transpose(Image.FLIP_TOP_BOTTOM)
        if offy <= ch:
            top = flip_v.crop((0, ch - offy, cw, ch))
            bottom = flip_v.crop((0, 0, cw, offy))
        else:
            top = flip_v.resize((cw, offy))
            bottom = flip_v.resize((cw, offy))
        bg.paste(top, (offx, 0))
        bg.paste(bottom, (offx, offy + ch))

    if offx > 0:
        flip_h = contained.transpose(Image.FLIP_LEFT_RIGHT)
        if offx <= cw:
            left = flip_h.crop((cw - offx, 0, cw, ch))
            right = flip_h.crop((0, 0, offx, ch))
        else:
            left = flip_h.resize((offx, ch))
            right = flip_h.resize((offx, ch))
        bg.paste(left, (0, offy))
        bg.paste(right, (offx + cw, offy))

    return bg


#  Основной рендер одного кадра
def render_frame(src: Image.Image, s: Settings) -> tuple[Image.Image, float, tuple[int, int]]:
    """Собрать итоговый кадр 1920x1080: фон по выбранному способу + вписанное
    по центру основное содержимое, возвращает (кадр, scale, (new_w, new_h))"""
    src_rgb = src.convert("RGB")
    scale, nw, nh = compute_fit(src_rgb.width, src_rgb.height)
    contained = src_rgb.resize((nw, nh), Image.LANCZOS)

    method = s.fill_method
    if method == "solid":
        color = hex_to_rgb(s.solid_color)
        bg = bg_solid(color)
    elif method == "gradient":
        bg = bg_gradient(hex_to_rgb(s.gradient_from), hex_to_rgb(s.gradient_to))
    elif method == "mirror":
        bg = bg_mirror(contained)
    else:
        bg = bg_blur(src_rgb, s.blur_radius)

    canvas = bg
    offx = (TARGET_W - nw) // 2
    offy = (TARGET_H - nh) // 2
    canvas.paste(contained, (offx, offy))
    return canvas, scale, (nw, nh)


#  Сохранение с веб-оптимизацией
def save_optimized(img: Image.Image, out_path: str, s: Settings) -> int:
    """Сохранить кадр в WebP или JPEG с параметрами веб-оптимизации, возвращает размер получившегося файла в байтах"""
    params: dict = {}
    if s.out_format == "jpeg":
        params = dict(format="JPEG", quality=s.quality, optimize=True, progressive=True)
    else:  # webp
        params = dict(format="WEBP", quality=s.quality, method=6)

    if s.keep_metadata:
        exif = img.info.get("exif")
        icc = img.info.get("icc_profile")
        if exif:
            params["exif"] = exif
        if icc:
            params["icc_profile"] = icc

    img.save(out_path, **params)
    return os.path.getsize(out_path)


def preview_bytes(img: Image.Image, s: Settings, max_w: int = 960) -> bytes:
    """Кадр: bytes для предпросмотра"""
    prev = img
    if img.width > max_w:
        ratio = max_w / img.width
        prev = img.resize((max_w, round(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    if s.out_format == "jpeg":
        prev.save(buf, format="JPEG", quality=s.quality, optimize=True, progressive=True)
    else:
        prev.save(buf, format="WEBP", quality=s.quality, method=4)
    return buf.getvalue()


def out_extension(s: Settings) -> str:
    return ".jpg" if s.out_format == "jpeg" else ".webp"


#  Обработка одного файла и целой пачки
def process_file(src_path: str, display_name: str, out_dir: str, s: Settings) -> ProcessResult:
    """Обработать один файл: отбраковать или сконвертировать и сохранить"""
    src_bytes = os.path.getsize(src_path)
    try:
        src = open_image(src_path)
        w, h = src.width, src.height
    except Exception as exc:
        return ProcessResult(name=display_name, status="error",
                             reason=f"не удалось открыть файл: {exc}",
                             src_bytes=src_bytes)

    scale, nw, nh = compute_fit(w, h)

    # Отбраковка
    if scale >= s.reject_threshold:
        return ProcessResult(
            name=display_name, status="rejected",
            reason=(f"требуется увеличение x{scale:.2f} (порог {s.reject_threshold:.2f}): "
                    f"исходник {w}x{h} слишком мал для укладки в {TARGET_W}x{TARGET_H}"),
            src_w=w, src_h=h, src_bytes=src_bytes, scale=round(scale, 3),
        )

    # Конвертация
    frame, scale, (nw, nh) = render_frame(src, s)
    base = os.path.splitext(os.path.basename(display_name))[0]
    out_name = base + out_extension(s)
    out_path = os.path.join(out_dir, out_name)
    i = 1
    while os.path.exists(out_path):
        out_name = f"{base}_{i}{out_extension(s)}"
        out_path = os.path.join(out_dir, out_name)
        i += 1

    out_bytes = save_optimized(frame, out_path, s)
    saved = (1 - out_bytes / src_bytes) * 100 if src_bytes else 0.0
    return ProcessResult(
        name=display_name, status="converted", reason="",
        src_w=w, src_h=h, src_bytes=src_bytes, scale=round(scale, 3),
        out_name=out_name, out_w=TARGET_W, out_h=TARGET_H, out_bytes=out_bytes,
        saved_percent=round(saved, 1),
    )


def summarize(results: list[ProcessResult], s: Settings) -> dict:
    """Сводка по всей пачке для отчёта и для UI"""
    converted = [r for r in results if r.status == "converted"]
    rejected = [r for r in results if r.status == "rejected"]
    errors = [r for r in results if r.status == "error"]
    src_total = sum(r.src_bytes for r in converted)
    out_total = sum(r.out_bytes for r in converted)
    return {
        "total": len(results),
        "converted": len(converted),
        "rejected": len(rejected),
        "errors": len(errors),
        "src_bytes_total": src_total,
        "out_bytes_total": out_total,
        "saved_percent_total": round((1 - out_total / src_total) * 100, 1) if src_total else 0.0,
        "settings": asdict(s),
        "target": f"{TARGET_W}x{TARGET_H}",
    }


#  Запись сводного отчёта
def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.0f} КБ" if n else "—"


def write_reports(out_dir: str, results: list[ProcessResult], summary: dict) -> None:
    """Сохранить отчёт рядом с результатами в трёх форматах"""
    rows = [asdict(r) for r in results]

    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "files": rows}, f, ensure_ascii=False, indent=2)

    with open(os.path.join(out_dir, "report.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["файл", "статус", "причина", "исходник", "масштаб",
                    "результат", "размер исходника, байт", "размер результата, байт",
                    "экономия, %"])
        for r in results:
            w.writerow([
                r.name, r.status, r.reason,
                f"{r.src_w}x{r.src_h}" if r.src_w else "",
                f"{r.scale:.2f}" if r.scale else "",
                r.out_name, r.src_bytes, r.out_bytes,
                f"{r.saved_percent:.1f}" if r.status == "converted" else "",
            ])

    def badge(status: str) -> str:
        m = {"converted": ("Преобразован", "#444828", "#e9ecd6"),
             "rejected": ("Отбракован", "#8a3413", "#f4e4cb"),
             "error": ("Ошибка", "#97281b", "#f3ddd6")}
        text, fg, bg = m.get(status, (status, "#333", "#eee"))
        return (f'<span style="font-family:monospace;color:{fg};background:{bg};'
                f'padding:2px 8px;font-size:11px;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.04em">{text}</span>')

    trs = []
    for r in results:
        trs.append(
            "<tr>"
            f"<td>{html.escape(r.name)}</td>"
            f"<td>{badge(r.status)}</td>"
            f"<td class='n'>{f'{r.src_w}×{r.src_h}' if r.src_w else '—'}</td>"
            f"<td class='n'>{f'×{r.scale:.2f}' if r.scale else '—'}</td>"
            f"<td>{html.escape(r.out_name) if r.out_name else '—'}</td>"
            f"<td class='n'>{_fmt_kb(r.src_bytes)}</td>"
            f"<td class='n'>{_fmt_kb(r.out_bytes)}</td>"
            f"<td class='n'>{f'{r.saved_percent:.1f}%' if r.status=='converted' else '—'}</td>"
            f"<td style='color:#8d7f6a'>{html.escape(r.reason)}</td>"
            "</tr>"
        )
    s = summary
    doc = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Отчёт</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=Golos+Text:wght@400;500;600&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
body{{font-family:"Golos Text",system-ui,sans-serif;background:#f1e8d8;color:#23190f;margin:0;padding:40px clamp(18px,5vw,60px)}}
.kick{{font-family:"JetBrains Mono",monospace;font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#b0461f;margin-bottom:10px}}
h1{{font-family:"Playfair Display",Georgia,serif;font-weight:800;font-size:30px;letter-spacing:-.01em;margin:0}}
.rule{{border-top:3px solid #23190f;border-bottom:1px solid #23190f;height:4px;margin:16px 0 26px}}
.sum{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));border:1px solid #d8c8ad;margin:0 0 22px}}
.card{{padding:14px 18px;border-right:1px solid #d8c8ad}}
.card:last-child{{border-right:none}}
.card b{{display:block;font-family:"Playfair Display",serif;font-weight:800;font-size:30px;line-height:1}}
.card span{{color:#8d7f6a;font-size:12px}}
.meta{{color:#5a4c3b;font-size:14px;margin:0 0 20px}}
table{{border-collapse:collapse;width:100%;font-size:13px;border:1px solid #d8c8ad;background:#fbf6ec}}
th,td{{border-bottom:1px solid #e6dac2;padding:9px 12px;text-align:left;vertical-align:top}}
th{{font-family:"JetBrains Mono",monospace;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:#8d7f6a;background:#f4ecdc}}
td.n{{font-family:"JetBrains Mono",monospace}}
</style></head><body>
<p class="kick">сводный отчёт</p>
<h1>Приведение к формату {s['target']}</h1>
<div class="rule"></div>
<div class="sum">
  <div class="card"><b>{s['total']}</b><span>всего файлов</span></div>
  <div class="card"><b>{s['converted']}</b><span>преобразовано</span></div>
  <div class="card"><b>{s['rejected']}</b><span>отбраковано</span></div>
  <div class="card"><b>{s['errors']}</b><span>ошибок</span></div>
  <div class="card"><b>{s['saved_percent_total']}%</b><span>экономия размера</span></div>
</div>
<p class="meta">Способ заливки: <b>{s['settings']['fill_method']}</b> / формат:
<b>{s['settings']['out_format']}</b> / качество: <b>{s['settings']['quality']}</b> /
порог отбраковки: <b>{s['settings']['reject_threshold']}</b></p>
<table><thead><tr>
<th>Файл</th><th>Статус</th><th>Исходник</th><th>Масштаб</th><th>Результат</th>
<th>Было</th><th>Стало</th><th>Экономия</th><th>Причина</th>
</tr></thead><tbody>{''.join(trs)}</tbody></table>
</body></html>"""
    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(doc)
