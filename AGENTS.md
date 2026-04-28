# Project Notes

## Purpose

This is a Python/PySide6 GUI app for converting Japanese EPUB, text, Markdown, and image archive inputs into Xteink-ready bitmap ebook files.

## Format Model

- `XTC` (`.xtc`) is a container of bitmap pages.
- `XTCH` (`.xtch`) is the grayscale container variant.
- `XTG` is the per-page 1-bit monochrome bitmap format used inside `XTC`.
- `XTH` is the per-page 2-bit / 4-level grayscale bitmap format used inside `XTCH`.
- The output is not reflowable text. EPUB/TXT/MD content is laid out and rasterized into page images before writing.
- This repo does not implement `XTCZ`.

## Important Files

- `tategakiXTC_gui_core.py`: conversion core, EPUB/text parsing, vertical text rendering, image filtering, XTG/XTH encoding, XTC/XTCH container writing.
- `tategakiXTC_gui_studio.py`: PySide6 GUI, settings, presets, preview generation, conversion worker, XTC/XTCH viewer.
- `tategakiXTC_gui_studio.ini`: local GUI settings. It can be dirty from app/runtime state; do not rewrite it unless requested.
- `Font/`: bundled Japanese fonts used by default presets and preview/conversion.

## Rendering Pipeline

Text-like inputs:

```text
EPUB/TXT/MD -> parsed blocks -> Pillow L-mode page images -> optional overlays -> XTG/XTH page blobs -> XTC/XTCH container
```

Image/archive inputs:

```text
images -> resize/center on device canvas -> XTG/XTH page blobs -> XTC/XTCH container
```

The font preview in the GUI generates a fresh sample bitmap from current settings. The device preview for a converted file reads the actual XTC/XTCH bytes, decodes the selected page blob, and displays the literal bitmap.

## Progress Bar Notes

- `progress_bar` and `progress_bar_side` live on `ConversionArgs`.
- Progress overlays are drawn after pagination, before `page_image_to_xt_bytes()`.
- EPUB chapter ticks currently use spine document boundaries.
- Full-page EPUB illustrations are marked with `is_illustration=True`; progress overlays should skip drawing on those pages.
- Image/archive conversions currently do not get progress overlays.

## Format Touch Points

- `png_to_xtg_bytes()`: writes one 1-bit `XTG` page blob.
- `png_to_xth_bytes()`: writes one 2-bit / 4-level `XTH` page blob.
- `build_xtc()`: writes the top-level `XTC` or `XTCH` container and page index table.
- `parse_xtc_pages()` and `xt_page_blob_to_qimage()` in the GUI decode existing files for preview.

## Validation

Use the project virtualenv when available:

```bash
.venv/bin/python -m py_compile tategakiXTC_gui_core.py tategakiXTC_gui_studio.py
```

For rendering-related changes, also run a small Pillow smoke test through `.venv/bin/python` if Pillow is not installed in system `python3`.

## Working Conventions

- Prefer small, scoped changes that match the existing procedural style.
- Avoid unrelated rewrites or formatting churn.
- Treat `tategakiXTC_gui_studio.ini` as user/runtime state unless explicitly asked to edit it.
- Do not revert dirty or untracked files that are unrelated to the current task.
