"""
tategakiXTC_gui_core.py — 変換コア

EPUB / ZIP / CBZ / CBR / TXT / Markdown を縦書き XTC 形式へ変換するロジック。
GUI (tategakiXTC_gui_studio.py) から呼び出して使用します。
"""

import base64
import hashlib
import io
import os
import re
import shutil
import struct
import tempfile
import posixpath
from dataclasses import dataclass, replace as dc_replace
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageDraw, ImageFont, ImageOps


__all__ = [
    "ConversionArgs",
    "get_font_list",
    "resolve_font_path",
    "iter_conversion_targets",
    "should_skip_conversion_target",
    "get_output_path_for_target",
    "make_unique_output_path",
    "find_output_conflicts",
    "generate_preview_base64",
    "process_archive",
    "process_epub",
    "process_text_file",
    "process_markdown_file",
    "SUPPORTED_INPUT_SUFFIXES",
    "ARCHIVE_INPUT_SUFFIXES",
    "TEXT_INPUT_SUFFIXES",
    "MARKDOWN_INPUT_SUFFIXES",
]


def _require_ebooklib_epub():
    """ebooklib.epub を必要時に読み込む。"""
    try:
        from ebooklib import epub as epub_module
    except ImportError as e:
        raise RuntimeError(
            "EPUB変換には ebooklib が必要です。`pip install ebooklib` を実行してください。"
        ) from e
    return epub_module


def _require_patoolib():
    """patoolib を必要時に読み込む。"""
    try:
        import patoolib as patoolib_module
    except ImportError as e:
        raise RuntimeError(
            "アーカイブ変換には patool が必要です。`pip install patool` を実行してください。"
        ) from e
    return patoolib_module




def _require_bs4_beautifulsoup():
    """bs4.BeautifulSoup を必要時に読み込む。"""
    try:
        from bs4 import BeautifulSoup as bs4_BeautifulSoup
    except ImportError as e:
        raise RuntimeError(
            "EPUB変換には beautifulsoup4 が必要です。`pip install beautifulsoup4` を実行してください。"
        ) from e
    return bs4_BeautifulSoup


def _require_tqdm():
    """tqdm を必要時に読み込む。"""
    try:
        from tqdm import tqdm as tqdm_func
    except ImportError as e:
        raise RuntimeError(
            "進捗表示には tqdm が必要です。`pip install tqdm` を実行してください。"
        ) from e
    return tqdm_func

def _natural_sort_key(path_like):
    """パスを自然順で比較するキーを返す。数値部分は整数として扱う。"""
    value = str(path_like)
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', value)]




# ==========================================
# --- 基本設定 & 定数 ---
# ==========================================

DEF_WIDTH, DEF_HEIGHT = 480, 800
IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
ARCHIVE_INPUT_SUFFIXES = ('.epub', '.zip', '.rar', '.cbz', '.cbr')
TEXT_INPUT_SUFFIXES = ('.txt',)
MARKDOWN_INPUT_SUFFIXES = ('.md', '.markdown')
SUPPORTED_INPUT_SUFFIXES = ARCHIVE_INPUT_SUFFIXES + TEXT_INPUT_SUFFIXES + MARKDOWN_INPUT_SUFFIXES

TATE_REPLACE = {
    # --- 括弧・句読点 ---
    "…": "︙", "‥": "︰", "─": "丨", "―": "丨", "-": "丨",
    "～": "≀", "〜": "≀", "〰": "≀",
    "「": "﹁", "」": "﹂", "『": "﹃", "』": "﹄",
    "（": "︵", "）": "︶", "(": "︵", ")": "︶",
    "【": "︻", "】": "︼", "〔": "︹", "〕": "︺",
    "［": "﹇", "］": "﹈", "[": "﹇", "]": "﹈",
    "｛": "︷", "｝": "︸", "{": "︷", "}": "︸",
    "＜": "︿", "＞": "﹀", "<": "︿", ">": "﹀",
    "《": "︽", "》": "︾",
    "、": "︑", "。": "︒",
    # --- 数学記号 ---
    "＝": "‖", "=": "‖",
    "＋": "＋",
    "±": "∓",
    "×": "×",
    "÷": "÷",
    "≠": "⧘",
    "≒": "≓",
    "≡": "⦀",
    "∞": "∞",
    "：": "‥",
    "；": "；",
}

KUTOTEN_OFFSET_X, KUTOTEN_OFFSET_Y = 18, -8
SMALL_KANA_CHARS = set("ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶ")
OPENING_BRACKET_CHARS = set("([｛{〔〈《「『【〖〘〝‘“｟（［｢＜<")
CLOSING_BRACKET_CHARS = set(")]}｝〕〉》」』】〙〗〟’”｠）］｣＞>")
TAIL_PUNCTUATION_CHARS = set("、。，．､｡！？!?:;：；…‥")
PROLONGED_SOUND_MARK_CHARS = set("ー－")
LINE_END_CONTIN_CHARS = set("―─〜～〰・")
LINE_HEAD_FORBIDDEN_CHARS = (
    TAIL_PUNCTUATION_CHARS
    | CLOSING_BRACKET_CHARS
    | PROLONGED_SOUND_MARK_CHARS
    | SMALL_KANA_CHARS
    | LINE_END_CONTIN_CHARS
)
LINE_END_FORBIDDEN_CHARS = OPENING_BRACKET_CHARS
DOUBLE_PUNCT_TOKENS = {"!!", "!?", "?!", "？？", "！！", "！？", "？！"}
HANGING_PUNCTUATION_CHARS = {"、", "。", "，", "．", "､", "｡"}
LOWERABLE_HANGING_CLOSING_BRACKET_CHARS = set(CLOSING_BRACKET_CHARS)
CONTINUOUS_PUNCTUATION_PAIRS = {
    "……", "‥‥",
    "――", "──", "ーー",
    "～～", "〜〜", "〰〰",
    "・・",
}
REPEATABLE_CONTINUOUS_PUNCT_CHARS = set("…‥―─ー〜～〰・")
CLOSE_BRACKET_CHARS = set(CLOSING_BRACKET_CHARS)


PARAGRAPH_LIKE_TAGS = {
    "p", "div", "section", "article", "header",
    "blockquote", "li", "dd", "dt",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
STRUCTURAL_TAGS = PARAGRAPH_LIKE_TAGS | {"body", "html"}
START_TEXT_RE = re.compile(r'^\s*start\s*text\s*[:：]?\s*', re.IGNORECASE)


# ==========================================
# --- 変換引数データクラス ---
# ==========================================

@dataclass
class ConversionArgs:
    """変換処理に渡すパラメータをまとめたデータクラス。"""
    width: int = DEF_WIDTH
    height: int = DEF_HEIGHT
    font_size: int = 26
    ruby_size: int = 12
    line_spacing: int = 44
    margin_t: int = 12
    margin_b: int = 14
    margin_r: int = 12
    margin_l: int = 12
    dither: bool = False
    night_mode: bool = False
    threshold: int = 128
    kinsoku_mode: str = "standard"
    output_format: str = "xtc"
    progress_bar: bool = False
    progress_bar_side: str = "left"


# ==========================================
# --- グリフ描画ヘルパー ---
# ==========================================

def _scaled_kutoten_offset(f_size):
    off_x = max(1, int(round(f_size * 0.06)))
    off_y = -max(1, int(round(f_size * 0.10)))
    return off_x, off_y


def _small_kana_offset(f_size):
    off_x = max(1, int(round(f_size * 0.08)))
    off_y = -max(2, int(round(f_size * 0.12)))
    return off_x, off_y


def draw_weighted_text(draw, pos_tuple, text, font, is_bold=False, is_italic=False):
    draw_kwargs = {"font": font, "fill": 0}
    x, y = pos_tuple
    if is_italic:
        glyph_img = _render_text_glyph_image(text, font, is_bold=is_bold, is_italic=True)
        draw._image.paste(glyph_img, (int(x), int(y)), glyph_img.point(lambda p: 255 - p))
        return
    draw.text((x, y), text, **draw_kwargs)
    if is_bold:
        # 疑似ボールド: 横方向 +1px、縦方向 +1px を重ねて太りを自然に増やす。
        draw.text((x + 1, y), text, **draw_kwargs)
        draw.text((x, y + 1), text, **draw_kwargs)


def _render_text_glyph_image(text, font, is_bold=False, rotate_degrees=0, canvas_size=None, is_italic=False):
    stroke_width = 1 if is_bold else 0
    bbox = font.getbbox(text, stroke_width=stroke_width)
    glyph_w = max(1, bbox[2] - bbox[0])
    glyph_h = max(1, bbox[3] - bbox[1])
    pad = max(4, stroke_width + 2)
    side = canvas_size or max(glyph_w, glyph_h) + pad * 4
    glyph_img = Image.new("L", (side, side), 255)
    glyph_draw = ImageDraw.Draw(glyph_img)
    draw_x = (side - glyph_w) // 2 - bbox[0]
    draw_y = (side - glyph_h) // 2 - bbox[1]
    draw_weighted_text(glyph_draw, (draw_x, draw_y), text, font, is_bold=is_bold)
    ink_bbox = ImageOps.invert(glyph_img).getbbox()
    if ink_bbox:
        glyph_img = glyph_img.crop(ink_bbox)
    if rotate_degrees:
        glyph_img = glyph_img.rotate(rotate_degrees, expand=True, fillcolor=255)
        ink_bbox = ImageOps.invert(glyph_img).getbbox()
        if ink_bbox:
            glyph_img = glyph_img.crop(ink_bbox)
    if is_italic:
        shear = -0.22
        extra_w = int(abs(shear) * glyph_img.height) + 4
        transformed = glyph_img.transform(
            (glyph_img.width + extra_w, glyph_img.height),
            Image.AFFINE,
            (1, shear, extra_w if shear < 0 else 0, 0, 1, 0),
            resample=Image.Resampling.BICUBIC,
            fillcolor=255,
        )
        ink_bbox = ImageOps.invert(transformed).getbbox()
        glyph_img = transformed.crop(ink_bbox) if ink_bbox else transformed
    return glyph_img


def _get_reference_glyph_center(font, is_bold=False, f_size=None):
    """通常の全角文字が流し込み時に占める"見た目の中心"を推定する。"""
    refs = ("口", "田", "国", "漢", "あ", "ア", "亜")
    stroke_width = 1 if is_bold else 0
    centers = []
    for ref in refs:
        try:
            bbox = font.getbbox(ref, stroke_width=stroke_width)
        except TypeError:
            bbox = font.getbbox(ref)
        if bbox and bbox[3] > bbox[1]:
            centers.append((bbox[1] + bbox[3]) / 2.0)
    if centers:
        return sum(centers) / len(centers)
    return (f_size / 2.0) if f_size else 0.0


def draw_centered_glyph(draw, char, pos_tuple, font, f_size, is_bold=False,
                        rotate_degrees=0, align_to_text_flow=False, is_italic=False):
    curr_x, curr_y = pos_tuple
    glyph_img = _render_text_glyph_image(
        char, font, is_bold=is_bold, rotate_degrees=rotate_degrees, canvas_size=f_size * 4, is_italic=is_italic
    )
    gw, gh = glyph_img.size
    paste_x = curr_x + max(0, (f_size - gw) // 2)
    if align_to_text_flow:
        target_center_y = _get_reference_glyph_center(font, is_bold=is_bold, f_size=f_size)
        paste_y = int(round(curr_y + target_center_y - (gh / 2.0)))
    else:
        paste_y = curr_y + max(0, (f_size - gh) // 2)
    draw._image.paste(glyph_img, (paste_x, paste_y), glyph_img.point(lambda p: 255 - p))


def _should_center_ascii_glyph(char):
    if not char or len(char) != 1:
        return False
    if not char.isascii() or char.isspace():
        return False
    return char.isalnum()


def _tokenize_vertical_text(text):
    tokens = []
    i = 0
    while i < len(text):
        if i + 1 < len(text) and text[i] in '！？!?' and text[i + 1] in '！？!?':
            token = text[i:i + 2]
            if token in DOUBLE_PUNCT_TOKENS:
                tokens.append(token)
                i += 2
                continue
        tokens.append(text[i])
        i += 1
    return tokens


def _is_line_head_forbidden(token):
    if not token:
        return False
    if token in DOUBLE_PUNCT_TOKENS:
        return True
    return all(ch in LINE_HEAD_FORBIDDEN_CHARS for ch in token)


def _is_line_end_forbidden(token):
    if not token:
        return False
    return all(ch in LINE_END_FORBIDDEN_CHARS for ch in token)


def _is_hanging_punctuation(token):
    return bool(token) and len(token) == 1 and token in HANGING_PUNCTUATION_CHARS


def _is_continuous_punctuation_pair(current_token, next_token):
    if not current_token or not next_token:
        return False
    if len(current_token) != 1 or len(next_token) != 1:
        return False
    return (current_token + next_token) in CONTINUOUS_PUNCTUATION_PAIRS


def _continuous_punctuation_run_length(tokens, start_idx):
    if start_idx >= len(tokens):
        return 0
    token = tokens[start_idx]
    if not token or len(token) != 1 or token not in REPEATABLE_CONTINUOUS_PUNCT_CHARS:
        return 0
    idx = start_idx + 1
    while idx < len(tokens) and tokens[idx] == token:
        idx += 1
    run_len = idx - start_idx
    return run_len if run_len >= 2 else 0


def _closing_punctuation_group_length(tokens, start_idx):
    if start_idx >= len(tokens):
        return 0
    idx = start_idx
    while idx < len(tokens):
        token = tokens[idx]
        if not token or len(token) != 1 or token not in CLOSE_BRACKET_CHARS:
            break
        idx += 1
    if idx == start_idx:
        return 0
    tail_idx = idx
    while tail_idx < len(tokens):
        token = tokens[tail_idx]
        if token in DOUBLE_PUNCT_TOKENS:
            tail_idx += 1
            continue
        if len(token) == 1 and token in TAIL_PUNCTUATION_CHARS:
            tail_idx += 1
            continue
        break
    if tail_idx == idx:
        return idx - start_idx
    return tail_idx - start_idx


def _minimum_safe_group_length(tokens, start_idx):
    """start_idx から、行末/行頭禁則を破らず同一行へ残すための最小まとまり長を返す。"""
    if start_idx >= len(tokens):
        return 0
    for end_idx in range(start_idx, len(tokens)):
        tail_token = tokens[end_idx]
        if _is_line_end_forbidden(tail_token):
            continue
        if end_idx + 1 < len(tokens):
            next_token = tokens[end_idx + 1]
            if _is_line_head_forbidden(next_token):
                continue
            if _is_continuous_punctuation_pair(tail_token, next_token):
                continue
        return end_idx - start_idx + 1
    return len(tokens) - start_idx


def _protected_token_group_length(tokens, start_idx):
    return max(
        _continuous_punctuation_run_length(tokens, start_idx),
        _closing_punctuation_group_length(tokens, start_idx),
        _minimum_safe_group_length(tokens, start_idx),
    )


def _normalize_kinsoku_mode(mode):
    mode = str(mode or 'standard').strip().lower()
    return mode if mode in {'off', 'simple', 'standard'} else 'standard'


def _remaining_vertical_slots(curr_y, height, margin_b, font_size):
    limit = height - margin_b - font_size
    if curr_y > limit:
        return 0
    return 1 + (limit - curr_y) // (font_size + 2)


def _would_start_forbidden_after_hang_pair(tokens, idx):
    """idx, idx+1 を同一行へ置いた直後、次行頭が禁則になるなら True。"""
    next_idx = idx + 2
    if next_idx >= len(tokens):
        return False
    next_token = tokens[next_idx]
    return _is_line_head_forbidden(next_token)


def _choose_vertical_layout_action(tokens, idx, curr_y, margin_t, height, margin_b, font_size, kinsoku_mode='standard'):
    if idx >= len(tokens):
        return 'done'
    token = tokens[idx]
    slots_left = _remaining_vertical_slots(curr_y, height, margin_b, font_size)
    if slots_left == 0:
        return 'advance'

    mode = _normalize_kinsoku_mode(kinsoku_mode)
    if mode == 'off':
        return 'draw'

    if (
        slots_left == 1
        and curr_y > margin_t
        and _is_line_end_forbidden(token)
    ):
        return 'advance'
    if slots_left == 1 and curr_y > margin_t and idx + 1 < len(tokens):
        next_token = tokens[idx + 1]
        if mode == 'standard' and _is_continuous_punctuation_pair(token, next_token):
            return 'advance'
        if _is_hanging_punctuation(next_token) and not _is_line_end_forbidden(token):
            if mode == 'standard' and _would_start_forbidden_after_hang_pair(tokens, idx):
                return 'advance'
            return 'hang_pair'
        if _is_line_head_forbidden(next_token):
            return 'advance'

    if mode == 'simple':
        return 'draw'

    protected_group_len = _protected_token_group_length(tokens, idx)
    if (
        protected_group_len >= 2
        and slots_left < protected_group_len
        and curr_y > margin_t
    ):
        return 'advance'
    if (
        _is_line_end_forbidden(token)
        and curr_y > margin_t
        and idx + 1 < len(tokens)
        and slots_left >= 2
    ):
        next_curr_y = curr_y + font_size + 2
        next_action = _choose_vertical_layout_action(
            tokens, idx + 1, next_curr_y, margin_t, height, margin_b, font_size,
            kinsoku_mode=mode,
        )
        if next_action == 'advance':
            return 'advance'
    return 'draw'


def _draw_hanging_text_near_bottom(draw, original_char, pos_tuple, font, f_size, canvas_height, *,
                                   is_bold=False, is_italic=False, extra_raise_ratio=0.0):
    curr_x, curr_y = pos_tuple
    char = TATE_REPLACE.get(original_char, original_char)
    stroke_width = 1 if is_bold else 0
    try:
        bbox = font.getbbox(char, stroke_width=stroke_width)
    except TypeError:
        bbox = font.getbbox(char)
    glyph_h = max(1, bbox[3] - bbox[1]) if bbox else max(1, f_size)
    draw_x = curr_x
    is_kutoten = original_char in {"、", "。", "，", "．", "､", "｡"}
    if is_kutoten:
        off_x, _off_y_unused = _scaled_kutoten_offset(f_size)
        draw_x += off_x
        # ぶら下げ時は通常句読点の上方向補正を使わない。これが残ると前の文字に寄りすぎる。
        base_raise = 0
        lower_ratio = max(0.22, 0.28 - extra_raise_ratio)
    else:
        base_raise = 0
        lower_ratio = max(0.12, 0.20 - extra_raise_ratio)

    # 行末ぶら下げは1マス内の下寄せを基本にしつつ、句読点は少し強めに下げる。
    local_lower = max(1, int(round(f_size * lower_ratio)))
    desired_y = curr_y + base_raise + local_lower
    cell_limit_y = curr_y + max(0, f_size - glyph_h - 1)
    page_limit_y = canvas_height - glyph_h - 1 if canvas_height else cell_limit_y
    draw_y = max(curr_y, min(desired_y, cell_limit_y, page_limit_y))

    draw_weighted_text(draw, (draw_x, draw_y), char, font, is_bold=is_bold, is_italic=is_italic)




def _is_lowerable_hanging_closing_bracket(token):
    return bool(token) and len(token) == 1 and token in LOWERABLE_HANGING_CLOSING_BRACKET_CHARS


def draw_hanging_closing_bracket(draw, char, pos_tuple, font, f_size, canvas_height, is_bold=False, ruby_mode=False, is_italic=False):
    _draw_hanging_text_near_bottom(
        draw, char, pos_tuple, font, f_size, canvas_height,
        is_bold=is_bold, is_italic=is_italic, extra_raise_ratio=0.18,
    )


def draw_hanging_punctuation(draw, char, pos_tuple, font, f_size, canvas_height, is_bold=False, ruby_mode=False, is_italic=False):
    curr_x, curr_y = pos_tuple
    glyph_char = TATE_REPLACE.get(char, char)
    glyph_img = _render_text_glyph_image(glyph_char, font, is_bold=is_bold, is_italic=is_italic)
    glyph_w, glyph_h = glyph_img.size

    # ぶら下げ句読点は、直前文字の「次に来るはずだったマス」の右上へ置く。
    # こうすると、直前文字より必ず下側に来つつ、縦方向の重なりを避けやすい。
    line_step = f_size + 2
    right_inset = max(0, int(round(f_size * 0.03)))
    # 直前文字と縦方向で重ならないことを最優先し、
    # 次セルの右上基準を保ったまま、セル内ではさらに下側へ寄せる。
    top_inset = max(4, int(round(f_size * 0.30)))

    draw_x = curr_x + max(0, f_size - glyph_w - right_inset)
    draw_y = curr_y + line_step + top_inset

    # 下余白は実質無視してよいが、画像最下端だけは越えない。
    if canvas_height:
        draw_y = min(draw_y, max(0, canvas_height - glyph_h - 1))

    if is_italic:
        mask = glyph_img.point(lambda p: 255 - p)
    else:
        mask = ImageOps.invert(glyph_img)
    draw._image.paste(glyph_img, (int(draw_x), int(draw_y)), mask)


def draw_char_tate(draw, char, pos_tuple, font, f_size, is_bold=False, ruby_mode=False, is_italic=False):
    curr_x, curr_y = pos_tuple

    # 2文字（！？や！！）を横並びにする
    if len(char) == 2:
        sub_f_size = int(f_size * 0.75)
        if hasattr(font, "font_variant"):
            sub_font = font.font_variant(size=sub_f_size)
        else:
            font_path = getattr(font, "path", None)
            sub_font = ImageFont.truetype(font_path, sub_f_size) if font_path else font
        half_w = f_size // 2
        char_offset = (half_w - sub_f_size) // 2
        draw_weighted_text(draw, (curr_x + char_offset + 10, curr_y), char[0], sub_font, is_bold=is_bold, is_italic=is_italic)
        draw_weighted_text(draw, (curr_x + half_w + char_offset + 10, curr_y), char[1], sub_font, is_bold=is_bold, is_italic=is_italic)
        return

    original_char = char
    char = TATE_REPLACE.get(char, char)

    if original_char in {"一"}:
        if ruby_mode:
            draw_centered_glyph(
                draw, original_char, (curr_x, curr_y), font, f_size,
                is_bold=is_bold, rotate_degrees=90, align_to_text_flow=True, is_italic=is_italic,
            )
        else:
            draw_weighted_text(draw, (curr_x, curr_y), char, font, is_bold=is_bold, is_italic=is_italic)
    elif original_char in {"ー", "－"}:
        draw_centered_glyph(
            draw, original_char, (curr_x, curr_y), font, f_size,
            is_bold=is_bold, rotate_degrees=90, align_to_text_flow=True, is_italic=is_italic,
        )
    elif original_char in {"、", "。", "，", "．", "､", "｡"}:
        off_x, off_y = _scaled_kutoten_offset(f_size)
        draw_weighted_text(draw, (curr_x + off_x, curr_y + off_y), char, font, is_bold=is_bold, is_italic=is_italic)
    elif original_char in SMALL_KANA_CHARS:
        off_x, off_y = _small_kana_offset(f_size)
        draw_weighted_text(draw, (curr_x + off_x, curr_y + off_y), char, font, is_bold=is_bold, is_italic=is_italic)
    elif _should_center_ascii_glyph(original_char):
        draw_centered_glyph(
            draw, original_char, (curr_x, curr_y), font, f_size,
            is_bold=is_bold, align_to_text_flow=False, is_italic=is_italic,
        )
    else:
        draw_weighted_text(draw, (curr_x, curr_y), char, font, is_bold=is_bold, is_italic=is_italic)


# ==========================================
# --- CSS / ボールド解析 ---
# ==========================================

def style_declares_bold(style_text):
    if not style_text:
        return False
    match = re.search(r'font-weight\s*:\s*([^;]+)', style_text, re.IGNORECASE)
    if not match:
        return False
    value = match.group(1).strip().lower()
    if value in {"bold", "bolder"}:
        return True
    num_match = re.search(r'\d+', value)
    return bool(num_match and int(num_match.group()) >= 600)


def extract_bold_rules(book):
    rules = {"classes": set(), "ids": set(), "tags": set()}
    for item in book.get_items():
        media_type = getattr(item, 'media_type', '') or ''
        file_name = getattr(item, 'file_name', '') or ''
        if 'css' not in media_type and not file_name.lower().endswith('.css'):
            continue
        try:
            css_text = item.get_content().decode('utf-8', errors='ignore')
        except Exception:
            continue
        for selector_block, declaration_block in re.findall(
            r'([^{}]+)\{([^{}]+)\}', css_text, re.DOTALL
        ):
            if not style_declares_bold(declaration_block):
                continue
            for selector in selector_block.split(','):
                selector = selector.strip()
                if not selector:
                    continue
                for class_name in re.findall(r'\.([A-Za-z0-9_-]+)', selector):
                    rules["classes"].add(class_name)
                for id_name in re.findall(r'#([A-Za-z0-9_-]+)', selector):
                    rules["ids"].add(id_name)
                tag_match = re.fullmatch(r'([A-Za-z][A-Za-z0-9_-]*)', selector)
                if tag_match:
                    rules["tags"].add(tag_match.group(1).lower())
    return rules


def node_is_bold(node, inherited_bold, bold_rules):
    if inherited_bold:
        return True
    if not getattr(node, 'name', None):
        return False
    if node.name.lower() in {"b", "strong"}:
        return True
    if node.name.lower() in bold_rules["tags"]:
        return True
    if style_declares_bold(node.get('style', '')):
        return True
    node_id = node.get('id')
    if node_id and node_id in bold_rules["ids"]:
        return True
    for class_name in node.get('class', []) or []:
        if class_name in bold_rules["classes"]:
            return True
    return False


def is_paragraph_like(node):
    node_name = getattr(node, 'name', None)
    if node_name not in PARAGRAPH_LIKE_TAGS:
        return False
    for child in getattr(node, 'contents', []):
        if getattr(child, 'name', None) in PARAGRAPH_LIKE_TAGS:
            return False
    return True


# ==========================================
# --- フォント・パス ユーティリティ ---
# ==========================================

def get_font_list():
    font_dir = Path(__file__).parent / "Font"
    files = (
        list(font_dir.glob("*.ttf"))
        + list(font_dir.glob("*.ttc"))
        + list(font_dir.glob("*.otf"))
    )
    fonts = [f.name for f in files]
    for f in ["C:/Windows/Fonts/msmincho.ttc", "C:/Windows/Fonts/msgothic.ttc"]:
        if os.path.exists(f):
            fonts.append(f)
    return fonts if fonts else ["(フォントなし)"]


def resolve_font_path(font_value):
    if not font_value:
        return None
    font_path = Path(font_value)
    if not font_path.is_absolute():
        font_path = Path(__file__).parent / "Font" / font_value
    return font_path


def require_font_path(font_value):
    """有効なフォントパスを返し、未指定や欠落時は分かりやすい例外を送出する。"""
    font_path = resolve_font_path(font_value)
    if not font_path:
        raise RuntimeError("フォントが指定されていません。")
    if not font_path.exists():
        raise RuntimeError(f"フォントが見つかりません: {font_path}")
    if not font_path.is_file():
        raise RuntimeError(f"フォントパスが不正です: {font_path}")
    return font_path


# ==========================================
# --- 変換対象ユーティリティ ---
# ==========================================

def iter_conversion_targets(target_path):
    if target_path.is_file():
        return [target_path]
    if target_path.is_dir():
        return sorted([p for p in target_path.iterdir() if p.is_file()])
    return []


def should_skip_conversion_target(path):
    return path.stem.endswith("_c") or path.suffix.lower() in {".xtc", ".xtch"}


def _normalize_output_format(value):
    fmt = str(value or 'xtc').strip().lower()
    return 'xtch' if fmt == 'xtch' else 'xtc'

def get_output_path_for_target(path, output_format='xtc'):
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_INPUT_SUFFIXES:
        ext = '.xtch' if _normalize_output_format(output_format) == 'xtch' else '.xtc'
        return path.with_suffix(ext)
    return None


def make_unique_output_path(path):
    path = Path(path)
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    idx = 1
    while True:
        candidate = path.with_name(f"{stem}({idx}){suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


def find_output_conflicts(targets, output_format='xtc'):
    conflicts = []
    for path in targets:
        out_path = get_output_path_for_target(path, output_format)
        if out_path and out_path.exists():
            conflicts.append((path, out_path))
    return conflicts


# ==========================================
# --- 画像フィルタ & XTG/XTC 変換 ---
# ==========================================

def _prepare_canvas_image(img, w, h):
    img = img.convert("L")
    img.thumbnail((w, h), Image.Resampling.LANCZOS)
    background = Image.new("L", (w, h), 255)
    offset = ((w - img.width) // 2, (h - img.height) // 2)
    background.paste(img, offset)
    return background


def apply_xtc_filter(img, dither, threshold, w, h):
    background = _prepare_canvas_image(img, w, h)
    if dither:
        return background.convert("1", dither=Image.FLOYDSTEINBERG)
    return background.point(lambda x: 255 if x > threshold else 0, mode="1")


def apply_xtch_filter(img, dither, threshold, w, h):
    background = _prepare_canvas_image(img, w, h)
    bias = max(-48, min(48, int(threshold) - 128))
    t1 = max(16, min(96, 64 + bias // 2))
    t2 = max(t1 + 16, min(176, 128 + bias))
    t3 = max(t2 + 16, min(240, 192 + bias // 2))
    if dither:
        work = background.copy()
        px = work.load()
        for y in range(h):
            for x in range(w):
                old = int(px[x, y])
                if old <= t1:
                    newv = 0
                elif old <= t2:
                    newv = 85
                elif old <= t3:
                    newv = 170
                else:
                    newv = 255
                err = old - newv
                px[x, y] = newv
                if x + 1 < w:
                    px[x + 1, y] = max(0, min(255, int(px[x + 1, y] + err * 7 / 16)))
                if y + 1 < h:
                    if x > 0:
                        px[x - 1, y + 1] = max(0, min(255, int(px[x - 1, y + 1] + err * 3 / 16)))
                    px[x, y + 1] = max(0, min(255, int(px[x, y + 1] + err * 5 / 16)))
                    if x + 1 < w:
                        px[x + 1, y + 1] = max(0, min(255, int(px[x + 1, y + 1] + err * 1 / 16)))
        background = work
    def q(v):
        v = int(v)
        if v <= t1:
            return 0
        if v <= t2:
            return 85
        if v <= t3:
            return 170
        return 255
    return background.point(q, mode='L')


def png_to_xtg_bytes(img, w, h, args):
    bw_img = apply_xtc_filter(img, args.dither, args.threshold, w, h)
    if getattr(args, 'night_mode', False):
        bw_img = ImageOps.invert(bw_img.convert("L"))
    row_bytes = (w + 7) // 8
    data = bytearray(row_bytes * h)
    pixels = bw_img.load()
    for y in range(h):
        for x in range(w):
            if pixels[x, y] > 0:
                data[y * row_bytes + (x // 8)] |= 1 << (7 - (x % 8))
    md5 = hashlib.md5(data).digest()[:8]
    return struct.pack("<4sHHBBI8s", b"XTG\x00", w, h, 0, 0, len(data), md5) + data


def png_to_xth_bytes(img, w, h, args):
    gray_img = apply_xtch_filter(img, args.dither, args.threshold, w, h)
    if getattr(args, 'night_mode', False):
        gray_img = ImageOps.invert(gray_img.convert('L'))
    pixels = gray_img.load()
    plane_size = ((w * h) + 7) // 8
    plane1 = bytearray(plane_size)
    plane2 = bytearray(plane_size)

    def to_val(v):
        v = int(v)
        if v >= 213:
            return 0
        if v >= 128:
            return 2
        if v >= 43:
            return 1
        return 3

    bit_index = 0
    for x in range(w - 1, -1, -1):
        for y in range(h):
            val = to_val(pixels[x, y])
            byte_index = bit_index >> 3
            shift = 7 - (bit_index & 7)
            if (val >> 1) & 1:
                plane1[byte_index] |= 1 << shift
            if val & 1:
                plane2[byte_index] |= 1 << shift
            bit_index += 1
    data = bytes(plane1 + plane2)
    md5 = hashlib.md5(data).digest()[:8]
    return struct.pack("<4sHHBBI8s", b"XTH\x00", w, h, 0, 0, len(data), md5) + data


def build_xtc(page_blobs, out_path, w, h, output_format='xtc'):
    cnt = len(page_blobs)
    if cnt == 0:
        raise ValueError("変換データがありません。")
    idx_off = 48
    data_off = 48 + cnt * 16
    idx_table = bytearray()
    curr_off = data_off
    for b in page_blobs:
        idx_table += struct.pack("<Q I H H", curr_off, len(b), w, h)
        curr_off += len(b)
    mark = b"XTCH" if _normalize_output_format(output_format) == 'xtch' else b"XTC\x00"
    header = struct.pack("<4sHHBBBBIQQQQ", mark, 1, cnt, 1, 0, 0, 0, 0, 0, idx_off, data_off, 0)
    with open(out_path, "wb") as f:
        f.write(header)
        f.write(idx_table)
        for blob in page_blobs:
            f.write(blob)


def page_image_to_xt_bytes(img, w, h, args):
    return png_to_xth_bytes(img, w, h, args) if _normalize_output_format(getattr(args, 'output_format', 'xtc')) == 'xtch' else png_to_xtg_bytes(img, w, h, args)


def _normalize_progress_bar_side(value):
    value = str(value or 'left').strip().lower()
    return value if value in {'left', 'right'} else 'left'


def _arg_get(args, key, default=None):
    return args.get(key, default) if isinstance(args, dict) else getattr(args, key, default)


def _arg_bool(args, key, default=False):
    value = _arg_get(args, key, default)
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def _load_progress_font():
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _text_size(draw, text, font):
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        return draw.textlength(text, font=font), 8


def _chapter_bounds_for_page(page_index, page_count, chapter_starts):
    starts = set()
    for value in chapter_starts or []:
        try:
            start = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= start < page_count:
            starts.add(start)
    starts = sorted(starts)
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    bounds = starts + [page_count]
    chapter_start, chapter_end = 0, page_count
    for idx, start in enumerate(starts):
        end = bounds[idx + 1]
        if start <= page_index < end:
            chapter_start, chapter_end = start, end
            break
    return starts, chapter_start, chapter_end


def draw_page_progress_bar(img, page_index, page_count, args, chapter_starts=None):
    """本文ページの端に章区切り付きの読書進捗バーを焼き込む。"""
    if not _arg_bool(args, 'progress_bar', False) or page_count <= 1:
        return img

    side = _normalize_progress_bar_side(_arg_get(args, 'progress_bar_side', 'left'))
    w, h = img.size
    top = max(1, min(h - 2, int(_arg_get(args, 'margin_t', 0))))
    bottom = max(top + 1, min(h - 2, h - int(_arg_get(args, 'margin_b', 0)) - 1))
    height = bottom - top
    filled_y = max(top, min(bottom, top + int(round(height * (page_index + 1) / page_count))))
    margin_l = int(_arg_get(args, 'margin_l', 0))
    margin_r = int(_arg_get(args, 'margin_r', 0))
    x = max(3, min(w - 4, margin_l // 2)) if side == 'left' else max(3, min(w - 4, w - max(2, margin_r // 2) - 1))

    draw = ImageDraw.Draw(img)
    output_format = _normalize_output_format(_arg_get(args, 'output_format', 'xtc'))
    track_fill = 176 if output_format == 'xtch' else 255
    starts, _chapter_start, chapter_end = _chapter_bounds_for_page(page_index, page_count, chapter_starts)
    remaining_in_chapter = max(0, chapter_end - page_index - 1)

    if output_format == 'xtch':
        draw.line((x, top, x, bottom), fill=track_fill, width=3)
        draw.line((x, top, x, filled_y), fill=0, width=3)
    else:
        draw.rectangle((x - 2, top, x + 2, bottom), outline=0, fill=track_fill)
        draw.rectangle((x - 2, top, x + 2, filled_y), fill=0)

    tick_half = 4
    for start in starts[1:]:
        y = max(top, min(bottom, top + int(round(height * start / page_count))))
        draw.line((x - tick_half, y, x + tick_half, y), fill=0, width=1)

    label = f"{page_index + 1}/{page_count} {remaining_in_chapter} left"
    label_font = _load_progress_font()
    label_w, label_h = _text_size(draw, label, label_font)
    label_y = min(h - int(label_h) - 1, bottom + 2)
    if label_y <= bottom:
        label_y = max(top, bottom - int(label_h))
    label_x = x + 6 if side == 'left' else x - int(label_w) - 6
    label_x = max(1, min(w - int(label_w) - 1, label_x))
    draw.text((label_x, label_y), label, fill=0, font=label_font)
    return img


def apply_page_progress_bars(rendered_pages, args, chapter_starts=None):
    if not _arg_bool(args, 'progress_bar', False):
        return rendered_pages
    page_count = len(rendered_pages)
    result = []
    for page_index, (page_image, is_illustration) in enumerate(rendered_pages):
        if is_illustration:
            result.append((page_image, is_illustration))
            continue
        draw_page_progress_bar(page_image, page_index, page_count, args, chapter_starts=chapter_starts)
        result.append((page_image, is_illustration))
    return result


# ==========================================
# --- プレビュー生成 ---
# ==========================================

def generate_preview_base64(args):
    try:
        w = int(args.get('width', DEF_WIDTH))
        h = int(args.get('height', DEF_HEIGHT))
        dither = args.get('dither') == 'true'
        threshold = int(args.get('threshold', 128))
        mode = args.get('mode', 'text')
        night_mode = str(args.get('night_mode', '')).lower() == 'true'
        kinsoku_mode = _normalize_kinsoku_mode(args.get('kinsoku_mode', 'standard'))
        output_format = _normalize_output_format(args.get('output_format', 'xtc'))

        if mode == 'image':
            file_b64 = args.get('file_b64')
            if file_b64:
                _header, encoded = file_b64.split(",", 1)
                src_img = Image.open(io.BytesIO(base64.b64decode(encoded)))
            else:
                src_img = Image.new('L', (w, h), 255)
                draw = ImageDraw.Draw(src_img)
                for i in range(16):
                    draw.rectangle(
                        [0, i * (h // 16), w, (i + 1) * (h // 16)],
                        fill=int(255 * (i / 15)),
                    )
            res_img = apply_xtch_filter(src_img, dither, threshold, w, h) if output_format == 'xtch' else apply_xtc_filter(src_img, dither, threshold, w, h)

        else:
            res_img = Image.new('L', (w, h), 255)
            draw = ImageDraw.Draw(res_img)
            f_size = int(args.get('font_size', 26))
            r_size = int(args.get('ruby_size', 12))
            l_space = int(args.get('line_spacing', 44))
            m_t = int(args.get('margin_t', 12))
            m_b = int(args.get('margin_b', 14))
            m_r = int(args.get('margin_r', 12))
            m_l = int(args.get('margin_l', 12))

            font_file = args.get('font_file', "")
            font_p = require_font_path(font_file)
            font = ImageFont.truetype(str(font_p), f_size)
            ruby_font = ImageFont.truetype(str(font_p), r_size)

            # 青空文庫『吾輩は猫である』公開XHTMLの冒頭段落構成に合わせて、
            # 文中の不必要な改行を入れず、段落単位でプレビュー用に構成する。
            preview_blocks = [
                {
                    "indent": True,
                    "blank_before": 0,
                    "runs": [
                        {"text": "吾輩", "ruby": "わがはい", "bold": False},
                        {"text": "は猫である。名前はまだ無い。", "ruby": "", "bold": False},
                    ],
                },
                {
                    "indent": True,
                    "blank_before": 1,
                    "runs": [
                        {"text": "どこで生れたかとんと", "ruby": "", "bold": False},
                        {"text": "見当", "ruby": "けんとう", "bold": False},
                        {"text": "がつかぬ。何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは", "ruby": "", "bold": False},
                        {"text": "記憶", "ruby": "きおく", "bold": False},
                        {"text": "している。", "ruby": "", "bold": False},
                    ],
                },
                {
                    "indent": True,
                    "blank_before": 1,
                    "runs": [
                        {"text": "吾輩はここで始めて人間というものを見た。しかもあとで聞くとそれは書生という人間中で一番", "ruby": "", "bold": False},
                        {"text": "獰悪", "ruby": "どうあく", "bold": False},
                        {"text": "な種族であったそうだ。", "ruby": "", "bold": False},
                    ],
                },
                {
                    "indent": True,
                    "blank_before": 1,
                    "runs": [
                        {"text": "この書生というのは時々我々を", "ruby": "", "bold": False},
                        {"text": "捕", "ruby": "つかま", "bold": False},
                        {"text": "えて", "ruby": "", "bold": False},
                        {"text": "煮", "ruby": "に", "bold": False},
                        {"text": "て食うという話である。しかしその当時は何という考もなかったから別段恐しいとも思わなかった。ただ彼の", "ruby": "", "bold": False},
                        {"text": "掌", "ruby": "てのひら", "bold": False},
                        {"text": "に載せられてスーと持ち上げられた時何だかフワフワした感じがあったばかりである。", "ruby": "", "bold": False},
                    ],
                },
                {
                    "indent": True,
                    "blank_before": 1,
                    "runs": [
                        {"text": "掌", "ruby": "てのひら", "bold": False},
                        {"text": "の上で少し落ちついて書生の顔を見たのがいわゆる人間というものの", "ruby": "", "bold": False},
                        {"text": "見始", "ruby": "みはじめ", "bold": False},
                        {"text": "であろう。この時妙なものだと思った感じが今でも残っている。第一毛をもって装飾されべきはずの顔がつるつるしてまるで", "ruby": "", "bold": False},
                        {"text": "薬缶", "ruby": "やかん", "bold": False},
                        {"text": "だ。その後猫にもだいぶ逢ったがこんな", "ruby": "", "bold": False},
                        {"text": "片輪", "ruby": "かたわ", "bold": False},
                        {"text": "には一度も出会わした事がない。", "ruby": "", "bold": False},
                    ],
                },
                {
                    "indent": True,
                    "blank_before": 1,
                    "runs": [
                        {"text": "のみならず顔の真中があまりに突起している。そうしてその穴の中から時々ぷうぷうと", "ruby": "", "bold": False},
                        {"text": "煙", "ruby": "けむり", "bold": False},
                        {"text": "を吹く。どうも", "ruby": "", "bold": False},
                        {"text": "咽", "ruby": "む", "bold": False},
                        {"text": "せぽくて実に弱った。これが人間の飲む", "ruby": "", "bold": False},
                        {"text": "煙草", "ruby": "たばこ", "bold": False},
                        {"text": "というものである事はようやくこの頃知った。", "ruby": "", "bold": False},
                    ],
                },
                {
                    "indent": True,
                    "blank_before": 1,
                    "runs": [
                        {"text": "この書生の掌の", "ruby": "", "bold": False},
                        {"text": "裏", "ruby": "うち", "bold": False},
                        {"text": "でしばらくはよい心持に坐っておったが、しばらくすると非常な速力で運転し始めた。", "ruby": "", "bold": False},
                    ],
                },
            ]

            curr_x = w - f_size - (r_size + 4) - m_r
            curr_y = m_t
            running = True
            has_drawn_content = False

            def preview_advance_column(count=1):
                nonlocal curr_x, curr_y, running
                for _ in range(max(0, count)):
                    curr_y = m_t
                    curr_x -= l_space
                    if curr_x < m_l:
                        running = False
                        break

            def preview_insert_paragraph_indent():
                nonlocal curr_y, has_drawn_content
                if not running:
                    return
                if curr_y > h - m_b - f_size:
                    preview_advance_column(1)
                if not running:
                    return
                draw_char_tate(draw, '　', (curr_x, curr_y), font, f_size)
                curr_y += f_size + 2
                has_drawn_content = True

            def draw_preview_run(run):
                nonlocal curr_x, curr_y, running, has_drawn_content
                seg_text = run.get("text", "")
                ruby = run.get("ruby", "")
                is_bold = bool(run.get("bold", False))
                seg_start_x = curr_x
                seg_start_y = curr_y
                drawn_chars = 0

                seg_tokens = _tokenize_vertical_text(seg_text)
                idx = 0
                while idx < len(seg_tokens):
                    action = _choose_vertical_layout_action(
                        seg_tokens, idx, curr_y, m_t, h, m_b, f_size,
                        kinsoku_mode=kinsoku_mode,
                    )
                    if action == 'advance':
                        preview_advance_column(1)
                        if not running:
                            break
                        seg_start_x = curr_x
                        seg_start_y = curr_y
                        drawn_chars = 0
                        continue

                    char = seg_tokens[idx]
                    if action == 'hang_pair':
                        if _is_lowerable_hanging_closing_bracket(char):
                            draw_hanging_closing_bracket(draw, char, (curr_x, curr_y), font, f_size, h, is_bold=is_bold)
                        else:
                            draw_char_tate(draw, char, (curr_x, curr_y), font, f_size, is_bold=is_bold)
                        drawn_chars += 1
                        draw_hanging_punctuation(
                            draw, seg_tokens[idx + 1], (curr_x, curr_y), font, f_size, h, is_bold=is_bold
                        )
                        drawn_chars += 1
                        has_drawn_content = True
                        preview_advance_column(1)
                        if not running:
                            break
                        seg_start_x = curr_x
                        seg_start_y = curr_y
                        drawn_chars = 0
                        idx += 2
                        continue

                    draw_char_tate(draw, char, (curr_x, curr_y), font, f_size, is_bold=is_bold)
                    drawn_chars += 1
                    curr_y += f_size + 2
                    has_drawn_content = True
                    idx += 1

                if ruby and drawn_chars > 0 and seg_start_x >= m_l:
                    rb_h = drawn_chars * (f_size + 2)
                    rt_h = len(ruby) * (r_size + 2)
                    ruby_y = seg_start_y + max(0, (rb_h - rt_h) // 2)
                    ruby_x = seg_start_x + f_size + 1
                    for r_char in ruby:
                        if ruby_y < h - m_b:
                            draw_char_tate(
                                draw, r_char, (ruby_x, ruby_y), ruby_font, r_size,
                                is_bold=is_bold, ruby_mode=True,
                            )
                        ruby_y += r_size + 2

            first_block = True
            for block in preview_blocks:
                if not running:
                    break
                gap = int(block.get("blank_before", 0) or 0)
                if first_block:
                    first_block = False
                elif has_drawn_content:
                    preview_advance_column(max(1, gap))
                    if not running:
                        break
                if block.get("indent", False):
                    preview_insert_paragraph_indent()
                    if not running:
                        break
                for run in block.get("runs", []):
                    draw_preview_run(run)
                    if not running:
                        break
                    if run.get("break_after"):
                        preview_advance_column(1)
                        if not running:
                            break

            draw_page_progress_bar(res_img, 3, 10, args, chapter_starts=[0, 2, 5, 8])

        if mode != 'image' and output_format == 'xtch':
            res_img = apply_xtch_filter(res_img, dither, threshold, w, h)
        elif mode != 'image':
            res_img = apply_xtc_filter(res_img, dither, threshold, w, h)
        if night_mode:
            res_img = ImageOps.invert(res_img.convert('L'))

        buf = io.BytesIO()
        res_img.convert('RGB').save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode('utf-8')

    except Exception as e:
        print(f"Preview Error: {e}")
        raise RuntimeError(f"プレビュー生成に失敗しました: {e}") from e


# ==========================================
# --- アーカイブ / EPUB 変換 ---
# ==========================================

def process_image_data(data, args):
    """画像データを読み込んでサイズ調整し、出力形式に応じた XTG / XTH バイト列を返す。"""
    try:
        with Image.open(io.BytesIO(data)) as s_img:
            s_img = s_img.convert("L")
            s_img.thumbnail((args.width, args.height), Image.Resampling.LANCZOS)
            bg = Image.new('L', (args.width, args.height), 255)
            bg.paste(s_img, ((args.width - s_img.width) // 2, (args.height - s_img.height) // 2))
            return page_image_to_xt_bytes(bg, args.width, args.height, args)
    except Exception as e:
        print(f"画像処理エラー: {e}")
        return None




def read_text_file_with_fallback(text_path):
    """TXT / Markdown を UTF-8(BOM付き含む) → CP932 の順で読み込む。"""
    raw = Path(text_path).read_bytes()
    last_error = None
    for encoding in ('utf-8-sig', 'cp932'):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    raise UnicodeDecodeError(
        last_error.encoding if last_error else 'unknown',
        last_error.object if last_error else raw,
        last_error.start if last_error else 0,
        last_error.end if last_error else 1,
        'テキストを UTF-8 / CP932 として読み込めませんでした。'
    )



def _markdown_inline_to_runs(value):
    value = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', lambda m: (m.group(1) or '').strip(), value)
    value = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', lambda m: (m.group(1) or m.group(2)).strip(), value)
    value = re.sub(r'`([^`]+)`', lambda m: m.group(1), value)
    runs = []
    pattern = re.compile(
        r'(\*\*\*.+?\*\*\*|'
        r'(?<!\w)___(?!_).+?(?<!_)___(?!\w)|'
        r'\*\*.+?\*\*|'
        r'(?<!\w)__(?!_).+?(?<!_)__(?!\w)|'
        r'\*(?!\*)(.+?)\*(?!\*)|'
        r'(?<!\w)_(?!_).+?(?<!_)_(?!\w))'
    )
    pos = 0
    for match in pattern.finditer(value):
        if match.start() > pos:
            runs.append({'text': value[pos:match.start()], 'bold': False, 'italic': False})
        token = match.group(0)
        bold = italic = False
        inner = token
        if (token.startswith('***') and token.endswith('***')) or (token.startswith('___') and token.endswith('___')):
            inner = token[3:-3]
            bold = True
            italic = True
        elif (token.startswith('**') and token.endswith('**')) or (token.startswith('__') and token.endswith('__')):
            inner = token[2:-2]
            bold = True
        elif (token.startswith('*') and token.endswith('*')) or (token.startswith('_') and token.endswith('_')):
            inner = token[1:-1]
            italic = True
        if inner:
            runs.append({'text': inner, 'bold': bold, 'italic': italic})
        pos = match.end()
    if pos < len(value):
        runs.append({'text': value[pos:], 'bold': False, 'italic': False})
    merged = []
    for run in runs:
        if not run['text']:
            continue
        if merged and merged[-1]['bold'] == run['bold'] and merged[-1]['italic'] == run['italic']:
            merged[-1]['text'] += run['text']
        else:
            merged.append(run)
    return merged


def _plain_inline_to_runs(value):

    return [{'text': value, 'bold': False, 'italic': False}] if value else []


def _normalize_text_line(value, has_started_document=False, strip_leading_for_indent=False):
    value = value.replace('\ufeff', '').replace('\r', '').replace('\t', '    ').replace('\xa0', ' ')
    if not has_started_document:
        value = START_TEXT_RE.sub('', value, count=1)
    value = value.rstrip()
    if strip_leading_for_indent:
        value = re.sub(r'^[\s\u3000]+', '', value)
    return value


def _blocks_from_plain_text(text):
    blocks = []
    for raw_line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        normalized = _normalize_text_line(raw_line, has_started_document=bool(blocks), strip_leading_for_indent=False)
        if not normalized:
            blocks.append({'kind': 'blank'})
            continue
        blocks.append({
            'kind': 'paragraph',
            'runs': _plain_inline_to_runs(normalized),
            'indent': True,
            'blank_before': 1,
        })
    return blocks


def _blocks_from_markdown(text):
    blocks = []
    in_code = False
    for raw_line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        source = raw_line.replace('\ufeff', '').replace('\t', '    ')
        stripped = source.strip()
        if stripped.startswith('```'):
            in_code = not in_code
            continue
        if in_code:
            normalized = _normalize_text_line(source, has_started_document=bool(blocks), strip_leading_for_indent=False)
            if not normalized:
                blocks.append({'kind': 'blank'})
            else:
                blocks.append({'kind': 'code', 'runs': _plain_inline_to_runs(normalized), 'indent': False, 'blank_before': 1})
            continue
        if not stripped:
            blocks.append({'kind': 'blank'})
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if heading:
            level = len(heading.group(1))
            content = _normalize_text_line(heading.group(2), has_started_document=bool(blocks), strip_leading_for_indent=True)
            runs = _markdown_inline_to_runs(content)
            if runs:
                for run in runs:
                    run['bold'] = True
                blocks.append({'kind': 'heading', 'runs': runs, 'indent': False, 'blank_before': 2 if level <= 2 else 1})
            continue
        bullet = re.match(r'^\s*[-*+]\s+(.*)$', source)
        if bullet:
            content = _normalize_text_line(bullet.group(1), has_started_document=bool(blocks), strip_leading_for_indent=True)
            runs = [{'text': '・', 'bold': False, 'italic': False}] + _markdown_inline_to_runs(content)
            blocks.append({'kind': 'bullet', 'runs': runs, 'indent': False, 'blank_before': 1})
            continue
        normalized = _normalize_text_line(source, has_started_document=bool(blocks), strip_leading_for_indent=True)
        if normalized:
            blocks.append({'kind': 'paragraph', 'runs': _markdown_inline_to_runs(normalized), 'indent': True, 'blank_before': 1})
    return blocks


def _has_renderable_text_blocks(blocks):
    for block in blocks:
        if block.get('kind') == 'blank':
            continue
        runs = block.get('runs', [])
        if any(run.get('text', '') for run in runs):
            return True
    return False


def _render_text_blocks_to_xtc(blocks, source_path, font_path, args, output_path=None):
    if not _has_renderable_text_blocks(blocks):
        raise RuntimeError("入力ファイルに変換できる本文がありません。")

    font_path = require_font_path(font_path)
    font = ImageFont.truetype(str(font_path), args.font_size)
    rendered_pages = []

    def add_page(image):
        rendered_pages.append((image.copy(), False))

    img = Image.new('L', (args.width, args.height), 255)
    draw = ImageDraw.Draw(img)
    curr_x = args.width - args.font_size - (args.ruby_size + 4) - args.margin_r
    curr_y = args.margin_t
    has_drawn_on_page = False

    def new_blank_page():
        nonlocal img, draw, curr_x, curr_y, has_drawn_on_page
        img = Image.new('L', (args.width, args.height), 255)
        draw = ImageDraw.Draw(img)
        curr_x = args.width - args.font_size - (args.ruby_size + 4) - args.margin_r
        curr_y = args.margin_t
        has_drawn_on_page = False

    def flush_page_if_needed():
        if has_drawn_on_page:
            add_page(img)
        new_blank_page()

    def advance_column(count=1):
        nonlocal curr_x, curr_y
        for _ in range(max(0, count)):
            curr_y = args.margin_t
            curr_x -= args.line_spacing
            if curr_x < args.margin_l:
                flush_page_if_needed()

    def ensure_room(char_height=None):
        nonlocal curr_y
        char_height = char_height or args.font_size
        if curr_y > args.height - args.margin_b - char_height:
            advance_column(1)

    def insert_paragraph_indent():
        nonlocal curr_y, has_drawn_on_page
        ensure_room(args.font_size)
        draw_char_tate(draw, '　', (curr_x, curr_y), font, args.font_size)
        curr_y += args.font_size + 2
        has_drawn_on_page = True

    def draw_runs(runs):
        nonlocal curr_y, has_drawn_on_page

        tokens = []
        for run in runs:
            text_value = run.get('text', '')
            is_bold = bool(run.get('bold'))
            is_italic = bool(run.get('italic'))
            for token in _tokenize_vertical_text(text_value):
                tokens.append({
                    'text': token,
                    'bold': is_bold,
                    'italic': is_italic,
                })
        token_texts = [token['text'] for token in tokens]

        idx = 0
        while idx < len(tokens):
            token_info = tokens[idx]
            action = _choose_vertical_layout_action(
                token_texts, idx, curr_y, args.margin_t, args.height, args.margin_b, args.font_size,
                kinsoku_mode=args.kinsoku_mode,
            )
            if action == 'advance':
                advance_column(1)
                continue
            if action == 'hang_pair':
                ensure_room(args.font_size)
                if _is_lowerable_hanging_closing_bracket(token_info['text']):
                    draw_hanging_closing_bracket(
                        draw, token_info['text'], (curr_x, curr_y), font, args.font_size, args.height,
                        is_bold=token_info['bold'], is_italic=token_info['italic'],
                    )
                else:
                    draw_char_tate(
                        draw, token_info['text'], (curr_x, curr_y), font, args.font_size,
                        is_bold=token_info['bold'], is_italic=token_info['italic'],
                    )
                draw_hanging_punctuation(
                    draw, tokens[idx + 1]['text'], (curr_x, curr_y), font, args.font_size, args.height,
                    is_bold=tokens[idx + 1]['bold'], is_italic=tokens[idx + 1]['italic'],
                )
                has_drawn_on_page = True
                advance_column(1)
                idx += 2
                continue
            ensure_room(args.font_size)
            draw_char_tate(
                draw, token_info['text'], (curr_x, curr_y), font, args.font_size,
                is_bold=token_info['bold'], is_italic=token_info['italic'],
            )
            curr_y += args.font_size + 2
            has_drawn_on_page = True
            idx += 1

    first_content = True
    previous_was_blank = False
    for block in blocks:
        if block.get('kind') == 'blank':
            previous_was_blank = True
            continue
        gap = block.get('blank_before', 1)
        if first_content:
            first_content = False
        elif has_drawn_on_page:
            advance_column(max(gap, 2 if previous_was_blank else gap))
        previous_was_blank = False
        if block.get('indent', False):
            insert_paragraph_indent()
        draw_runs(block.get('runs', []))

    if has_drawn_on_page:
        add_page(img)

    out_path = Path(output_path) if output_path else Path(source_path).with_suffix('.xtc')
    rendered_pages = apply_page_progress_bars(rendered_pages, args, chapter_starts=[0])
    page_blobs = [page_image_to_xt_bytes(page_image, args.width, args.height, args) for page_image, _ in rendered_pages]
    build_xtc(page_blobs, out_path, args.width, args.height, getattr(args, 'output_format', 'xtc'))
    return out_path


def process_text_file(text_path, font_path, args, output_path=None):
    """プレーンテキストを縦書き XTC へ変換する。

    簡易対応として、改行は段落区切りとして扱い、ルビ記法は解釈しない。
    """
    text, _encoding = read_text_file_with_fallback(text_path)
    return _render_text_blocks_to_xtc(_blocks_from_plain_text(text), text_path, font_path, args, output_path=output_path)


def process_markdown_file(text_path, font_path, args, output_path=None):
    """Markdown を簡易整形して縦書き XTC へ変換する。"""
    text, _encoding = read_text_file_with_fallback(text_path)
    return _render_text_blocks_to_xtc(_blocks_from_markdown(text), text_path, font_path, args, output_path=output_path)


def process_archive(archive_path, args, output_path=None):
    """ZIP / CBZ / CBR / RAR 形式の画像アーカイブを XTC へ変換する。"""
    archive_path = Path(archive_path)
    print(f"\n[アーカイブ変換開始] {archive_path.name}")
    ext = '.xtch' if _normalize_output_format(getattr(args, 'output_format', 'xtc')) == 'xtch' else '.xtc'
    out_path = Path(output_path) if output_path else archive_path.with_suffix(ext)
    tqdm = _require_tqdm()
    xtg_blobs = []

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            patoolib = _require_patoolib()
            patoolib.extract_archive(str(archive_path), outdir=tmpdir, verbosity=-1)
        except Exception as e:
            raise RuntimeError(f"解凍に失敗しました: {e}") from e

        img_files = sorted(
            [p for p in Path(tmpdir).rglob("*") if p.suffix.lower() in IMG_EXTS],
            key=lambda p: _natural_sort_key(p.relative_to(tmpdir)),
        )
        print(f"デバッグ: {len(img_files)} 枚の画像を検出しました。変換を開始します...")

        for img_p in tqdm(img_files, desc="通常変換中", unit="枚", leave=False):
            try:
                with open(img_p, 'rb') as f:
                    blob = process_image_data(f.read(), args)
                    if blob:
                        xtg_blobs.append(blob)
            except Exception as e:
                print(f"画像スキップ ({img_p.name}): {e}")
                continue

    if len(xtg_blobs) > 0:
        build_xtc(xtg_blobs, out_path, args.width, args.height, getattr(args, 'output_format', 'xtc'))
        print(f"✓ 通常変換完了: {out_path.name}")
        return out_path
    raise RuntimeError("変換できる画像が見つかりませんでした。")


def process_epub(epub_path, font_path, args, output_path=None):
    """EPUB ファイルを縦書き XTC へ変換する。"""
    epub_path = Path(epub_path)
    epub = _require_ebooklib_epub()
    BeautifulSoup = _require_bs4_beautifulsoup()
    tqdm = _require_tqdm()
    book = epub.read_epub(str(epub_path))
    font_path = require_font_path(font_path)
    font = ImageFont.truetype(str(font_path), args.font_size)
    ruby_font = ImageFont.truetype(str(font_path), args.ruby_size)
    rendered_pages = []
    chapter_starts = []
    bold_rules = extract_bold_rules(book)

    def add_page(image, is_illustration=False):
        rendered_pages.append((image.copy(), is_illustration))

    def normalize_epub_href(href):
        """EPUB 内 href を比較しやすい相対 POSIX パスへ正規化する。"""
        value = str(href or '').strip().replace('\\', '/')
        if not value:
            return ''
        if value.startswith('data:') or '://' in value:
            return ''
        value = unquote(value).split('#', 1)[0].split('?', 1)[0]
        if not value:
            return ''
        norm = posixpath.normpath(value)
        if norm in {'', '.'}:
            return ''
        return norm.lstrip('/')

    image_map = {
        normalize_epub_href(getattr(item, 'file_name', '')): item.get_content()
        for item in book.get_items()
        if (
            getattr(item, 'media_type', '').startswith('image/')
            or Path(getattr(item, 'file_name', '')).suffix.lower() in IMG_EXTS
        )
        and normalize_epub_href(getattr(item, 'file_name', ''))
    }
    image_basename_map = {}
    for normalized_name, image_bytes in image_map.items():
        image_basename_map.setdefault(posixpath.basename(normalized_name), []).append((normalized_name, image_bytes))

    def resolve_epub_image_data(doc_file_name, raw_src):
        """文書相対パスを考慮して EPUB 内画像を解決する。"""
        src_norm = normalize_epub_href(raw_src)
        if not src_norm:
            return None, None

        candidates = []
        doc_norm = normalize_epub_href(doc_file_name)
        if doc_norm:
            base_dir = posixpath.dirname(doc_norm)
            joined = normalize_epub_href(posixpath.join(base_dir, src_norm))
            if joined:
                candidates.append(joined)
        candidates.append(src_norm)

        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate in image_map:
                return candidate, image_map[candidate]

        basename = posixpath.basename(src_norm)
        basename_matches = image_basename_map.get(basename, [])
        if len(basename_matches) == 1:
            return basename_matches[0]

        return None, None

    docs = []
    for item_id in book.spine:
        item_key = item_id[0] if isinstance(item_id, tuple) else item_id
        it = book.get_item_with_id(item_key)
        if not it:
            continue
        media_type = (getattr(it, 'media_type', '') or '').lower()
        file_name = (getattr(it, 'file_name', '') or '').lower()
        if (
            media_type in ('application/xhtml+xml', 'text/html')
            or file_name.endswith(('.xhtml', '.html', '.htm'))
        ):
            docs.append(it)

    for item in tqdm(docs, desc="描画中", unit="章", leave=False):
        chapter_start = len(rendered_pages)
        current_doc_file_name = getattr(item, 'file_name', '')
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        body = soup.find('body') or soup
        img = Image.new('L', (args.width, args.height), 255)
        draw = ImageDraw.Draw(img)
        curr_x = args.width - args.font_size - (args.ruby_size + 4) - args.margin_r
        curr_y = args.margin_t
        has_drawn_on_page = False
        has_started_document = False
        pending_paragraph_indent = False

        def new_blank_page():
            nonlocal img, draw, curr_x, curr_y, has_drawn_on_page
            img = Image.new('L', (args.width, args.height), 255)
            draw = ImageDraw.Draw(img)
            curr_x = args.width - args.font_size - (args.ruby_size + 4) - args.margin_r
            curr_y = args.margin_t
            has_drawn_on_page = False

        def flush_page_if_needed():
            nonlocal has_drawn_on_page
            if has_drawn_on_page:
                add_page(img)
            new_blank_page()

        def advance_column(count=1):
            nonlocal curr_x, curr_y
            for _ in range(count):
                curr_y = args.margin_t
                curr_x -= args.line_spacing
                if curr_x < args.margin_l:
                    flush_page_if_needed()

        def ensure_room(char_height=None):
            nonlocal curr_y
            char_height = char_height or args.font_size
            if curr_y > args.height - args.margin_b - char_height:
                advance_column(1)

        def insert_paragraph_indent():
            nonlocal curr_y, pending_paragraph_indent, has_drawn_on_page, has_started_document
            if not pending_paragraph_indent:
                return
            ensure_room(args.font_size)
            draw_char_tate(draw, '　', (curr_x, curr_y), font, args.font_size)
            curr_y += args.font_size + 2
            pending_paragraph_indent = False
            has_drawn_on_page = True
            has_started_document = True

        def normalize_text(text, strip_leading_for_indent=False):
            text = text.replace('\r', '').replace('\n', '').replace('\xa0', ' ')
            if not has_started_document:
                text = START_TEXT_RE.sub('', text, count=1)
            text = text.rstrip()
            if strip_leading_for_indent:
                text = re.sub(r'^[\s\u3000]+', '', text)
            return text

        def draw_text_run(text, is_bold=False, segment_infos=None):
            nonlocal curr_y, has_drawn_on_page, has_started_document
            if not text:
                return

            tokens = _tokenize_vertical_text(text)
            idx = 0
            while idx < len(tokens):
                token = tokens[idx]
                action = _choose_vertical_layout_action(
                    tokens, idx, curr_y, args.margin_t, args.height, args.margin_b, args.font_size,
                    kinsoku_mode=args.kinsoku_mode,
                )
                if action == 'advance':
                    advance_column(1)
                    continue
                if action == 'hang_pair':
                    ensure_room(args.font_size)
                    if _is_lowerable_hanging_closing_bracket(token):
                        draw_hanging_closing_bracket(draw, token, (curr_x, curr_y), font, args.font_size, args.height, is_bold=is_bold)
                    else:
                        draw_char_tate(draw, token, (curr_x, curr_y), font, args.font_size, is_bold=is_bold)
                    draw_hanging_punctuation(draw, tokens[idx + 1], (curr_x, curr_y), font, args.font_size, args.height, is_bold=is_bold)
                    if segment_infos is not None:
                        segment_infos.append({
                            'page_index': len(rendered_pages),
                            'x': curr_x,
                            'y': curr_y,
                            'base_len': len(token) + len(tokens[idx + 1]),
                        })
                    has_drawn_on_page = True
                    has_started_document = True
                    advance_column(1)
                    idx += 2
                    continue
                ensure_room(args.font_size)
                draw_char_tate(draw, token, (curr_x, curr_y), font, args.font_size, is_bold=is_bold)
                if segment_infos is not None:
                    segment_infos.append({
                        'page_index': len(rendered_pages),
                        'x': curr_x,
                        'y': curr_y,
                        'base_len': len(token),
                    })
                curr_y += args.font_size + 2
                has_drawn_on_page = True
                has_started_document = True
                idx += 1

        def split_ruby_text(rt_text, segment_lengths):
            if not rt_text:
                return ['' for _ in segment_lengths]
            total_base = sum(segment_lengths) or 1
            total_ruby = len(rt_text)
            target_counts = []
            for seg_len in segment_lengths:
                est = round(total_ruby * seg_len / total_base)
                target_counts.append(est)
            if total_ruby >= len(segment_lengths):
                target_counts = [max(1, c) for c in target_counts]
            diff = total_ruby - sum(target_counts)
            if diff > 0:
                order = sorted(range(len(segment_lengths)), key=lambda i: (-segment_lengths[i], i))
                idx = 0
                while diff > 0:
                    target_counts[order[idx % len(order)]] += 1
                    diff -= 1
                    idx += 1
            elif diff < 0:
                order = sorted(range(len(segment_lengths)), key=lambda i: (target_counts[i], i), reverse=True)
                idx = 0
                while diff < 0 and order:
                    i = order[idx % len(order)]
                    min_allowed = 1 if total_ruby >= len(segment_lengths) else 0
                    if target_counts[i] > min_allowed:
                        target_counts[i] -= 1
                        diff += 1
                    idx += 1
                    if idx > len(order) * (total_ruby + len(segment_lengths) + 2):
                        break
            parts = []
            pos = 0
            for count in target_counts:
                parts.append(rt_text[pos:pos + count])
                pos += count
            if pos < total_ruby:
                if parts:
                    parts[-1] += rt_text[pos:]
                else:
                    parts = [rt_text[pos:]]
            while len(parts) < len(segment_lengths):
                parts.append('')
            return parts

        def draw_split_ruby(segment_infos, rt_text, is_bold=False):
            if not rt_text or not segment_infos:
                return
            grouped = []
            current = None
            for info in segment_infos:
                key = (info['page_index'], info['x'])
                if current and current['page_index'] == key[0] and current['x'] == key[1]:
                    current['chars'].append(info)
                else:
                    current = {'page_index': key[0], 'x': key[1], 'chars': [info]}
                    grouped.append(current)
            segment_lengths = [sum(ch.get('base_len', 1) for ch in g['chars']) for g in grouped]
            ruby_parts = split_ruby_text(rt_text, segment_lengths)
            total_segments = len(grouped)
            for idx, (group, ruby_part) in enumerate(zip(grouped, ruby_parts)):
                if not ruby_part:
                    continue
                target_img = img if group['page_index'] == len(rendered_pages) else rendered_pages[group['page_index']][0]
                target_draw = draw if group['page_index'] == len(rendered_pages) else ImageDraw.Draw(target_img)
                start_y = group['chars'][0]['y']
                end_y = group['chars'][-1]['y']
                rb_h = len(group['chars']) * (args.font_size + 2)
                rt_h = len(ruby_part) * (args.ruby_size + 2)
                if total_segments == 1:
                    ry = start_y + (rb_h - rt_h) // 2
                elif idx == 0:
                    ry = end_y + args.font_size - rt_h
                elif idx == total_segments - 1:
                    ry = start_y
                else:
                    ry = start_y + (rb_h - rt_h) // 2
                min_ry = args.margin_t
                max_ry = args.height - args.margin_b - args.ruby_size
                ry = max(min_ry, min(ry, max_ry))
                ruby_x = group['x'] + args.font_size + 1
                for r_char in ruby_part:
                    if args.margin_t <= ry < args.height - args.margin_b:
                        draw_char_tate(
                            target_draw, r_char, (ruby_x, ry), ruby_font, args.ruby_size,
                            is_bold=is_bold, ruby_mode=True,
                        )
                    ry += args.ruby_size + 2

        def walk_xml(node, inherited_bold=False):
            nonlocal curr_x, curr_y, pending_paragraph_indent, has_drawn_on_page, has_started_document

            if isinstance(node, str):
                text = normalize_text(node, strip_leading_for_indent=pending_paragraph_indent)
                if not text:
                    return
                insert_paragraph_indent()
                draw_text_run(text, is_bold=inherited_bold)
                return

            if not getattr(node, 'name', None):
                return

            node_name = node.name.lower()
            node_bold = node_is_bold(node, inherited_bold, bold_rules)

            if node_name == 'br':
                advance_column(1)
                pending_paragraph_indent = True
                return

            is_img_tag = node_name in {'img', 'image'}
            has_src = node.get('src') or node.get('xlink:href')
            if is_img_tag and has_src:
                raw_src = node.get('src', node.get('xlink:href', ''))
                resolved_src, img_data = resolve_epub_image_data(current_doc_file_name, raw_src)
                if img_data:
                    try:
                        with Image.open(io.BytesIO(img_data)) as s_img:
                            aspect = s_img.width / s_img.height if s_img.height > 0 else 1
                            is_illustration = s_img.height >= 400 or (aspect > 0.5 and s_img.height > args.font_size * 4)
                            if not is_illustration:
                                ensure_room(args.font_size)
                                scale = args.font_size / s_img.height
                                char_img = s_img.resize(
                                    (int(s_img.width * scale), args.font_size),
                                    Image.Resampling.LANCZOS,
                                ).convert('L')
                                if args.night_mode:
                                    char_img = ImageOps.invert(char_img)
                                img.paste(char_img, (curr_x + (args.font_size - char_img.width) // 2, curr_y + 4))
                                curr_y += args.font_size + 4
                                has_drawn_on_page = True
                                has_started_document = True
                            else:
                                if has_drawn_on_page:
                                    add_page(img)
                                add_page(s_img, is_illustration=True)
                                new_blank_page()
                    except Exception as e:
                        print(f"画像処理エラー ({resolved_src or raw_src}): {e}")
                return

            if is_paragraph_like(node):
                if has_drawn_on_page:
                    advance_column(1)
                pending_paragraph_indent = True

            if node_name == 'ruby':
                rb = ''.join(
                    child.get_text() if hasattr(child, 'get_text') else str(child)
                    for child in node.contents if getattr(child, 'name', '') not in {'rt', 'rp'}
                )
                rt = ''.join(rt_node.get_text() for rt_node in node.find_all('rt'))
                rb = normalize_text(rb, strip_leading_for_indent=pending_paragraph_indent)
                if not rb:
                    return
                insert_paragraph_indent()
                ruby_segments = []
                draw_text_run(rb, is_bold=node_bold, segment_infos=ruby_segments)
                if rt and ruby_segments:
                    draw_split_ruby(ruby_segments, rt, is_bold=node_bold)
                return

            for child in node.contents:
                if isinstance(child, str):
                    text = normalize_text(child, strip_leading_for_indent=pending_paragraph_indent)
                    if not text:
                        continue
                    insert_paragraph_indent()
                    draw_text_run(text, is_bold=node_bold)
                else:
                    walk_xml(child, node_bold)

        walk_xml(body)
        if has_drawn_on_page:
            add_page(img)
        if len(rendered_pages) > chapter_start:
            chapter_starts.append(chapter_start)

    ext = '.xtch' if _normalize_output_format(getattr(args, 'output_format', 'xtc')) == 'xtch' else '.xtc'
    out_path = Path(output_path) if output_path else epub_path.with_suffix(ext)
    rendered_pages = apply_page_progress_bars(rendered_pages, args, chapter_starts=chapter_starts)
    xtg_blobs = []
    for page_image, is_illustration in rendered_pages:
        # イラストページは night_mode を無効にして出力する
        page_args = dc_replace(args, night_mode=False) if is_illustration else args
        xtg_blobs.append(page_image_to_xt_bytes(page_image, args.width, args.height, page_args))
    build_xtc(xtg_blobs, out_path, args.width, args.height, getattr(args, 'output_format', 'xtc'))
    return out_path


def main():
    raise SystemExit("GUI版は tategakiXTC_gui_studio.py を起動してください。")


if __name__ == "__main__":
    main()
