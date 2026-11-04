"""Conservative local repair using the same Sina image's unwatermarked preview.

The reference supplies real pixels, not an inpainted or generated replacement.
Only a bounded bottom watermark region is changed; uncertain pairs are skipped.
"""
from __future__ import annotations

import re
import warnings
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import cv2
import numpy as np
from PIL import Image, ImageOps

from .archive import atomic_output

MAX_PATCH_PIXELS = 24_000_000
MAX_REFERENCE_PIXELS = 2_000_000
EXTENSIONS = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif'}


class PatchSkipped(ValueError):
    """A valid original can still be saved when this pair is unsafe to patch."""


def reference_url(source: str) -> str | None:
    """Change only the size segment of a known public Sina image URL."""
    try:
        parts = urlsplit(source)
        host = (parts.hostname or '').lower()
        if (parts.scheme not in ('http', 'https') or parts.username or parts.password
                or parts.port not in (None, 80, 443)
                or not host.endswith('.sinaimg.cn')):
            return None
        if not re.fullmatch(r'/(?:large|original|woriginal|largest|(?:orj|mw)\d+)/[A-Za-z0-9]+\.(?:jpe?g|png|webp|gif)', parts.path, re.I):
            return None
        return urlunsplit(parts._replace(path=re.sub(r'^/[^/]+/', '/oslarge/', parts.path), fragment=''))
    except (TypeError, ValueError):
        return None


def image_info(path: str | Path, *, max_decode_pixels=MAX_PATCH_PIXELS) -> dict:
    """Validate the source and preserve its real extension on an unmodified save."""
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.format not in EXTENSIONS:
                raise ValueError('不支持的图片格式')
            result = {'extension': EXTENSIONS[image.format], 'size': list(image.size),
                      'animated': bool(getattr(image, 'is_animated', False)),
                      'transparent': image.mode in ('RGBA', 'LA') or 'transparency' in image.info}
            image.verify()
        # Decode ordinary-sized originals before publishing, including a fallback.
        if result['size'][0] * result['size'][1] <= max_decode_pixels:
            with Image.open(path) as image:
                image.load()
    return result


def eligibility(info: dict) -> str | None:
    width, height = info['size']
    if info['animated']:
        return '动图保留原文件'
    if info['transparent']:
        return '透明图片保留原文件'
    if width * height > MAX_PATCH_PIXELS or max(width, height) > 16383:
        return '图片超出局部补图尺寸上限'
    if min(width, height) < 240 or max(width, height) / min(width, height) > 4:
        return '图片尺寸不适合可靠定位水印'
    return None


def _rgb(path):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode != 'RGB':
            converted = image.convert('RGB')
            image.close()
            image = converted
        return image


def _watermark_box(high_small, reference):
    """Find a compact group of watermark glyphs near the bottom edge."""
    h, w = reference.shape[:2]
    delta = high_small.astype(np.float32) - reference.astype(np.float32)
    error = np.abs(delta)
    baseline = error[:int(h * .85)]
    if float(baseline.mean()) > 9 or float((baseline.max(axis=2) > 40).mean()) > .04:
        raise PatchSkipped('无水印参考图与原图内容不匹配')
    band = np.zeros((h, w), np.uint8)
    bottom = max(0, h - int(w * .15))
    band[bottom:] = (delta[bottom:].mean(axis=2) > 30).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(band, 8)
    kept = [n for n, row in enumerate(stats[1:], 1) if row[4] >= 3]
    glyphs = np.isin(labels, kept).astype(np.uint8)
    # White lettering can disappear against a bright background. Bridge gaps
    # horizontally, but keep the vertical join narrow and require a text-like
    # group below. Measure the original glyphs, not the filled closing mask.
    joined = cv2.morphologyEx(glyphs, cv2.MORPH_CLOSE,
                             np.ones((3, max(13, round(w * .1))), np.uint8))
    _, groups, boxes, _ = cv2.connectedComponentsWithStats(joined, 8)
    candidates = []
    for group_id, (left, top, bw, bh, _) in enumerate(boxes[1:], 1):
        window = np.s_[top:top+bh, left:left+bw]
        region = (groups[window] == group_id) & (glyphs[window] != 0)
        rows, columns = np.nonzero(region)
        if not len(rows):
            continue
        x, y = int(left + columns.min()), int(top + rows.min())
        width, height = int(columns.max()-columns.min()+1), int(rows.max()-rows.min()+1)
        if (height < max(6, w * .009) or height > w * .09
                or width < height * 2.5 or width > w * .55
                or h - (y + height) > w * .05):
            continue
        members = set(np.unique(labels[window][region])) & set(kept)
        ink = len(rows)
        if len(members) < 4 or ink < 50 or not .06 <= ink / (width * height) <= .7:
            continue
        candidates.append((int(x), int(y), int(x+width), int(y+height)))
    if len(candidates) != 1:
        raise PatchSkipped('未能唯一、可靠地定位平台水印')
    x0, y0, x1, y1 = candidates[0]
    box = (max(0, x0-6), max(0, y0-4), min(w, x1+6), min(h, y1+5))
    outside = np.ones((h, w), bool)
    outside[box[1]:box[3], box[0]:box[2]] = False
    if float((error.max(axis=2)[outside] > 40).mean()) > .04:
        raise PatchSkipped('水印区域外存在过多差异')
    return box


def patch_image(original_path, reference_path, output_path) -> dict:
    """Publish a lossless WebP only after alignment, region and format checks."""
    info = image_info(original_path)
    reason = eligibility(info)
    if reason:
        raise PatchSkipped(reason)
    ref_info = image_info(reference_path, max_decode_pixels=MAX_REFERENCE_PIXELS)
    if (ref_info['animated'] or ref_info['transparent']
            or ref_info['size'][0] * ref_info['size'][1] > MAX_REFERENCE_PIXELS):
        raise PatchSkipped('无水印参考图格式或尺寸异常')
    with _rgb(original_path) as original, _rgb(reference_path) as reference:
        w, h = original.size
        rw, rh = reference.size
        if rw > w or rh > h or min(rw, rh) < 160 or abs(w/h - rw/rh) / (w/h) > .003:
            raise PatchSkipped('无水印参考图的尺寸或裁切不匹配')
        reference.thumbnail((720, 1800), Image.Resampling.LANCZOS)
        lw, lh = reference.size
        small = np.array(original.resize((lw, lh), Image.Resampling.BOX))
        low = np.array(reference)
        box = _watermark_box(small, low)
        x0, y0, x1, y1 = (round(box[0]*w/lw), round(box[1]*h/lh),
                          round(box[2]*w/lw), round(box[3]*h/lh))
        if (x1-x0)*(y1-y0) > w*h*.05:
            raise PatchSkipped('待修补区域过大，保留原图')
        # Resize only the small patch, avoiding a second full-sized RGB array.
        sample_box = (x0*lw/w, y0*lh/h, x1*lw/w, y1*lh/h)
        with reference.resize((x1-x0, y1-y0), Image.Resampling.LANCZOS, box=sample_box) as crop:
            replacement = np.array(crop)
        with original.crop((x0,y0,x1,y1)) as crop:
            previous = np.array(crop)
        feather = max(3, round(w/lw*1.5))
        alpha = np.zeros(previous.shape[:2], np.float32)
        alpha[feather:-feather, feather:-feather] = 1
        alpha = cv2.GaussianBlur(alpha, (feather*2+1, feather*2+1), 0)
        blended = np.rint(previous*(1-alpha[...,None]) + replacement*alpha[...,None]).astype(np.uint8)
        with Image.fromarray(blended) as patch:
            original.paste(patch, (x0,y0))
        with atomic_output(output_path) as temporary:
            options = {'icc_profile':original.info['icc_profile']} if original.info.get('icc_profile') else {}
            original.save(temporary, 'WEBP', lossless=True, quality=100, method=4, **options)
            with Image.open(temporary) as check:
                if check.format != 'WEBP' or check.size != (w,h):
                    raise ValueError('局部补图输出校验失败')
                check.load()
        return {'method':'paired-preview-patch-v1', 'size':[w,h], 'reference_size':[rw,rh],
                'patch_box':[x0,y0,x1,y1],
                'changed_pixels_percent':round(np.any(previous != blended,axis=2).sum()*100/(w*h),4)}
