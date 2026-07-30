#!/usr/bin/env python3
"""
unpacker.py - 递归解包引擎
支持: zip / tar / gz / bz2 / xz / 7z / rar / binwalk(二进制签名)
"""

import os
import subprocess
import logging
import shutil
from pathlib import Path

log = logging.getLogger("unpacker")

# 归档 magic 前缀映射
ARCHIVE_SIGNATURES = {
    b"PK\x03\x04": "zip",
    b"\x1f\x8b":    "gzip",
    b"BZh":         "bzip2",
    b"\xfd7zXZ":    "xz",
    b"7z\xbc\xaf\x27\x1c": "7z",
    b"Rar!\x1a\x07": "rar",
    b"ustar":       "tar",
    b"\x1f\x9d":    "compress",
}

# 二进制容器 (ELF/PE/Mach-O) - 用 binwalk 提取内嵌内容
BIN_CONTAINERS = {"elf", "pe", "mach-o"}


def detect_type_by_magic(filepath: str) -> str:
    """读取文件头判断类型"""
    with open(filepath, "rb") as f:
        header = f.read(16)
    for sig, name in ARCHIVE_SIGNATURES.items():
        if header.startswith(sig):
            return name
    # 二进制容器（用 binwalk 进一步提取内嵌内容）
    if header.startswith(b"\x7fELF"):
        return "elf"
    if header.startswith(b"MZ"):
        return "pe"
    if header.startswith(b"\xcf\xfa\xed\xfe") or header.startswith(b"\xfe\xed\xfa\xcf"):
        return "mach-o"
    return "unknown"


def is_container(filepath: str, ftype: str) -> bool:
    """判断是否需要进一步解包"""
    container_types = {"zip", "tar", "gzip", "bzip2", "xz", "7z", "rar", "compress"}
    return ftype in container_types or ftype in BIN_CONTAINERS


def unpack(filepath: str, out_dir: Path) -> list:
    """
    解包文件，返回解出的子文件列表
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted = []
    ftype = detect_type_by_magic(filepath)

    try:
        if ftype == "zip":
            extracted = _unzip(filepath, out_dir)
        elif ftype in ("gzip", "compress"):
            extracted = _ungzip(filepath, out_dir)
        elif ftype == "bzip2":
            extracted = _unbzip2(filepath, out_dir)
        elif ftype == "xz":
            extracted = _unxz(filepath, out_dir)
        elif ftype == "7z":
            extracted = _un7z(filepath, out_dir)
        elif ftype == "rar":
            extracted = _unrar(filepath, out_dir)
        elif ftype in BIN_CONTAINERS:
            extracted = _binwalk(filepath, out_dir)
        else:
            # 尝试 tar
            if filepath.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
                extracted = _untar(filepath, out_dir)
    except Exception as e:
        log.error(f"Unpack failed for {filepath}: {e}")

    log.info(f"Unpacked {filepath} -> {len(extracted)} files")
    return extracted


def _unzip(filepath, out_dir):
    import zipfile
    files = []
    with zipfile.ZipFile(filepath) as z:
        for name in z.namelist():
            safe = out_dir / Path(name).name
            with z.open(name) as src, open(safe, "wb") as dst:
                shutil.copyfileobj(src, dst)
            files.append(str(safe))
    return files


def _untar(filepath, out_dir):
    import tarfile
    files = []
    with tarfile.open(filepath) as t:
        for m in t.getmembers():
            if m.isfile():
                t.extract(m, path=str(out_dir))
                files.append(str(out_dir / m.name))
    return files


def _ungzip(filepath, out_dir):
    import gzip
    out = out_dir / Path(filepath).stem
    with gzip.open(filepath, "rb") as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return [str(out)]


def _unbzip2(filepath, out_dir):
    import bz2
    out = out_dir / Path(filepath).stem
    with bz2.open(filepath, "rb") as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return [str(out)]


def _unxz(filepath, out_dir):
    import lzma
    out = out_dir / Path(filepath).stem
    with lzma.open(filepath, "rb") as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return [str(out)]


def _un7z(filepath, out_dir):
    subprocess.run(["7z", "x", filepath, f"-o{out_dir}", "-y"], check=True, capture_output=True)
    return [str(p) for p in out_dir.iterdir() if p.is_file()]


def _unrar(filepath, out_dir):
    subprocess.run(["unrar", "x", filepath, str(out_dir), "-y"], check=True, capture_output=True)
    return [str(p) for p in out_dir.iterdir() if p.is_file()]


def _binwalk(filepath, out_dir):
    """用 binwalk 提取二进制内嵌内容"""
    subprocess.run(
        ["binwalk", "-e", "--dd=.*", f"--directory={out_dir}", filepath],
        check=False, capture_output=True, timeout=120
    )
    # binwalk 提取到子目录
    extracted_dir = out_dir / Path(filepath).name.split(".")[0]
    files = []
    for p in out_dir.rglob("*"):
        if p.is_file() and p.suffix not in ("",):
            files.append(str(p))
    return files
