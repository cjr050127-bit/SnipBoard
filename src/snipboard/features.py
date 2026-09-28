"""Offline color and visual similarity features. No model or network required."""
from __future__ import annotations

import io
import numpy as np
from PIL import Image, ImageCms, ImageOps

VERSION = 3
COLORS = {
    '红': '#e34242', '橙': '#ec8c32', '黄': '#e5ce45', '绿': '#55a85e',
    '青': '#43bdb1', '蓝': '#4385db', '紫': '#9360ca', '粉': '#ed90b1',
    '棕': '#926346', '黑': '#151515', '灰': '#888888', '白': '#ededed',
}


def rgb_lab(rgb):
    values = np.asarray(rgb, dtype=np.float32) / 255
    values = np.where(values > .04045, ((values + .055) / 1.055) ** 2.4, values / 12.92)
    xyz = values @ np.array([[.4124564, .2126729, .0193339],
                            [.3575761, .7151522, .119192],
                            [.1804375, .072175, .9503041]], dtype=np.float32)
    xyz /= np.array([.95047, 1, 1.08883])
    f = np.where(xyz > .008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def normalized_image(path):
    with Image.open(path) as original:
        original.load()
        picture = ImageOps.exif_transpose(original)
        profile = original.info.get('icc_profile')
        if profile:
            try:
                picture = ImageCms.profileToProfile(picture, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                                   ImageCms.createProfile('sRGB'), outputMode='RGBA')
            except (OSError, ValueError, ImageCms.PyCMSError):
                pass
        return picture.convert('RGBA')


def extract(image):
    sample = image.copy()
    sample.thumbnail((96, 96))
    rgba = np.asarray(sample, dtype=np.float32)
    mask = rgba[..., 3] >= 32
    pixels = rgba[..., :3][mask]
    weights = rgba[..., 3][mask] / 255
    if not len(pixels):
        return {'palette': [], 'brightness': None, 'hist': [0.] * 48, 'spatial': [0.] * 48, 'hash': '0'}
    # Quantized palette retains multiple colors, unlike a single mean RGB value.
    quantized = np.minimum((pixels / 32).astype(int), 7)
    ids = quantized[:, 0] * 64 + quantized[:, 1] * 8 + quantized[:, 2]
    counts = np.bincount(ids, weights=weights, minlength=512)
    top = np.argsort(counts)[::-1]
    palette = []
    for index in top:
        if counts[index] <= 0:
            continue
        selected = ids == index
        mean = np.average(pixels[selected], weights=weights[selected], axis=0)
        palette.append([*map(float, mean), float(counts[index] / weights.sum())])
    # RGB marginal histograms + spatial color layout, independent of source resolution.
    hist = np.concatenate([np.histogram(pixels[:, c], bins=16, range=(0, 256), weights=weights)[0]
                           for c in range(3)])
    hist = hist / max(float(np.linalg.norm(hist)), 1e-8)
    background = Image.new('RGBA', image.size, '#808080')
    background.alpha_composite(image)
    small = background.convert('RGB').resize((4, 4), Image.Resampling.BILINEAR)
    spatial = np.asarray(small, dtype=np.float32).ravel() / 255
    gray = np.asarray(background.convert('L').resize((9, 8), Image.Resampling.BILINEAR))
    dhash = 0
    for bit in (gray[:, 1:] > gray[:, :-1]).ravel():
        dhash = (dhash << 1) | int(bit)
    # Decimal text also survives Qt QVariant's signed 64-bit integer boundary.
    brightness = float(np.average(rgb_lab(pixels)[:, 0], weights=weights))
    return {'palette': palette, 'brightness': brightness,
            'hist': hist.tolist(), 'spatial': spatial.tolist(), 'hash': str(dhash)}


def color_score(feature, color, tolerance=38):
    palette = feature.get('palette', [])
    if not palette:
        return 0.
    target = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    rows = np.asarray(palette)
    distance = np.linalg.norm(rgb_lab(rows[:, :3]) - rgb_lab(target), axis=1)
    return float(rows[distance <= tolerance, 3].sum())


def similarity(first, second):
    hist = float(np.dot(first['hist'], second['hist']))
    spatial = 1 - float(np.mean(np.abs(np.array(first['spatial']) - second['spatial'])))
    structure = 1 - (int(first['hash']) ^ int(second['hash'])).bit_count() / 64
    return .60 * hist + .25 * spatial + .15 * structure
