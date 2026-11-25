from __future__ import annotations

import hashlib
import io
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import UnityPy
from UnityPy.enums import TextureFormat
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from PIL import Image, ImageOps
from UnityPy.helpers import CompressionHelper

try:
    import lz4.block
except Exception:  # LZ4 replacement remains unavailable without this extra
    lz4 = None

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("JAYN_DATA_DIR", ROOT / "data"))
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
ASTCENC = Path(os.environ.get("ASTCENC_BIN", ROOT / "tools" / "astcenc"))
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))
MAX_PIXELS = int(os.environ.get("MAX_TEXTURE_PIXELS", "16777216"))

app = FastAPI(title="JAYN GMG Texture Replacer", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"]
)

JOBS: dict[str, dict[str, Any]] = {}


class ProcessError(Exception):
    """Expected, user-facing file processing failure."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_name(value: str, fallback: str = "Texture") -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", (value or "").strip())
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:72] or fallback


def read_cstring(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\0", pos)
    if end < 0:
        raise ProcessError("Header Unity tidak lengkap.")
    return data[pos:end].decode("utf-8", "replace"), end + 1


def lzma_filter_from_props(props: bytes) -> dict[str, Any]:
    if len(props) != 5:
        raise ProcessError("Properti kompresi LZMA tidak valid.")
    first = props[0]
    if first >= 9 * 5 * 5:
        raise ProcessError("Properti LZMA tidak didukung.")
    return {
        "id": lzma.FILTER_LZMA1,
        "dict_size": int.from_bytes(props[1:5], "little"),
        "lc": first % 9,
        "lp": (first // 9) % 5,
        "pb": first // 45,
    }


def lzma_decode_unity(blob: bytes, expected_size: int | None = None) -> bytes:
    if len(blob) < 6:
        raise ProcessError("Blok LZMA terlalu pendek.")
    props = blob[:5]
    try:
        data = lzma.decompress(blob[5:], format=lzma.FORMAT_RAW, filters=[lzma_filter_from_props(props)])
    except Exception as exc:
        raise ProcessError("Blok LZMA gagal dibuka.") from exc
    if expected_size is not None and len(data) != expected_size:
        raise ProcessError("Ukuran data setelah dekompresi tidak sesuai.")
    return data


def lzma_encode_unity(raw: bytes, filters: dict[str, Any]) -> bytes:
    comp = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=[filters])
    encoded = comp.compress(raw) + comp.flush()
    # Unity LZMA block header: properties byte + dictionary size.
    first = filters["lc"] + 9 * (filters["lp"] + 5 * filters["pb"])
    return bytes([first]) + int(filters["dict_size"]).to_bytes(4, "little") + encoded


def decode_block(blob: bytes, uncompressed_size: int, block_flags: int) -> bytes:
    method = block_flags & 0x3F
    if method == 0:
        result = blob
    elif method == 1:
        result = lzma_decode_unity(blob, uncompressed_size)
    elif method in (2, 3):
        if lz4 is None:
            raise ProcessError("File ini memakai LZ4. Paket LZ4 backend belum tersedia.")
        try:
            result = lz4.block.decompress(blob, uncompressed_size=uncompressed_size)
        except Exception as exc:
            raise ProcessError("Blok LZ4 gagal dibuka.") from exc
    else:
        raise ProcessError(f"Jenis kompresi blok {method} belum didukung untuk penggantian.")
    if len(result) != uncompressed_size:
        raise ProcessError("Ukuran blok hasil dekompresi tidak cocok.")
    return result


@dataclass
class UnityFSData:
    source: bytes
    signature: str
    version: int
    prefix: bytes
    flags: int
    info_compressed_size: int
    info_uncompressed_size: int
    info_raw: bytes
    info_hash: bytes
    old_blocks: list[tuple[int, int, int]]
    directory_suffix: bytes
    entries: list[tuple[int, int, int, str]]
    raw_data: bytes
    header_pos: int
    data_offset: int
    end_of_blocks: int


def parse_unityfs(data: bytes) -> UnityFSData:
    pos = 0
    signature, pos = read_cstring(data, pos)
    if signature != "UnityFS":
        raise ProcessError("File ini bukan UnityFS yang didukung.")
    if pos + 4 > len(data):
        raise ProcessError("Header UnityFS tidak lengkap.")
    version = struct.unpack_from(">I", data, pos)[0]
    pos += 4
    _player, pos = read_cstring(data, pos)
    _engine, pos = read_cstring(data, pos)
    header_pos = pos
    if pos + 20 > len(data):
        raise ProcessError("Header UnityFS tidak lengkap.")
    declared_size, info_csize, info_usize, flags = struct.unpack_from(">QIII", data, pos)
    pos += 20
    if declared_size != len(data):
        raise ProcessError("Ukuran yang tercatat pada header UnityFS tidak cocok dengan file.")
    # This implementation deliberately handles the tested UnityFS v6, info-at-front layout.
    # Other variants are scanned with UnityPy but fail closed for exact-byte replacement.
    if version != 6 or (flags & 0x80):
        raise ProcessError("Penggantian saat ini hanya mendukung UnityFS versi 6 dengan tabel blok di awal file.")
    info_start = pos
    if info_csize <= 0 or info_start + info_csize > len(data):
        raise ProcessError("Tabel blok UnityFS berada di luar file.")
    info_blob = data[info_start:info_start + info_csize]
    info_method = flags & 0x3F
    if info_method == 1:
        info_raw = lzma_decode_unity(info_blob, info_usize)
    elif info_method == 0:
        info_raw = info_blob
    elif info_method in (2, 3):
        if lz4 is None:
            raise ProcessError("Tabel blok LZ4 tidak dapat dibuka karena dependensi LZ4 belum tersedia.")
        info_raw = lz4.block.decompress(info_blob, uncompressed_size=info_usize)
    else:
        raise ProcessError("Kompresi tabel blok belum didukung.")
    if len(info_raw) != info_usize or len(info_raw) < 20:
        raise ProcessError("Tabel blok UnityFS tidak valid.")
    info_hash = info_raw[:16]
    count = struct.unpack_from(">I", info_raw, 16)[0]
    if count < 1 or count > 100000:
        raise ProcessError("Jumlah blok UnityFS tidak masuk akal.")
    q = 20
    blocks: list[tuple[int, int, int]] = []
    for _ in range(count):
        if q + 10 > len(info_raw):
            raise ProcessError("Daftar blok UnityFS terpotong.")
        us, cs, fl = struct.unpack_from(">IIH", info_raw, q)
        q += 10
        blocks.append((us, cs, fl))
    dir_start = q
    if q + 4 > len(info_raw):
        raise ProcessError("Daftar file UnityFS tidak ditemukan.")
    node_count = struct.unpack_from(">I", info_raw, q)[0]
    q += 4
    if node_count < 1 or node_count > 100000:
        raise ProcessError("Jumlah file internal UnityFS tidak masuk akal.")
    entries: list[tuple[int, int, int, str]] = []
    for _ in range(node_count):
        if q + 20 > len(info_raw):
            raise ProcessError("Direktori UnityFS terpotong.")
        offset, size, node_flags = struct.unpack_from(">QQI", info_raw, q)
        q += 20
        name, q = read_cstring(info_raw, q)
        if offset + size > sum(x[0] for x in blocks):
            raise ProcessError("Entri internal UnityFS melewati batas data.")
        entries.append((offset, size, node_flags, name))
    if q != len(info_raw):
        raise ProcessError("Ada data direktori UnityFS yang tidak dikenali; proses dibatalkan.")
    directory_suffix = info_raw[dir_start:]

    data_pos = info_start + info_csize
    # UnityFS v6 test layout has no extra data padding flags. Reject unfamiliar flags.
    if flags & 0x200:
        # BlockInfoNeedPaddingAtStart is a defined UnityFS flag. Align as UnityPy does.
        data_pos += (16 - data_pos % 16) % 16
    raw_parts = []
    for uncompressed_size, compressed_size, block_flags in blocks:
        if compressed_size < 0 or data_pos + compressed_size > len(data):
            raise ProcessError("Data blok melewati batas file.")
        block = data[data_pos:data_pos + compressed_size]
        raw_parts.append(decode_block(block, uncompressed_size, block_flags))
        data_pos += compressed_size
    raw_data = b"".join(raw_parts)
    if len(raw_data) != sum(x[0] for x in blocks):
        raise ProcessError("Panjang data UnityFS setelah dekompresi tidak cocok.")
    return UnityFSData(
        source=data,
        signature=signature,
        version=version,
        prefix=data[:header_pos],
        flags=flags,
        info_compressed_size=info_csize,
        info_uncompressed_size=info_usize,
        info_raw=info_raw,
        info_hash=info_hash,
        old_blocks=blocks,
        directory_suffix=directory_suffix,
        entries=entries,
        raw_data=raw_data,
        header_pos=header_pos,
        data_offset=info_start + info_csize,
        end_of_blocks=data_pos,
    )


def bundle_header_output(parsed: UnityFSData, info_raw: bytes, blocks: list[tuple[int, int, int]], compressed_blocks: bytes, target_size: int) -> bytes:
    flags = parsed.flags
    info_method = flags & 0x3F
    if info_method != 1:
        raise ProcessError("Penyimpanan ulang tabel UnityFS saat ini hanya mendukung kompresi LZMA.")
    info_blob = CompressionHelper.compress_lzma(info_raw)
    header = parsed.prefix + struct.pack(">QIII", target_size, len(info_blob), len(info_raw), flags)
    padding = b""
    if flags & 0x200:
        current = len(header) + len(info_blob)
        padding = b"\0" * ((16 - current % 16) % 16)
    return header + info_blob + padding + compressed_blocks


def pack_unityfs_exact(parsed: UnityFSData, patched_raw: bytes, target_size: int) -> tuple[bytes, dict[str, Any]]:
    if (parsed.flags & 0x3F) != 1:
        raise ProcessError("Strict rebuild tabel saat ini hanya mendukung UnityFS dengan tabel LZMA.")
    if len(patched_raw) != len(parsed.raw_data):
        raise ProcessError("Panjang data internal berubah; strict byte dibatalkan.")
    last_error = "Tidak ditemukan susunan blok yang menghasilkan ukuran identik."
    # Use fixed, standards-compliant UnityFS blocks. No padding or truncation is ever added.
    chunk_sizes = [32768, 24576, 16384, 49152, 65536, 8192]
    filter_profiles = [
        {"id": lzma.FILTER_LZMA1, "dict_size": 1 << 16, "lc": 3, "lp": 0, "pb": 2, "mode": lzma.MODE_FAST, "mf": lzma.MF_HC3, "nice_len": 4, "depth": 1},
        {"id": lzma.FILTER_LZMA1, "dict_size": 1 << 20, "lc": 3, "lp": 0, "pb": 2, "mode": lzma.MODE_FAST, "mf": lzma.MF_HC3, "nice_len": 8, "depth": 2},
    ]
    base_flags = parsed.flags & ~0x3F
    for chunk_size in chunk_sizes:
        chunks = [patched_raw[i:i + chunk_size] for i in range(0, len(patched_raw), chunk_size)]
        for profile in filter_profiles:
            encoded_chunks = [lzma_encode_unity(chunk, profile) for chunk in chunks]
            gains = [max(0, len(chunk) - len(enc)) for chunk, enc in zip(chunks, encoded_chunks)]

            def make(selected: set[int]) -> tuple[bytes, list[tuple[int, int, int]], bytes]:
                data_blob = bytearray()
                records: list[tuple[int, int, int]] = []
                for idx, (chunk, encoded) in enumerate(zip(chunks, encoded_chunks)):
                    if idx in selected or len(encoded) >= len(chunk):
                        data_blob.extend(chunk)
                        records.append((len(chunk), len(chunk), base_flags))
                    else:
                        data_blob.extend(encoded)
                        records.append((len(chunk), len(encoded), parsed.flags))
                info_raw = parsed.info_hash + struct.pack(">I", len(records))
                info_raw += b"".join(struct.pack(">IIH", *r) for r in records)
                info_raw += parsed.directory_suffix
                result = bundle_header_output(parsed, info_raw, records, bytes(data_blob), target_size)
                return result, records, info_raw

            base, base_records, _ = make(set())
            need = target_size - len(base)
            if need < 0:
                last_error = f"Packer menghasilkan file {abs(need)} byte lebih besar dari sumber."
                continue
            if need == 0:
                if len(base) == target_size:
                    return base, {"chunk_size": chunk_size, "raw_blocks": 0, "lzma_blocks": len(chunks)}
                continue
            # Subset sum of candidate raw-block size gains. Restrict search near the required size.
            cap = need + 1024
            prev: dict[int, tuple[int, int] | None] = {0: None}
            for idx, gain in enumerate(gains):
                if gain <= 0:
                    continue
                for total in sorted(tuple(prev.keys()), reverse=True):
                    new_total = total + gain
                    if new_total <= cap and new_total not in prev:
                        prev[new_total] = (total, idx)
            candidate_sums = sorted((s for s in prev if max(0, need - 1024) <= s <= need + 1024), key=lambda s: abs(s - need))
            for amount in candidate_sums:
                selected: set[int] = set()
                cur = amount
                while cur:
                    step = prev.get(cur)
                    if step is None:
                        break
                    prev_total, idx = step
                    selected.add(idx)
                    cur = prev_total
                candidate, records, _info = make(selected)
                if len(candidate) == target_size:
                    return candidate, {
                        "chunk_size": chunk_size,
                        "raw_blocks": sum(1 for _, _, f in records if (f & 0x3F) == 0),
                        "lzma_blocks": sum(1 for _, _, f in records if (f & 0x3F) == 1),
                    }
    raise ProcessError(last_error + " Tidak ada padding atau pemotongan yang digunakan.")


def texture_format_name(value: Any) -> str:
    try:
        return TextureFormat(int(value)).name
    except Exception:
        return getattr(value, "name", str(value))


def texture_format_info(fmt_name: str) -> tuple[int, int, str] | None:
    if "ASTC_HDR" in fmt_name:
        return None
    match = re.search(r"ASTC_(RGB|RGBA)_(\d+)x(\d+)$", fmt_name)
    if not match:
        return None
    return int(match.group(2)), int(match.group(3)), match.group(1)


def astc_header(width: int, height: int, bx: int, by: int) -> bytes:
    return (
        struct.pack("<I", 0x5CA1AB13)
        + bytes((bx, by, 1))
        + int(width).to_bytes(3, "little")
        + int(height).to_bytes(3, "little")
        + (1).to_bytes(3, "little")
    )


def mip_dimensions(width: int, height: int, count: int) -> list[tuple[int, int]]:
    result = []
    w, h = width, height
    for _ in range(max(1, count)):
        result.append((w, h))
        w, h = max(1, w // 2), max(1, h // 2)
    return result


def astc_mip_size(width: int, height: int, bx: int, by: int) -> int:
    return ((width + bx - 1) // bx) * ((height + by - 1) // by) * 16


def run_astc(args: list[str]) -> None:
    if not ASTCENC.is_file():
        raise ProcessError("Mesin ASTC belum tersedia di server.")
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
    if proc.returncode != 0:
        message = (proc.stdout or "").strip().splitlines()[-1:] or ["Encoding tekstur gagal."]
        raise ProcessError(message[0][:220])


def stream_bytes_for_texture(obj: Any, texture: Any) -> tuple[str, int, int, bytes]:
    stream = texture.m_StreamData
    if not stream or not stream.path:
        raise ProcessError("Tekstur ini tidak memakai resource eksternal; penggantian otomatis belum tersedia.")
    resource_name = PurePosixPath(stream.path.replace("\\", "/")).name
    bundle = obj.assets_file.parent
    reader = bundle.files.get(resource_name)
    if reader is None:
        # UnityPy may key bundle resources by the full virtual path; try basename match.
        reader = next((v for k, v in bundle.files.items() if str(k).endswith(resource_name)), None)
    if reader is None or not hasattr(reader, "bytes"):
        raise ProcessError("Resource tekstur eksternal tidak ditemukan di dalam bundle.")
    data = reader.bytes
    start, size = int(stream.offset), int(stream.size)
    if start < 0 or size <= 0 or start + size > len(data):
        raise ProcessError("Offset resource tekstur berada di luar batas file.")
    return resource_name, start, size, data[start:start + size]


def scan_bundle(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        env = UnityPy.load(str(path))
        objects = list(env.objects)
    except Exception as exc:
        raise ProcessError("File Unity tidak dapat dibaca. Pastikan file lengkap dan tidak rusak.") from exc
    if not objects:
        raise ProcessError("Tidak ada objek Unity yang dapat dibaca.")
    # The current strict patcher supports only UnityFS v6 with a front-loaded LZMA block table.
    replacement_layout_ready = False
    try:
        layout = parse_unityfs(path.read_bytes())
        replacement_layout_ready = layout.version == 6 and (layout.flags & 0x3F) == 1
    except Exception:
        replacement_layout_ready = False
    textures: list[dict[str, Any]] = []
    for obj in objects:
        if getattr(obj.type, "name", "") != "Texture2D":
            continue
        try:
            tex = obj.read()
            fmt = texture_format_name(tex.m_TextureFormat)
            fmt_info = texture_format_info(fmt)
            stream = tex.m_StreamData
            supported = bool(fmt_info and stream and stream.path and replacement_layout_ready and int(tex.m_Width) * int(tex.m_Height) <= MAX_PIXELS)
            textures.append({
                "id": str(obj.path_id),
                "path_id": str(obj.path_id),
                "name": str(tex.m_Name or f"Texture_{obj.path_id}"),
                "width": int(tex.m_Width),
                "height": int(tex.m_Height),
                "format": fmt,
                "mipmaps": int(tex.m_MipCount or 1),
                "color_space": "sRGB" if int(getattr(tex, "m_ColorSpace", 0) or 0) == 1 else "Linear",
                "payload_size": int(stream.size if stream and stream.path else len(tex.image_data or b"")),
                "resource": PurePosixPath(stream.path.replace("\\", "/")).name if stream and stream.path else "Internal",
                "offset": int(stream.offset) if stream and stream.path else 0,
                "supported": supported,
                "support_reason": "ASTC eksternal siap diganti" if supported else ("Bundle belum masuk jalur strict patch" if not replacement_layout_ready else "Format atau penyimpanan tekstur belum didukung untuk penggantian"),
                "block": f"{fmt_info[0]}x{fmt_info[1]}" if fmt_info else None,
                "channels": fmt_info[2] if fmt_info else None,
            })
        except Exception:
            continue
    try:
        bundles = [v for v in env.files.values() if getattr(v, "signature", None) == "UnityFS"]
        bundle_version = bundles[0].version if bundles else None
        unity_version = bundles[0].version_engine if bundles else None
    except Exception:
        bundle_version = None
        unity_version = None
    summary = {
        "bytes": path.stat().st_size,
        "sha256": sha256(path.read_bytes()),
        "objects": len(objects),
        "textures": len(textures),
        "supported_textures": sum(1 for t in textures if t["supported"]),
        "bundle_version": bundle_version,
        "unity_version": unity_version,
        "strict_layout_ready": replacement_layout_ready,
    }
    return summary, textures


def get_texture(env: Any, path_id: str) -> tuple[Any, Any]:
    for obj in env.objects:
        if str(obj.path_id) == str(path_id) and getattr(obj.type, "name", "") == "Texture2D":
            return obj, obj.read()
    raise ProcessError("Tekstur yang dipilih tidak ditemukan lagi di file sumber.")


def extract_first_mip(obj: Any, tex: Any, out_png: Path) -> dict[str, Any]:
    fmt = texture_format_name(tex.m_TextureFormat)
    fmt_info = texture_format_info(fmt)
    if not fmt_info:
        raise ProcessError("Format tekstur ini belum didukung untuk preview.")
    bx, by, channel_kind = fmt_info
    _resname, _offset, _size, payload = stream_bytes_for_texture(obj, tex)
    dims = mip_dimensions(tex.m_Width, tex.m_Height, tex.m_MipCount or 1)
    expected = sum(astc_mip_size(w, h, bx, by) for w, h in dims)
    if expected != len(payload):
        raise ProcessError("Ukuran payload ASTC tidak sesuai dengan metadata mipmap.")
    first_size = astc_mip_size(tex.m_Width, tex.m_Height, bx, by)
    source = out_png.with_suffix(".astc")
    source.write_bytes(astc_header(tex.m_Width, tex.m_Height, bx, by) + payload[:first_size])
    decode_profile = "-ds" if int(getattr(tex, "m_ColorSpace", 0) or 0) == 1 else "-dl"
    run_astc([str(ASTCENC), decode_profile, str(source), str(out_png)])
    return {"mipmap_count": len(dims), "payload_bytes": len(payload), "first_mip_bytes": first_size}


def encode_png_to_original_astc(image_path: Path, tex: Any, fmt_info: tuple[int, int, str], work_dir: Path) -> bytes:
    bx, by, channels = fmt_info
    width, height = int(tex.m_Width), int(tex.m_Height)
    if width * height > MAX_PIXELS:
        raise ProcessError("Resolusi tekstur terlalu besar untuk batas pemrosesan server.")
    with Image.open(image_path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGBA" if channels == "RGBA" else "RGB")
        if im.size != (width, height):
            im = ImageOps.fit(im, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    dims = mip_dimensions(width, height, int(tex.m_MipCount or 1))
    generated: list[bytes] = []
    current = im
    for level, (mw, mh) in enumerate(dims):
        if current.size != (mw, mh):
            current = current.resize((mw, mh), Image.Resampling.LANCZOS)
        png = work_dir / f"mip_{level}.png"
        astc_file = work_dir / f"mip_{level}.astc"
        current.save(png, format="PNG", optimize=False)
        encode_profile = "-cs" if int(getattr(tex, "m_ColorSpace", 0) or 0) == 1 else "-cl"
        run_astc([str(ASTCENC), encode_profile, str(png), str(astc_file), f"{bx}x{by}", "-medium"])
        data = astc_file.read_bytes()
        if len(data) < 16 or data[:4] != bytes.fromhex("13aba15c"):
            raise ProcessError("Encoder ASTC menghasilkan file yang tidak valid.")
        generated.append(data[16:])
        if level + 1 < len(dims):
            current = current.resize(dims[level + 1], Image.Resampling.LANCZOS)
    result = b"".join(generated)
    expected = sum(astc_mip_size(w, h, bx, by) for w, h in dims)
    if len(result) != expected:
        raise ProcessError("Panjang hasil encoding tidak cocok dengan format ASTC asli.")
    return result


def get_bundle_resource_target(path: Path, texture_name: str, stream_path: str, offset: int, size: int) -> tuple[UnityFSData, str, int]:
    parsed = parse_unityfs(path.read_bytes())
    basename = PurePosixPath(stream_path.replace("\\", "/")).name
    entry = next((e for e in parsed.entries if PurePosixPath(e[3].replace("\\", "/")).name == basename), None)
    if entry is None:
        raise ProcessError("Resource tekstur tidak tercatat di direktori UnityFS.")
    entry_offset, entry_size, _flags, entry_name = entry
    if offset < 0 or size <= 0 or offset + size > entry_size:
        raise ProcessError("Payload tekstur berada di luar batas resource.")
    return parsed, entry_name, entry_offset + offset


def texture_inventory(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    env = UnityPy.load(str(path))
    objects = list(env.objects)
    items: list[dict[str, Any]] = []
    for obj in objects:
        if getattr(obj.type, "name", "") == "Texture2D":
            tex = obj.read()
            stream = tex.m_StreamData
            fmt = texture_format_name(tex.m_TextureFormat)
            payload = b""
            if stream and stream.path:
                res_name = PurePosixPath(stream.path.replace("\\", "/")).name
                reader = obj.assets_file.parent.files.get(res_name)
                if reader is not None and hasattr(reader, "bytes"):
                    rb = reader.bytes
                    payload = rb[stream.offset:stream.offset + stream.size]
            else:
                payload = bytes(tex.image_data or b"")
            items.append({
                "path_id": int(obj.path_id),
                "name": str(tex.m_Name),
                "width": int(tex.m_Width),
                "height": int(tex.m_Height),
                "format": fmt,
                "mipmaps": int(tex.m_MipCount or 1),
                "stream_path": str(stream.path) if stream else "",
                "offset": int(stream.offset) if stream else 0,
                "size": int(stream.size) if stream else len(payload),
                "payload_sha256": sha256(payload),
                "payload_bytes": len(payload),
            })
    return {"objects": len(objects), "textures": len(items)}, items


def apply_resource_patch(source_path: Path, output_path: Path, path_id: str, image_path: Path, replacement_name: str) -> dict[str, Any]:
    source_bytes = source_path.read_bytes()
    source_size = len(source_bytes)
    source_hash = sha256(source_bytes)
    env = UnityPy.load(str(source_path))
    obj, tex = get_texture(env, path_id)
    fmt = texture_format_name(tex.m_TextureFormat)
    fmt_info = texture_format_info(fmt)
    if not fmt_info:
        raise ProcessError("Hanya tekstur ASTC LDR eksternal yang dapat diganti pada versi ini.")
    if not tex.m_StreamData or not tex.m_StreamData.path:
        raise ProcessError("Tekstur ini tidak memakai resource eksternal; proses dibatalkan.")
    work_dir = output_path.parent / f"work-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        new_payload = encode_png_to_original_astc(image_path, tex, fmt_info, work_dir)
        resource_name, res_offset, old_size, old_payload = stream_bytes_for_texture(obj, tex)
        if len(new_payload) != old_size:
            raise ProcessError(f"Ukuran payload baru ({len(new_payload):,} byte) tidak sama dengan payload asli ({old_size:,} byte). Proses dibatalkan.")
        if new_payload == old_payload:
            raise ProcessError("Hasil encoding sama dengan tekstur lama; tidak ada perubahan yang perlu disimpan.")
        parsed, entry_name, global_offset = get_bundle_resource_target(source_path, str(tex.m_Name), tex.m_StreamData.path, res_offset, old_size)
        entry = next(e for e in parsed.entries if e[3] == entry_name)
        raw = bytearray(parsed.raw_data)
        absolute = entry[0] + res_offset
        if raw[absolute:absolute + old_size] != old_payload:
            raise ProcessError("Payload yang ditemukan parser UnityFS tidak cocok dengan UnityPy.")
        raw[absolute:absolute + old_size] = new_payload
        output_bytes, pack_report = pack_unityfs_exact(parsed, bytes(raw), source_size)
        output_path.write_bytes(output_bytes)
        if output_path.stat().st_size != source_size:
            raise ProcessError("Ukuran file hasil berubah. Proses dibatalkan.")
        # Independent re-open and inventory check.
        out_env = UnityPy.load(str(output_path))
        out_objects = list(out_env.objects)
        if len(out_objects) != len(list(env.objects)):
            raise ProcessError("Jumlah objek Unity berubah setelah penyimpanan.")
        before_summary, before_items = texture_inventory(source_path)
        after_summary, after_items = texture_inventory(output_path)
        before_by_id = {x["path_id"]: x for x in before_items}
        after_by_id = {x["path_id"]: x for x in after_items}
        if before_by_id.keys() != after_by_id.keys():
            raise ProcessError("Daftar Texture2D berubah setelah penyimpanan.")
        changed = []
        for pid, old in before_by_id.items():
            new = after_by_id[pid]
            for key in ("name", "width", "height", "format", "mipmaps", "stream_path", "offset", "size"):
                if old[key] != new[key]:
                    raise ProcessError(f"Metadata tekstur {old['name']} berubah pada {key}.")
            if old["payload_sha256"] != new["payload_sha256"]:
                changed.append(old["name"])
        if changed != [str(tex.m_Name)]:
            raise ProcessError("Validasi gagal: ada payload tekstur lain yang ikut berubah.")
        # Serialized object records must remain unchanged; the patch only alters .resS.
        before_objects = {int(o.path_id): o for o in env.objects}
        after_objects = {int(o.path_id): o for o in out_env.objects}
        if before_objects.keys() != after_objects.keys():
            raise ProcessError("Inventaris objek berubah setelah penyimpanan.")
        for pid, before_obj in before_objects.items():
            after_obj = after_objects[pid]
            if before_obj.type != after_obj.type or before_obj.get_raw_data() != after_obj.get_raw_data():
                raise ProcessError("Data objek SerializedFile berubah di luar payload resource.")
        # The raw UnityFS stream must end exactly at EOF; there is no appended padding.
        output_parsed = parse_unityfs(output_bytes)
        if output_parsed.end_of_blocks != len(output_bytes):
            raise ProcessError("Hasil mengandung byte tambahan setelah blok terakhir.")
        if output_parsed.raw_data != bytes(raw):
            raise ProcessError("Pembacaan ulang blok tidak menghasilkan data resource yang sama.")
        # Confirm the selected payload bytes and create a decoded proof image.
        final_obj, final_tex = get_texture(out_env, path_id)
        _, _, _, final_payload = stream_bytes_for_texture(final_obj, final_tex)
        if final_payload != new_payload:
            raise ProcessError("Payload hasil tidak cocok setelah dibaca ulang.")
        decoded = work_dir / "verified-preview.png"
        extract_first_mip(final_obj, final_tex, decoded)
        shutil.copy2(decoded, output_path.parent / "verified-preview.png")
        report = {
            "status": "PASS",
            "original_bytes": source_size,
            "output_bytes": output_path.stat().st_size,
            "delta_bytes": output_path.stat().st_size - source_size,
            "original_sha256": source_hash,
            "output_sha256": sha256(output_path.read_bytes()),
            "texture_name": str(tex.m_Name),
            "replacement_name": replacement_name,
            "path_id": str(obj.path_id),
            "resolution": f"{tex.m_Width}x{tex.m_Height}",
            "format": fmt,
            "mipmaps": int(tex.m_MipCount or 1),
            "payload_bytes": old_size,
            "changed_texture_payloads": changed,
            "texture_count_before": before_summary["textures"],
            "texture_count_after": after_summary["textures"],
            "object_count_before": before_summary["objects"],
            "object_count_after": after_summary["objects"],
            "metadata_unchanged": True,
            "serialized_objects_unchanged": True,
            "no_padding": True,
            "packer": pack_report,
        }
        return report
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def make_zip(bundle_path: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(bundle_path, arcname=bundle_path.name)


def cleanup_expired_jobs(max_age_seconds: int = 12 * 60 * 60) -> None:
    now = time.time()
    for job_id, job in list(JOBS.items()):
        if now - float(job.get("created_at", now)) > max_age_seconds:
            JOBS.pop(job_id, None)
            shutil.rmtree(Path(job["source_path"]).parent, ignore_errors=True)
    # Also remove abandoned upload folders from interrupted requests or server restarts.
    for folder in JOBS_DIR.iterdir():
        if folder.is_dir():
            try:
                if now - folder.stat().st_mtime > max_age_seconds:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass


def get_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Sesi file tidak ditemukan atau sudah kedaluwarsa.")
    return job


@app.get("/")
def index():
    return JSONResponse({"ok": True, "service": "JAYN GMG Texture Replacer API", "health": "/api/health", "docs": "/docs", "note": "Frontend berada di file index.html terpisah."})


@app.get("/api/health")
def health():
    astc_ok = ASTCENC.is_file() and os.access(ASTCENC, os.X_OK)
    return {
        "ok": True,
        "service": "JAYN GMG 2D Texture Replacer",
        "unitypy": getattr(UnityPy, "__version__", "installed"),
        "astcenc": astc_ok,
        "max_upload_bytes": MAX_UPLOAD,
        "supported_replacement": "External ASTC LDR in UnityFS v6; exact encoded payload length required",
    }


@app.post("/api/scan")
async def scan(file: UploadFile = File(...)):
    cleanup_expired_jobs()
    filename = Path(file.filename or "unity-file").name
    job_id = uuid.uuid4().hex[:14]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    source_path = job_dir / clean_name(filename, "unity-file")
    total = 0
    try:
        with source_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise HTTPException(status_code=413, detail="File melebihi batas upload server.")
                out.write(chunk)
        if total < 24:
            raise ProcessError("File terlalu kecil atau bukan file Unity yang valid.")
        summary, textures = await run_in_threadpool(scan_bundle, source_path)
        if not textures:
            raise ProcessError("Texture2D tidak ditemukan di dalam file ini.")
        job = {
            "id": job_id,
            "filename": source_path.name,
            "source_path": str(source_path),
            "summary": summary,
            "textures": textures,
            "created_at": time.time(),
            "status": "scanned",
            "report": None,
            "zip_path": None,
            "zip_name": None,
        }
        JOBS[job_id] = job
        return {"job_id": job_id, "filename": source_path.name, "summary": summary, "textures": textures}
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except ProcessError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="File gagal dipindai. Pastikan file Unity tidak rusak.") from exc
    finally:
        await file.close()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    return {
        "id": job["id"],
        "status": job["status"],
        "filename": job["filename"],
        "summary": job["summary"],
        "report": job.get("report"),
        "zip_name": job.get("zip_name"),
    }


@app.get("/api/jobs/{job_id}/textures")
def list_textures(job_id: str):
    job = get_job(job_id)
    return {"textures": job["textures"]}


@app.get("/api/jobs/{job_id}/textures/{path_id}/preview")
def texture_preview(job_id: str, path_id: str):
    job = get_job(job_id)
    try:
        env = UnityPy.load(job["source_path"])
        obj, tex = get_texture(env, path_id)
        if not texture_format_info(texture_format_name(tex.m_TextureFormat)):
            raise ProcessError("Preview hanya tersedia untuk ASTC LDR pada versi ini.")
        out = Path(job["source_path"]).parent / f"preview-{path_id.replace('-', 'm')}.png"
        extract_first_mip(obj, tex, out)
        return FileResponse(out, media_type="image/png", filename=f"{clean_name(tex.m_Name)}.png")
    except ProcessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/textures/{path_id}/replace")
async def replace_texture(job_id: str, path_id: str, image: UploadFile = File(...), replacement_name: str = Form("New_Texture")):
    job = get_job(job_id)
    if not ASTCENC.is_file() or not os.access(ASTCENC, os.X_OK):
        raise HTTPException(status_code=503, detail="Mesin ASTC belum aktif di server.")
    job_dir = Path(job["source_path"]).parent
    image_path = job_dir / "replacement-upload"
    out_bundle: Path | None = None
    zip_path: Path | None = None
    total = 0
    try:
        with image_path.open("wb") as out:
            while True:
                chunk = await image.read(1024 * 512)
                if not chunk:
                    break
                total += len(chunk)
                if total > 30 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Gambar pengganti terlalu besar.")
                out.write(chunk)
        try:
            with Image.open(image_path) as im:
                if im.width * im.height > MAX_PIXELS:
                    raise ProcessError("Resolusi gambar melebihi batas server.")
                im.verify()
        except ProcessError:
            raise
        except Exception as exc:
            raise ProcessError("Gambar pengganti tidak valid. Gunakan PNG atau JPG.") from exc
        env = UnityPy.load(job["source_path"])
        obj, tex = get_texture(env, path_id)
        original_texture_name = str(tex.m_Name or "Texture")
        replacement_name = clean_name(replacement_name, "New_Texture")
        out_bundle = job_dir / f"{Path(job['filename']).stem}-replaced{Path(job['filename']).suffix or '.bundle'}"
        report = await run_in_threadpool(apply_resource_patch, Path(job["source_path"]), out_bundle, path_id, image_path, replacement_name)
        zip_name = f"JAYN GMG - {clean_name(original_texture_name)} to {replacement_name}.zip"
        zip_path = job_dir / zip_name
        await run_in_threadpool(make_zip, out_bundle, zip_path)
        job["status"] = "verified"
        job["report"] = report
        job["zip_path"] = str(zip_path)
        job["zip_name"] = zip_name
        job["output_path"] = str(out_bundle)
        job["preview_path"] = str(job_dir / "verified-preview.png")
        return {
            "status": "verified",
            "zip_name": zip_name,
            "report": report,
            "download_url": f"/api/jobs/{job_id}/download",
            "preview_url": f"/api/jobs/{job_id}/result-preview",
        }
    except HTTPException:
        job["status"] = "scanned"
        if out_bundle and out_bundle.exists(): out_bundle.unlink(missing_ok=True)
        if zip_path and zip_path.exists(): zip_path.unlink(missing_ok=True)
        raise
    except ProcessError as exc:
        job["status"] = "scanned"
        if out_bundle and out_bundle.exists(): out_bundle.unlink(missing_ok=True)
        if zip_path and zip_path.exists(): zip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        job["status"] = "scanned"
        if out_bundle and out_bundle.exists(): out_bundle.unlink(missing_ok=True)
        if zip_path and zip_path.exists(): zip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=504, detail="Proses encoding melewati batas waktu.") from exc
    except Exception as exc:
        job["status"] = "scanned"
        if out_bundle and out_bundle.exists(): out_bundle.unlink(missing_ok=True)
        if zip_path and zip_path.exists(): zip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Penggantian gagal. File sumber tetap aman.") from exc
    finally:
        image_path.unlink(missing_ok=True)
        await image.close()


@app.get("/api/jobs/{job_id}/result-preview")
def result_preview(job_id: str):
    job = get_job(job_id)
    path = job.get("preview_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Preview hasil belum tersedia.")
    return FileResponse(path, media_type="image/png")


@app.get("/api/jobs/{job_id}/download")
def download_result(job_id: str):
    job = get_job(job_id)
    path = job.get("zip_path")
    if not path or not Path(path).is_file() or job.get("status") != "verified":
        raise HTTPException(status_code=404, detail="File hasil belum lolos verifikasi.")
    return FileResponse(path, media_type="application/zip", filename=job["zip_name"], headers={"Cache-Control": "no-store"})


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = JOBS.pop(job_id, None)
    if not job:
        return {"deleted": True}
    shutil.rmtree(Path(job["source_path"]).parent, ignore_errors=True)
    return {"deleted": True}


@app.get("/api/health/details")
def health_details():
    try:
        proc = subprocess.run([str(ASTCENC), "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
        astc_version = proc.stdout.strip().splitlines()[0] if proc.stdout else "unknown"
    except Exception:
        astc_version = "unavailable"
    return {"astcenc_version": astc_version, "astcenc_path": str(ASTCENC), "data_directory": str(DATA_DIR)}
