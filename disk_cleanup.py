#!/usr/bin/env python3
"""本机大文件扫描。只监听 127.0.0.1。删除时把文件移到废纸篓。

用法：python3 disk_cleanup.py
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
MAX_FILES = 2000
USED_LOOKUP_LIMIT = 400
HEAVY_DIR_NAMES = {
    "node_modules",
    ".git",
    "DerivedData",
    ".next",
    "Pods",
    "__pycache__",
    "target",
    "vendor",
}

# 永远不扫描、不删除的绝对路径前缀。按自己的机器填写。
# 例：PROTECTED_PREFIXES = ("/opt/important", "/data/keep")
PROTECTED_PREFIXES: tuple[str, ...] = ()
# 扫描类型：id、按钮文字、扩展名、按钮说明。空选择表示全部文件。
SCAN_TYPES = (
    ("pdf", "PDF", ("pdf",), "pdf"),
    ("image", "图片", (
        "jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "avif",
        "bmp", "tif", "tiff", "psd", "raw", "svg", "jfif",
    ), "jpg、png、heic、gif、webp、tif 等"),
    ("excel", "Excel", ("xls", "xlsx", "xlsm", "xlsb", "csv"), "xls、xlsx、xlsm、csv"),
    ("word", "Word", ("doc", "docx"), "doc、docx"),
    ("slides", "PPT", ("ppt", "pptx", "key"), "ppt、pptx、key"),
    ("video", "视频", (
        "mp4", "mov", "mkv", "avi", "m4v", "webm", "flv", "wmv", "m2ts",
    ), "mp4、mov、mkv、avi 等"),
    ("audio", "音频", ("mp3", "wav", "flac", "m4a", "aac", "aiff"), "mp3、wav、flac、m4a 等"),
    ("archive", "压缩包", ("zip", "7z", "rar", "gz", "tgz", "tar", "bz2", "xz"), "zip、7z、rar、tar、gz"),
)
_SCAN_TYPE_BY_ID = {item[0]: item for item in SCAN_TYPES}


def scan_type_buttons() -> str:
    rows = ['<button type="button" data-type="" class="on" aria-pressed="true">全部</button>']
    for type_id, label, _exts, title in SCAN_TYPES:
        rows.append(
            f'<button type="button" data-type="{type_id}" title="{title}" aria-pressed="false">{label}</button>'
        )
    return "\n          ".join(rows)

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>大文件清理</title>
<style>
  :root {
    --bg: #e7ebf0;
    --panel: #ffffff;
    --ink: #17202a;
    --muted: #5d6b7a;
    --line: #d5dde6;
    --accent: #0b57d0;
    --accent-soft: #e8f0fe;
    --danger: #b3261e;
    --warn: #8a4b08;
    --warn-bg: #fff4e5;
    --shadow: 0 1px 2px rgba(23, 32, 42, 0.06), 0 8px 24px rgba(23, 32, 42, 0.04);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    color: var(--ink);
    background: var(--bg);
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", sans-serif;
  }
  button, input { font: inherit; color: inherit; }
  button { cursor: pointer; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .app {
    height: 100vh;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    gap: clamp(8px, 0.8vw, 14px);
    padding: clamp(10px, 1.1vw, 22px);
  }
  .top, .panel, .dock {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    box-shadow: var(--shadow);
  }
  .top { padding: 8px 12px 0; }
  .top-row { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
  h1 { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -0.02em; }
  .lead { margin: 1px 0 0; color: var(--muted); font-size: 12px; line-height: 1.35; }
  .stats { display: flex; gap: 14px; }
  .stats b { display: block; font-size: 15px; font-variant-numeric: tabular-nums; font-weight: 650; line-height: 1.2; }
  .stats span { color: var(--muted); font-size: 11px; }
  .meter { margin-top: 4px; }
  .disk-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin-top: 6px; }
  .disk-row h2 { margin: 0; font-size: 13px; font-weight: 650; }
  .disks { display: flex; flex-wrap: wrap; gap: 6px; }
  .disk {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--line);
    background: #fbfcfd;
    border-radius: 999px;
    padding: 4px 10px;
  }
  .disk.on { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }
  .disk .disk-size { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
  .disk.on .disk-size { color: var(--accent); }
  .place.off { opacity: 0.4; }
  #diskScope { color: var(--muted); font-size: 12px; }
  .meter-label { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; }
  .bar { height: 6px; border-radius: 99px; background: #e6ebf1; overflow: hidden; margin: 3px 0 0; }
  .bar > span { display: block; height: 100%; background: linear-gradient(90deg, #0b57d0, #3d7eeb); }
  .bar > span.warm { background: linear-gradient(90deg, #d97706, #f59e0b); }
  .bar > span.hot { background: linear-gradient(90deg, #dc2626, #f97316); }
  button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .progress { height: 2px; margin-top: 4px; overflow: hidden; }
  .progress.on { background: #d6e4ff; }
  .progress.on::after {
    content: "";
    display: block;
    width: 35%;
    height: 100%;
    background: var(--accent);
    animation: slide 1.1s ease-in-out infinite;
  }
  @keyframes slide { from { transform: translateX(-120%); } to { transform: translateX(320%); } }
  .workspace {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .panel { min-height: 0; padding: clamp(8px, 0.8vw, 12px); }
  .side h2, .log-head h2, .toolbar h2 { margin: 0; font-size: 13px; font-weight: 650; }
  .side {
    display: flex;
    flex-direction: row;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 16px;
    flex: 0 0 auto;
    overflow: visible;
    padding: 8px 12px;
  }
  .side-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin: 0; }
  .side-row h2 { margin: 0; }
  .places { display: flex; flex-wrap: wrap; gap: 4px; }
  .place {
    display: inline-flex;
    flex-direction: row;
    align-items: center;
    gap: 4px;
    width: auto;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 2px 8px 2px 6px;
    background: #fbfcfd;
  }
  .place input { margin: 0; }
  .place.on { border-color: #9dbcf2; background: var(--accent-soft); }
  .place-name { font-weight: 600; font-size: 13px; }
  .place-ico, .mini-ico, .file-ico, .cell-icon, .hc-icon {
    display: inline-grid;
    place-items: center;
    flex: none;
    background: var(--cat-bg);
    color: var(--cat);
  }
  .place-ico, .mini-ico { width: 18px; height: 18px; border-radius: 6px; }
  .place-ico svg, .mini-ico svg { width: 13px; height: 13px; display: block; }
  .file-ico { width: 32px; height: 32px; border-radius: 9px; }
  .file-ico svg, .cell-icon svg, .hc-icon svg { width: 18px; height: 18px; display: block; }
  .place-path { display: none; }
  .block { margin-top: 0; }
  .pick {
    width: auto;
    border: 1px dashed #b7c6da;
    background: #f7faff;
    color: var(--accent);
    border-radius: 10px;
    padding: 4px 10px;
    font-weight: 600;
  }
  .pick:hover:not(:disabled) { background: var(--accent-soft); }
  .chips { display: flex; flex-direction: row; flex-wrap: wrap; gap: 4px; margin-top: 0; }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    width: auto;
    text-align: left;
    border: 1px solid var(--line);
    background: #fff;
    border-radius: 999px;
    padding: 2px 4px 2px 8px;
  }
  .chip strong { font-size: 12px; font-weight: 600; }
  .chip small { display: none; }
  .chip .x {
    border: 0;
    background: transparent;
    color: var(--muted);
    width: 28px;
    height: 28px;
    border-radius: 8px;
  }
  .chip .x:hover { background: #f1f4f8; color: var(--ink); }
  .sizes { display: flex; flex-wrap: wrap; gap: 6px; }
  .sizes button, .ghost, .primary, .danger {
    border-radius: 9px;
    padding: 7px 12px;
  }
  .sizes button, .ghost {
    border: 1px solid var(--line);
    background: #fff;
  }
  .sizes button.on { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); font-weight: 650; }
  .size-custom, .size-range { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
  .size-range { white-space: nowrap; }
  input[type="number"], input[type="text"] {
    border: 1px solid var(--line);
    background: #fff;
    border-radius: 9px;
    padding: 7px 10px;
  }
  input[type="number"] { width: 76px; }
  .switch { display: inline-flex; gap: 6px; align-items: center; margin: 0; font-size: 13px; }
  .actions { display: flex; gap: 8px; margin: 0; }
  .primary { border: 0; background: var(--accent); color: white; font-weight: 650; }
  .danger { border: 0; background: var(--danger); color: white; font-weight: 650; }
  .status { color: var(--muted); font-size: 12px; }
  .stage { display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: hidden; padding: 0; }
  .side-row.scan-row { margin-left: auto; }
  .log-panel { display: flex; flex-direction: column; flex: 0 0 auto; min-height: 0; }
  .log-panel.filling { flex: 1; }
  .log-panel.compact { flex: 0 0 auto; border-bottom: 1px solid var(--line); }
  .log-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 10px 14px 8px; }
  .log-head h2 { white-space: nowrap; flex: none; }
  .log-tools { display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-width: 0; flex: 1; }
  .log-tools .status { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: min(560px, 46vw); }
  .log-head h2 { margin: 0; }
  #scanLog {
    list-style: none;
    margin: 0;
    padding: 6px 14px 10px;
    overflow: auto;
    flex: 1;
    font-family: "SF Mono", ui-monospace, Menlo, monospace;
    font-size: 12px;
    line-height: 1.55;
  }
  #scanLog { max-height: 168px; }
  .log-panel.compact #scanLog, .log-panel.compact .log-empty { display: none; }
  #scanLog li { padding: 2px 0; border-bottom: 1px solid #f2f5f8; }
  .log-empty { margin: 0; padding: 8px 14px 16px; color: var(--muted); }
  .scan-wait { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--muted); text-align: center; padding: 28px 16px; }
  .scan-wait[hidden] { display: none; }
  .scan-wait strong { color: var(--ink); font-size: 16px; margin-bottom: 6px; }
  .result-panel { display: flex; flex-direction: column; flex: 1; min-height: 0; }
  .result-panel[hidden] { display: none; }
  .toolbar {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: center;
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
  }
  .filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end; flex: 1; min-width: 0; margin-left: auto; }
  .views, .typechips { display: flex; flex-wrap: wrap; gap: 4px; }
  .views button.on { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
  .typechips { padding: 8px 14px 6px; }
  #workFull, #logFull { flex: none; }
  .typechips:empty { display: none; }
  .typechip { display: inline-flex; align-items: center; gap: 4px; border-radius: 999px; padding: 3px 8px 3px 4px; }
  .typechip em { font-style: normal; color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  .typechip.on { border-color: var(--cat); color: var(--cat); background: var(--cat-bg); }
  #filter { width: min(280px, 36vw); }
  .summary { padding: 8px 14px 0; color: var(--muted); font-size: 12px; }
  .map-wrap, .list-block { min-height: 0; display: flex; flex-direction: column; }
  .map-wrap { flex: 0 0 auto; border-bottom: 1px solid var(--line); }
  .list-block { flex: 1; }
  .block-bar { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 10px 0; min-height: 34px; }
  .block-title { font-size: 13px; font-weight: 650; }
  .crumb { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; padding: 0; min-height: 28px; flex: 1; min-width: 0; }
  .crumb button { border: 0; background: transparent; color: var(--accent); padding: 2px 4px; border-radius: 6px; }
  .crumb button:hover { background: var(--accent-soft); }
  .crumb button[disabled] { color: var(--ink); font-weight: 650; background: transparent; }
  .crumb .sep { color: var(--muted); }
  .map { position: relative; height: auto; min-height: 0; margin: 0; background: transparent; overflow: visible; }
  .map-empty { display: flex; align-items: center; justify-content: center; color: var(--muted); padding: 22px 16px 26px; text-align: center; }
  .sharebar { display: flex; gap: 3px; height: 12px; margin: 4px 14px 0; }
  .sharebar button { height: 12px; border: 0; border-radius: 99px; padding: 0; min-width: 6px; background: var(--cat); }
  .sharebar button.dim { opacity: 0.28; }
  .sharebar button:hover { filter: brightness(1.05); }
  .band {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 8px;
    align-items: stretch;
    padding: 8px 12px 12px;
  }
  .cell {
    position: relative;
    container-type: inline-size;
    flex: 1 1 0;
    min-width: 0;
    height: 78px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 26px 8px 10px;
    border: 1px solid #e1e7ee;
    border-left: 3px solid var(--cat);
    border-radius: 12px;
    background: #fff;
    color: var(--ink);
    overflow: hidden;
    text-align: left;
    cursor: pointer;
  }
  .cell:hover { background: var(--cat-bg); }
  .cell.on { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
  .cell.more { flex: 0.35 0 132px; background: #f6f8fb; }
  .cell-icon { width: 36px; height: 36px; border-radius: 10px; }
  .cell-body { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
  .cell .name, .cell .meta, .cell .sub, .cell-path {
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .cell .name { font-weight: 650; }
  .cell .meta { font-size: 12px; font-variant-numeric: tabular-nums; }
  .cell .sub, .cell-path { font-size: 11px; color: var(--muted); }
  .cell-path { display: none; margin-left: auto; max-width: 42%; font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 11px; }
  .cell .tick { position: absolute; right: 6px; top: 6px; margin: 0; }
  .cell-reveal {
    position: absolute;
    right: 6px;
    bottom: 6px;
    opacity: 0;
    border: 0;
    background: rgba(255, 255, 255, 0.92);
    color: var(--accent);
    border-radius: 6px;
    padding: 2px 6px;
    font-size: 11px;
    font-weight: 650;
  }
  .cell:hover .cell-reveal, .cell:focus-within .cell-reveal { opacity: 1; }
  @container (min-width: 360px) {
    .cell-path { display: block; }
  }
  @container (max-width: 180px) {
    .cell .sub { display: none; }
    .cell-icon { width: 28px; height: 28px; }
  }
  .table-wrap { flex: 1; min-height: 200px; overflow: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 760px; }
  th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid #edf1f5; vertical-align: top; }
  th {
    position: sticky;
    top: 0;
    background: #f7f9fb;
    color: var(--muted);
    font-size: 12px;
    font-weight: 650;
    cursor: pointer;
    white-space: nowrap;
    z-index: 1;
  }
  td.size, td.time, td.acts { font-variant-numeric: tabular-nums; white-space: nowrap; }
  td.item { min-width: 220px; }
  .item-line { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .item-text { display: flex; flex-direction: column; min-width: 0; }
  .name-main { display: block; font-weight: 640; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .name-sub { color: var(--muted); font-size: 12px; }
  .thumb { display: block; width: 72px; height: 48px; object-fit: cover; border-radius: 7px; flex: none; background: #e8edf3; }
  .item-line:has(.thumb) .file-ico { display: none; }
  .cell-thumb { width: 36px; height: 36px; border-radius: 10px; object-fit: cover; flex: none; }
  .hc-shot { display: block; width: 100%; height: 112px; object-fit: cover; border-radius: 8px; margin-bottom: 8px; background: #e8edf3; }
  tbody tr { cursor: pointer; }
  tbody tr.dir:hover .name-main, tbody tr.file:hover .name-main { color: var(--accent); }
  th.check, td.check {
    position: sticky;
    left: 0;
    z-index: 2;
    width: 44px;
    background: #fff;
    cursor: default;
  }
  td.acts, th.acts {
    position: sticky;
    right: 0;
    z-index: 2;
    background: #fff;
    box-shadow: -10px 0 10px -10px rgba(15, 23, 42, .16);
  }
  thead th.check, thead th.acts { z-index: 4; background: #f7f9fb; }
  tr:hover td.check, tr:hover td.acts { background: #f6f9fc; }
  tr.picked td.check, tr.picked td.acts { background: #eef4ff; }
  td.acts { text-align: right; width: 1%; }
  td.path { font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 12px; word-break: break-all; }
  tr:hover td { background: #f6f9fc; }
  tr.picked td { background: #eef4ff; }
  .tag {
    display: inline-block;
    background: #eef2f6;
    color: #314155;
    border-radius: 999px;
    padding: 2px 8px;
    font-size: 12px;
  }
  .tag.warn { background: var(--warn-bg); color: var(--warn); }
  .mini { padding: 4px 8px; font-size: 12px; }
  .empty { color: var(--muted); padding: 48px 20px; text-align: center; }
  .empty strong { display: block; color: var(--ink); font-size: 16px; margin-bottom: 6px; }
  #dock[hidden] { display: none; }
  .dock {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: center;
    padding: 10px 14px;
  }
  #selectedText { font-variant-numeric: tabular-nums; }
  #selectedText b { font-weight: 700; }
  .mask {
    position: fixed;
    inset: 0;
    z-index: 80;
    background: rgba(17, 24, 33, 0.46);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 4vh 4vw;
  }
  .mask.show { display: flex; }
  .dialog {
    width: min(760px, 100%);
    max-height: 86vh;
    overflow: auto;
    background: white;
    border-radius: 16px;
    padding: clamp(16px, 2vw, 24px);
    box-shadow: 0 24px 80px rgba(17, 24, 33, 0.28);
  }
  .dialog h2 { margin: 0 0 8px; font-size: 20px; }
  .dialog ul { list-style: none; margin: 12px 0; padding: 0; max-height: 42vh; overflow: auto; }
  .dialog li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 2px 12px;
    align-items: baseline;
    padding: 8px 0;
    border-top: 1px solid var(--line);
    font-size: 13px;
  }
  .dialog .dlg-name { font-weight: 650; word-break: break-all; }
  .dialog .dlg-size { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .dialog .dlg-path {
    grid-column: 1 / -1;
    color: var(--muted);
    font-family: "SF Mono", ui-monospace, Menlo, monospace;
    font-size: 12px;
    word-break: break-all;
  }
  .warnbox { background: var(--warn-bg); color: var(--warn); border-radius: 10px; padding: 8px 10px; }
  .dialog-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .dialog-head h2 { margin: 0; }
  .log-dialog { width: min(880px, 100%); }
  .toolbar-title { display: flex; align-items: center; gap: 8px; }
  #logFilter { display: none; width: min(240px, 32vw); margin: 0; }
  .log-only { display: none; }
  body.fs-log #logFilter { display: block; }
  body.fs-log .log-only { display: inline-block; }
  #previewMask { z-index: 90; }
  .preview-dialog { width: min(1080px, 100%); max-height: 90vh; display: flex; flex-direction: column; gap: 10px; }
  .preview-bar { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .preview-bar h2 { margin: 0; font-size: 16px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .preview-stage { background: #0f141b; border-radius: 12px; min-height: 180px; display: flex; align-items: center; justify-content: center; overflow: hidden; }
  .preview-stage img, .preview-stage video { max-width: 100%; max-height: 72vh; }
  #previewNote { margin: 0; color: var(--muted); font-size: 13px; }
  #previewNote:empty { display: none; }
  body.fs-work .top, body.fs-work .side, body.fs-work .log-panel, body.fs-work .scan-wait,
  body.fs-log .top, body.fs-log .side, body.fs-log .scan-wait, body.fs-log .result-panel,
  body.fs-log #dock { display: none !important; }
  body.fs-work .workspace, body.fs-work .stage, body.fs-work .result-panel,
  body.fs-log .workspace, body.fs-log .stage { flex: 1; min-height: 0; }
  body.fs-work .map-wrap { flex: 0 0 auto; max-height: 42%; overflow: auto; }
  body.fs-work .map { flex: 0 0 auto; }
  body.fs-work .list-block { flex: 1; min-height: 0; }
  body.fs-work .cell { height: 88px; }
  body.fs-log .log-panel, body.fs-log .log-panel.compact {
    display: flex !important;
    flex: 1;
    border-bottom: 0;
  }
  body.fs-log #scanLog, body.fs-log .log-panel.compact #scanLog {
    display: block !important;
    max-height: none;
    flex: 1;
  }
  body.fs-log .log-empty, body.fs-log .log-panel.compact .log-empty { display: none !important; }
  .hovercard {
    display: none;
    position: fixed;
    z-index: 40;
    width: min(390px, calc(100vw - 24px));
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 14px;
    box-shadow: 0 18px 50px rgba(17, 24, 33, 0.18);
    padding: 12px;
  }
  .hovercard.show { display: block; }
  .hc-top { display: flex; gap: 10px; align-items: flex-start; }
  .hc-icon { width: 40px; height: 40px; border-radius: 12px; }
  .hc-name { font-weight: 700; word-break: break-all; }
  .hc-meta { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .hc-path { margin-top: 8px; color: var(--muted); font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 12px; word-break: break-all; }
  .hc-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  @media (max-width: 980px) {
    .app { height: auto; min-height: 100dvh; }
    .top-row, .toolbar, .dock { flex-direction: column; align-items: stretch; }
    .filters { margin-left: 0; justify-content: flex-start; }
    #workFull, #logFull { align-self: flex-end; }
    .table-wrap { max-height: 68vh; }
  }
  @media (max-width: 640px) {
    .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  }
</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="top-row">
      <div>
        <h1>大文件清理</h1>
        <p class="lead">勾选位置后扫描。删除会移到废纸篓，可以从废纸篓找回。</p>
      </div>
      <div class="stats" id="stats">
        <div><b id="usedStat">—</b><span>已用</span></div>
        <div><b id="freeStat">—</b><span>可用</span></div>
        <div><b id="totalStat">—</b><span>容量</span></div>
        <div><b id="pctStat">—</b><span>已用比例</span></div>
      </div>
    </div>
    <div class="disk-row">
      <h2>磁盘</h2>
      <div id="disks" class="disks"></div>
      <span id="diskScope">正在读取磁盘…</span>
    </div>
    <div class="meter">
      <div class="meter-label"><span id="diskText">正在读取磁盘空间…</span><span id="diskPath"></span></div>
      <div class="bar"><span id="diskBar" style="width:0"></span></div>
    </div>
    <div class="progress" id="progress"></div>
  </header>
  <div class="workspace">
    <aside class="panel side">
      <div class="side-row">
        <h2>位置</h2>
        <button id="placePick" class="ghost mini" type="button" title="点一下全选，再点一下反选">全选</button>
        <div id="presets" class="places"></div>
      </div>
      <div class="side-row">
        <button class="pick" id="pickBtn" type="button">选择文件夹</button>
        <div id="extraRoots" class="chips"></div>
      </div>
      <div class="side-row">
        <h2>大小</h2>
        <label class="size-range">
          <span>从</span>
          <input id="minMb" type="number" min="0" max="10240" step="1" value="100" inputmode="numeric" aria-label="最小，单位 MB">
          <span>到</span>
          <input id="maxMb" type="number" min="0" max="10240" step="1" inputmode="numeric" placeholder="不限" aria-label="最大，单位 MB">
          <span>MB</span>
        </label>
      </div>
      <div class="side-row">
        <h2>类型</h2>
        <div class="sizes" id="scanTypes">
          <!--SCAN_TYPES-->
        </div>
      </div>
      <div class="side-row">
        <label class="switch"><input id="includeFiles" type="checkbox" checked><span>列出大文件</span></label>
        <label class="switch"><input id="skipHeavy" type="checkbox" checked><span>跳过依赖目录</span></label>
      </div>
      <div class="side-row scan-row">
        <button id="scanBtn" class="primary" type="button">开始扫描</button>
        <button id="stopBtn" class="ghost" type="button" disabled>停止</button>
      </div>
    </aside>
    <section class="panel stage" id="stage">
      <div class="log-panel" id="logPanel">
        <div class="log-head">
          <h2>扫描过程</h2>
          <div class="log-tools">
            <span class="status" id="status">还没有扫描。</span>
            <input id="logFilter" class="log-only" type="text" placeholder="筛选过程记录" spellcheck="false">
            <button id="copyLog" class="ghost mini log-only" type="button">复制</button>
            <button id="logFull" class="ghost mini" type="button" aria-pressed="false" title="全屏查看扫描过程">全屏</button>
          </div>
        </div>
        <ol id="scanLog"></ol>
        <p class="log-empty" id="logEmpty">选好位置后点「开始扫描」。这里会记下正在看哪个文件夹、在统计占用，还是在找大文件。记录多了可以全屏查看。</p>
      </div>
      <div class="scan-wait" id="scanWait" hidden>
        <strong>正在扫描</strong>
        <p>完成后这里会按文件类型画出占用，并列出可以清理的项目。</p>
      </div>
      <div class="result-panel" id="resultPanel" hidden>
      <div class="toolbar">
        <div class="toolbar-title">
          <h2>扫描结果</h2>
        </div>
        <div class="filters">
          <div class="views" id="views">
            <button type="button" class="ghost on" data-view="all">全部</button>
            <button type="button" class="ghost" data-view="dir">文件夹</button>
            <button type="button" class="ghost" data-view="file">文件</button>
            <button type="button" class="ghost" data-view="hint">有说明的</button>
          </div>
          <input id="filter" type="text" placeholder="筛选名称或路径" spellcheck="false">
        </div>
        <button id="workFull" class="ghost mini" type="button" aria-pressed="false" title="文件夹和文件列表一起铺满窗口">全屏</button>
      </div>
      <div class="summary" id="summary"></div>
      <div class="map-wrap" id="mapBlock">
        <div class="block-bar">
          <nav class="crumb" id="crumb" aria-label="层级"></nav>
        </div>
        <div class="map" id="map"><div class="map-empty">扫描后，这里会按占用大小画出每一层。点文件夹可以进入下一层，图片和视频可以预览。</div></div>
      </div>
      <div class="list-block" id="listBlock">
      <div class="block-bar">
        <span class="block-title">文件列表</span>
      </div>
      <div class="typechips" id="typechips"></div>
      <div class="table-wrap">
        <table title="点文件夹进入下一层。图片和视频可以预览，其他文件用默认应用打开。勾选后可以删除。">
          <thead>
            <tr>
              <th class="check"><input id="checkVisible" type="checkbox" title="勾选当前筛出来的行"></th>
              <th data-sort="name" data-label="名称">名称</th>
              <th data-sort="size" data-label="大小">大小</th>
              <th data-sort="mtime" data-label="修改时间">修改时间</th>
              <th data-label="路径">路径</th>
              <th class="acts"></th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
        <div id="empty" class="empty">
          <strong>这一页没有符合筛选的项目</strong>
        </div>
      </div>
      </div>
      </div>
    </section>
  </div>
  <footer class="dock" id="dock" hidden>
    <div id="selectedText">还没有勾选</div>
    <button id="deleteBtn" class="danger" type="button" disabled>删除所选</button>
  </footer>
</div>
<div class="mask" id="mask">
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dialogTitle">
    <h2 id="dialogTitle">确认删除</h2>
    <p id="dialogLead"></p>
    <div id="dialogWarn"></div>
    <ul id="dialogList"></ul>
    <div class="actions">
      <button id="confirmDelete" class="danger" type="button">确认，移到废纸篓</button>
      <button id="cancelDelete" class="ghost" type="button">返回</button>
    </div>
  </div>
</div>
<div class="mask" id="previewMask">
  <div class="dialog preview-dialog" role="dialog" aria-modal="true" aria-labelledby="previewName">
    <div class="preview-bar">
      <h2 id="previewName"></h2>
      <div class="actions">
        <button id="previewOpen" class="primary mini" type="button" data-act="open">打开</button>
        <button id="closePreview" class="ghost mini" type="button">关闭</button>
      </div>
    </div>
    <div class="preview-stage">
      <img id="previewImage" alt="" hidden>
      <video id="previewVideo" controls playsinline hidden></video>
    </div>
    <p id="previewNote"></p>
  </div>
</div>
<div class="hovercard" id="hovercard" role="tooltip"></div>
<script>
const state = {
  items: [],
  running: false,
  sortKey: "size",
  sortDir: -1,
  selected: new Set(),
  extraRoots: [],
  presets: [],
  pendingDelete: [],
  home: "",
  view: "all",
  catalog: new Map(),
  mapNodes: [],
  mapIndex: -1,
  stage: "idle",
  pendingMap: null,
  disks: [],
  diskId: "",
  full: "",
  scanTypes: new Set(),
  note: "",
};
let mapToken = 0;
let mapWidth = 0;
let hoverHide = 0;

const $ = (id) => document.getElementById(id);

const CATS = {
  video: { label: "视频", color: "#7c3aed", bg: "#f3e8ff" },
  audio: { label: "音频", color: "#db2777", bg: "#fce7f3" },
  image: { label: "图片", color: "#059669", bg: "#d1fae5" },
  archive: { label: "压缩包", color: "#c2410c", bg: "#ffedd5" },
  disk: { label: "磁盘映像", color: "#b45309", bg: "#fef3c7" },
  installer: { label: "安装包", color: "#ea580c", bg: "#fff1e6" },
  document: { label: "文档", color: "#1d4ed8", bg: "#dbeafe" },
  code: { label: "代码", color: "#0e7490", bg: "#cffafe" },
  app: { label: "应用程序", color: "#4338ca", bg: "#e0e7ff" },
  cache: { label: "缓存", color: "#475569", bg: "#f1f5f9" },
  backup: { label: "备份", color: "#a16207", bg: "#fef3c7" },
  log: { label: "日志", color: "#64748b", bg: "#f1f5f9" },
  folder: { label: "文件夹", color: "#0f766e", bg: "#ccfbf1" },
  other: { label: "其他", color: "#475569", bg: "#f1f5f9" },
};
const PLACE = {
  downloads: ["download", "document"],
  desktop: ["desktop", "code"],
  documents: ["document", "document"],
  movies: ["video", "video"],
  music: ["audio", "audio"],
  pictures: ["image", "image"],
  caches: ["cache", "cache"],
  app_support: ["app", "app"],
  home: ["home", "folder"],
};
const ICONS = {
  folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" fill-opacity=".16" d="M3.5 8.2A2.2 2.2 0 0 1 5.7 6h3.2l1.6 1.7h7.8a2.2 2.2 0 0 1 2.2 2.2v7.4a2.2 2.2 0 0 1-2.2 2.2H5.7a2.2 2.2 0 0 1-2.2-2.2V8.2Z"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M3.5 8.2A2.2 2.2 0 0 1 5.7 6h3.2l1.6 1.7h7.8a2.2 2.2 0 0 1 2.2 2.2v7.4a2.2 2.2 0 0 1-2.2 2.2H5.7a2.2 2.2 0 0 1-2.2-2.2V8.2Z"/></svg>',
  video: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><path fill="none" stroke="currentColor" stroke-width="1.7" d="M8 5v14M16 5v14M3.5 9.5h4.5M3.5 14.5h4.5M16 9.5h4.5M16 14.5h4.5"/></svg>',
  audio: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M9 17V6.5l10-2V15"/><circle cx="7" cy="17" r="2.2" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="17" cy="15" r="2.2" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  image: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="9" cy="10" r="1.4" fill="currentColor"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="m7 16 3.2-3.2a1 1 0 0 1 1.4 0L14 15l1.2-1.2a1 1 0 0 1 1.4 0L20 17"/></svg>',
  archive: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M4 7.5h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-11Z"/><path fill="none" stroke="currentColor" stroke-width="1.7" d="M3.5 4.8h17v2.7h-17zM10 12h4"/></svg>',
  disk: '<svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="7" rx="7.5" ry="3" fill="none" stroke="currentColor" stroke-width="1.7"/><path fill="none" stroke="currentColor" stroke-width="1.7" d="M4.5 7v10c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V7"/></svg>',
  installer: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" d="M12 4v9m0 0 3.2-3.2M12 13 8.8 9.8M5 16.5V18a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-1.5"/></svg>',
  document: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M7 3.8h7l5 5V20a1.2 1.2 0 0 1-1.2 1.2H7A1.2 1.2 0 0 1 5.8 20V5A1.2 1.2 0 0 1 7 3.8Z"/><path fill="none" stroke="currentColor" stroke-width="1.7" d="M14 3.8V9h5.2M8.5 13h7M8.5 16.5h5"/></svg>',
  code: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" d="m8 8-4 4 4 4M16 8l4 4-4 4M13 6l-2 12"/></svg>',
  app: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="6.5" height="6.5" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.7"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.7"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.7"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  cache: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M12 6.2V4M12 6.2a5.5 5.5 0 1 1-3.8 9.4"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" d="M7.4 14.4 8.2 17l-2.8-.1"/></svg>',
  backup: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="7.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M12 8v4.2l2.8 1.8"/></svg>',
  log: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M6 4h9l4 4v12H6V4Z"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M9 12h6M9 16h4"/></svg>',
  other: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M7 3.8h7l5 5V20a1.2 1.2 0 0 1-1.2 1.2H7A1.2 1.2 0 0 1 5.8 20V5A1.2 1.2 0 0 1 7 3.8Z"/><path fill="none" stroke="currentColor" stroke-width="1.7" d="M14 3.8V9h5.2"/></svg>',
  download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" d="M12 4v10m0 0 3.2-3.2M12 14 8.8 10.8M5 18h14"/></svg>',
  desktop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="11" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.7"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M8 19.5h8M12 15.5v4"/></svg>',
  home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="m4 11 8-6.5L20 11v8a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 19v-8Z"/><path fill="none" stroke="currentColor" stroke-width="1.7" d="M10 20.5v-5.5h4v5.5"/></svg>',
};
const EXT_CAT = {
  mp4: "video", mov: "video", mkv: "video", avi: "video", m4v: "video", webm: "video",
  flv: "video", wmv: "video", ts: "video", mpeg: "video", mpg: "video", m2ts: "video", "3gp": "video",
  mp3: "audio", wav: "audio", flac: "audio", m4a: "audio", aac: "audio",
  jpg: "image", jpeg: "image", png: "image", gif: "image", heic: "image", heif: "image",
  webp: "image", avif: "image", jfif: "image", bmp: "image", tif: "image", tiff: "image", psd: "image",
  zip: "archive", "7z": "archive", rar: "archive", gz: "archive", tgz: "archive", tar: "archive",
  dmg: "disk", iso: "disk", vmdk: "disk",
  pkg: "installer", ipsw: "installer",
  pdf: "document", doc: "document", docx: "document", ppt: "document", pptx: "document",
  xls: "document", xlsx: "document", txt: "document",
  app: "app", log: "log",
};

function catStyle(cat) {
  const meta = CATS[cat] || CATS.other;
  return "--cat:" + meta.color + ";--cat-bg:" + meta.bg;
}

function categoryOf(item) {
  if (item && item.category && CATS[item.category]) return item.category;
  if (!item || item.kind === "group") return "other";
  if (item.kind === "dir") return "folder";
  const name = (item.name || baseName(item.path || "")).toLowerCase();
  const ext = name.includes(".") ? name.split(".").pop() : "";
  return EXT_CAT[ext] || "other";
}

function itemName(item) {
  if (item && item.name) return item.name;
  return baseName((item && item.path) || "");
}

function isPreviewable(item) {
  if (!item || item.kind !== "file" || !item.path) return false;
  const cat = categoryOf(item);
  return cat === "image" || cat === "video";
}

function thumbUrl(path) {
  return "/api/thumb?path=" + encodeURIComponent(path);
}

function mediaUrl(kind, path) {
  return "/api/" + kind + "?path=" + encodeURIComponent(path);
}

function fmtDay(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getMonth() + 1) + "-" + p(d.getDate());
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtSize(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n) || 0;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  const digits = i === 0 ? 0 : (v >= 10 ? 1 : 2);
  return v.toFixed(digits) + " " + units[i];
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
    + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function warnHint(hint) {
  return /照片|音乐库|应用程序|Docker|备份/.test(hint || "");
}

function shortPath(path) {
  if (state.home && (path === state.home || path.startsWith(state.home + "/"))) {
    return "~" + path.slice(state.home.length);
  }
  return path;
}

function baseName(path) {
  const parts = path.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

async function loadMeta() {
  const res = await fetch("/api/meta");
  const meta = await res.json();
  state.home = meta.home || "";
  state.disks = meta.disks || [];
  state.presets = meta.presets;
  const defaults = new Set(["downloads", "desktop", "documents", "movies"]);
  $("presets").innerHTML = meta.presets.map((item) => {
    const on = defaults.has(item.id);
    const spec = PLACE[item.id] || ["folder", "folder"];
    const metaCat = CATS[spec[1]] || CATS.folder;
    return '<label class="place' + (on ? " on" : "") + '" title="' + esc(item.path) + '">'
      + '<input type="checkbox" data-root="' + esc(item.path) + '"' + (on ? " checked" : "") + ">"
      + '<span class="place-ico" style="--cat:' + metaCat.color + ';--cat-bg:' + metaCat.bg + '">'
      + (ICONS[spec[0]] || ICONS.folder) + "</span>"
      + '<span class="place-name">' + esc(item.label) + "</span>"
      + '<span class="place-path">' + esc(shortPath(item.path)) + "</span></label>";
  }).join("");
  renderDisks();
  syncPlacePick();
}

function currentDisk() {
  return (state.disks || []).find((disk) => disk.id === state.diskId) || null;
}

function showDisk(disk) {
  const pct = disk.total ? Math.round(disk.used / disk.total * 100) : 0;
  $("usedStat").textContent = fmtSize(disk.used);
  $("freeStat").textContent = fmtSize(disk.free);
  $("totalStat").textContent = fmtSize(disk.total);
  $("pctStat").textContent = pct + "%";
  $("diskText").textContent = disk.name + " 已用 " + pct + "%";
  $("diskPath").textContent = disk.path;
  $("diskBar").style.width = Math.max(2, pct) + "%";
  $("diskBar").classList.toggle("warm", pct >= 80 && pct < 90);
  $("diskBar").classList.toggle("hot", pct >= 90);
  const home = !!disk.home;
  $("diskScope").textContent = home
    ? "默认是这块盘。勾选下面的位置再扫描，也可以切换到其他磁盘。"
    : "已切换到「" + disk.name + "」。开始扫描会查看整块磁盘。";
  document.querySelectorAll("#presets input").forEach((el) => {
    el.disabled = state.running || !home;
    const place = el.closest(".place");
    if (place) place.classList.toggle("off", !home);
  });
  document.querySelectorAll("#disks button").forEach((el) => {
    el.disabled = state.running;
    el.classList.toggle("on", el.dataset.disk === disk.id);
  });
  syncPlacePick();
}

function renderDisks() {
  const disks = state.disks || [];
  if (!disks.some((disk) => disk.id === state.diskId)) {
    const home = disks.find((disk) => disk.home) || disks[0];
    state.diskId = home ? home.id : "";
  }
  $("disks").innerHTML = disks.map((disk) => (
    '<button type="button" class="disk" data-disk="' + esc(disk.id) + '">'
    + '<span class="place-ico" style="--cat:#1d4ed8;--cat-bg:#dbeafe">' + ICONS.disk + "</span>"
    + '<span class="place-name">' + esc(disk.name) + "</span>"
    + '<span class="disk-size">' + fmtSize(disk.total) + "</span></button>"
  )).join("");
  const disk = currentDisk();
  if (disk) showDisk(disk);
}

function selectedRoots() {
  const disk = currentDisk();
  if (disk && !disk.home) {
    const under = state.extraRoots.filter((path) => path === disk.path || path.startsWith(disk.path + "/"));
    return under.length ? under : [disk.path];
  }
  const roots = [...document.querySelectorAll("#presets input:checked")].map((el) => el.dataset.root);
  return roots.concat(state.extraRoots);
}

function renderExtra() {
  $("extraRoots").innerHTML = state.extraRoots.map((path, index) => (
    '<div class="chip" title="' + esc(path) + '"><span><strong>' + esc(baseName(path)) + "</strong><small>"
    + esc(shortPath(path)) + '</small></span><button class="x" type="button" data-remove="'
    + index + '" title="移除">×</button></div>'
  )).join("");
}

function currentLevel() {
  if (!(state.mapIndex > 0) || !state.mapNodes[state.mapIndex]) return null;
  const node = state.mapNodes[state.mapIndex];
  const rows = [];
  for (const item of node.children || []) {
    if (item.kind === "group" && item.members) {
      rows.push(...item.members.filter((member) => member.path));
    } else if (item.path && item.kind !== "group") {
      rows.push(item);
    }
  }
  return { name: node.name || "当前目录", rows };
}

function visibleItems() {
  const q = $("filter").value.trim().toLowerCase();
  const level = currentLevel();
  let items = (level ? level.rows : state.items).slice();
  if (state.view === "dir" || state.view === "file") {
    items = items.filter((item) => item.kind === state.view);
  } else if (state.view === "hint") {
    items = items.filter((item) => item.hint);
  } else if (state.view.indexOf("cat:") === 0) {
    const cat = state.view.slice(4);
    items = items.filter((item) => categoryOf(item) === cat);
  }
  if (q) items = items.filter((item) => item.path.toLowerCase().includes(q) || (item.hint || "").toLowerCase().includes(q) || itemName(item).toLowerCase().includes(q));
  const key = state.sortKey;
  const dir = state.sortDir;
  const valueOf = (item) => {
    if (key === "name") return itemName(item);
    return item[key] == null ? -1 : item[key];
  };
  items.sort((a, b) => {
    const av = valueOf(a);
    const bv = valueOf(b);
    if (typeof av === "string" || typeof bv === "string") {
      return String(av).localeCompare(String(bv), "zh") * dir;
    }
    return (av - bv) * dir;
  });
  return items;
}

function renderTypeChips(source) {
  const counts = new Map();
  for (const item of source) {
    const cat = categoryOf(item);
    const row = counts.get(cat) || { n: 0, size: 0 };
    row.n += 1;
    row.size += item.size || 0;
    counts.set(cat, row);
  }
  const order = ["video", "audio", "image", "archive", "disk", "installer", "document", "app", "code", "cache", "backup", "log", "folder", "other"];
  $("typechips").innerHTML = order.filter((cat) => counts.has(cat)).map((cat) => {
    const meta = CATS[cat];
    const row = counts.get(cat);
    const on = state.view === "cat:" + cat ? " on" : "";
    return '<button type="button" class="ghost typechip' + on + '" data-view="cat:' + cat
      + '" style="' + catStyle(cat) + '"><span class="mini-ico">' + (ICONS[cat] || ICONS.other)
      + "</span>" + meta.label + "<em>" + row.n + "</em></button>";
  }).join("");
}

function typeBrief(source) {
  const counts = new Map();
  for (const item of source) {
    const cat = categoryOf(item);
    counts.set(cat, (counts.get(cat) || 0) + (item.size || 0));
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3)
    .map(([cat, size]) => CATS[cat].label + " " + fmtSize(size)).join("，");
}

function renderTable() {
  if ($("resultPanel").hidden) return;
  const items = visibleItems();
  const emptyTitle = $("empty").querySelector("strong");
  const level = currentLevel();
  if (!items.length) {
    $("empty").style.display = "block";
    emptyTitle.textContent = level ? "这一层没有能列出来的内容" : (state.items.length ? "没有符合筛选的项目" : "还没有结果");
  } else {
    $("empty").style.display = "none";
  }
  $("rows").innerHTML = items.map((item) => {
    const checked = state.selected.has(item.path) ? " checked" : "";
    const cat = categoryOf(item);
    const meta = CATS[cat];
    const ext = (item.ext || "").toUpperCase();
    const previewable = isPreviewable(item);
    const cue = item.kind === "dir" ? " · 点击进入" : (previewable ? " · 点击预览" : " · 点击打开");
    const catLabel = meta.label + (ext ? " · " + ext : "") + cue;
    const picked = state.selected.has(item.path) ? " picked" : "";
    const thumb = previewable
      ? '<img class="thumb" alt="" loading="lazy" src="' + thumbUrl(item.path) + '" onerror="this.remove()">'
      : "";
    return '<tr class="' + item.kind + picked + '" data-path="' + esc(item.path) + '" data-kind="' + esc(item.kind)
      + '" data-preview="' + (previewable ? "1" : "") + '"><td class="check"><input type="checkbox" data-path="'
      + esc(item.path) + '"' + checked + "></td>"
      + '<td class="item"><span class="item-line">' + thumb + '<span class="file-ico" style="' + catStyle(cat) + '">'
      + (ICONS[cat] || ICONS.other) + '</span><span class="item-text"><span class="name-main">'
      + esc(itemName(item)) + '</span><span class="name-sub">' + esc(catLabel) + "</span></span></span></td>"
      + '<td class="size">' + fmtSize(item.size) + "</td>"
      + '<td class="time">' + fmtTime(item.mtime) + "</td>"
      + '<td class="path">' + esc(item.path) + "</td>"
      + '<td class="acts">'
      + (previewable
        ? '<button class="ghost mini" type="button" data-act="preview" data-path="' + esc(item.path) + '">预览</button> '
        : "")
      + (item.kind === "dir"
        ? '<button class="ghost mini" type="button" data-act="enter" data-path="' + esc(item.path) + '">进入</button> '
        : '<button class="ghost mini" type="button" data-act="open" data-path="' + esc(item.path) + '">打开</button> ')
      + '<button class="ghost mini" type="button" data-act="reveal" data-path="' + esc(item.path) + '">定位</button> '
      + (item.kind === "dir"
        ? '<button class="ghost mini" type="button" data-act="drill" data-path="' + esc(item.path) + '">只扫描这里</button>'
        : "")
      + "</td></tr>";
  }).join("");
  document.querySelectorAll("[data-sort]").forEach((th) => {
    const mark = state.sortKey === th.dataset.sort ? (state.sortDir < 0 ? " ↓" : " ↑") : "";
    th.textContent = th.dataset.label + mark;
  });
  const allOn = items.length > 0 && items.every((item) => state.selected.has(item.path));
  $("checkVisible").checked = allOn;
  $("checkVisible").disabled = state.running || items.length === 0;
  updateSelected();
  const source = level ? level.rows : state.items;
  renderTypeChips(source);
  const files = source.filter((item) => item.kind === "file").length;
  const dirs = source.length - files;
  const total = source.reduce((sum, item) => sum + item.size, 0);
  const brief = typeBrief(source);
  $("summary").textContent = source.length
    ? ((level ? (level.name + " 这一层：") : "")
      + "文件夹 " + dirs + " 个，文件 " + files + " 个，合计 " + fmtSize(total)
      + (brief ? (" · " + brief) : "")
      + qNote(items.length))
    : "";
}

function qNote(shown) {
  const q = $("filter").value.trim();
  return q ? ("，筛选后 " + shown + " 行") : "";
}

function lookupItem(path) {
  if (state.catalog.has(path)) return state.catalog.get(path);
  return state.items.find((item) => item.path === path) || null;
}

function rememberItems(items) {
  for (const item of items || []) {
    if (item.path) state.catalog.set(item.path, item);
    if (item.members) rememberItems(item.members);
  }
}

function updateSelected() {
  let bytes = 0;
  let n = 0;
  for (const path of state.selected) {
    const item = lookupItem(path);
    if (!item) continue;
    n += 1;
    bytes += item.size || 0;
  }
  $("selectedText").innerHTML = n
    ? ("已勾选 <b>" + n + "</b> 项，合计 <b>" + fmtSize(bytes) + "</b>")
    : "还没有勾选";
  $("deleteBtn").disabled = state.running || n === 0;
}

const LOG_PREVIEW = 8;

function applyLogs(logs) {
  state.logs = logs || [];
  renderScanLog();
}

function renderScanLog() {
  const filtering = state.full === "log";
  const q = filtering ? $("logFilter").value.trim().toLowerCase() : "";
  let lines = state.logs;
  if (q) lines = lines.filter((line) => line.toLowerCase().includes(q));
  else if (!filtering) lines = lines.slice(-LOG_PREVIEW);
  const box = $("scanLog");
  const follow = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
  box.innerHTML = lines.length
    ? lines.map((line) => "<li>" + esc(line) + "</li>").join("")
    : (q ? "<li>没有匹配的记录</li>" : "");
  $("logEmpty").hidden = state.logs.length > 0 || filtering;
  if (follow) box.scrollTop = box.scrollHeight;
}

function setStage(stage) {
  state.stage = stage;
  const show = stage === "results";
  $("resultPanel").hidden = !show;
  $("dock").hidden = !show;
  $("logPanel").classList.toggle("compact", show);
  $("logPanel").classList.toggle("filling", stage === "idle");
  $("scanWait").hidden = stage !== "scanning";
}

function applyStatus(data) {
  const was = state.stage;
  state.running = data.running;
  state.items = data.items || [];
  if (!data.running && state.note) $("status").textContent = state.note;
  else {
    state.note = "";
    $("status").textContent = data.message || "";
  }
  applyLogs(data.logs);
  $("scanBtn").disabled = data.running;
  $("stopBtn").disabled = !data.running;
  $("pickBtn").disabled = data.running;
  $("progress").classList.toggle("on", data.running);
  if (currentDisk()) showDisk(currentDisk());
  if (!state.booted) {
    state.booted = true;
    if (!data.running && state.items.length) state.stage = "results";
  }
  if (data.running) state.stage = "scanning";
  else if (was === "scanning") state.stage = "results";
  setStage(state.stage);
  if (was === "scanning" && !data.running && state.pendingMap && state.mapIndex < 0) {
    showLevel(state.pendingMap, true);
  } else if (state.stage === "results" && state.mapIndex < 0) {
    setMapMessage("占用还在计算。下面的文件夹可以点进下一层，文件可以直接打开。");
  }
  for (const item of state.items) {
    if (item.path) state.catalog.set(item.path, item);
  }
  for (const path of [...state.selected]) {
    if (!lookupItem(path)) state.selected.delete(path);
  }
  renderTable();
}

async function poll() {
  const res = await fetch("/api/status");
  const data = await res.json();
  applyStatus(data);
  if (data.running) setTimeout(poll, 700);
}

function showStatus(text) {
  state.note = text;
  $("status").textContent = text;
}

function readMb(id) {
  const raw = $(id).value.trim();
  if (raw === "") return 0;
  const value = Number(raw);
  if (!Number.isInteger(value)) return null;
  return value;
}

async function startScan(roots) {
  const minMb = readMb("minMb");
  const maxMb = readMb("maxMb");
  if (minMb === null || maxMb === null || minMb < 0 || maxMb < 0 || minMb > 10240 || maxMb > 10240) {
    showStatus("大小需要是 0 到 10240 的整数。留空或填 0 表示这一侧不限。");
    return;
  }
  if (maxMb > 0 && minMb > maxMb) {
    showStatus("大小上限要大于或等于下限。");
    return;
  }
  const body = {
    roots: roots && roots.length ? roots : selectedRoots(),
    min_mb: minMb,
    max_mb: maxMb,
    types: [...state.scanTypes],
    include_files: $("includeFiles").checked,
    skip_heavy: $("skipHeavy").checked,
  };
  if (!body.roots.length) {
    showStatus("先勾选一个位置，或点「选择文件夹」。");
    return;
  }
  state.selected.clear();
  state.catalog.clear();
  state.pendingMap = null;
  state.mapNodes = [];
  state.mapIndex = -1;
  state.stage = "scanning";
  setStage("scanning");
  applyLogs([]);
  state.note = "";
  $("status").textContent = "正在开始扫描…";
  loadProportion(body.roots);
  $("scanBtn").disabled = true;
  const res = await fetch("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    showStatus(data.error || "扫描没有开始");
    $("scanBtn").disabled = false;
    state.stage = "idle";
    setStage("idle");
    return;
  }
  poll();
}

function chosenItems() {
  const chosen = [];
  for (const path of state.selected) {
    const item = lookupItem(path);
    if (item) chosen.push(item);
  }
  return chosen;
}

function openDialog() {
  const chosen = chosenItems();
  if (!chosen.length) return;
  state.pendingDelete = chosen.map((item) => item.path);
  const bytes = chosen.reduce((sum, item) => sum + item.size, 0);
  $("dialogLead").textContent = "将把 " + chosen.length + " 项移到废纸篓，合计 " + fmtSize(bytes) + "。";
  const risky = chosen.filter((item) => warnHint(item.hint));
  $("dialogWarn").innerHTML = risky.length
    ? '<div class="warnbox">其中 ' + risky.length + ' 项看起来不像临时文件（照片、备份、应用程序或虚拟磁盘）。请再看一眼路径。</div>'
    : "";
  const shown = chosen.slice(0, 16);
  $("dialogList").innerHTML = shown.map((item) => (
    "<li><span class=\"dlg-name\">" + esc(itemName(item)) + "</span><span class=\"dlg-size\">"
    + fmtSize(item.size) + "</span><span class=\"dlg-path\">" + esc(item.path) + "</span></li>"
  )).join("")
    + (chosen.length > shown.length
      ? "<li><span class=\"dlg-name\">还有 " + (chosen.length - shown.length) + " 项</span><span></span></li>"
      : "");
  $("hovercard").classList.remove("show");
  $("mask").classList.add("show");
}

async function confirmDelete() {
  $("confirmDelete").disabled = true;
  try {
    const res = await fetch("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: state.pendingDelete }),
    });
    const data = await res.json();
    $("mask").classList.remove("show");
    if (!res.ok) {
      $("status").textContent = data.error || "删除失败";
      return;
    }
    const removed = new Map();
    for (const path of data.ok || []) {
      const item = lookupItem(path);
      removed.set(path, item ? (item.size || 0) : 0);
      state.selected.delete(path);
      state.catalog.delete(path);
    }
    if (data.disks && data.disks.length) {
      state.disks = data.disks;
      renderDisks();
    }
    dropFromMap(removed);
    const status = await fetch("/api/status");
    applyStatus(await status.json());
    const failed = data.failed || [];
    if (failed.length) {
      const moved = (data.trashed || data.ok || []).length;
      $("status").textContent = "已移到废纸篓 " + moved + " 项，失败 "
        + failed.length + " 项：" + failed[0].error;
    }
  } finally {
    $("confirmDelete").disabled = false;
  }
}

document.addEventListener("click", async (event) => {
  const remove = event.target.closest("[data-remove]");
  if (remove) {
    state.extraRoots.splice(Number(remove.dataset.remove), 1);
    renderExtra();
    return;
  }
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const path = button.dataset.path;
  if (button.dataset.act === "reveal") {
    await revealPath(path);
    return;
  }
  if (button.dataset.act === "open") {
    await openItem(path);
    return;
  }
  if (button.dataset.act === "preview") {
    openPreview(path);
    return;
  }
  if (button.dataset.act === "enter") {
    openUsage(path);
    return;
  }
  if (button.dataset.act === "drill") {
    document.querySelectorAll("#presets input").forEach((el) => {
      el.checked = false;
      el.closest(".place").classList.remove("on");
    });
    state.extraRoots = [path];
    renderExtra();
    startScan([path]);
  }
});

$("presets").addEventListener("change", (event) => {
  const input = event.target.closest("input[data-root]");
  if (!input) return;
  input.closest(".place").classList.toggle("on", input.checked);
  syncPlacePick();
});

function syncPlacePick() {
  const boxes = [...document.querySelectorAll("#presets input")];
  const usable = boxes.filter((el) => !el.disabled);
  const allOn = usable.length > 0 && usable.every((el) => el.checked);
  $("placePick").textContent = allOn ? "反选" : "全选";
  $("placePick").disabled = state.running || usable.length === 0;
}

$("placePick").addEventListener("click", () => {
  const boxes = [...document.querySelectorAll("#presets input")].filter((el) => !el.disabled);
  if (!boxes.length) return;
  const allOn = boxes.every((el) => el.checked);
  boxes.forEach((el) => {
    el.checked = allOn ? !el.checked : true;
    el.closest(".place").classList.toggle("on", el.checked);
  });
  syncPlacePick();
});

$("pickBtn").addEventListener("click", async () => {
  $("pickBtn").disabled = true;
  $("pickBtn").textContent = "请在弹出的窗口中选择…";
  try {
    const res = await fetch("/api/pick", { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      $("status").textContent = data.error || "没能打开选择窗口";
      return;
    }
    const known = new Set([...document.querySelectorAll("#presets input")].map((el) => el.dataset.root));
    for (const path of data.paths || []) {
      const preset = [...document.querySelectorAll("#presets input")].find((el) => el.dataset.root === path);
      if (preset) {
        preset.checked = true;
        preset.closest(".place").classList.add("on");
        continue;
      }
      if (!known.has(path) && !state.extraRoots.includes(path)) state.extraRoots.push(path);
    }
    renderExtra();
    if ((data.paths || []).length) {
      $("status").textContent = "已加入 " + data.paths.length + " 个文件夹。";
    }
  } finally {
    $("pickBtn").disabled = state.running;
    $("pickBtn").textContent = "选择文件夹";
  }
});

function syncScanTypes() {
  document.querySelectorAll("#scanTypes [data-type]").forEach((button) => {
    const type = button.dataset.type;
    const on = type ? state.scanTypes.has(type) : state.scanTypes.size === 0;
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

$("scanTypes").addEventListener("click", (event) => {
  const button = event.target.closest("[data-type]");
  if (!button) return;
  const type = button.dataset.type;
  if (!type) state.scanTypes.clear();
  else if (state.scanTypes.has(type)) state.scanTypes.delete(type);
  else state.scanTypes.add(type);
  syncScanTypes();
});

function setView(view) {
  state.view = view;
  document.querySelectorAll("#views button").forEach((el) => {
    el.classList.toggle("on", el.dataset.view === view);
  });
  renderTable();
}

$("views").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  setView(button.dataset.view);
});

$("typechips").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  setView(state.view === button.dataset.view ? "all" : button.dataset.view);
});

$("rows").addEventListener("change", (event) => {
  const box = event.target.closest("input[data-path]");
  if (!box) return;
  if (box.checked) state.selected.add(box.dataset.path);
  else state.selected.delete(box.dataset.path);
  const tr = box.closest("tr");
  if (tr) tr.classList.toggle("picked", box.checked);
  updateSelected();
  const items = visibleItems();
  $("checkVisible").checked = items.length > 0 && items.every((item) => state.selected.has(item.path));
  if (state.mapIndex >= 0) renderMap();
});

$("rows").addEventListener("click", (event) => {
  if (event.target.closest("button, input, a, td.check")) return;
  const tr = event.target.closest("tr[data-path]");
  if (!tr) return;
  if (tr.dataset.kind === "dir") openUsage(tr.dataset.path);
  else if (tr.dataset.preview === "1") openPreview(tr.dataset.path);
  else openItem(tr.dataset.path);
});

$("checkVisible").addEventListener("change", () => {
  for (const item of visibleItems()) {
    if ($("checkVisible").checked) state.selected.add(item.path);
    else state.selected.delete(item.path);
  }
  renderTable();
});

document.querySelector("thead").addEventListener("click", (event) => {
  const th = event.target.closest("[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  if (state.sortKey === key) state.sortDir *= -1;
  else { state.sortKey = key; state.sortDir = key === "name" ? 1 : -1; }
  renderTable();
});

$("filter").addEventListener("input", renderTable);
$("scanBtn").addEventListener("click", () => startScan());
$("stopBtn").addEventListener("click", () => fetch("/api/cancel", { method: "POST" }));
$("deleteBtn").addEventListener("click", openDialog);
$("cancelDelete").addEventListener("click", () => $("mask").classList.remove("show"));
$("confirmDelete").addEventListener("click", confirmDelete);
$("logFilter").addEventListener("input", renderScanLog);
$("copyLog").addEventListener("click", async () => {
  const button = $("copyLog");
  try {
    await navigator.clipboard.writeText((state.logs || []).join("\n"));
    button.textContent = "已复制";
  } catch (error) {
    button.textContent = "复制失败";
  }
  setTimeout(() => { button.textContent = "复制"; }, 1200);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if ($("previewMask").classList.contains("show")) {
    closePreview();
    return;
  }
  if ($("mask").classList.contains("show")) {
    $("mask").classList.remove("show");
    return;
  }
  if (state.full) {
    setFull("");
    return;
  }
  $("hovercard").classList.remove("show");
});

function setFull(mode) {
  state.full = mode || "";
  document.body.classList.toggle("fs-work", state.full === "work");
  document.body.classList.toggle("fs-log", state.full === "log");
  $("workFull").textContent = state.full === "work" ? "退出全屏" : "全屏";
  $("logFull").textContent = state.full === "log" ? "退出全屏" : "全屏";
  $("workFull").setAttribute("aria-pressed", state.full === "work" ? "true" : "false");
  $("logFull").setAttribute("aria-pressed", state.full === "log" ? "true" : "false");
  renderScanLog();
  requestAnimationFrame(() => {
    if (state.mapIndex >= 0) renderMap();
    if (state.full === "log") $("scanLog").scrollTop = $("scanLog").scrollHeight;
  });
}

$("workFull").addEventListener("click", () => setFull(state.full === "work" ? "" : "work"));
$("logFull").addEventListener("click", () => setFull(state.full === "log" ? "" : "log"));

function setMapMessage(text) {
  $("map").innerHTML = '<div class="map-empty">' + esc(text) + "</div>";
}

function renderCrumb() {
  const parts = state.mapNodes.map((node, index) => {
    const current = index === state.mapIndex;
    return '<button type="button" data-crumb="' + index + '"' + (current ? " disabled" : "") + ">"
      + esc(node.name) + "</button>";
  });
  $("crumb").innerHTML = parts.join('<span class="sep">/</span>');
}


function pctTextOf(size, total) {
  const pct = total ? size / total * 100 : 0;
  return (pct >= 10 ? pct.toFixed(0) : pct.toFixed(1)) + "%";
}

function explodeMembers(item, into) {
  if (item.kind === "group" && item.members) into.push(...item.members);
  else into.push(item);
}

function layoutBand(children, width) {
  const list = children.filter((item) => item.size > 0).slice().sort((a, b) => b.size - a.size);
  const total = list.reduce((sum, item) => sum + item.size, 0) || 1;
  const maxCards = width < 640 ? 3 : (width < 1100 ? 5 : 7);
  const minShare = width < 640 ? 0 : 0.08;
  const kept = [];
  const rest = [];
  for (const item of list) {
    const bigEnough = minShare === 0 || item.size / total >= minShare;
    if (kept.length < maxCards && bigEnough) kept.push(item);
    else rest.push(item);
  }
  if (!kept.length && rest.length) kept.push(rest.shift());
  let restItem = null;
  if (rest.length === 1 && rest[0].kind === "group") restItem = rest[0];
  else if (rest.length) {
    const members = [];
    rest.forEach((item) => explodeMembers(item, members));
    restItem = {
      name: "其他 " + members.length + " 项",
      path: "",
      size: members.reduce((sum, item) => sum + (item.size || 0), 0),
      kind: "group",
      hint: "点击查看较小的项目",
      category: "other",
      members,
    };
  }
  return { total, kept, restItem };
}

function itemKey(item) {
  return (item && (item.path || item.name)) || "";
}

function subLine(item) {
  const cat = categoryOf(item);
  const ext = (item.ext || "").toUpperCase();
  if (item.kind === "group") return item.hint || "点击查看";
  if (item.kind === "dir") return "文件夹 · 点击进入" + (item.mtime ? " · " + fmtDay(item.mtime) : "");
  const cue = isPreviewable(item) ? " · 点击预览" : "";
  return CATS[cat].label + (ext ? " · " + ext : "") + cue + (item.mtime ? " · " + fmtDay(item.mtime) : "");
}

function paintCell(cell, item, total) {
  const cat = categoryOf(item);
  cell.className = "cell" + (item.kind === "group" ? " more" : "") + (item.path && state.selected.has(item.path) ? " on" : "");
  cell.style.setProperty("--cat", CATS[cat].color);
  cell.style.setProperty("--cat-bg", CATS[cat].bg);
  cell.style.flex = item.kind === "group" ? (item.size + " 0 132px") : (item.size + " 1 0px");
  cell.dataset.key = itemKey(item);
  const icon = document.createElement("span");
  icon.className = "cell-icon";
  icon.innerHTML = ICONS[cat] || ICONS.other;
  const body = document.createElement("span");
  body.className = "cell-body";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = itemName(item);
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = fmtSize(item.size) + " · " + pctTextOf(item.size, total);
  const sub = document.createElement("span");
  sub.className = "sub";
  sub.textContent = subLine(item);
  body.append(name, meta, sub);
  cell.append(icon, body);
  if (isPreviewable(item)) {
    const thumb = new Image();
    thumb.className = "cell-thumb";
    thumb.alt = "";
    thumb.onload = () => { if (icon.isConnected) icon.replaceWith(thumb); };
    thumb.src = thumbUrl(item.path);
  }
  if (item.path && item.kind !== "group") {
    const path = document.createElement("span");
    path.className = "cell-path";
    path.textContent = shortPath(item.path);
    cell.append(path);
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.className = "tick";
    tick.checked = state.selected.has(item.path);
    tick.title = "勾选后可删除";
    cell.append(tick);
    const rev = document.createElement("button");
    rev.type = "button";
    rev.className = "cell-reveal";
    rev.textContent = item.kind === "file" ? (isPreviewable(item) ? "预览" : "打开") : "定位";
    cell.append(rev);
  }
}

function renderMap() {
  const node = state.mapNodes[state.mapIndex];
  renderCrumb();
  const map = $("map");
  if (!node) return;
  const width = map.clientWidth || (map.parentElement ? map.parentElement.clientWidth : 0);
  if (width < 40) return;
  const children = node.children || [];
  if (!children.length) {
    setMapMessage("这一层没有能列出来的内容。");
    state.mapView = [];
    state.barView = [];
    return;
  }
  const keepCard = $("hovercard").matches(":hover");
  const total = children.reduce((sum, item) => sum + item.size, 0) || node.size || 1;
  const band = layoutBand(children, width);
  const cards = band.kept.slice();
  if (band.restItem) cards.push(band.restItem);
  state.mapView = cards;
  state.barView = children.filter((item) => item.size > 0).slice().sort((a, b) => b.size - a.size);
  if (state.barView.length > 18) {
    const head = state.barView.slice(0, 17);
    const tail = state.barView.slice(17);
    const members = [];
    tail.forEach((item) => explodeMembers(item, members));
    head.push({
      name: "其他 " + members.length + " 项",
      path: "",
      size: members.reduce((sum, item) => sum + (item.size || 0), 0),
      kind: "group",
      category: "other",
      members,
    });
    state.barView = head;
  }
  map.innerHTML = "";
  const bar = document.createElement("div");
  bar.className = "sharebar";
  state.barView.forEach((item, index) => {
    const seg = document.createElement("button");
    seg.type = "button";
    seg.dataset.bar = String(index);
    seg.dataset.key = itemKey(item);
    seg.style.flex = item.size + " 1 4px";
    seg.style.setProperty("--cat", CATS[categoryOf(item)].color);
    seg.title = itemName(item) + "  " + fmtSize(item.size) + "  " + pctTextOf(item.size, total);
    bar.append(seg);
  });
  const row = document.createElement("div");
  row.className = "band";
  cards.forEach((item, index) => {
    const cell = document.createElement("div");
    cell.dataset.view = String(index);
    paintCell(cell, item, total);
    row.append(cell);
  });
  map.append(bar, row);
  mapWidth = width;
  if (!keepCard) $("hovercard").classList.remove("show");
}

function showLevel(node, replace) {
  rememberItems(node.children);
  if (replace) {
    state.mapNodes = [node];
    state.mapIndex = 0;
  } else {
    state.mapNodes = state.mapNodes.slice(0, state.mapIndex + 1);
    state.mapNodes.push(node);
    state.mapIndex = state.mapNodes.length - 1;
  }
  renderMap();
  renderTable();
}

async function fetchUsage(body) {
  const res = await fetch("/api/usage", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "占比没有算出来");
  return data;
}

async function loadProportion(paths) {
  const token = ++mapToken;
  if (state.stage !== "scanning") {
    state.mapNodes = [];
    state.mapIndex = -1;
    $("crumb").innerHTML = "";
    setMapMessage("正在按文件夹计算占用占比…");
  }
  try {
    const data = await fetchUsage({ paths });
    if (token !== mapToken) return;
    state.pendingMap = data;
    const status = await fetch("/api/status");
    const snap = await status.json();
    if (token !== mapToken) return;
    applyStatus(snap);
    if (state.stage !== "scanning" && state.pendingMap) {
      if (state.stage !== "results") {
        state.stage = "results";
        setStage("results");
      }
      showLevel(state.pendingMap, true);
    }
  } catch (error) {
    if (token !== mapToken) return;
    if (state.stage !== "scanning") setMapMessage(error.message || "占比没有算出来");
  }
}

async function revealPath(path) {
  const res = await fetch("/api/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    $("status").textContent = data.error || "没有定位到";
  }
}

async function openItem(path) {
  const res = await fetch("/api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    $("status").textContent = data.error || "没有打开";
  }
}

function playableVideo(path) {
  const ext = ((path || "").split(".").pop() || "").toLowerCase();
  return ext === "mp4" || ext === "mov" || ext === "m4v" || ext === "webm";
}

function closePreview() {
  const video = $("previewVideo");
  video.hidden = true;
  video.pause();
  video.removeAttribute("src");
  video.load();
  const img = $("previewImage");
  img.hidden = true;
  img.removeAttribute("src");
  $("previewNote").textContent = "";
  $("previewMask").classList.remove("show");
}

function openPreview(path) {
  if (!path) return;
  const item = lookupItem(path) || { path, kind: "file", name: baseName(path) };
  const cat = categoryOf(item);
  const img = $("previewImage");
  const video = $("previewVideo");
  $("hovercard").classList.remove("show");
  $("previewName").textContent = itemName(item);
  $("previewNote").textContent = "";
  $("previewOpen").dataset.path = path;
  video.hidden = true;
  video.pause();
  video.removeAttribute("src");
  video.load();
  img.hidden = true;
  img.removeAttribute("src");
  if (cat === "video" && playableVideo(path)) {
    img.hidden = true;
    video.hidden = false;
    video.poster = mediaUrl("preview", path);
    video.src = mediaUrl("media", path);
    video.play().catch(() => {});
  } else {
    video.hidden = true;
    img.hidden = false;
    img.src = mediaUrl("preview", path);
    if (cat === "video") $("previewNote").textContent = "这个格式先看封面，播放请用「打开」。";
  }
  $("previewMask").classList.add("show");
}

$("closePreview").addEventListener("click", closePreview);
$("previewMask").addEventListener("click", (event) => {
  if (event.target === $("previewMask")) closePreview();
});
$("previewImage").addEventListener("error", () => {
  $("previewImage").hidden = true;
  $("previewNote").textContent = "做不了画面预览，可以用「打开」。";
});
$("previewVideo").addEventListener("error", () => {
  if (!$("previewMask").classList.contains("show") || $("previewVideo").hidden) return;
  $("previewVideo").hidden = true;
  $("previewNote").textContent = "浏览器放不了这个视频，可以用「打开」。";
});

async function openUsage(path) {
  const token = ++mapToken;
  setMapMessage("正在进入 " + shortPath(path) + " …");
  try {
    const data = await fetchUsage({ path });
    if (token !== mapToken) return;
    showLevel(data, false);
    const status = await fetch("/api/status");
    const snap = await status.json();
    if (token !== mapToken) return;
    applyLogs(snap.logs);
    if (snap.message) $("status").textContent = snap.message;
  } catch (error) {
    if (token !== mapToken) return;
    setMapMessage(error.message || "这一层没有算出来");
  }
}

function isUnderPath(path, root) {
  if (!path || !root) return false;
  const base = String(root).replace(/\/+$/, "");
  return path === base || path.startsWith(base + "/");
}

function treeHasPath(item, path) {
  if (!item) return false;
  if (item.path === path) return true;
  for (const member of item.members || []) {
    if (treeHasPath(member, path)) return true;
  }
  return false;
}

function pruneDeleted(items, removed) {
  const roots = [...removed.keys()];
  const gone = (path) => roots.some((root) => isUnderPath(path, root));
  let dropped = 0;
  const kept = [];
  for (const item of items || []) {
    const path = item.path || "";
    if (path && gone(path)) {
      dropped += item.size || 0;
      continue;
    }
    let inner = 0;
    let members = item.members;
    if (members && members.length) {
      const pruned = pruneDeleted(members, removed);
      members = pruned[0];
      inner += pruned[1];
    }
    if (path) {
      for (const [delPath, delSize] of removed) {
        if (delPath === path || !isUnderPath(delPath, path) || treeHasPath(item, delPath)) continue;
        const covered = roots.some((root) => root !== delPath && isUnderPath(delPath, root));
        if (covered) continue;
        inner += delSize || 0;
      }
    }
    if (!path && item.kind === "group" && !(members || []).length) {
      dropped += item.size || 0;
      continue;
    }
    if (inner || members !== item.members) {
      const next = Object.assign({}, item, {
        members,
        size: Math.max(0, (item.size || 0) - inner),
      });
      if (next.kind === "group") next.hint = "还有 " + (members || []).length + " 个较小项目";
      if (next.kind === "group" && next.size <= 0) continue;
      kept.push(next);
    } else {
      kept.push(item);
    }
    dropped += inner;
  }
  return [kept, dropped];
}

function dropFromMap(removed) {
  if (!removed || !removed.size || !state.mapNodes.length) {
    renderTable();
    return;
  }
  const roots = [...removed.keys()];
  const gone = (path) => roots.some((root) => isUnderPath(path, root));
  while (state.mapIndex > 0) {
    const node = state.mapNodes[state.mapIndex];
    if (node && node.path && gone(node.path)) {
      state.mapNodes.pop();
      state.mapIndex -= 1;
      continue;
    }
    break;
  }
  for (const node of state.mapNodes) {
    const pruned = pruneDeleted(node.children, removed);
    node.children = pruned[0];
    node.size = Math.max(0, (node.size || 0) - pruned[1]);
  }
  renderMap();
  renderTable();
}

$("crumb").addEventListener("click", (event) => {
  const button = event.target.closest("[data-crumb]");
  if (!button || button.disabled) return;
  state.mapIndex = Number(button.dataset.crumb);
  renderMap();
  renderTable();
});

function mapItemFrom(anchor) {
  if (!anchor) return null;
  if (anchor.dataset.view != null && anchor.classList.contains("cell")) {
    return state.mapView && state.mapView[Number(anchor.dataset.view)];
  }
  if (anchor.dataset.bar != null) return state.barView && state.barView[Number(anchor.dataset.bar)];
  return null;
}

function openGroup(item) {
  const node = state.mapNodes[state.mapIndex] || {};
  showLevel({
    name: "其他",
    path: node.path || "",
    size: item.size,
    children: item.members || [],
  }, false);
}

function showHover(item, anchor, total) {
  clearTimeout(hoverHide);
  state.hoverItem = item;
  state.hoverTotal = total;
  const cat = categoryOf(item);
  const meta = CATS[cat];
  const ext = (item.ext || "").toUpperCase();
  const bits = [fmtSize(item.size), pctTextOf(item.size, total), meta.label];
  if (ext) bits.push(ext);
  if (item.mtime) bits.push(fmtTime(item.mtime));
  const actions = [];
  if (item.path && item.kind !== "group") {
    actions.push('<button type="button" class="ghost mini" data-hover="toggle">'
      + (state.selected.has(item.path) ? "取消勾选" : "勾选") + "</button>");
    actions.push('<button type="button" class="ghost mini" data-hover="reveal">定位</button>');
    actions.push('<button type="button" class="ghost mini" data-hover="copy">复制路径</button>');
  }
  if (isPreviewable(item)) {
    actions.unshift('<button type="button" class="primary mini" data-hover="preview">预览</button>');
  } else if (item.kind === "file" && item.path) {
    actions.unshift('<button type="button" class="primary mini" data-hover="open">打开</button>');
  }
  if (item.kind === "dir" && item.path) {
    actions.push('<button type="button" class="primary mini" data-hover="enter">进入这一层</button>');
  }
  if (item.kind === "group") {
    actions.push('<button type="button" class="primary mini" data-hover="enter">查看这些项</button>');
  }
  const hint = item.hint && item.hint !== meta.label
    ? '<div class="hc-meta">' + esc(item.hint) + "</div>" : "";
  const shot = isPreviewable(item)
    ? '<img class="hc-shot" alt="" src="' + thumbUrl(item.path) + '" onerror="this.remove()">'
    : "";
  const card = $("hovercard");
  card.innerHTML = shot + '<div class="hc-top"><span class="hc-icon" style="' + catStyle(cat) + '">'
    + (ICONS[cat] || ICONS.other) + '</span><div><div class="hc-name">' + esc(itemName(item))
    + '</div><div class="hc-meta">' + esc(bits.join(" · ")) + "</div>" + hint + "</div></div>"
    + (item.path ? '<div class="hc-path">' + esc(item.path) + "</div>" : "")
    + '<div class="hc-actions">' + actions.join("") + "</div>";
  card.classList.add("show");
  const rect = anchor.getBoundingClientRect();
  let left = rect.left;
  let top = rect.bottom + 8;
  const width = card.offsetWidth;
  const height = card.offsetHeight;
  if (left + width > window.innerWidth - 12) left = window.innerWidth - width - 12;
  if (top + height > window.innerHeight - 12) top = Math.max(8, rect.top - height - 8);
  card.style.left = Math.max(8, left) + "px";
  card.style.top = top + "px";
}

function hideHoverSoon() {
  clearTimeout(hoverHide);
  hoverHide = setTimeout(() => $("hovercard").classList.remove("show"), 160);
}

$("map").addEventListener("click", (event) => {
  const reveal = event.target.closest(".cell-reveal");
  const tick = event.target.closest("input.tick");
  const cell = event.target.closest(".cell");
  const seg = event.target.closest(".sharebar button");
  if (reveal) {
    const item = mapItemFrom(cell);
    if (item && item.path) {
      if (isPreviewable(item)) openPreview(item.path);
      else if (item.kind === "file") openItem(item.path);
      else revealPath(item.path);
    }
    return;
  }
  const anchor = cell || seg;
  if (!anchor) return;
  const item = mapItemFrom(anchor);
  if (!item) return;
  if (tick) {
    if (tick.checked) state.selected.add(item.path);
    else state.selected.delete(item.path);
    cell.classList.toggle("on", tick.checked);
    updateSelected();
    renderTable();
    return;
  }
  if (item.kind === "dir" && item.path) {
    openUsage(item.path);
    return;
  }
  if (item.kind === "group") {
    openGroup(item);
    return;
  }
  if (item.kind === "file" && item.path) {
    if (isPreviewable(item)) openPreview(item.path);
    else openItem(item.path);
    return;
  }
  if (!item.path) return;
  if (state.selected.has(item.path)) state.selected.delete(item.path);
  else state.selected.add(item.path);
  renderMap();
  renderTable();
});

function folderHover(item) {
  return !!item && (item.kind === "dir" || item.kind === "group");
}

$("map").addEventListener("mouseover", (event) => {
  const anchor = event.target.closest(".cell, .sharebar button");
  if (!anchor || anchor.contains(event.relatedTarget)) return;
  const item = mapItemFrom(anchor);
  if (!item) return;
  document.querySelectorAll("#map .sharebar button").forEach((el) => {
    el.classList.toggle("dim", el.dataset.key !== anchor.dataset.key);
  });
  if (folderHover(item)) {
    $("hovercard").classList.remove("show");
    return;
  }
  const node = state.mapNodes[state.mapIndex];
  const total = ((node && node.children) || []).reduce((sum, child) => sum + child.size, 0) || item.size || 1;
  showHover(item, anchor, total);
});

$("map").addEventListener("mouseout", (event) => {
  const anchor = event.target.closest(".cell, .sharebar button");
  if (!anchor || anchor.contains(event.relatedTarget)) return;
  if ($("hovercard").contains(event.relatedTarget)) return;
  document.querySelectorAll("#map .sharebar button").forEach((el) => el.classList.remove("dim"));
  hideHoverSoon();
});

$("hovercard").addEventListener("mouseenter", () => clearTimeout(hoverHide));
$("hovercard").addEventListener("mouseleave", hideHoverSoon);
$("hovercard").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-hover]");
  const item = state.hoverItem;
  if (!button || !item) return;
  const act = button.dataset.hover;
  if (act === "toggle" && item.path) {
    if (state.selected.has(item.path)) state.selected.delete(item.path);
    else state.selected.add(item.path);
    button.textContent = state.selected.has(item.path) ? "取消勾选" : "勾选";
    renderTable();
    if (state.mapIndex >= 0) renderMap();
    return;
  }
  if (act === "reveal" && item.path) {
    revealPath(item.path);
    return;
  }
  if (act === "open" && item.path) {
    openItem(item.path);
    return;
  }
  if (act === "preview" && item.path) {
    $("hovercard").classList.remove("show");
    openPreview(item.path);
    return;
  }
  if (act === "copy" && item.path) {
    try {
      await navigator.clipboard.writeText(item.path);
      button.textContent = "已复制";
    } catch (error) {
      button.textContent = "复制失败";
    }
    return;
  }
  if (act === "enter") {
    $("hovercard").classList.remove("show");
    if (item.kind === "dir" && item.path) openUsage(item.path);
    else if (item.kind === "group") openGroup(item);
  }
});

new ResizeObserver(() => {
  const width = $("map").clientWidth;
  if (state.mapIndex >= 0 && Math.abs(width - mapWidth) > 8) renderMap();
}).observe($("map"));

$("disks").addEventListener("click", (event) => {
  const button = event.target.closest("[data-disk]");
  if (!button || state.running) return;
  state.diskId = button.dataset.disk;
  const disk = currentDisk();
  if (disk) showDisk(disk);
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || state.running) return;
  fetch("/api/meta").then((res) => res.json()).then((meta) => {
    state.disks = meta.disks || state.disks;
    renderDisks();
  }).catch(() => {});
});

setStage(state.stage);
loadMeta().then(poll);
</script>
</body>
</html>
"""

PAGE = PAGE.replace("<!--SCAN_TYPES-->", scan_type_buttons())


def _realpath(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _is_under(path: str, root: str) -> bool:
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    return path == root or path.startswith(root + os.sep)


def _protected(path: str) -> bool:
    real = _realpath(path) if os.path.exists(path) else os.path.normpath(path)
    raw = os.path.normpath(path)
    for prefix in PROTECTED_PREFIXES:
        if _is_under(real, prefix) or _is_under(raw, prefix):
            return True
        if prefix.startswith(raw + os.sep) or prefix == raw:
            return True
    return False


def _home_dir_blocks() -> set[str]:
    home = str(Path.home())
    names = (
        "",
        "Desktop",
        "Documents",
        "Downloads",
        "Library",
        "Movies",
        "Music",
        "Pictures",
        "Public",
        "Applications",
    )
    blocked = {
        os.path.normpath(os.path.join(home, folder) if folder else home)
        for folder in names
    }
    blocked.add("/Applications")
    return blocked


def delete_block_reason(path: str) -> str | None:
    if _protected(path):
        return "这是受保护的目录，不能删除"
    real = _realpath(path)
    if real in _home_dir_blocks() or os.path.normpath(path) in _home_dir_blocks():
        return "这是主目录或常用顶层文件夹，不能整夹删除"
    parts = Path(real).parts
    name = Path(real).name
    for part in parts:
        if part.endswith(".app") and part != name:
            return "这是应用程序内部文件，不能单独删除"
        if part.endswith(".photoslibrary") and part != name:
            return "这是照片图库内部文件，不能单独删除"
        if part.endswith(".musiclibrary") and part != name:
            return "这是音乐库内部文件，不能单独删除"
    return None


def root_allowed(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    if _protected(path):
        return False
    real = _realpath(path)
    allowed = []
    for prefix in ("/Users", "/Volumes", "/Applications", "/opt", "/usr/local", "/private/tmp", "/tmp"):
        if os.path.exists(prefix):
            allowed.append(_realpath(prefix))
        else:
            allowed.append(prefix)
    return any(_is_under(real, prefix) for prefix in allowed)


_EXT_CATEGORY = {
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".avi": "video",
    ".m4v": "video", ".webm": "video", ".flv": "video", ".wmv": "video",
    ".ts": "video", ".mpeg": "video", ".mpg": "video", ".m2ts": "video", ".3gp": "video",
    ".heif": "image", ".avif": "image", ".jfif": "image",
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".flac": "audio",
    ".aac": "audio", ".aiff": "audio", ".ogg": "audio",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".heic": "image", ".webp": "image", ".tif": "image", ".tiff": "image",
    ".raw": "image", ".psd": "image", ".svg": "image", ".bmp": "image",
    ".zip": "archive", ".7z": "archive", ".rar": "archive", ".bz2": "archive",
    ".xz": "archive", ".gz": "archive", ".tgz": "archive", ".tar": "archive",
    ".dmg": "disk", ".iso": "disk", ".sparseimage": "disk", ".sparsebundle": "disk",
    ".vmdk": "disk", ".qcow2": "disk",
    ".ipsw": "installer", ".pkg": "installer", ".mpkg": "installer",
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".ppt": "document", ".pptx": "document", ".xls": "document", ".xlsx": "document",
    ".key": "document", ".pages": "document", ".numbers": "document",
    ".txt": "document", ".rtf": "document", ".csv": "document",
    ".app": "app", ".log": "log",
}
_CODE_DIR_NAMES = {"node_modules", ".git", "pods", "vendor", ".next", "__pycache__", "target"}
_ENTRY_KEYS = ("name", "path", "size", "mtime", "used", "kind", "hint", "category", "ext")


def file_ext(name: str) -> str:
    lower = name.lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lower.endswith(compound):
            return compound[1:]
    suffix = Path(name).suffix.lower()
    if suffix in {"", "."}:
        return ""
    return suffix[1:]


def category_for(path: str, kind: str) -> str:
    name = os.path.basename(path.rstrip(os.sep))
    low = name.lower()
    folded = path.lower()
    if kind == "dir":
        if low in _CODE_DIR_NAMES:
            return "code"
        if low in {"caches", "cache"} or "deriveddata" in folded or "/caches/" in folded:
            return "cache"
        if "mobilesync/backup" in folded:
            return "backup"
        if low.endswith(".photoslibrary"):
            return "image"
        if low.endswith(".musiclibrary"):
            return "audio"
        if low.endswith(".app"):
            return "app"
        return "folder"
    if "docker" in folded and (low.endswith((".raw", ".qcow2", ".vmdk", ".img")) or low == "docker.raw"):
        return "disk"
    if low.endswith(".log") or "/logs/" in folded:
        return "log"
    ext = file_ext(name)
    if ext in {"tar.gz", "tar.bz2", "tar.xz", "tar.zst"}:
        return "archive"
    return _EXT_CATEGORY.get("." + ext, "other")


def hint_for(path: str, kind: str) -> str:
    name = os.path.basename(path)
    low = name.lower()
    folded = path.lower()
    if kind == "dir" and low == "node_modules":
        return "依赖目录，确认不用后可以删，以后还能再安装"
    if low in {"caches", "cache"} or "/library/caches/" in folded or "/caches/" in folded:
        return "缓存，删了通常会自动再生"
    if "deriveddata" in folded:
        return "Xcode 编译缓存"
    if "mobilesync/backup" in folded:
        return "iPhone / iPad 备份"
    if low.endswith(".photoslibrary"):
        return "照片图库，删除会丢掉里面的照片"
    if low.endswith(".musiclibrary"):
        return "音乐库"
    if low.endswith(".app"):
        return "应用程序"
    if "docker" in folded and (low.endswith((".raw", ".qcow2", ".vmdk", ".img")) or low == "docker.raw"):
        return "Docker 虚拟磁盘，删除会丢掉容器数据"
    if low.endswith((".dmg", ".iso", ".pkg", ".ipsw")):
        return "安装包或固件，装完后通常可以删"
    if low.endswith((".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz")):
        return "压缩包，确认已经解压过再删"
    if low.endswith(".log") or "/logs/" in folded:
        return "日志"
    if low.endswith((".mp4", ".mov", ".mkv", ".avi", ".m4v")):
        return "视频"
    return ""


def display_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def disk_payload() -> dict:
    path = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
    usage = shutil.disk_usage(path)
    return {"path": path, "total": usage.total, "used": usage.used, "free": usage.free}


def _mount_of(path: str) -> str:
    out = subprocess.check_output(["df", "-P", path], text=True, timeout=8)
    line = out.splitlines()[-1]
    return line.split(None, 5)[-1]


def _volume_name(path: str) -> str:
    try:
        out = subprocess.check_output(
            ["diskutil", "info", path],
            text=True,
            timeout=8,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    for line in out.splitlines():
        if line.strip().startswith("Volume Name:"):
            return line.split(":", 1)[1].strip()
    return ""


def _skip_mount(mount: str) -> bool:
    if mount in {"/", "/dev"} or mount.startswith("/dev/"):
        return True
    if mount.startswith("/System/Volumes/") and mount != "/System/Volumes/Data":
        return True
    if mount.startswith("/private/var/") or "/X/" in mount:
        return True
    if not os.path.isdir(mount):
        return True
    return False


def list_disks() -> list[dict]:
    try:
        out = subprocess.check_output(["df", "-kP"], text=True, timeout=8)
    except (subprocess.SubprocessError, OSError):
        out = ""
    try:
        home_mount = _mount_of(str(Path.home()))
    except (subprocess.SubprocessError, OSError, IndexError):
        home_mount = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
    startup_name = _volume_name("/") or "Macintosh HD"
    rows: list[dict] = []
    seen: set[str] = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            total = int(parts[1]) * 1024
            used = int(parts[2]) * 1024
            free = int(parts[3]) * 1024
        except ValueError:
            continue
        mount = " ".join(parts[5:])
        if mount in seen or _skip_mount(mount) or total <= 0:
            continue
        seen.add(mount)
        name = _volume_name(mount)
        if mount == "/System/Volumes/Data" and name in {"", "Data"}:
            name = startup_name
        if not name:
            name = os.path.basename(mount.rstrip(os.sep)) or mount
        rows.append({
            "id": mount,
            "name": name,
            "path": mount,
            "total": total,
            "used": used,
            "free": free,
            "home": os.path.normpath(mount) == os.path.normpath(home_mount),
        })
    if not rows:
        base = disk_payload()
        base.update({"id": base["path"], "name": startup_name, "home": True})
        rows.append(base)
    rows.sort(key=lambda item: (not item["home"], item["name"]))
    return rows


def presets() -> list[dict]:
    home = Path.home()
    rows = [
        ("downloads", "下载", home / "Downloads"),
        ("desktop", "桌面", home / "Desktop"),
        ("documents", "文稿", home / "Documents"),
        ("movies", "影片", home / "Movies"),
        ("music", "音乐", home / "Music"),
        ("pictures", "图片", home / "Pictures"),
        ("caches", "资源库缓存", home / "Library" / "Caches"),
        ("app_support", "应用程序支持", home / "Library" / "Application Support"),
        ("home", "整个用户目录", home),
    ]
    return [{"id": key, "label": label, "path": str(path)} for key, label, path in rows if path.is_dir()]


def dedupe_roots(roots: list[str]) -> list[str]:
    ordered = sorted({_realpath(root) for root in roots}, key=len)
    kept: list[str] = []
    for root in ordered:
        if any(_is_under(root, earlier) for earlier in kept):
            continue
        kept.append(root)
    return kept


def parse_mdls_date(raw: str) -> int | None:
    text = raw.strip()
    if not text or text == "(null)":
        return None
    try:
        parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(parsed.timestamp())


def last_used(path: str) -> int | None:
    try:
        done = subprocess.run(
            ["mdls", "-name", "kMDItemLastUsedDate", "-raw", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_mdls_date(done.stdout)


def last_used_batch(paths: list[str]) -> dict[str, int | None]:
    found = {path: None for path in paths}
    if not paths:
        return found
    try:
        done = subprocess.run(
            ["mdls", "-name", "kMDItemLastUsedDate", *paths],
            check=False,
            capture_output=True,
            text=True,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        return found
    lines = [
        line.strip()
        for line in done.stdout.splitlines()
        if line.strip().startswith("kMDItemLastUsedDate")
    ]
    if len(lines) != len(paths):
        for path in paths:
            found[path] = last_used(path)
        return found
    for path, line in zip(paths, lines):
        raw = line.split("=", 1)[1].strip() if "=" in line else ""
        found[path] = parse_mdls_date(raw)
    return found


def parse_mb(value: object, *, default: int, label: str) -> tuple[int | None, str | None]:
    if value is None or value == "":
        return default, None
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None, f"{label}需要是整数"
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, f"{label}需要是整数"
    if not 0 <= number <= 10240:
        return None, f"{label}需要在 0 到 10240 MB 之间，0 表示不限"
    return number, None


def parse_scan_types(value: object) -> tuple[list[str] | None, str | None]:
    if value is None or value == "":
        return [], None
    if not isinstance(value, list):
        return None, "文件类型不对"
    chosen: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return None, "文件类型不对"
        if item == "":
            continue
        if item not in _SCAN_TYPE_BY_ID:
            return None, f"不认识的文件类型：{item}"
        if item not in seen:
            seen.add(item)
            chosen.append(item)
    return chosen, None


def extensions_for(type_ids: list[str]) -> frozenset[str] | None:
    if not type_ids:
        return None
    found: set[str] = set()
    for type_id in type_ids:
        found.update(_SCAN_TYPE_BY_ID[type_id][2])
    return frozenset(found)


def size_rule_text(min_mb: int, max_mb: int) -> str:
    if min_mb <= 0 and max_mb <= 0:
        return "大小不限"
    if min_mb <= 0:
        return f"不超过 {max_mb} MB"
    if max_mb <= 0:
        return f"至少 {min_mb} MB"
    return f"{min_mb}–{max_mb} MB"


def type_rule_text(type_ids: list[str]) -> str:
    if not type_ids:
        return ""
    return "只查找 " + "、".join(_SCAN_TYPE_BY_ID[type_id][1] for type_id in type_ids)


def find_name_clause(exts: frozenset[str] | None) -> list[str]:
    if not exts:
        return []
    clause = ["("]
    for index, ext in enumerate(sorted(exts)):
        if index:
            clause.append("-o")
        clause.extend(["-iname", f"*.{ext}"])
    clause.append(")")
    return clause


def find_size_clause(min_bytes: int, max_bytes: int) -> list[str]:
    clause: list[str] = []
    if min_bytes > 0:
        clause.extend(["-size", f"+{min_bytes - 1}c"])
    if max_bytes > 0:
        clause.extend(["-size", f"-{max_bytes + 1}c"])
    return clause


def within_size(size: int, min_bytes: int, max_bytes: int) -> bool:
    if min_bytes > 0 and size < min_bytes:
        return False
    if max_bytes > 0 and size > max_bytes:
        return False
    return True


def matches_scan_type(name: str, exts: frozenset[str] | None) -> bool:
    if not exts:
        return True
    low = name.lower()
    return any(low.endswith("." + ext) for ext in exts)


class Scanner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.cancel = False
        self.message = "还没有扫描。"
        self.usage_message = ""
        self.logs: list[str] = []
        self.items: list[dict] = []
        self._progress_kind = ""
        self.allowed: set[str] = set()
        self.procs: list[subprocess.Popen] = []
        self._hit_cap = False
        self.log_path = Path(__file__).with_name("delete-log.txt")

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "message": self.message,
                "usage_message": self.usage_message,
                "logs": list(self.logs),
                "items": list(self.items),
            }

    def _append_log_locked(
        self,
        message: str,
        *,
        progress: bool = False,
        keep_message: bool = False,
        kind: str = "scan",
    ) -> None:
        home = str(Path.home())
        text = message.replace(home, "~")
        if not keep_message:
            self.message = text
        line = datetime.now().strftime("%H:%M:%S") + "  " + text
        if progress and self.logs and self._progress_kind == kind:
            self.logs[-1] = line
        elif not self.logs or not self.logs[-1].endswith("  " + text):
            self.logs.append(line)
        self._progress_kind = kind if progress else ""
        if len(self.logs) > 500:
            del self.logs[:-500]
            self._progress_kind = ""

    def _set_message(self, message: str, *, progress: bool = False) -> None:
        with self.lock:
            self._append_log_locked(message, progress=progress, kind="scan")

    def _note_usage(self, message: str, *, progress: bool = False) -> None:
        with self.lock:
            self.usage_message = message.replace(str(Path.home()), "~")
            self._append_log_locked(
                message,
                progress=progress,
                keep_message=self.running,
                kind="usage",
            )

    def _replace_items(self, items: list[dict], allowed: set[str] | None = None) -> None:
        with self.lock:
            self.items = items
            if allowed is not None:
                # 占比图在扫描过程中也会把路径放进可删除名单，结束时要留着。
                self.allowed = set(allowed) | self.allowed

    def cancel_scan(self) -> None:
        with self.lock:
            self.cancel = True
            procs = list(self.procs)
        for proc in procs:
            self._stop_proc(proc)

    def _stop_proc(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=2)

    def _track(self, proc: subprocess.Popen) -> None:
        with self.lock:
            self.procs.append(proc)

    def _untrack(self, proc: subprocess.Popen) -> None:
        with self.lock:
            if proc in self.procs:
                self.procs.remove(proc)

    def _cancelled(self) -> bool:
        with self.lock:
            return self.cancel

    def start(
        self,
        roots: list[str],
        min_mb: int,
        max_mb: int,
        include_files: bool,
        skip_heavy: bool,
        type_ids: list[str],
    ) -> str | None:
        with self.lock:
            if self.running:
                return "已经有一次扫描在进行"
            self.running = True
            self.cancel = False
            self.items = []
            self.allowed = set()
            self.logs = []
            self._progress_kind = ""
            self.usage_message = ""
            self._hit_cap = False
        rule = size_rule_text(min_mb, max_mb)
        kinds = type_rule_text(type_ids)
        detail = f"{rule}，{kinds}" if kinds else rule
        self._set_message(f"开始扫描 {len(roots)} 个位置，{detail}")
        if type_ids:
            extra = "，并跳过依赖目录" if skip_heavy else ""
            self._set_message(f"这次只查找所选类型的文件，不统计文件夹{extra}")
        elif include_files and skip_heavy:
            self._set_message("查找大文件时会跳过 node_modules、.git 这类依赖目录")
        elif not include_files:
            self._set_message("这次只统计文件夹占用，不逐个查找大文件")
        thread = threading.Thread(
            target=self._run,
            args=(roots, min_mb, max_mb, include_files, skip_heavy, type_ids),
            daemon=True,
        )
        thread.start()
        return None

    def _run(
        self,
        roots: list[str],
        min_mb: int,
        max_mb: int,
        include_files: bool,
        skip_heavy: bool,
        type_ids: list[str],
    ) -> None:
        min_bytes = min_mb * 1024 * 1024
        max_bytes = max_mb * 1024 * 1024 if max_mb > 0 else 0
        exts = extensions_for(type_ids)
        file_scan = include_files or bool(type_ids)
        items: list[dict] = []
        try:
            for index, root in enumerate(roots, start=1):
                if self._cancelled():
                    break
                shown = display_path(root)
                if not type_ids:
                    self._set_message(f"正在统计这一层文件夹占用（{index}/{len(roots)}）：{shown}")
                    found_dirs = self._du_children(root, min_bytes, max_bytes)
                    items.extend(found_dirs)
                    self._replace_items(sorted(items, key=lambda item: item["size"], reverse=True))
                    if min_mb <= 0 and max_mb <= 0:
                        self._set_message(f"文件夹统计完：{shown}，这一层有 {len(found_dirs)} 个")
                    else:
                        self._set_message(
                            f"文件夹统计完：{shown}，{size_rule_text(min_mb, max_mb)} 的有 {len(found_dirs)} 个"
                        )
                if file_scan and not self._cancelled():
                    self._set_message(f"正在查找文件（{index}/{len(roots)}）：{shown}")
                    found_files = self._find_files(root, min_bytes, max_bytes, skip_heavy, exts)
                    items.extend(found_files)
                    self._replace_items(sorted(items, key=lambda item: item["size"], reverse=True)[:MAX_FILES])
                    self._set_message(f"文件查找完：{shown}，找到 {len(found_files)} 个")
            items = sorted(items, key=lambda item: item["size"], reverse=True)
            truncated = len(items) > MAX_FILES or self._hit_cap
            items = items[:MAX_FILES]
            allowed = {item["path"] for item in items}
            self._replace_items(items, allowed)
            if self._cancelled():
                self._set_message(f"已停止。目前列出 {len(items)} 项。")
            else:
                extra = f" 只保留了最大的 {MAX_FILES} 项。" if truncated else ""
                self._set_message(f"扫描完成，共 {len(items)} 项。{extra}勾选后可以删除。")
        except Exception as exc:  # noqa: BLE001  把扫描错误显示到页面上
            self._set_message(f"扫描中断：{exc}")
        finally:
            with self.lock:
                self.running = False
                self.procs.clear()

    def _du_children(self, root: str, min_bytes: int, max_bytes: int) -> list[dict]:
        proc = subprocess.Popen(
            ["du", "-k", "-d", "1", root],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self._track(proc)
        found: list[dict] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            if self._cancelled():
                self._stop_proc(proc)
                break
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            raw_size, path = line.split("\t", 1)
            if os.path.normpath(path) == os.path.normpath(root):
                continue
            if _protected(path):
                continue
            try:
                size = int(raw_size) * 1024
            except ValueError:
                continue
            if not within_size(size, min_bytes, max_bytes):
                continue
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            found.append(self._item(path, size, st.st_mtime, "dir"))
        proc.wait()
        self._untrack(proc)
        return found

    def _find_files(
        self,
        root: str,
        min_bytes: int,
        max_bytes: int,
        skip_heavy: bool,
        exts: frozenset[str] | None,
    ) -> list[dict]:
        prune = ["-name", ".Trash", "-o", "-name", ".Trashes", "-o", "-name", ".fseventsd",
                 "-o", "-name", "*.photoslibrary", "-o", "-name", "*.musiclibrary", "-o", "-name", "*.app"]
        if skip_heavy:
            for name in sorted(HEAVY_DIR_NAMES):
                prune.extend(["-o", "-name", name])
        for prefix in PROTECTED_PREFIXES:
            real_prefix = _realpath(prefix) if os.path.exists(prefix) else prefix
            if _is_under(real_prefix, _realpath(root)):
                prune.extend(["-o", "-path", real_prefix])
        command = [
            "find", root, "-xdev",
            "(", *prune, ")", "-prune",
            "-o", "-type", "f",
            *find_name_clause(exts),
            *find_size_clause(min_bytes, max_bytes),
            "-print0",
        ]
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._track(proc)
        found: list[dict] = []
        assert proc.stdout is not None
        buffer = b""
        while True:
            if self._cancelled():
                self._stop_proc(proc)
                break
            chunk = proc.stdout.read(1024 * 256)
            if not chunk:
                break
            buffer += chunk
            parts = buffer.split(b"\0")
            buffer = parts.pop() if parts else b""
            for raw in parts:
                if not raw:
                    continue
                path = os.fsdecode(raw)
                item = self._file_item(path, min_bytes, max_bytes, exts)
                if item is not None:
                    found.append(item)
                    if len(found) >= MAX_FILES * 3:
                        found.sort(key=lambda one: one["size"], reverse=True)
                        del found[MAX_FILES:]
                        self._hit_cap = True
                    if len(found) == 1 or len(found) % 10 == 0:
                        self._set_message(
                            f"已发现 {len(found)} 个文件，最新是 {display_path(path)}（{fmt_bytes(item['size'])}）",
                            progress=True,
                        )
        proc.wait()
        self._untrack(proc)
        return found

    def _file_item(
        self,
        path: str,
        min_bytes: int,
        max_bytes: int,
        exts: frozenset[str] | None,
    ) -> dict | None:
        if _protected(path):
            return None
        if not matches_scan_type(os.path.basename(path), exts):
            return None
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or not within_size(st.st_size, min_bytes, max_bytes):
            return None
        return self._item(path, st.st_size, st.st_mtime, "file")

    def _item(self, path: str, size: int, mtime: float, kind: str) -> dict:
        return {
            "path": path,
            "size": int(size),
            "mtime": int(mtime),
            "used": None,
            "kind": kind,
            "hint": hint_for(path, kind),
            "category": category_for(path, kind),
            "ext": file_ext(os.path.basename(path)) if kind == "file" else "",
        }

    def _fill_last_used(self, items: list[dict]) -> None:
        targets = [item["path"] for item in items if item["kind"] == "file"][:USED_LOOKUP_LIMIT]
        if not targets:
            return
        self._set_message(f"扫描完成。正在读取 {len(targets)} 个大文件的上次使用时间…")
        found: dict[str, int | None] = {}
        step = 40
        for start in range(0, len(targets), step):
            if self._cancelled():
                break
            batch = targets[start:start + step]
            found.update(last_used_batch(batch))
            done = min(start + step, len(targets))
            self._set_message(f"正在读取上次使用时间（{done}/{len(targets)}）…", progress=True)
        with self.lock:
            for item in self.items:
                if item["path"] in found:
                    item["used"] = found[item["path"]]
            if not self.cancel:
                self._append_log_locked(f"扫描完成，共 {len(self.items)} 项。勾选后可以删除。")

    def can_delete(self, path: str) -> str | None:
        reason = delete_block_reason(path)
        if reason:
            return reason
        with self.lock:
            if path not in self.allowed:
                return "这项不在本次扫描结果里，请重新扫描后再删"
            running = self.running
        if running:
            return "扫描还没结束"
        if not os.path.lexists(path):
            return "文件已经不在了"
        if os.path.islink(path):
            return "这是符号链接，已跳过"
        return None

    def delete_paths(self, paths: list[str]) -> dict:
        failed: list[dict] = []
        ordered: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            ordered.append(path)
        ordered.sort(key=lambda item: (len(Path(item).parts), item))
        moved: list[str] = []
        removed_dirs: list[str] = []
        for path in ordered:
            reason = self.can_delete(path)
            if reason:
                if reason == "文件已经不在了" and any(_is_under(path, root) for root in removed_dirs):
                    continue
                failed.append({"path": path, "error": reason})
                continue
            try:
                is_dir = os.path.isdir(path) and not os.path.islink(path)
                size = os.lstat(path).st_size if os.path.isfile(path) else 0
                move_to_trash(path)
            except Exception as exc:  # noqa: BLE001  把失败原因返回给页面
                failed.append({"path": path, "error": str(exc)})
                continue
            moved.append(path)
            if is_dir:
                removed_dirs.append(os.path.normpath(path))
            self._log(path, size)
        removed = set(moved)
        if moved or failed:
            with self.lock:
                sizes = {item["path"]: int(item.get("size") or 0) for item in self.items}
                extra: list[str] = []
                for item in self.items:
                    item_path = item["path"]
                    if item_path in removed:
                        continue
                    if any(_is_under(item_path, root) for root in removed_dirs):
                        extra.append(item_path)
                still_failed: list[dict] = []
                for row in failed:
                    item_path = row["path"]
                    if any(_is_under(item_path, root) for root in removed_dirs):
                        if item_path not in removed:
                            extra.append(item_path)
                        continue
                    still_failed.append(row)
                failed = still_failed
                removed.update(extra)
                for item in self.items:
                    item_path = item["path"]
                    if item_path in removed or item.get("kind") != "dir":
                        continue
                    shrink = 0
                    for path in removed:
                        if path == item_path or not _is_under(path, item_path):
                            continue
                        if any(
                            root != path
                            and root != item_path
                            and _is_under(path, root)
                            and _is_under(root, item_path)
                            for root in removed_dirs
                        ):
                            continue
                        shrink += sizes.get(path, 0)
                    if shrink:
                        item["size"] = max(0, int(item.get("size") or 0) - shrink)
                self.items = [item for item in self.items if item["path"] not in removed]
                self.allowed -= removed
                if failed:
                    self.message = (
                        f"已移到废纸篓 {len(moved)} 项，失败 {len(failed)} 项：{failed[0]['error']}"
                    )
                else:
                    self.message = f"已移到废纸篓 {len(moved)} 项。列表里还有 {len(self.items)} 项。"
        disks: list[dict] = []
        try:
            disks = list_disks()
        except (OSError, subprocess.SubprocessError):
            disks = []
        return {"ok": list(removed), "trashed": moved, "failed": failed, "disks": disks}

    def _log(self, path: str, size: int) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp}\t{size}\t{path}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)


SCAN = Scanner()


def unique_trash_dest(name: str) -> Path:
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    dest = trash / name
    if not dest.exists():
        return dest
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = Path(name).stem
    suffix = "".join(Path(name).suffixes)
    for index in range(1, 1000):
        candidate = trash / f"{stem} {stamp}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("废纸篓里同名文件太多")


def move_to_trash(path: str) -> None:
    dest = unique_trash_dest(os.path.basename(path.rstrip(os.sep)) or "未命名")
    try:
        os.rename(path, dest)
        return
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
            raise
    trash_with_file_manager(path)


_HELPER: Path | None = None


def trash_with_file_manager(path: str) -> None:
    helper = swift_trash_helper()
    done = subprocess.run([str(helper), path], check=False, capture_output=True, text=True)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "无法移到废纸篓").strip()
        raise RuntimeError(detail)


def swift_trash_helper() -> Path:
    global _HELPER
    if _HELPER and _HELPER.is_file():
        return _HELPER
    cache = Path.home() / ".cache" / "disk-cleanup"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / "trash-helper"
    source = cache / "trash-helper.swift"
    program = (
        "import Foundation\n"
        "for arg in CommandLine.arguments.dropFirst() {\n"
        "    let url = URL(fileURLWithPath: arg)\n"
        "    try FileManager.default.trashItem(at: url, resultingItemURL: nil)\n"
        "}\n"
    )
    if not binary.is_file():
        source.write_text(program, encoding="utf-8")
        done = subprocess.run(
            ["swiftc", "-O", "-o", str(binary), str(source)],
            check=False,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            detail = (done.stderr or "没有找到 swiftc").strip()
            raise RuntimeError(detail)
    _HELPER = binary
    return binary


def _du_total(path: str) -> int:
    done = subprocess.run(["du", "-sk", path], check=False, capture_output=True, text=True)
    raw = done.stdout.split("\t", 1)[0].strip() if done.stdout else "0"
    try:
        return int(raw) * 1024
    except ValueError:
        return 0


def _du_children(path: str) -> list[tuple[str, int]]:
    done = subprocess.run(["du", "-k", "-d", "1", path], check=False, capture_output=True, text=True)
    found: list[tuple[str, int]] = []
    for line in done.stdout.splitlines():
        if "\t" not in line:
            continue
        raw, child = line.split("\t", 1)
        if os.path.normpath(child) == os.path.normpath(path):
            continue
        try:
            size = int(raw) * 1024
        except ValueError:
            continue
        found.append((child, size))
    return found


def _allocated(path: str) -> int:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return 0
    blocks = getattr(st, "st_blocks", 0) or 0
    if blocks:
        return int(blocks) * 512
    return int(st.st_size)


def _usage_entry(path: str, size: int, kind: str) -> dict:
    mtime = 0
    try:
        mtime = int(os.lstat(path).st_mtime)
    except OSError:
        mtime = 0
    return {
        "name": os.path.basename(path.rstrip(os.sep)) or path,
        "path": path,
        "size": int(size),
        "mtime": mtime,
        "used": None,
        "kind": kind,
        "hint": hint_for(path, kind),
        "category": category_for(path, kind),
        "ext": file_ext(os.path.basename(path)) if kind == "file" else "",
    }


def _pack_children(children: list[dict]) -> list[dict]:
    ranked = sorted((child for child in children if child["size"] > 0), key=lambda child: child["size"], reverse=True)
    total = sum(child["size"] for child in ranked) or 1
    kept: list[dict] = []
    rest: list[dict] = []
    for index, child in enumerate(ranked):
        if index < 24 or child["size"] >= total * 0.01:
            kept.append(child)
        else:
            rest.append(child)
    if rest:
        members = [
            {key: member[key] for key in _ENTRY_KEYS if key in member}
            for member in rest[:300]
        ]
        kept.append({
            "name": "其他",
            "path": "",
            "size": sum(member["size"] for member in rest),
            "kind": "group",
            "hint": f"还有 {len(rest)} 个较小项目",
            "category": "other",
            "ext": "",
            "members": members,
        })
    return kept


def level_usage(path: str) -> dict:
    expanded = os.path.expanduser(path.strip())
    if not root_allowed(expanded):
        raise ValueError(f"这个路径不能查看：{expanded}")
    real = _realpath(expanded)
    SCAN._note_usage(f"正在计算这一层的占比：{display_path(real)}", progress=True)
    children: list[dict] = []
    for child, size in _du_children(real):
        if _protected(child) or os.path.islink(child) or not os.path.isdir(child):
            continue
        children.append(_usage_entry(child, size, "dir"))
    try:
        with os.scandir(real) as scan:
            for entry in scan:
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    size = _allocated(entry.path)
                except OSError:
                    continue
                if size <= 0:
                    continue
                children.append(_usage_entry(entry.path, size, "file"))
    except OSError as exc:
        raise ValueError(f"没有权限读取：{real}") from exc
    packed = _pack_children(children)
    total = sum(child["size"] for child in packed)
    name = os.path.basename(real.rstrip(os.sep)) or real
    SCAN._note_usage(f"这一层占比算完：{name}，{len(packed)} 块，合计 {fmt_bytes(total)}")
    return {
        "name": name,
        "path": real,
        "size": total,
        "children": packed,
    }


def build_usage(paths: list[str]) -> dict:
    if len(paths) == 1:
        return level_usage(paths[0])
    SCAN._note_usage(f"正在汇总 {len(paths)} 个位置的占用")
    children = []
    for path in paths:
        expanded = os.path.expanduser(path.strip())
        if not root_allowed(expanded):
            raise ValueError(f"这个路径不能查看：{expanded}")
        real = _realpath(expanded)
        SCAN._note_usage(f"正在计算占用：{display_path(real)}", progress=True)
        children.append(_usage_entry(real, _du_total(real), "dir"))
    packed = _pack_children(children)
    total = sum(child["size"] for child in packed)
    SCAN._note_usage(f"所选位置占比已汇总，{len(packed)} 块，合计 {fmt_bytes(total)}")
    return {
        "name": "所选位置",
        "path": "",
        "size": total,
        "children": packed,
    }


def allow_usage(node: dict) -> None:
    paths: list[str] = []

    def walk(items: list[dict]) -> None:
        for item in items:
            if item.get("path") and item.get("kind") != "group":
                paths.append(item["path"])
            members = item.get("members") or []
            if isinstance(members, list):
                walk(members)

    walk(node.get("children") or [])
    with SCAN.lock:
        SCAN.allowed.update(paths)


def pick_folders() -> list[str]:
    """打开系统的文件夹多选窗口。用户取消时返回空列表。"""
    script = """
try
  set picked to choose folder with prompt "选择要扫描的文件夹，可以多选" default location (path to home folder) with multiple selections allowed
  set output to ""
  repeat with oneFolder in picked
    set output to output & POSIX path of oneFolder & linefeed
  end repeat
  return output
on error errMsg number errNum
  if errNum is -128 then
    return ""
  end if
  error errMsg number errNum
end try
"""
    try:
        done = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("选择文件夹超时") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "没能打开选择窗口").strip()
        raise RuntimeError(detail)
    paths: list[str] = []
    for line in done.stdout.splitlines():
        path = line.strip()
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        if path:
            paths.append(path)
    return paths


def reveal(path: str) -> None:
    if not os.path.lexists(path):
        raise FileNotFoundError("文件已经不在了")
    subprocess.run(["open", "-R", path], check=False)


def open_item(path: str) -> None:
    if not os.path.lexists(path):
        raise FileNotFoundError("文件已经不在了")
    done = subprocess.run(["open", path], check=False, capture_output=True, text=True)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "没有打开").strip()
        raise RuntimeError(detail)


_PREVIEW_SLOTS = threading.Semaphore(2)
_PREVIEW_LOCKS: dict[str, threading.Lock] = {}
_PREVIEW_GUARD = threading.Lock()
_THUMB_DIR = Path.home() / ".cache" / "disk-cleanup" / "thumbs"
_RASTER_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp",
    ".avif", ".jfif",
}
_IMAGE_EXT = _RASTER_EXT | {".psd", ".raw", ".svg"}
_VIDEO_EXT = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".ts",
    ".mpeg", ".mpg", ".m2ts", ".3gp",
}
_MEDIA_TYPE = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def _query_path(url: str) -> str:
    values = parse_qs(urlparse(url).query)
    found = values.get("path") or [""]
    return found[0]


def _preview_lock(key: str) -> threading.Lock:
    with _PREVIEW_GUARD:
        lock = _PREVIEW_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PREVIEW_LOCKS[key] = lock
        return lock


def _checked_preview_path(raw: str) -> tuple[str, str]:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("没有路径")
    if os.path.islink(raw):
        raise ValueError("不能预览链接")
    if not os.path.isfile(raw):
        raise FileNotFoundError("文件已经不在了")
    real = _realpath(raw)
    if not os.path.isfile(real):
        raise FileNotFoundError("文件已经不在了")
    parent = os.path.dirname(real)
    if not parent or not root_allowed(parent) or _protected(real):
        raise ValueError("这个文件不能预览")
    with SCAN.lock:
        known = raw in SCAN.allowed or real in SCAN.allowed
    if not known:
        raise ValueError("这项不在本次扫描结果里，请重新扫描后再看")
    ext = "." + file_ext(os.path.basename(real))
    if ext not in _IMAGE_EXT and ext not in _VIDEO_EXT:
        raise ValueError("这个文件没有预览")
    return real, ext


def _run_quiet(command: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, check=False, capture_output=True, timeout=timeout)


def _ql_thumb(real: str, dim: str, jpg: Path, work: Path) -> bool:
    done = _run_quiet(["qlmanage", "-t", "-s", dim, "-o", str(work), real], 30)
    pngs = [path for path in work.iterdir() if path.suffix.lower() == ".png"]
    if done.returncode != 0 and not pngs:
        return False
    if not pngs:
        return False
    made = _run_quiet(
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "72", str(pngs[0]), "--out", str(jpg)],
        20,
    )
    return made.returncode == 0 and jpg.is_file() and jpg.stat().st_size > 0


def _ffmpeg_thumb(real: str, dim: str, jpg: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    for stamp in ("1", "0"):
        done = _run_quiet(
            [ffmpeg, "-y", "-ss", stamp, "-i", real, "-frames:v", "1", "-vf", f"scale={dim}:-2", str(jpg)],
            25,
        )
        if done.returncode == 0 and jpg.is_file() and jpg.stat().st_size > 0:
            return True
    return False


def _render_preview(real: str, ext: str, large: bool) -> Path | None:
    st = os.stat(real)
    label = "l" if large else "s"
    digest = hashlib.sha256(f"{real}|{st.st_mtime_ns}|{st.st_size}|{label}".encode()).hexdigest()[:32]
    dest = _THUMB_DIR / f"{digest}.jpg"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    _THUMB_DIR.mkdir(parents=True, exist_ok=True)
    with _preview_lock(digest):
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        with _PREVIEW_SLOTS:
            work = _THUMB_DIR / "work" / digest
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            try:
                jpg = work / "out.jpg"
                dim = "1600" if large else "320"
                ok = False
                if ext in _RASTER_EXT:
                    done = _run_quiet(
                        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "72", "-Z", dim, real, "--out", str(jpg)],
                        40,
                    )
                    ok = done.returncode == 0 and jpg.is_file() and jpg.stat().st_size > 0
                if not ok and ext in _VIDEO_EXT:
                    ok = _ffmpeg_thumb(real, dim, jpg)
                if not ok:
                    ok = _ql_thumb(real, dim, jpg, work)
                if not ok:
                    return None
                os.replace(jpg, dest)
                return dest
            finally:
                shutil.rmtree(work, ignore_errors=True)


def preview_image(raw: str, large: bool) -> tuple[Path, str]:
    real, ext = _checked_preview_path(raw)
    direct = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    if large and ext in direct and os.path.getsize(real) <= 20 * 1024 * 1024:
        return Path(real), direct[ext]
    made = _render_preview(real, ext, large)
    if made is None:
        raise FileNotFoundError("这个文件做不了预览")
    return made, "image/jpeg"


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length > 2_000_000:
        raise ValueError("请求太大")
    raw = handler.rfile.read(length) if length else b"{}"
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("请求格式不对")
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "DiskCleanup/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _send_cached(self, file_path: Path, content_type: str) -> None:
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=600")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_range(self, real: str, content_type: str) -> None:
        size = os.path.getsize(real)
        start, end = 0, size - 1
        status = 200
        header = self.headers.get("Range")
        if header:
            if not header.startswith("bytes="):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            spec = header.split("=", 1)[1].split(",", 1)[0].strip()
            try:
                left, right = spec.split("-", 1)
                if left == "":
                    take = int(right)
                    if take <= 0:
                        raise ValueError
                    start = max(0, size - take)
                else:
                    start = int(left)
                    end = int(right) if right else size - 1
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if start < 0 or start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "private, max-age=600")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(real, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _preview_image(self, large: bool) -> None:
        try:
            file_path, content_type = preview_image(_query_path(self.path), large)
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})
            return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except subprocess.TimeoutExpired:
            self._json(504, {"error": "预览生成超时"})
            return
        self._send_cached(file_path, content_type)

    def _media(self) -> None:
        try:
            real, ext = _checked_preview_path(_query_path(self.path))
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})
            return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        mime = _MEDIA_TYPE.get(ext)
        if not mime:
            self._json(415, {"error": "这个视频只能用默认应用打开"})
            return
        self._send_range(real, mime)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/thumb":
            self._preview_image(False)
            return
        if path == "/api/preview":
            self._preview_image(True)
            return
        if path == "/api/media":
            self._media()
            return
        if path == "/api/meta":
            disks = list_disks()
            self._json(200, {
                "presets": presets(),
                "disks": disks,
                "disk": disks[0],
                "home": str(Path.home()),
            })
            return
        if path == "/api/status":
            self._json(200, SCAN.snapshot())
            return
        self._json(404, {"error": "没有这个地址"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            data = read_json(self)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        if path == "/api/scan":
            self._scan(data)
            return
        if path == "/api/cancel":
            SCAN.cancel_scan()
            self._json(200, {"ok": True})
            return
        if path == "/api/delete":
            self._delete(data)
            return
        if path == "/api/reveal":
            self._reveal(data)
            return
        if path == "/api/open":
            self._open(data)
            return
        if path == "/api/pick":
            self._pick()
            return
        if path == "/api/usage":
            self._usage(data)
            return
        self._json(404, {"error": "没有这个地址"})

    def _usage(self, data: dict) -> None:
        path = data.get("path")
        paths = data.get("paths")
        if isinstance(path, str) and path.strip():
            chosen = [path]
        elif isinstance(paths, list) and paths and all(isinstance(item, str) for item in paths):
            chosen = paths
        else:
            self._json(400, {"error": "没有要查看的文件夹"})
            return
        try:
            payload = build_usage(chosen)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        allow_usage(payload)
        self._json(200, payload)

    def _pick(self) -> None:
        try:
            paths = pick_folders()
        except RuntimeError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"paths": paths, "cancelled": not paths})

    def _scan(self, data: dict) -> None:
        roots = data.get("roots")
        if not isinstance(roots, list) or not roots or not all(isinstance(item, str) for item in roots):
            self._json(400, {"error": "请提供要扫描的文件夹"})
            return
        min_mb, min_error = parse_mb(data.get("min_mb", 100), default=100, label="大小下限")
        if min_error:
            self._json(400, {"error": min_error})
            return
        max_mb, max_error = parse_mb(data.get("max_mb", 0), default=0, label="大小上限")
        if max_error:
            self._json(400, {"error": max_error})
            return
        assert min_mb is not None and max_mb is not None
        if max_mb > 0 and min_mb > max_mb:
            self._json(400, {"error": "大小上限要大于或等于下限"})
            return
        type_ids, type_error = parse_scan_types(data.get("types", []))
        if type_error:
            self._json(400, {"error": type_error})
            return
        assert type_ids is not None
        cleaned: list[str] = []
        for root in roots:
            expanded = os.path.expanduser(root.strip())
            if not root_allowed(expanded):
                self._json(400, {"error": f"这个路径不能扫描：{expanded}"})
                return
            cleaned.append(expanded)
        error = SCAN.start(
            dedupe_roots(cleaned),
            min_mb,
            max_mb,
            bool(data.get("include_files", True)),
            bool(data.get("skip_heavy", True)),
            type_ids,
        )
        if error:
            self._json(409, {"error": error})
            return
        self._json(200, {"ok": True})

    def _delete(self, data: dict) -> None:
        paths = data.get("paths")
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
            self._json(400, {"error": "没有收到要删除的路径"})
            return
        if not paths:
            self._json(400, {"error": "先勾选要删除的项目"})
            return
        if len(paths) > 500:
            self._json(400, {"error": "一次最多删除 500 项"})
            return
        self._json(200, SCAN.delete_paths(paths))

    def _reveal(self, data: dict) -> None:
        path = data.get("path")
        if not isinstance(path, str) or not path:
            self._json(400, {"error": "没有路径"})
            return
        try:
            reveal(path)
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"ok": True})

    def _open(self, data: dict) -> None:
        path = data.get("path")
        if not isinstance(path, str) or not path:
            self._json(400, {"error": "没有路径"})
            return
        try:
            open_item(path)
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描本机大文件，并在确认后移到废纸篓")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}/"
    print(f"大文件清理已打开：{url}")
    print("删除会移到废纸篓。关闭这个窗口即停止服务。")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
