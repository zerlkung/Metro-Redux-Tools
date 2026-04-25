"""
Metro Redux Mod Tools  —  standalone, no setup required
Supports: Metro 2033 Redux  ·  Metro Last Light Redux

Formats:
  .vfx   Master catalogue  (archives list + complete file directory)
  .vfs0  Data archive       (LZ4-linked-block compressed game assets)

Mod workflow:
  1. Open game folder (contains content.vfx + content##.vfs0)
  2. Browse / extract files
  3. Drop modified files into a mod folder (preserving directory structure)
  4. Build Mod → creates content_mod.vfs0 + patched content.vfx
"""

import os, struct, math, threading, shutil, queue, time, json, csv, io, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

LZ4_OK = False
try:
    import lz4.block as _lz4
    LZ4_OK = True
except Exception:
    pass

# ── Theme ─────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Colors for treeview / log tags (CTk dark palette)
DIM = "#636876"
OK_GREEN = "#6cc87a"
ERR_RED = "#e06060"

MONO = ("Consolas", 12); UI = ("Verdana", 12)
BOLD = ("Verdana", 12, "bold"); HEAD = ("Verdana", 14, "bold")

# ── VFX parser ────────────────────────────────────────────────────────────────
_BLOCK = 196608  # LZ4 decompressed block size (192 KB)

def _rcstr(data: bytes, pos: int):
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", errors="replace"), end + 1

def _xor(data: bytes, key: int) -> str:
    return bytes(b ^ key for b in data).decode("utf-8", errors="replace")

def _xor_enc(text: str, key: int) -> bytes:
    raw = text.encode("utf-8")
    return bytes(b ^ key for b in raw)


def parse_vfx(path: str) -> dict:
    """
    Parse content.vfx and return a dict with:
      archives   : list of archive dicts  {fname, folders, arc_len}
      dir1       : list of entry dicts    {type, name, …}
      dir2_raw   : bytes  (dir2 entries, 16 bytes each, copied verbatim)
      guid       : bytes (16)
      num_dir2   : int
      path, dir  : str
    """
    data = open(path, "rb").read()
    pos = 0

    # 36-byte header
    ver1 = struct.unpack_from("<I", data, pos)[0]; pos += 4
    ver2 = struct.unpack_from("<I", data, pos)[0]; pos += 4
    guid = data[pos:pos+16];                       pos += 16
    num_archives = struct.unpack_from("<I", data, pos)[0]; pos += 4
    num_dir1     = struct.unpack_from("<I", data, pos)[0]; pos += 4
    num_dir2     = struct.unpack_from("<I", data, pos)[0]; pos += 4

    # Archive list: fname\0 + n(4) + n×folder\0 + arc_len(4)
    archives = []
    for _ in range(num_archives):
        fname, pos = _rcstr(data, pos)
        n = struct.unpack_from("<I", data, pos)[0]; pos += 4
        folders = []
        for __ in range(n):
            f, pos = _rcstr(data, pos)
            folders.append(f)
        arc_len = struct.unpack_from("<I", data, pos)[0]; pos += 4
        archives.append({"fname": fname, "folders": folders, "arc_len": arc_len})

    # Dir1: variable-length entries
    dir1 = []
    for _ in range(num_dir1):
        t = struct.unpack_from("<H", data, pos)[0]; pos += 2
        if t == 0:  # FILE
            arc_id = struct.unpack_from("<H", data, pos)[0]; pos += 2
            offset = struct.unpack_from("<I", data, pos)[0]; pos += 4
            decomp = struct.unpack_from("<I", data, pos)[0]; pos += 4
            comp   = struct.unpack_from("<I", data, pos)[0]; pos += 4
            nlen   = data[pos]; pos += 1
            xk     = data[pos]; pos += 1
            nb     = data[pos:pos+nlen-1]; pos += nlen  # includes null
            name   = _xor(nb, xk)
            dir1.append({"type": "file", "arc_id": arc_id, "offset": offset,
                         "decomp": decomp, "comp": comp, "name": name,
                         "xk": xk, "nlen": nlen})
        elif t == 8:  # FOLDER
            num_ch = struct.unpack_from("<H", data, pos)[0]; pos += 2
            first  = struct.unpack_from("<I", data, pos)[0]; pos += 4
            nlen   = data[pos]; pos += 1
            xk     = data[pos]; pos += 1
            nb     = data[pos:pos+nlen-1]; pos += nlen
            name   = _xor(nb, xk)
            dir1.append({"type": "folder", "num_ch": num_ch, "first": first,
                         "name": name, "xk": xk, "nlen": nlen})
        else:
            break  # malformed

    # Dir2: 16 bytes each, no filenames — copy verbatim
    dir2_raw = data[pos:pos + num_dir2 * 16]

    return {"ver1": ver1, "ver2": ver2, "guid": guid,
            "num_archives": num_archives, "num_dir1": num_dir1,
            "num_dir2": num_dir2, "archives": archives,
            "dir1": dir1, "dir2_raw": dir2_raw,
            "path": path, "dir": os.path.dirname(path)}


def build_path_map(dir1: list) -> dict:
    """Return {normalized_path: entry_index} for all FILE entries."""
    path_map = {}

    def dfs(idx: int, prefix: str):
        if idx >= len(dir1):
            return
        e = dir1[idx]
        if e["type"] == "folder":
            seg = e["name"]
            new_prefix = (prefix + "/" + seg).lstrip("/") if seg else prefix
            for k in range(e["num_ch"]):
                dfs(e["first"] + k, new_prefix)
        else:
            full = (prefix + "/" + e["name"]).lstrip("/")
            path_map[full.replace("\\", "/").lower()] = idx

    dfs(0, "")
    return path_map


# ── VFS0 extraction ───────────────────────────────────────────────────────────

def extract_file(vfs_path: str, offset: int, comp: int, decomp: int) -> bytes:
    """
    Decompress a file from a VFS0 archive.

    Two storage modes:
      - comp == decomp : raw bytes stored directly, no block framing
      - comp != decomp : LZ4 linked blocks (8-byte header per block)
    """
    if not LZ4_OK:
        raise RuntimeError("lz4 library not installed. Run: pip install lz4")
    with open(vfs_path, "rb") as f:
        f.seek(offset)
        if comp == decomp:
            # Raw storage: data written directly without block headers
            return f.read(decomp)

        # LZ4 linked blocks
        result = bytearray()
        dict_data = b""
        remaining = comp
        while remaining > 0:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            blk_comp   = struct.unpack_from("<I", hdr, 0)[0]
            blk_decomp = struct.unpack_from("<I", hdr, 4)[0]
            data_len   = blk_comp - 8
            raw = f.read(data_len)
            remaining -= blk_comp
            if blk_comp == blk_decomp:       # raw / uncompressed block
                result += raw
            else:                            # LZ4 linked block
                result += _lz4.decompress(raw, uncompressed_size=blk_decomp,
                                          dict=dict_data)
            dict_data = bytes(result[-65536:])
        return bytes(result[:decomp])


# ── VFS0 packer ───────────────────────────────────────────────────────────────

def _pack_raw_blocks(data: bytes) -> tuple[bytes, int, int]:
    """
    Pack data as uncompressed LZ4 blocks (comp == decomp per block → raw).
    Returns (block_bytes, comp_total, decomp_total).
    comp_total = sum of (blk_size+8) for all blocks.
    """
    buf = bytearray()
    for i in range(0, len(data), _BLOCK):
        chunk = data[i:i + _BLOCK]
        size  = len(chunk)
        buf  += struct.pack("<II", size + 8, size + 8) + chunk
    comp_total = len(buf)
    return bytes(buf), comp_total, len(data)


def create_vfs0(files: list[tuple[str, bytes]], out_path: str, guid: bytes) -> list[dict]:
    """
    Create a new VFS0 archive.
    files = [(game_path, file_bytes), …]   (game_path for reference only)
    Returns list of dicts {path, offset, comp, decomp} for VFX patching.
    guid must be the 16-byte GUID from content.vfx.
    """
    entries = []
    data_buf = bytearray()

    for game_path, file_bytes in files:
        offset = len(data_buf)
        blocks, comp, decomp = _pack_raw_blocks(file_bytes)
        data_buf += blocks
        entries.append({"path": game_path, "offset": offset,
                        "comp": comp, "decomp": decomp})

    # Footer: num_dir2=0 + 0x10 + GUID
    footer = struct.pack("<II", 0, 0x10) + guid

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(data_buf)
        f.write(footer)

    return entries


# ── VFX writer ────────────────────────────────────────────────────────────────

def _ser_entry(e: dict, arc_id_override: int | None = None,
               offset_override: int | None = None,
               comp_override: int | None = None,
               decomp_override: int | None = None) -> bytes:
    """Serialize one dir1 entry back to bytes."""
    if e["type"] == "file":
        arc_id = arc_id_override if arc_id_override is not None else e["arc_id"]
        offset = offset_override if offset_override is not None else e["offset"]
        decomp = decomp_override if decomp_override is not None else e["decomp"]
        comp   = comp_override   if comp_override   is not None else e["comp"]
        xk     = e["xk"]
        name_enc = _xor_enc(e["name"], xk)
        nlen   = len(name_enc) + 1          # length INCLUDING null terminator
        return (struct.pack("<HHIIIBB", 0, arc_id, offset, decomp, comp,
                            nlen, xk) + name_enc + b"\x00")
    else:
        xk    = e["xk"]
        name_enc = _xor_enc(e["name"], xk)
        nlen  = len(name_enc) + 1
        return (struct.pack("<HHIBB", 8, e["num_ch"], e["first"],
                            nlen, xk) + name_enc + b"\x00")


def write_vfx(vfx: dict, new_archive: dict | None,
              updates: dict[int, dict]) -> bytes:
    """
    Serialise a complete VFX file.
    vfx         : result of parse_vfx()
    new_archive : {fname, arc_len} to append, or None
    updates     : {entry_idx: {arc_id, offset, comp, decomp}} overrides
    """
    archives = list(vfx["archives"])
    if new_archive:
        archives.append({"fname": new_archive["fname"],
                         "folders": [],
                         "arc_len": new_archive["arc_len"]})
    num_archives = len(archives)

    # Header
    buf = struct.pack("<II", vfx["ver1"], vfx["ver2"])
    buf += vfx["guid"]
    buf += struct.pack("<III", num_archives, vfx["num_dir1"], vfx["num_dir2"])

    # Archive list
    for a in archives:
        buf += a["fname"].encode("utf-8") + b"\x00"
        buf += struct.pack("<I", len(a["folders"]))
        for fld in a["folders"]:
            buf += fld.encode("utf-8") + b"\x00"
        buf += struct.pack("<I", a["arc_len"])

    # Dir1
    for idx, e in enumerate(vfx["dir1"]):
        ov = updates.get(idx, {})
        buf += _ser_entry(e,
                          arc_id_override=ov.get("arc_id"),
                          offset_override=ov.get("offset"),
                          comp_override=ov.get("comp"),
                          decomp_override=ov.get("decomp"))

    # Dir2 (verbatim)
    buf += vfx["dir2_raw"]
    return buf


# ── Lng parser (stable_us.lng localization) ────────────────────────────────────

def parse_lng(data: bytes) -> list[tuple[str, str]]:
    """
    Parse a Metro Redux .lng localization file.

    Format:
      [0x00]  6 × uint32  header
      [0x18]  N × uint16  character substitution table (UTF-16LE, null-terminated)
      ...     padding / small data section
      [...]   alternating null-terminated pairs: (ASCII key, encoded value)

    Decoding: each byte in the encoded value maps to char_table[byte - 2].

    Returns list of (key, value) tuples.
    """
    # ── Header ──
    if len(data) < 24:
        raise ValueError("File too small for .lng header")
    # header: 6 × uint32 (we only need to validate magic-ish values)
    hdr = struct.unpack_from("<6I", data, 0)
    # [0]=version, [1]=4, [2]=0, [3]=1, [4]=num_entries?, [5]=0xf8ff

    # ── Character table (UTF-16LE, null-terminated) ──
    char_table: list[str] = []
    pos = 0x18
    while pos < len(data) - 1:
        v = struct.unpack_from("<H", data, pos)[0]
        if v == 0:
            break
        char_table.append(chr(v))
        pos += 2
    table_end = pos + 2  # skip the terminating null

    # ── Skip padding / small data to reach string section ──
    # The string data starts at the first null-terminated ASCII string (len ≥ 2)
    pos = table_end
    while pos < len(data) - 1:
        end = data.find(b"\x00", pos)
        if end == -1:
            break
        chunk = data[pos:end]
        if len(chunk) >= 2 and all(32 <= b < 127 for b in chunk):
            break  # found first ASCII key
        pos = end + 1

    # ── Decode encoded value ──
    # Encoding scheme:
    #   byte 0x01          → newline
    #   byte 0x02..0xDF    → single byte → char_table[byte - 2]
    #   byte 0xE0..0xFF    → two-byte sequence → char_table[(0xDF + next_byte) - 2]
    def _decode(raw: bytes) -> str:
        out: list[str] = []
        i = 0
        while i < len(raw):
            b = raw[i]
            if b == 0x01:
                out.append("\n")
                i += 1
            elif b >= 0xE0 and i + 1 < len(raw):
                table_byte = 0xDF + raw[i + 1]
                idx = table_byte - 2
                if 0 <= idx < len(char_table):
                    out.append(char_table[idx])
                else:
                    out.append(f"[{table_byte}]")
                i += 2
            else:
                idx = b - 2
                if 0 <= idx < len(char_table):
                    out.append(char_table[idx])
                else:
                    out.append(f"[{b}]")
                i += 1
        return "".join(out)

    # ── Read alternating (key, value) pairs ──
    pairs: list[tuple[str, str]] = []
    while pos < len(data):
        # key
        end = data.find(b"\x00", pos)
        if end == -1:
            break
        key = data[pos:end].decode("ascii", errors="replace")
        pos = end + 1
        # value
        end = data.find(b"\x00", pos)
        if end == -1:
            break
        val_raw = data[pos:end]
        pos = end + 1
        if key:
            pairs.append((key, _decode(val_raw)))

    return pairs


def export_lng_to_json(pairs: list[tuple[str, str]], path: str) -> None:
    """Export parsed .lng pairs to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in pairs}, f, ensure_ascii=False, indent=2)


def export_lng_to_csv(pairs: list[tuple[str, str]], path: str) -> None:
    """Export parsed .lng pairs to a CSV file."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "value"])
        for k, v in pairs:
            w.writerow([k, v])


def import_lng_from_json(path: str) -> dict[str, str]:
    """Read a JSON file exported by this tool. Returns {key: value}."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def import_lng_from_csv(path: str) -> dict[str, str]:
    """Read a CSV file exported by this tool. Returns {key: value}."""
    result: dict[str, str] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header and len(header) >= 2 and header[0].lower() == "key":
            pass  # skip header row
        else:
            # no header — treat first row as data
            if header and len(header) >= 2:
                result[header[0]] = header[1]
        for row in reader:
            if len(row) >= 2:
                result[row[0]] = row[1]
    return result


def build_lng(original_data: bytes, updates: dict[str, str]) -> bytes:
    """
    Rebuild a .lng file with modified key-value pairs.

    original_data : raw bytes of the original .lng file
    updates       : {key: new_value} — keys not in updates keep their original value

    The header and character table are preserved from the original.
    """
    # ── Parse header + char table from original ──
    if len(original_data) < 24:
        raise ValueError("File too small for .lng header")
    header = original_data[:24]

    # Read char table
    char_table: list[str] = []
    pos = 0x18
    while pos < len(original_data) - 1:
        v = struct.unpack_from("<H", original_data, pos)[0]
        if v == 0:
            break
        char_table.append(chr(v))
        pos += 2
    table_end = pos + 2  # include null terminator

    # Preserve everything between char table end and string data start
    # Find first ASCII key
    str_start = table_end
    pos = table_end
    while pos < len(original_data) - 1:
        end = original_data.find(b"\x00", pos)
        if end == -1:
            break
        chunk = original_data[pos:end]
        if len(chunk) >= 2 and all(32 <= b < 127 for b in chunk):
            str_start = pos
            break
        pos = end + 1
    padding = original_data[table_end:str_start]

    # ── Read original pairs ──
    original_pairs: list[tuple[str, str]] = []
    pos = str_start
    while pos < len(original_data):
        end = original_data.find(b"\x00", pos)
        if end == -1:
            break
        key = original_data[pos:end].decode("ascii", errors="replace")
        pos = end + 1
        end = original_data.find(b"\x00", pos)
        if end == -1:
            break
        val_raw = original_data[pos:end]
        pos = end + 1
        if key:
            # Decode value (same 2-byte scheme as parse_lng)
            decoded = []
            vi = 0
            while vi < len(val_raw):
                b = val_raw[vi]
                if b == 0x01:
                    decoded.append("\n")
                    vi += 1
                elif b >= 0xE0 and vi + 1 < len(val_raw):
                    table_byte = 0xDF + val_raw[vi + 1]
                    idx = table_byte - 2
                    if 0 <= idx < len(char_table):
                        decoded.append(char_table[idx])
                    else:
                        decoded.append(f"[{table_byte}]")
                    vi += 2
                else:
                    idx = b - 2
                    if 0 <= idx < len(char_table):
                        decoded.append(char_table[idx])
                    else:
                        decoded.append(f"[{b}]")
                    vi += 1
            original_pairs.append((key, "".join(decoded)))

    # ── Apply updates ──
    merged: list[tuple[str, str]] = []
    for key, val in original_pairs:
        merged.append((key, updates.get(key, val)))

    # ── Build reverse lookup: char → table index (0-based) ──
    char_to_idx: dict[str, int] = {}
    for i, ch in enumerate(char_table):
        if ch not in char_to_idx:
            char_to_idx[ch] = i

    # ── Encode value ──
    # byte_val = idx + 2
    # if byte_val < 0xE0 → single byte
    # if byte_val >= 0xE0 → two bytes: 0xE0, (byte_val - 0xDF)
    def _encode(text: str) -> bytes:
        out = bytearray()
        for ch in text:
            if ch == "\n":
                out.append(0x01)
            elif ch in char_to_idx:
                byte_val = char_to_idx[ch] + 2
                if byte_val < 0xE0:
                    out.append(byte_val)
                else:
                    out.append(0xE0)
                    out.append(byte_val - 0xDF)
            else:
                out.append(0x02)  # fallback to space
        return bytes(out)

    # ── Rebuild binary ──
    buf = bytearray()
    buf += header
    # Char table (UTF-16LE)
    for ch in char_table:
        buf += struct.pack("<H", ord(ch))
    buf += b"\x00\x00"  # null terminator
    # Padding between table and string data
    buf += padding
    # String pairs
    for key, val in merged:
        buf += key.encode("ascii", errors="replace") + b"\x00"
        buf += _encode(val) + b"\x00"

    return bytes(buf)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024


# ── Texture .512 / .1024 / .2048 ─────────────────────────────────────────────
# Metro Redux stores BC7 textures as LZ4-compressed blobs.
#   .512  = 512×512  with 10 mip levels (512→256→128→64→32→16→8→4)
#   .1024 = 1024×1024 with 1 mip level
#   .2048 = 2048×2048 with 1 mip level

TEXDEC_OK = False
try:
    import texture2ddecoder as _t2d
    TEXDEC_OK = True
except Exception:
    pass

PIL_OK = False
try:
    from PIL import Image as _Img
    PIL_OK = True
except Exception:
    pass


def _bc7_mip_chain_sizes(dimension: int) -> list[int]:
    """Return BC7 byte sizes for each mip level (4×4 blocks, 16 bytes each)."""
    sizes = []
    d = dimension
    while d >= 4:
        blocks = (d // 4) * (d // 4)
        sizes.append(blocks * 16)
        d //= 2
    return sizes


def _bc4_mip_chain_sizes(dimension: int) -> list[int]:
    """Return BC4/BC1 byte sizes for each mip level (4×4 blocks, 8 bytes each).

    Both BC4 (single-channel SDF) and BC1 (DXT1 hard-edge) use 8 bytes per
    4×4 block.  Supports the full mip chain down to 1×1 (padded to 1 block).
    """
    sizes = []
    d = dimension
    while d >= 1:
        blocks = max(d // 4, 1) * max(d // 4, 1)
        sizes.append(blocks * 8)
        d //= 2
    return sizes


def _sniff_bc1_or_bc4(data: bytes) -> str:
    """Determine whether 8-byte-block texture data is BC1 (DXT1) or BC4.

    PS4 fonts use BC4 (single-channel SDF): endpoint bytes 0 and 1 vary
    widely across blocks — the inside-glyph endpoint is typically >128 and
    the outside-glyph endpoint is near 0, giving a smooth distance gradient.

    PC fonts use BC1 (DXT1 hard-edge): color0 (bytes 0-1) is always 0x0000
    (black in RGB565) and color1 (bytes 2-3) is either 0x0000 (empty block)
    or 0xFFFF (white), so byte 0 of every block is 0x00.

    Returns 'bc4' if any sampled block looks like a BC4 SDF gradient block,
    otherwise 'bc1'.
    """
    block_count = len(data) // 8
    sample = min(512, block_count)
    step = max(1, block_count // sample)
    for i in range(0, block_count, step):
        ep0 = data[i * 8]
        ep1 = data[i * 8 + 1]
        # BC4 SDF block: high inside-endpoint >> low outside-endpoint
        if ep0 > 128 and ep1 < 64:
            return 'bc4'
    return 'bc1'


def parse_texture_512(data: bytes, dimension: int, num_mips: int,
                      ) -> tuple[bytes | None, str, int]:
    """Detect format and decompress/read a .512/.1024/.2048 texture file.

    Returns (raw_bc_data, tex_format, actual_dimension) where:
      tex_format      : 'bc7' for standard game textures,
                        'bc4' for single-channel font atlases
      actual_dimension: detected real texture size — PC font .512 files may
                        contain a 1024×1024 BC4 mip despite the .512 extension

    Returns (None, '', dimension) on failure.

    Detection order:
      1. LZ4 → BC7  (standard game textures, compressed blob)
      2. Raw BC7 exact size  (uncompressed game textures)
      3. Raw BC4 exact full-mip-chain size  (PS4/PC font atlases at nominal dim)
      4. Raw BC4 single mip at 2× dimension  (PC font .512 → 1024×1024 BC4)
      5. Raw BC7 size ≥ expected fallback
    """
    # ── 1 & 2: BC7 path (standard game textures) ──────────────────────────────
    bc7_expected = sum(_bc7_mip_chain_sizes(dimension)[:num_mips])
    if LZ4_OK:
        try:
            out = _lz4.decompress(data, uncompressed_size=bc7_expected)
            if len(out) == bc7_expected:
                return out, 'bc7', dimension
        except Exception:
            pass
    if len(data) == bc7_expected:
        return data, 'bc7', dimension

    # ── 3: 8-bpp block (BC4 or BC1) at nominal dimension ─────────────────────
    # PS4 .512 font = 174,776 B  (512×512 BC4, 10 mips)
    # PS4 .1024 font = 524,288 B (1024×1024 BC4, 1 mip)
    # PC  .512/.1024 = 524,288 B (1024×1024 BC1, 1 mip — upscaled from dim)
    bc8_expected = sum(_bc4_mip_chain_sizes(dimension)[:num_mips])
    if len(data) == bc8_expected:
        fmt = _sniff_bc1_or_bc4(data)
        return data, fmt, dimension

    # ── 4: 8-bpp single top-mip at 2× the nominal dimension ──────────────────
    # PC .512 font files store a 1024×1024 texture in the .512 file = 524,288 B
    if dimension in (512, 1024):
        upscaled = dimension * 2
        bc8_mip0 = max(upscaled // 4, 1) ** 2 * 8
        if len(data) == bc8_mip0:
            fmt = _sniff_bc1_or_bc4(data)
            return data, fmt, upscaled

    # ── 5: Raw BC7 fallback ────────────────────────────────────────────────────
    if len(data) >= bc7_expected:
        return data[:bc7_expected], 'bc7', dimension

    return None, '', dimension


def export_texture_to_png(bc_data: bytes, dimension: int, path: str,
                          tex_fmt: str = 'bc7') -> None:
    """Decode the top mip level and save as PNG.

    tex_fmt 'bc7' → RGBA PNG.
    tex_fmt 'bc4' → grayscale PNG with SDF decode applied so glyph edges are
                    crisp and readable.  Metro Redux font atlases are SDF
                    textures where the raw BC4 value is a distance field:
                    128 = glyph edge, >128 = inside, <128 = outside.
                    We remap [SDF_LO..SDF_HI] → [0..255] to recover sharp edges
                    with smooth anti-aliasing; raw BC4 data is preserved in DDS.
    """
    if not TEXDEC_OK:
        raise RuntimeError("pip install texture2ddecoder")
    if not PIL_OK:
        raise RuntimeError("pip install Pillow")
    if tex_fmt == 'bc4':
        mip0_size = max(dimension // 4, 1) ** 2 * 8
        rgba = _t2d.decode_bc4(bc_data[:mip0_size], dimension, dimension)
        img  = _Img.frombytes("RGBA", (dimension, dimension), rgba)
        # BC4 value is in the Blue channel
        gray = img.split()[2]
        # SDF fonts are resolution-independent distance fields.
        # Upscale the raw distance field 2× with bilinear interpolation
        # BEFORE thresholding so that small (≤512px) atlases produce
        # crisp 1024px output — interpolation works on the gradient, not
        # on already-quantised pixel values.
        if dimension <= 512:
            gray = gray.resize((dimension * 2, dimension * 2), _Img.BILINEAR)
        # SDF decode: remap the distance field so glyph edges become sharp.
        # Values below SDF_LO → fully transparent (outside); above SDF_HI →
        # fully opaque (inside); between → smooth anti-aliased edge.
        SDF_LO, SDF_HI = 80, 176
        span = SDF_HI - SDF_LO
        gray = gray.point(lambda x: max(0, min(255,
            int((x - SDF_LO) / span * 255))))
        gray.save(path)
    elif tex_fmt == 'bc1':
        # BC1 (DXT1) hard-edge font atlases used by the PC version.
        # color0=black (0x0000), color1=white (0xFFFF) in RGB565.
        # No SDF gradient — just a binary/grayscale glyph mask.
        mip0_size = max(dimension // 4, 1) ** 2 * 8
        rgba = _t2d.decode_bc1(bc_data[:mip0_size], dimension, dimension)
        img  = _Img.frombytes("RGBA", (dimension, dimension), rgba)
        # R=G=B for grayscale BC1; use R channel as the glyph mask.
        img.split()[0].save(path)
    else:
        mip0_size = max(dimension // 4, 1) ** 2 * 16
        rgba = _t2d.decode_bc7(bc_data[:mip0_size], dimension, dimension)
        _Img.frombytes("RGBA", (dimension, dimension), rgba).save(path)


def export_texture_to_tga(bc_data: bytes, dimension: int, path: str,
                          tex_fmt: str = 'bc7') -> None:
    """Decode the top mip level and save as TGA.

    tex_fmt 'bc7': 32-bit BGRA TGA, rows bottom-to-top (MetroEX SaveAsTGA layout).
    tex_fmt 'bc4': 8-bit grayscale TGA (font atlases).
    """
    if not TEXDEC_OK:
        raise RuntimeError("pip install texture2ddecoder")
    if tex_fmt in ('bc4', 'bc1'):
        mip0_size = max(dimension // 4, 1) ** 2 * 8
        if tex_fmt == 'bc4':
            rgba = _t2d.decode_bc4(bc_data[:mip0_size], dimension, dimension)
            # BC4 value is in the Blue channel
            gray = bytes(rgba[i + 2] for i in range(0, len(rgba), 4))
        else:
            rgba = _t2d.decode_bc1(bc_data[:mip0_size], dimension, dimension)
            # BC1 grayscale: use R channel
            gray = bytes(rgba[i] for i in range(0, len(rgba), 4))
        with open(path, "wb") as f:
            # Type 3 = uncompressed black-and-white, 8 bpp
            f.write(struct.pack("<BBBHHBHHHHBB",
                                0, 0, 3, 0, 0, 0, 0, 0,
                                dimension, dimension, 8, 0))
            for y in range(dimension - 1, -1, -1):
                f.write(gray[y * dimension:(y + 1) * dimension])
    else:
        mip0_size = (dimension // 4) ** 2 * 16
        rgba = _t2d.decode_bc7(bc_data[:mip0_size], dimension, dimension)
        with open(path, "wb") as f:
            # Type 2 = uncompressed true-color, 32 bpp, bottom-left origin
            f.write(struct.pack("<BBBHHBHHHHBB",
                                0, 0, 2, 0, 0, 0, 0, 0,
                                dimension, dimension, 32, 0))
            pitch = dimension * 4
            for y in range(dimension - 1, -1, -1):
                row = rgba[y * pitch:(y + 1) * pitch]
                for i in range(0, len(row), 4):
                    f.write(bytes([row[i + 2], row[i + 1], row[i], row[i + 3]]))


def _build_dds_header_dx10(dimension: int, num_mips: int,
                           dxgi_format: int = 98) -> bytes:
    """Build 124-byte DDSURFACEDESC2 + 20-byte DX10 header.

    dxgi_format: 98 = BC7_UNORM (default), 80 = BC4_UNORM (font atlases).
    Layout matches MetroEX DDS_MakeDX10Headers.
    """
    mip_count = max(1, num_mips)

    flags = 0x1 | 0x2 | 0x4 | 0x1000          # CAPS | HEIGHT | WIDTH | PIXELFORMAT
    caps  = 0x1000                               # DDSCAPS_TEXTURE
    if mip_count > 1:
        flags |= 0x20000                         # DDSD_MIPMAPCOUNT
        caps  |= 0x8                             # DDSCAPS_COMPLEX

    buf = bytearray()
    # ── DDSURFACEDESC2 (124 bytes) ──
    buf += struct.pack("<7I", 124, flags, dimension, dimension, 0, 0, mip_count)
    buf += b"\x00" * (11 * 4)                    # dwReserved1[11]
    buf += struct.pack("<2I", 32, 0x4)           # pfSize, pfFlags (DDPF_FOURCC)
    buf += struct.pack("<I", 0x30315844)          # dwFourCC = 'DX10'
    buf += struct.pack("<5I", 0, 0, 0, 0, 0)     # RGBBitCount, masks
    buf += struct.pack("<5I", caps, 0, 0, 0, 0)  # caps, caps2-4, reserved2

    # ── DX10 header (20 bytes) ──
    buf += struct.pack("<5I", dxgi_format, 3, 0, 1, 0)  # format, TEXTURE2D, misc, arraySize, misc2
    return bytes(buf)


def _build_dds_header_dx9(dimension: int) -> bytes:
    """Build 124-byte DDSURFACEDESC2 for uncompressed RGBA (DX9 legacy).

    Layout matches MetroEX DDS_MakeDX9Header but stores raw RGBA pixels
    instead of BC3 blocks, so any DDS viewer can open it.
    """
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x8      # CAPS | HEIGHT | WIDTH | PIXELFORMAT | PITCH

    buf = bytearray()
    buf += struct.pack("<7I", 124, flags, dimension, dimension,
                       dimension * 4, 0, 0)       # pitch = width*4
    buf += b"\x00" * (11 * 4)                     # dwReserved1[11]
    buf += struct.pack("<2I", 32, 0x1 | 0x40)     # pfSize, pfFlags (ALPHAPIXELS | RGB)
    buf += struct.pack("<I", 0)                    # dwFourCC = 0 (no FourCC)
    buf += struct.pack("<5I", 32, 0x00FF0000, 0x0000FF00,
                       0x000000FF, 0xFF000000)     # bpp=32, RGBA masks
    buf += struct.pack("<5I", 0x1000, 0, 0, 0, 0) # DDSCAPS_TEXTURE, caps2-4, reserved2
    return bytes(buf)


def export_texture_to_dds(bc_data: bytes, dimension: int, num_mips: int,
                          path: str, tex_fmt: str = 'bc7') -> None:
    """Save raw BC data as DDS with DX10 extended header.

    tex_fmt 'bc7': DXGI_FORMAT_BC7_UNORM (98).
    tex_fmt 'bc4': DXGI_FORMAT_BC4_UNORM (80) — PS4 SDF font atlases.
    tex_fmt 'bc1': DXGI_FORMAT_BC1_UNORM (71) — PC hard-edge font atlases.
    """
    dxgi_map = {'bc4': 80, 'bc1': 71}
    dxgi = dxgi_map.get(tex_fmt, 98)
    hdr = _build_dds_header_dx10(dimension, num_mips, dxgi_format=dxgi)
    with open(path, "wb") as f:
        f.write(b"\x44\x44\x53\x20")  # DDS magic
        f.write(hdr)
        f.write(bc_data)


def export_texture_to_legacy_dds(bc_data: bytes, dimension: int,
                                 path: str, tex_fmt: str = 'bc7') -> None:
    """Decode BC → RGBA, save as uncompressed RGBA DDS with DX9 header."""
    if not TEXDEC_OK:
        raise RuntimeError("pip install texture2ddecoder")
    if tex_fmt == 'bc4':
        mip0_size = max(dimension // 4, 1) ** 2 * 8
        rgba_raw = _t2d.decode_bc4(bc_data[:mip0_size], dimension, dimension)
        # BC4 value is in Blue channel; expand to RGBA grayscale
        rgba = bytearray()
        for i in range(0, len(rgba_raw), 4):
            v = rgba_raw[i + 2]
            rgba += bytes([v, v, v, 255])
        rgba = bytes(rgba)
    elif tex_fmt == 'bc1':
        mip0_size = max(dimension // 4, 1) ** 2 * 8
        rgba_raw = _t2d.decode_bc1(bc_data[:mip0_size], dimension, dimension)
        # BC1 grayscale: R=G=B; expand to RGBA grayscale
        rgba = bytearray()
        for i in range(0, len(rgba_raw), 4):
            v = rgba_raw[i]  # R channel
            rgba += bytes([v, v, v, 255])
        rgba = bytes(rgba)
    else:
        mip0_size = (dimension // 4) ** 2 * 16
        rgba = _t2d.decode_bc7(bc_data[:mip0_size], dimension, dimension)
    hdr = _build_dds_header_dx9(dimension)
    with open(path, "wb") as f:
        f.write(b"\x44\x44\x53\x20")  # DDS magic
        f.write(hdr)
        f.write(rgba)


def _bc7_encode_block_mode6(rgba: bytes) -> bytes:
    """Encode a 4×4 RGBA block (64 bytes) to BC7 mode 6 (128 bits / 16 bytes).

    Mode 6 layout (128 bits):
      [0]    mode bit 0 = 1
      [1:3]  rotation (2 bits) — 0 = no rotation
      [3:18] R endpoints (2 × 8 bits)
      [18:33] G endpoints (2 × 8 bits, minus 1 overlap bit)
      ...
    Simplified: store as mode byte 0x40, rotation 0, then quantised endpoints + indices.
    We use a fixed layout that decoders accept.
    """
    # Collect 16 RGBA pixels
    px = [(rgba[i], rgba[i+1], rgba[i+2], rgba[i+3]) for i in range(0, 64, 4)]

    # Find min/max per channel
    r_vals = [p[0] for p in px]
    g_vals = [p[1] for p in px]
    b_vals = [p[2] for p in px]
    a_vals = [p[3] for p in px]

    def endpoints(vals, bits):
        lo, hi = min(vals), max(vals)
        if lo == hi:
            return lo, hi
        scale = (1 << bits) - 1
        lo_q = round(lo * scale / 255)
        hi_q = round(hi * scale / 255)
        lo_d = round(lo_q * 255 / scale)
        hi_d = round(hi_q * 255 / scale)
        return lo_d, hi_d

    r0, r1 = endpoints(r_vals, 7)
    g0, g1 = endpoints(g_vals, 7)
    b0, b1 = endpoints(b_vals, 7)
    a0, a1 = endpoints(a_vals, 7)

    # Compute 4-bit indices
    def index_4bit(val, lo, hi):
        if hi == lo:
            return 0
        t = (val - lo) / (hi - lo)
        return min(15, max(0, round(t * 15)))

    indices = []
    for r, g, b, a in px:
        ri = index_4bit(r, r0, r1)
        gi = index_4bit(g, g0, g1)
        bi = index_4bit(b, b0, b1)
        ai = index_4bit(a, a0, a1)
        indices.append((ri, gi, bi, ai))

    # Build 128-bit block
    # Using a simplified mode 6 encoding that most decoders accept
    block = bytearray(16)

    # Mode 6: bit 0 set, no rotation, no swap
    block[0] = 0x40  # mode bit 0 = 1 (mode 6 = bit 6 set)

    # Quantise endpoints to 7-bit
    def q7(v):
        return round(v * 127 / 255)

    r0q, r1q = q7(r0), q7(r1)
    g0q, g1q = q7(g0), q7(g1)
    b0q, b1q = q7(b0), q7(b1)
    a0q, a1q = q7(a0), q7(a1)

    # Pack endpoints (7 bits each × 4 channels × 2 endpoints)
    # This is a simplified packing — real BC7 has complex bit layout
    # We write a minimal valid block
    bits = 0
    nbits = 0

    def push(val, n):
        nonlocal bits, nbits
        bits |= (val & ((1 << n) - 1)) << nbits
        nbits += n

    push(0x40, 8)      # mode byte (mode 6)
    push(0, 2)         # rotation (none)
    push(r0q, 7)       # R0
    push(r1q, 7)       # R1
    push(g0q, 7)       # G0
    push(g1q, 7)       # G1
    push(b0q, 7)       # B0
    push(b1q, 7)       # B1
    push(a0q, 7)       # A0
    push(a1q, 7)       # A1

    # P bit (subset index) — always 0 for single subset
    push(0, 1)

    # 16 × 4-bit indices (color index per pixel)
    for ri, gi, bi, ai in indices:
        push(ri, 4)

    # 16 × 4-bit alpha indices
    for ri, gi, bi, ai in indices:
        push(ai, 4)

    # Write bits to block
    for i in range(16):
        block[i] = (bits >> (i * 8)) & 0xFF

    return bytes(block)


def _resize_nearest(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    """Nearest-neighbour resize RGBA pixels."""
    out = bytearray(dw * dh * 4)
    for dy in range(dh):
        sy = dy * sh // dh
        for dx in range(dw):
            sx = dx * sw // dw
            si = (sy * sw + sx) * 4
            di = (dy * dw + dx) * 4
            out[di:di+4] = src[si:si+4]
    return bytes(out)


def _encode_bc7_image(rgba: bytes, w: int, h: int) -> bytes:
    """Encode full RGBA image to BC7 blocks (mode 6 only)."""
    blocks = bytearray()
    for by in range(h // 4):
        for bx in range(w // 4):
            block_px = bytearray()
            for py in range(4):
                for px in range(4):
                    idx = ((by * 4 + py) * w + (bx * 4 + px)) * 4
                    block_px.extend(rgba[idx:idx+4])
            blocks.extend(_bc7_encode_block_mode6(bytes(block_px)))
    return bytes(blocks)


def build_texture_512(rgba: bytes, dimension: int) -> bytes:
    """Encode RGBA pixels → LZ4-compressed .512 file (BC7, full mip chain)."""
    if not LZ4_OK:
        raise RuntimeError("pip install lz4")

    bc7_all = bytearray()
    cur_w, cur_h = dimension, dimension
    cur_rgba = rgba

    num_mips = 10 if dimension == 512 else 1
    for i in range(num_mips):
        bc7_all.extend(_encode_bc7_image(cur_rgba, cur_w, cur_h))
        if i < num_mips - 1:
            nw, nh = max(4, cur_w // 2), max(4, cur_h // 2)
            cur_rgba = _resize_nearest(cur_rgba, cur_w, cur_h, nw, nh)
            cur_w, cur_h = nw, nh

    return _lz4.compress(bytes(bc7_all), store_size=False)


def find_game_dir(start: str) -> str | None:
    """Walk up from start looking for a content.vfx file."""
    for d in [start, os.path.dirname(start)]:
        vfx = os.path.join(d, "content.vfx")
        if os.path.isfile(vfx):
            return d
    return None


# ══════════════════════════════════════════════════════════════════════════════
# GUI  (CustomTkinter)
# ══════════════════════════════════════════════════════════════════════════════

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Metro Redux — Mod Tools")
        self.geometry("1100x700")
        self.minsize(860, 540)

        self._vfx:       dict | None = None
        self._pmap:      dict        = {}
        self._pmap_full: dict        = {}
        self._logq = queue.Queue()

        self._build()
        self._style_treeview()
        self._poll_log()
        self._log_startup()

    # ── Treeview dark theme ───────────────────────────────────────────────────
    def _style_treeview(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        bg = "#1a1d24"
        s.configure("Dark.Treeview", background=bg, foreground="#e2e4e8",
                    fieldbackground=bg, rowheight=26, font=UI, borderwidth=0)
        s.configure("Dark.Treeview.Heading", background="#24272e",
                    foreground="#9aa0ab", relief="flat", font=BOLD, padding=[10, 6])
        s.map("Dark.Treeview",
              background=[("selected", "#1e3a5f")],
              foreground=[("selected", "#a8c4ff")])

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color="#1a1d24", corner_radius=0, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="METRO REDUX", font=("Verdana", 15, "bold"),
                     text_color="#e2e4e8").pack(side="left", padx=16)
        ctk.CTkLabel(hdr, text="Mod Tools", font=("Verdana", 13),
                     text_color="#636876").pack(side="left", padx=(0, 12))
        lz4_txt = "lz4 ready" if LZ4_OK else "lz4 missing"
        lz4_clr = "#6cc87a" if LZ4_OK else "#e06060"
        ctk.CTkLabel(hdr, text=lz4_txt, font=MONO,
                     text_color=lz4_clr).pack(side="right", padx=16)

        # Tabs
        self.tabview = ctk.CTkTabview(self, corner_radius=8, segmented_button_fg_color="#1a1d24",
                                       segmented_button_selected_color="#3b82f6",
                                       segmented_button_unselected_color="#24272e",
                                       segmented_button_selected_hover_color="#2563eb")
        self.tabview.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        self.tabview.add("Game Files")
        self.tabview.add("Extract")
        self.tabview.add("Mod Builder")
        self.tabview.add("Log")
        self._build_files_tab()
        self._build_extract_tab()
        self._build_mod_tab()
        self._build_log_tab()

        # Status bar
        self._stvar = ctk.StringVar(value="Ready")
        ctk.CTkLabel(self, textvariable=self._stvar, font=MONO,
                     text_color="#636876", anchor="w").pack(fill="x", padx=14, pady=(4, 4))

    # ── Tab: Game Files ───────────────────────────────────────────────────────
    def _build_files_tab(self):
        f = self.tabview.tab("Game Files")

        # Toolbar
        tb = ctk.CTkFrame(f, fg_color="#1a1d24", corner_radius=6)
        tb.pack(fill="x", padx=6, pady=(6, 4))
        ctk.CTkButton(tb, text="Open game folder", width=140,
                      command=self._open_game).pack(side="left", padx=(10, 4), pady=8)
        ctk.CTkButton(tb, text="Open VFS0 file", width=120,
                      command=self._open_vfs0).pack(side="left", padx=4, pady=8)
        self._gdir_lbl = ctk.CTkLabel(tb, text="No folder loaded", font=MONO,
                                       text_color="#636876")
        self._gdir_lbl.pack(side="left", padx=12)

        # Filter row
        flt = ctk.CTkFrame(f, fg_color="transparent")
        flt.pack(fill="x", padx=6, pady=4)
        ctk.CTkLabel(flt, text="Filter", font=UI, text_color="#636876"
                     ).pack(side="left", padx=(8, 4))
        self._flt_var = ctk.StringVar()
        self._flt_var.trace_add("write", self._filter_files)
        ctk.CTkEntry(flt, textvariable=self._flt_var, width=320,
                     placeholder_text="path fragment…").pack(side="left", padx=4)

        # File list (ttk.Treeview — CTk has no tree widget)
        cols = ("path", "arc", "offset", "comp", "decomp")
        tree_frame = ctk.CTkFrame(f, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        self._ftree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                   style="Dark.Treeview")
        for c, h, w, a in [
            ("path", "File Path", 520, "w"), ("arc", "Archive", 130, "w"),
            ("offset", "Offset", 110, "e"), ("comp", "Comp", 90, "e"),
            ("decomp", "Decomp", 90, "e")]:
            self._ftree.heading(c, text=h)
            self._ftree.column(c, width=w, anchor=a)
        vsb = ctk.CTkScrollbar(tree_frame, command=self._ftree.yview)
        hsb = ctk.CTkScrollbar(tree_frame, orientation="horizontal",
                                command=self._ftree.xview)
        self._ftree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")
        vsb.pack(side="right", fill="y")
        self._ftree.pack(fill="both", expand=True)

        self._fsum = ctk.CTkLabel(f, text="", font=MONO, text_color="#636876",
                                   anchor="w")
        self._fsum.pack(fill="x", padx=10)

        # Action bar
        ab = ctk.CTkFrame(f, fg_color="transparent")
        ab.pack(fill="x", padx=6, pady=6)
        ctk.CTkButton(ab, text="Extract selected", width=140,
                      command=self._extract_sel).pack(side="left")
        ctk.CTkButton(ab, text="Extract matching filter", width=160,
                      command=self._extract_filter).pack(side="left", padx=8)

    def _open_game(self):
        d = filedialog.askdirectory(title="Select game data folder (contains content.vfx)")
        if not d: return
        self._load_game(d)

    def _open_vfs0(self):
        p = filedialog.askopenfilename(
            title="Select a VFS0 archive (content.vfx must be in the same folder)",
            filetypes=[("VFS0 archives", "*.vfs0"), ("All files", "*.*")])
        if not p: return
        folder = os.path.dirname(p)
        vfx_path = os.path.join(folder, "content.vfx")
        if not os.path.isfile(vfx_path):
            messagebox.showerror("Not found",
                f"content.vfx not found in the same folder:\n{folder}")
            return
        arc_name = os.path.basename(p)
        self._status(f"Parsing VFX for {arc_name}…")
        self._log(f"Loading VFS0: {p}", "hd")

        def worker():
            try:
                vfx  = parse_vfx(vfx_path)
                pmap = build_path_map(vfx["dir1"])
                self.after(0, lambda: self._on_vfs0_loaded(vfx, pmap, arc_name))
            except Exception as e:
                traceback.print_exc()
                self.after(0, lambda: messagebox.showerror("Parse error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def _on_vfs0_loaded(self, vfx: dict, pmap: dict, arc_name: str):
        self._vfx       = vfx
        self._pmap_full = pmap   # keep full map for mod builder matching
        # Find which archive index this vfs0 maps to
        arc_id = next((i for i, a in enumerate(vfx["archives"])
                       if a["fname"].lower() == arc_name.lower()), None)
        if arc_id is None:
            messagebox.showwarning("Not listed",
                f"{arc_name!r} is not referenced in content.vfx.\n"
                "Showing all files instead.")
            self._pmap = pmap
        else:
            # Replace pmap with only files from this archive so that
            # preset extract / filter extract only touch this one VFS0
            self._pmap = {p: i for p, i in pmap.items()
                          if vfx["dir1"][i]["arc_id"] == arc_id}
        n = len(self._pmap)
        self._gdir_lbl.configure(text=f"{arc_name}  ({n:,} files)", text_color="#e2e4e8")
        self._log(f"  archive index {arc_id}  ·  {n:,} files", "dim")
        self._populate_file_tree(list(self._pmap.items()))
        self._status(f"Loaded {n:,} files from {arc_name}")
        self._extract_all_lbl.configure(text=f"{n:,} files")
        self._game_dir_var.set(vfx["dir"])

    def _load_game(self, folder: str):
        vfx_path = os.path.join(folder, "content.vfx")
        if not os.path.isfile(vfx_path):
            messagebox.showerror("Not found", f"content.vfx not found in:\n{folder}")
            return
        self._status("Parsing VFX…")
        self._log(f"Loading: {vfx_path}", "hd")

        def worker():
            try:
                vfx = parse_vfx(vfx_path)
                pmap = build_path_map(vfx["dir1"])
                self.after(0, lambda: self._on_vfx_loaded(vfx, pmap))
            except Exception as e:
                traceback.print_exc()
                self.after(0, lambda: messagebox.showerror("Parse error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def _on_vfx_loaded(self, vfx: dict, pmap: dict):
        self._vfx       = vfx
        self._pmap      = pmap
        self._pmap_full = pmap
        n = len(pmap)
        self._gdir_lbl.configure(text=vfx["dir"], text_color="#e2e4e8")
        self._log(f"  {vfx['num_archives']} archives · {n:,} files · GUID {vfx['guid'].hex()[:16]}…", "dim")
        self._populate_file_tree(list(pmap.items()))
        self._status(f"Loaded {n:,} files from {os.path.basename(vfx['dir'])}")
        self._extract_all_lbl.configure(text=f"{n:,} files")
        # Also populate mod tab
        self._game_dir_var.set(vfx["dir"])

    def _populate_file_tree(self, items: list):
        self._ftree.delete(*self._ftree.get_children())
        for path, idx in items:
            e = self._vfx["dir1"][idx]
            arc = self._vfx["archives"][e["arc_id"]]["fname"]
            self._ftree.insert("", "end", iid=str(idx),
                               values=(path, arc, e["offset"],
                                       _fmt(e["comp"]), _fmt(e["decomp"])))
        self._fsum.configure(text=f"{len(items):,} files shown")

    def _filter_files(self, *_):
        if not self._vfx:
            return
        q = self._flt_var.get().lower().replace("\\", "/")
        items = [(p, i) for p, i in self._pmap.items() if q in p]
        self._populate_file_tree(items)

    def _extract_sel(self):
        sel = self._ftree.selection()
        if not sel:
            messagebox.showinfo("Select", "Select one or more files first."); return
        out = filedialog.askdirectory(title="Extract to folder")
        if not out: return
        items = [(self._vfx["dir1"][int(iid)]["name"],
                  self._vfx["dir1"][int(iid)]) for iid in sel]
        self._run_extract(items, out)

    def _extract_filter(self):
        if not self._vfx:
            messagebox.showinfo("Load", "Open a game folder first."); return
        q = self._flt_var.get().lower().replace("\\", "/")
        items_kv = [(p, i) for p, i in self._pmap.items() if q in p]
        if not items_kv:
            messagebox.showinfo("No match", "No files match the current filter."); return
        out = filedialog.askdirectory(title="Extract to folder")
        if not out: return
        items = [(path, self._vfx["dir1"][idx]) for path, idx in items_kv]
        self._run_extract(items, out)

    def _run_extract(self, items: list, out_dir: str):
        """Extract files in a background thread."""
        self._status("Extracting…")
        self.tabview.set("Log")

        def worker():
            done = skip = err = 0
            for path, e in items:
                arc = self._vfx["archives"][e["arc_id"]]["fname"]
                arc_path = os.path.join(self._vfx["dir"], arc)
                if not os.path.isfile(arc_path):
                    self._log(f"  SKIP (archive not found): {arc}", "err"); skip += 1; continue
                try:
                    raw = extract_file(arc_path, e["offset"], e["comp"], e["decomp"])
                    dest = os.path.join(out_dir, path.replace("/", os.sep))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    open(dest, "wb").write(raw)
                    done += 1
                    self._log(f"  ✓ {path}  ({_fmt(len(raw))})", "ok")
                except Exception as ex:
                    traceback.print_exc()
                    self._log(f"  ✗ {path}: {ex}", "err"); err += 1
            msg = f"Done: {done} extracted  ·  {skip} skipped  ·  {err} errors"
            self._log(msg, "ok"); self.after(0, lambda: self._status(msg))
        threading.Thread(target=worker, daemon=True).start()

    # ── Tab: Extract ──────────────────────────────────────────────────────────
    def _build_extract_tab(self):
        f = self.tabview.tab("Extract")

        # Scrollable area
        self._ext_scroll = ctk.CTkScrollableFrame(f, fg_color="transparent")
        self._ext_scroll.pack(fill="both", expand=True)
        inner = self._ext_scroll

        # Helper to create a card section
        def _card(parent, title, pady_top=4):
            ctk.CTkLabel(parent, text=title, font=BOLD, text_color="#636876",
                         anchor="w").pack(fill="x", padx=4, pady=(pady_top, 2))
            card = ctk.CTkFrame(parent, fg_color="#1a1d24", corner_radius=8)
            card.pack(fill="x", padx=4, pady=(0, 6))
            return card

        def _row(parent, label, pady=3):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=pady, padx=8)
            ctk.CTkLabel(row, text=label, width=280, anchor="w", font=UI,
                         text_color="#e2e4e8").pack(side="left")
            return row

        # ── Extract All ──
        card = _card(inner, "EXTRACT ALL", 0)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(row, text="All loaded files", anchor="w", font=UI,
                     text_color="#e2e4e8").pack(side="left")
        ctk.CTkButton(row, text="Extract all", width=120,
                      command=self._extract_all).pack(side="right")
        self._extract_all_lbl = ctk.CTkLabel(row, text="", font=MONO,
                                              text_color="#636876")
        self._extract_all_lbl.pack(side="right", padx=10)

        # ── Presets ──
        card = _card(inner, "PRESETS")
        presets = [
            ("Localization (stable_us.lng)", "localization/stable_us"),
            ("Font textures (textures/font/)",  "textures/font"),
            ("All localization files",          "localization/"),
            ("All font files",                  "font_"),
        ]
        for label, flt in presets:
            row = _row(card, label)
            ctk.CTkButton(row, text="Extract", width=90,
                          command=lambda fl=flt: self._preset_extract(fl)).pack(side="right")

        # ── Localization Export ──
        card = _card(inner, "LOCALIZATION EXPORT")
        row = _row(card, "stable_us.lng to JSON / CSV")
        ctk.CTkButton(row, text="CSV", width=60,
                      command=lambda: self._export_lng("csv")).pack(side="right")
        ctk.CTkButton(row, text="JSON", width=60,
                      command=lambda: self._export_lng("json")).pack(side="right", padx=4)
        row2 = _row(card, ".lng file from disk")
        ctk.CTkButton(row2, text="Browse .lng", width=100,
                      command=self._export_lng_file).pack(side="right")

        # ── Localization Import ──
        card = _card(inner, "LOCALIZATION IMPORT")
        row = _row(card, "Import JSON / CSV, rebuild .lng")
        ctk.CTkButton(row, text="CSV", width=60,
                      command=lambda: self._import_lng("csv")).pack(side="right")
        ctk.CTkButton(row, text="JSON", width=60,
                      command=lambda: self._import_lng("json")).pack(side="right", padx=4)

        # ── Texture Export ──
        card = _card(inner, "TEXTURE EXPORT")
        row = _row(card, ".512 / .1024 / .2048 from disk")
        ctk.CTkButton(row, text="Browse", width=90,
                      command=self._export_texture).pack(side="right")

        # ── Texture Import ──
        card = _card(inner, "TEXTURE IMPORT")
        row = _row(card, "Import PNG, rebuild .512")
        ctk.CTkButton(row, text="Browse PNG", width=100,
                      command=self._import_texture).pack(side="right")

    def _extract_all(self):
        if not self._vfx:
            messagebox.showinfo("Load", "Open a game folder or VFS0 file first."); return
        n = len(self._pmap)
        if not n:
            messagebox.showinfo("Empty", "No files loaded."); return
        if not messagebox.askyesno("Extract all",
                f"Extract all {n:,} files?\nThis may take a while."):
            return
        out = filedialog.askdirectory(title="Extract all files to folder")
        if not out: return
        items = [(path, self._vfx["dir1"][idx]) for path, idx in self._pmap.items()]
        self._log(f"\nExtract all  →  {len(items):,} files", "hd")
        self._run_extract(items, out)

    def _preset_extract(self, flt: str):
        if not self._vfx:
            messagebox.showinfo("Load", "Open a game folder first."); return
        out = filedialog.askdirectory(title="Extract to folder")
        if not out: return
        flt_n = flt.lower().replace("\\", "/")
        items = [(path, self._vfx["dir1"][idx])
                 for path, idx in self._pmap.items() if flt_n in path]
        if not items:
            messagebox.showinfo("No match", f"No files match: {flt!r}"); return
        self._log(f"\nPreset extract: {flt!r}  →  {len(items)} files", "hd")
        self._run_extract(items, out)

    def _export_lng(self, fmt: str):
        """Extract stable_us.lng from VFS0 and export to JSON or CSV."""
        if not self._vfx:
            messagebox.showinfo("Load", "Open a game folder or VFS0 file first."); return
        if not LZ4_OK:
            messagebox.showerror("Missing", "pip install lz4  is required for extraction."); return

        # Find stable_us.lng in the file map
        lng_idx = None
        for path, idx in self._pmap.items():
            if "stable_us" in path and path.endswith(".lng"):
                lng_idx = idx
                break
        if lng_idx is None:
            # Try full map
            for path, idx in self._pmap_full.items():
                if "stable_us" in path and path.endswith(".lng"):
                    lng_idx = idx
                    break
        if lng_idx is None:
            messagebox.showinfo("Not found", "stable_us.lng not found in loaded archives."); return

        # Ask save location
        ftypes = [("JSON", "*.json")] if fmt == "json" else [("CSV", "*.csv")]
        out_path = filedialog.asksaveasfilename(
            title=f"Export stable_us.lng as {fmt.upper()}",
            defaultextension=f".{fmt}",
            filetypes=ftypes + [("All files", "*.*")])
        if not out_path:
            return

        e = self._vfx["dir1"][lng_idx]
        arc = self._vfx["archives"][e["arc_id"]]["fname"]
        arc_path = os.path.join(self._vfx["dir"], arc)
        self._status(f"Extracting stable_us.lng…")
        self._log(f"\nExport stable_us.lng → {fmt.upper()}", "hd")

        def worker():
            try:
                raw = extract_file(arc_path, e["offset"], e["comp"], e["decomp"])
                self._log(f"  extracted ({_fmt(len(raw))})", "dim")
                pairs = parse_lng(raw)
                self._log(f"  parsed {len(pairs):,} key-value pairs", "dim")
                if fmt == "json":
                    export_lng_to_json(pairs, out_path)
                else:
                    export_lng_to_csv(pairs, out_path)
                msg = f"Exported {len(pairs):,} entries → {out_path}"
                self._log("✓ " + msg, "ok")
                self.after(0, lambda: self._status(msg))
                self.after(0, lambda: messagebox.showinfo("Done", msg))
            except Exception as ex:
                traceback.print_exc()
                self._log(f"✗ {ex}", "err")
                self.after(0, lambda e=ex: messagebox.showerror("Error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _export_lng_file(self):
        """Export an already-extracted .lng file from disk to JSON or CSV."""
        lng_path = filedialog.askopenfilename(
            title="Select a .lng file",
            filetypes=[("Lng files", "*.lng"), ("All files", "*.*")])
        if not lng_path:
            return

        # Ask format
        fmt = messagebox.askquestion("Format", "Export as JSON?\n\nYes = JSON, No = CSV",
                                     icon="question")
        fmt = "json" if fmt == "yes" else "csv"

        ftypes = [("JSON", "*.json")] if fmt == "json" else [("CSV", "*.csv")]
        out_path = filedialog.asksaveasfilename(
            title=f"Save as {fmt.upper()}",
            defaultextension=f".{fmt}",
            initialfile=os.path.splitext(os.path.basename(lng_path))[0],
            filetypes=ftypes + [("All files", "*.*")])
        if not out_path:
            return

        self._status(f"Exporting {os.path.basename(lng_path)}…")
        self._log(f"\nExport {os.path.basename(lng_path)} → {fmt.upper()}", "hd")

        def worker():
            try:
                raw = open(lng_path, "rb").read()
                pairs = parse_lng(raw)
                self._log(f"  parsed {len(pairs):,} key-value pairs", "dim")
                if fmt == "json":
                    export_lng_to_json(pairs, out_path)
                else:
                    export_lng_to_csv(pairs, out_path)
                msg = f"Exported {len(pairs):,} entries → {out_path}"
                self._log("✓ " + msg, "ok")
                self.after(0, lambda: self._status(msg))
                self.after(0, lambda: messagebox.showinfo("Done", msg))
            except Exception as ex:
                traceback.print_exc()
                self._log(f"✗ {ex}", "err")
                self.after(0, lambda e=ex: messagebox.showerror("Error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _import_lng(self, fmt: str):
        """Import JSON/CSV and rebuild a .lng file."""
        # 1. Select the JSON/CSV source
        ftypes = [("JSON", "*.json")] if fmt == "json" else [("CSV", "*.csv")]
        src_path = filedialog.askopenfilename(
            title=f"Select {fmt.upper()} file to import",
            filetypes=ftypes + [("All files", "*.*")])
        if not src_path:
            return

        # 2. Select the original .lng file (provides header + char table)
        lng_path = filedialog.askopenfilename(
            title="Select original .lng file (header + charset reference)",
            filetypes=[("Lng files", "*.lng"), ("All files", "*.*")])
        if not lng_path:
            return

        # 3. Select output path
        out_path = filedialog.asksaveasfilename(
            title="Save rebuilt .lng as",
            defaultextension=".lng",
            initialfile=os.path.splitext(os.path.basename(lng_path))[0] + "_mod",
            filetypes=[("Lng files", "*.lng"), ("All files", "*.*")])
        if not out_path:
            return

        self._status(f"Importing {fmt.upper()} → .lng…")
        self._log(f"\nImport {fmt.upper()} → .lng", "hd")

        def worker():
            try:
                # Read original .lng
                original = open(lng_path, "rb").read()
                self._log(f"  original .lng: {_fmt(len(original))}", "dim")

                # Read JSON/CSV updates
                if fmt == "json":
                    updates = import_lng_from_json(src_path)
                else:
                    updates = import_lng_from_csv(src_path)
                self._log(f"  loaded {len(updates):,} entries from {fmt.upper()}", "dim")

                # Rebuild
                new_data = build_lng(original, updates)
                open(out_path, "wb").write(new_data)

                msg = f"Rebuilt .lng: {len(updates):,} entries → {out_path}  ({_fmt(len(new_data))})"
                self._log("✓ " + msg, "ok")
                self.after(0, lambda: self._status(msg))
                self.after(0, lambda: messagebox.showinfo("Done", msg))
            except Exception as ex:
                traceback.print_exc()
                self._log(f"✗ {ex}", "err")
                self.after(0, lambda e=ex: messagebox.showerror("Error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    # ── Texture export / import ───────────────────────────────────────────────
    def _ask_export_format(self) -> str | None:
        """Show a 4-button dialog and return the chosen format key, or None."""
        result = [None]
        dlg = ctk.CTkToplevel(self)
        dlg.title("Export Format")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("420x160")
        ctk.CTkLabel(dlg, text="Export format", font=HEAD,
                     text_color="#e2e4e8").pack(pady=(16, 8))
        frm = ctk.CTkFrame(dlg, fg_color="transparent")
        frm.pack(padx=16, pady=4)
        def pick(fmt):
            result[0] = fmt
            dlg.destroy()
        for label, key in [("PNG", "png"), ("DDS (BC7)", "dds"),
                           ("Legacy DDS", "legacy_dds"), ("TGA", "tga")]:
            ctk.CTkButton(frm, text=label, width=90,
                          command=lambda k=key: pick(k)).pack(side="left", padx=4)
        ctk.CTkButton(dlg, text="Cancel", width=80, fg_color="#3a3f4b",
                      hover_color="#4a4f5b",
                      command=dlg.destroy).pack(pady=(8, 12))
        dlg.wait_window()
        return result[0]

    def _export_texture(self):
        """Export a .512/.1024/.2048 texture file to PNG / DDS / TGA."""
        if not TEXDEC_OK:
            messagebox.showerror("Missing", "pip install texture2ddecoder"); return
        if not PIL_OK:
            messagebox.showerror("Missing", "pip install Pillow"); return

        path = filedialog.askopenfilename(
            title="Select .512 / .1024 / .2048 texture file",
            filetypes=[("Metro textures", "*.512 *.1024 *.2048"), ("All files", "*.*")])
        if not path:
            return

        ext = os.path.splitext(path)[1].lower()
        dim_map = {".512": (512, 10), ".1024": (1024, 1), ".2048": (2048, 1)}
        if ext not in dim_map:
            messagebox.showerror("Error", f"Unknown extension: {ext}"); return
        dimension, num_mips = dim_map[ext]

        # Ask format
        fmt = self._ask_export_format()
        if not fmt:
            return

        fmt_ext = {"png": ".png", "dds": ".dds", "legacy_dds": ".dds", "tga": ".tga"}
        fmt_name = {"png": "PNG", "dds": "DDS (BC7)", "legacy_dds": "Legacy DDS", "tga": "TGA"}
        out_path = filedialog.asksaveasfilename(
            title=f"Save as {fmt_name[fmt]}",
            defaultextension=fmt_ext[fmt],
            initialfile=os.path.splitext(os.path.basename(path))[0],
            filetypes=[(fmt_name[fmt], f"*{fmt_ext[fmt]}"), ("All files", "*.*")])
        if not out_path:
            return

        self._status(f"Exporting {os.path.basename(path)}…")
        self._log(f"\nExport texture {os.path.basename(path)} → {fmt_name[fmt]}", "hd")

        def worker():
            try:
                with open(path, "rb") as _f:
                    data = _f.read()
                self._log(f"  file: {_fmt(len(data))}", "dim")
                bc_data, tex_fmt_det, actual_dim = parse_texture_512(data, dimension, num_mips)
                if bc_data is None:
                    raise ValueError(
                        "Cannot read texture — not a recognised Metro BC7/BC4/BC1 or LZ4 file\n"
                        f"  file size {len(data)} B does not match any known format "
                        f"for {dimension}×{dimension}")
                fmt_label = tex_fmt_det.upper()
                self._log(
                    f"  {fmt_label} data: {_fmt(len(bc_data))}  ({actual_dim}×{actual_dim})",
                    "dim")
                # For PC font .512 → 1024×1024 BC4: only 1 top mip in the file
                actual_num_mips = 1 if actual_dim != dimension else num_mips
                if fmt == "png":
                    export_texture_to_png(bc_data, actual_dim, out_path, tex_fmt_det)
                elif fmt == "dds":
                    export_texture_to_dds(bc_data, actual_dim, actual_num_mips,
                                          out_path, tex_fmt_det)
                elif fmt == "legacy_dds":
                    export_texture_to_legacy_dds(bc_data, actual_dim, out_path, tex_fmt_det)
                elif fmt == "tga":
                    export_texture_to_tga(bc_data, actual_dim, out_path, tex_fmt_det)
                msg = f"Exported {actual_dim}×{actual_dim} {fmt_label} → {out_path}"
                self._log("✓ " + msg, "ok")
                self.after(0, lambda: self._status(msg))
                self.after(0, lambda: messagebox.showinfo("Done", msg))
            except Exception as ex:
                traceback.print_exc()
                self._log(f"✗ {ex}", "err")
                self.after(0, lambda e=ex: messagebox.showerror("Error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _import_texture(self):
        """Import a PNG and rebuild a .512 texture file."""
        if not PIL_OK:
            messagebox.showerror("Missing", "pip install Pillow"); return
        if not LZ4_OK:
            messagebox.showerror("Missing", "pip install lz4"); return

        png_path = filedialog.askopenfilename(
            title="Select PNG image",
            filetypes=[("PNG", "*.png"), ("All files", "*.*")])
        if not png_path:
            return

        # Ask dimension
        dim = messagebox.askquestion("Dimension", "512×512 ?\n\nYes = 512, No = 1024",
                                     icon="question")
        dimension = 512 if dim == "yes" else 1024

        out_path = filedialog.asksaveasfilename(
            title="Save as .512 texture",
            defaultextension=".512",
            initialfile=os.path.splitext(os.path.basename(png_path))[0],
            filetypes=[("Metro texture", "*.512"), ("All files", "*.*")])
        if not out_path:
            return

        self._status(f"Importing {os.path.basename(png_path)}…")
        self._log(f"\nImport PNG → .{dimension} texture", "hd")

        def worker():
            try:
                img = _Img.open(png_path).convert("RGBA")
                if img.size != (dimension, dimension):
                    img = img.resize((dimension, dimension), _Img.NEAREST)
                rgba = img.tobytes()
                self._log(f"  PNG: {img.size[0]}×{img.size[1]}", "dim")
                new_data = build_texture_512(rgba, dimension)
                open(out_path, "wb").write(new_data)
                msg = f"Built .{dimension} texture: {_fmt(len(new_data))} → {out_path}"
                self._log("✓ " + msg, "ok")
                self.after(0, lambda: self._status(msg))
                self.after(0, lambda: messagebox.showinfo("Done", msg))
            except Exception as ex:
                traceback.print_exc()
                self._log(f"✗ {ex}", "err")
                self.after(0, lambda e=ex: messagebox.showerror("Error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    # ── Tab: Mod Builder ──────────────────────────────────────────────────────
    def _build_mod_tab(self):
        f = self.tabview.tab("Mod Builder")

        # Header
        ctk.CTkLabel(f, text="Build a mod archive", font=HEAD,
                     text_color="#e2e4e8", anchor="w").pack(fill="x", padx=14, pady=(10, 2))
        ctk.CTkLabel(f, text="content_mod.vfs0 + patched content.vfx",
                     font=MONO, text_color="#636876", anchor="w").pack(fill="x", padx=14)
        ctk.CTkLabel(f, text="Place modified files in a folder keeping the same "
                     "directory structure as the game.",
                     font=UI, text_color="#636876", anchor="w").pack(fill="x", padx=14, pady=(0, 6))

        # Fields card
        card = ctk.CTkFrame(f, fg_color="#1a1d24", corner_radius=8)
        card.pack(fill="x", padx=14, pady=(0, 6))
        self._game_dir_var = ctk.StringVar()
        self._mod_dir_var  = ctk.StringVar()
        self._out_dir_var  = ctk.StringVar()
        self._arc_name_var = ctk.StringVar(value="content_mod.vfs0")

        def _field(parent, label, var, browse_cmd=None):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=4, padx=10)
            ctk.CTkLabel(row, text=label, width=120, anchor="w", font=UI,
                         text_color="#636876").pack(side="left")
            ctk.CTkEntry(row, textvariable=var, width=380).pack(side="left", padx=(0, 6))
            if browse_cmd:
                ctk.CTkButton(row, text="Browse", width=70,
                              command=browse_cmd).pack(side="left")

        _field(card, "Game folder", self._game_dir_var,
               browse_cmd=lambda: self._game_dir_var.set(
                   filedialog.askdirectory() or self._game_dir_var.get()))
        _field(card, "Mod files", self._mod_dir_var,
               browse_cmd=lambda: self._mod_dir_var.set(
                   filedialog.askdirectory() or self._mod_dir_var.get()))
        _field(card, "Output folder", self._out_dir_var,
               browse_cmd=lambda: self._out_dir_var.set(
                   filedialog.askdirectory() or self._out_dir_var.get()))
        _field(card, "Archive name", self._arc_name_var)

        # Scan
        scan_row = ctk.CTkFrame(f, fg_color="transparent")
        scan_row.pack(fill="x", padx=14, pady=4)
        ctk.CTkButton(scan_row, text="Scan mod files", width=120,
                      command=self._scan_mod).pack(side="left")
        self._scan_lbl = ctk.CTkLabel(scan_row, text="", font=MONO, text_color="#636876")
        self._scan_lbl.pack(side="left", padx=12)

        # Mod file tree
        tree_frame = ctk.CTkFrame(f, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self._mod_tree = ttk.Treeview(tree_frame, columns=("game_path", "status", "size"),
                                      show="headings", height=6, style="Dark.Treeview")
        for c, h, w, a in [("game_path", "Game Path", 540, "w"),
                            ("status", "Match", 90, "center"),
                            ("size", "Size", 80, "e")]:
            self._mod_tree.heading(c, text=h)
            self._mod_tree.column(c, width=w, anchor=a)
        self._mod_tree.pack(fill="both", expand=True)

        # Build
        bb = ctk.CTkFrame(f, fg_color="transparent")
        bb.pack(fill="x", padx=14, pady=6)
        ctk.CTkButton(bb, text="Build Mod", width=120,
                      command=self._build_mod).pack(side="left")
        ctk.CTkLabel(bb, text="Creates .vfs0 and patched .vfx in output folder",
                     font=MONO, text_color="#636876").pack(side="left", padx=12)

        self._mod_prog = ctk.CTkProgressBar(f, height=4)
        self._mod_prog.pack(fill="x", padx=14, pady=(0, 4))
        self._mod_prog.set(0)

    def _pulse_prog(self):
        if not getattr(self, "_prog_running", False):
            return
        v = self._mod_prog.get()
        self._mod_prog.set(0.0 if v >= 0.95 else v + 0.05)
        self.after(100, self._pulse_prog)

    def _stop_prog(self):
        self._prog_running = False
        self._mod_prog.set(1.0)

    def _browse_vfx_for_mod(self):
        p = filedialog.askopenfilename(
            title="Select content.vfx",
            filetypes=[("VFX catalogue", "*.vfx"), ("All files", "*.*")])
        if p:
            self._game_dir_var.set(os.path.dirname(p))

    def _resolve_game_dir(self) -> str | None:
        """Return the game folder from the field (accepts folder or .vfx file path)."""
        val = self._game_dir_var.get().strip()
        if not val:
            return None
        if val.lower().endswith(".vfx") and os.path.isfile(val):
            return os.path.dirname(val)
        if os.path.isdir(val):
            return val
        return None

    def _scan_mod(self):
        mod_dir  = self._mod_dir_var.get().strip()
        if not mod_dir or not os.path.isdir(mod_dir):
            messagebox.showerror("Missing", "Select a valid mod files folder."); return

        if not self._vfx:
            game_dir = self._resolve_game_dir()
            if not game_dir:
                messagebox.showerror("Missing",
                    "Specify a game folder or select a content.vfx file."); return
            self._load_game(game_dir)
            messagebox.showinfo("Wait", "Loading VFX… try Scan again in a moment.")
            return

        self._mod_files = []  # list of (game_path, local_path, matched_idx or None)
        for root, _, files in os.walk(mod_dir):
            for fn in files:
                local = os.path.join(root, fn)
                rel   = os.path.relpath(local, mod_dir).replace("\\", "/").lower()
                idx   = self._pmap_full.get(rel)   # match against ALL files
                self._mod_files.append((rel, local, idx))

        self._mod_tree.delete(*self._mod_tree.get_children())
        matched = 0
        for rel, local, idx in self._mod_files:
            sz  = os.path.getsize(local)
            hit = idx is not None
            if hit: matched += 1
            self._mod_tree.insert("", "end",
                                  values=(rel, "✓ found" if hit else "✗ no match",
                                          _fmt(sz)),
                                  tags=("ok",) if hit else ("miss",))
        self._mod_tree.tag_configure("ok",   foreground="#6cc87a")
        self._mod_tree.tag_configure("miss", foreground="#e06060")
        self._scan_lbl.configure(
            text=f"{len(self._mod_files)} files · {matched} matched in VFX")

    def _build_mod(self):
        game_dir = self._game_dir_var.get().strip()
        out_dir  = self._out_dir_var.get().strip()
        arc_name = self._arc_name_var.get().strip() or "content_mod.vfs0"

        if not hasattr(self, "_mod_files") or not self._mod_files:
            self._scan_mod()
            if not hasattr(self, "_mod_files") or not self._mod_files:
                return

        matched = [(rel, loc, idx) for rel, loc, idx in self._mod_files
                   if idx is not None]
        if not matched:
            messagebox.showerror("No matches",
                                 "None of the mod files were found in the VFX directory."); return
        if not out_dir:
            messagebox.showerror("Missing", "Select an output folder."); return
        if not self._vfx:
            messagebox.showerror("Missing", "Load the game VFX first."); return
        if not LZ4_OK:
            messagebox.showerror("Missing", "pip install lz4  is required for extraction."); return

        os.makedirs(out_dir, exist_ok=True)
        self._prog_running = True
        self._pulse_prog()
        self._log(f"\n{'─'*56}", "dim")
        self._log(f"Building mod: {arc_name}  ({len(matched)} files)", "hd")
        self.tabview.set("Log")

        def worker():
            try:
                vfs_out  = os.path.join(out_dir, arc_name)
                vfx_out  = os.path.join(out_dir, "content.vfx")
                new_arc_id = self._vfx["num_archives"]  # 0-based index of new archive

                # Build list of (game_path, file_bytes)
                files_to_pack = []
                for rel, loc, idx in matched:
                    data = open(loc, "rb").read()
                    files_to_pack.append((rel, data))
                    self._log(f"  packing {rel}  ({_fmt(len(data))})", "dim")

                # Create VFS0
                self._log("Creating VFS0…", "ok")
                entries_info = create_vfs0(files_to_pack, vfs_out, self._vfx["guid"])
                arc_len = os.path.getsize(vfs_out)
                self._log(f"  → {arc_name}  ({_fmt(arc_len)})", "ok")

                # Build update map for VFX
                updates = {}
                path_to_entry = {r: (l, i) for r, l, i in matched}
                for ei, (rel, data) in zip(entries_info, files_to_pack):
                    _, _, idx = path_to_entry[rel]
                    updates[idx] = {
                        "arc_id": new_arc_id,
                        "offset": ei["offset"],
                        "comp":   ei["comp"],
                        "decomp": ei["decomp"],
                    }

                # Write patched VFX
                self._log("Patching content.vfx…", "ok")
                new_arc = {"fname": arc_name, "arc_len": arc_len}
                vfx_bytes = write_vfx(self._vfx, new_arc, updates)
                open(vfx_out, "wb").write(vfx_bytes)
                self._log(f"  → content.vfx  ({_fmt(len(vfx_bytes))})", "ok")

                msg = f"Mod built: {len(matched)} files  →  {out_dir}"
                self._log("✓ " + msg, "ok")
                self.after(0, lambda: self._status(msg))
                self.after(0, lambda: messagebox.showinfo("Done", msg))
            except Exception as ex:
                traceback.print_exc()
                self._log(f"✗ {ex}", "err")
                self.after(0, lambda e=ex: messagebox.showerror("Error", str(e)))
            finally:
                self.after(0, self._stop_prog)

        threading.Thread(target=worker, daemon=True).start()

    # ── Tab: Log ──────────────────────────────────────────────────────────────
    def _build_log_tab(self):
        f = self.tabview.tab("Log")
        tb = ctk.CTkFrame(f, fg_color="#1a1d24", corner_radius=6)
        tb.pack(fill="x", padx=6, pady=(6, 4))
        ctk.CTkButton(tb, text="Clear", width=70,
                      command=self._clear_log).pack(side="left", padx=10, pady=6)
        # Use standard tk.Text for colored log output (CTkTextbox lacks tag support)
        log_frame = ctk.CTkFrame(f, fg_color="#12141a", corner_radius=8)
        log_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._log_w = tk.Text(log_frame, bg="#12141a", fg="#e2e4e8",
                              insertbackground="#e2e4e8", font=MONO,
                              relief="flat", wrap="word", state="disabled",
                              padx=8, pady=6, borderwidth=0,
                              selectbackground="#1e3a5f", selectforeground="#a8c4ff")
        log_sb = ctk.CTkScrollbar(log_frame, command=self._log_w.yview)
        self._log_w.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self._log_w.pack(fill="both", expand=True)
        for tag, col in [("ok", "#6cc87a"), ("err", "#e06060"),
                         ("hd", "#e2e4e8"), ("dim", "#636876")]:
            self._log_w.tag_configure(tag, foreground=col)

    def _log(self, msg: str, tag: str | None = None):
        self._logq.put((msg, tag))

    def _poll_log(self):
        try:
            while True:
                msg, tag = self._logq.get_nowait()
                self._log_w.configure(state="normal")
                self._log_w.insert("end", msg + "\n", tag or "")
                self._log_w.see("end")
                self._log_w.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._poll_log)

    def _clear_log(self):
        self._log_w.configure(state="normal")
        self._log_w.delete("1.0", "end")
        self._log_w.configure(state="disabled")

    def _log_startup(self):
        self._log("Metro Redux — Mod Tools", "hd")
        self._log("  Supports: Metro 2033 Redux · Metro Last Light Redux", "dim")
        self._log(f"  LZ4: {'available ✓' if LZ4_OK else 'NOT installed  (pip install lz4)  ✗'}")
        self._log("")

    def _status(self, msg: str):
        self._stvar.set(msg)


# ── Global exception handler (prints to terminal) ────────────────────────────
def _handle_exception(exc, val, tb):
    traceback.print_exception(exc, val, tb)
    print(f"\n[FATAL] {exc.__name__}: {val}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ctk.CTk.report_callback_exception = _handle_exception
    App().mainloop()
