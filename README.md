# Metro Redux Tools

Mod tools for **Metro 2033 Redux** & **Metro Last Light Redux** (PC / PS4) — VFX/VFS0/UPK parser, .lng localization editor, texture converter, mod builder.

เครื่องมือ mod สำหรับ **Metro 2033 Redux** & **Metro Last Light Redux** (PC / PS4) — ตัวแยกไฟล์ VFX/VFS0/UPK, ตัวแก้ไขไฟล์ .lng, ตัวแปลง texture, ตัวสร้าง mod

---

## Features / ความสามารถ

| Feature | Description |
|---------|-------------|
| **VFX Index** | Open `content.vfx` — view entries, level mapping, file size, GUID |
| **UPK Archive** | Open `.upk9` — view file list (XOR-decrypted), filter, extract |
| **VFS0 Archive** | Read VFS0 data archives, extract GUID and data size |
| **Mod Builder** | Create VFS0 archives with VFX index for modding |
| **.lng Localization** | Export/import `.lng` files to JSON/CSV with full Unicode support |
| **Texture Export** | Export `.512`/`.1024`/`.2048` textures to **PNG**, **DDS (BC7)**, **Legacy DDS (RGBA)**, **TGA** |
| **Texture Import** | Import PNG → rebuild `.512` with BC7 + LZ4 compression |
| **QuickBMS Integration** | Auto-detect and run QuickBMS for compressed extraction |
| **PS4 Support** | Supports both PC and PS4 platform file structures |
| **Modern UI** | CustomTkinter dark theme with Verdana font, tabbed interface |

| Feature | คำอธิบาย |
|---------|----------|
| **VFX Index** | เปิด `content.vfx` — ดู entries, level mapping, file size, GUID |
| **UPK Archive** | เปิด `.upk9` — ดู file list (XOR-decrypt อัตโนมัติ), filter, extract |
| **VFS0 Archive** | อ่าน VFS0 data archives, ดึง GUID และ data size |
| **Mod Builder** | สร้าง VFS0 archives พร้อม VFX index สำหรับ modding |
| **.lng Localization** | Export/Import ไฟล์ `.lng` เป็น JSON/CSV รองรับ Unicode เต็มรูปแบบ |
| **Texture Export** | Export ไฟล์ `.512`/`.1024`/`.2048` เป็น **PNG**, **DDS (BC7)**, **Legacy DDS (RGBA)**, **TGA** |
| **Texture Import** | Import PNG → สร้าง `.512` ใหม่ด้วย BC7 + LZ4 compression |
| **QuickBMS Integration** | ตรวจจับและรัน QuickBMS อัตโนมัติสำหรับไฟล์ที่ถูกบีบอัด |
| **PS4 Support** | รองรับโครงสร้างไฟล์ทั้ง PC และ PS4 |
| **Modern UI** | CustomTkinter dark theme พร้อม Verdana font, แท็บอินเทอร์เฟซ |

---

## Quick Start / เริ่มต้นใช้งาน

```bash
# Requires Python 3.9+ (tested with 3.11, 3.13, 3.14)
python metro_redux_tools.py

# Required dependencies
pip install customtkinter lz4

# Optional: texture export/import support
pip install texture2ddecoder Pillow
```

---

## .lng Localization Format

### Encoding Scheme / รูปแบบการเข้ารหัส

The `.lng` files use a **character substitution table** with **variable-length encoding**:

ไฟล์ `.lng` ใช้**ตารางแทนที่ตัวอักษร** แบบ**เข้ารหัสความยาวแปรผัน**:

```
Header: 6 × uint32 (24 bytes)
Char table: N × uint16 (UTF-16LE, null-terminated)
String data: alternating null-terminated (ASCII key, encoded value)
```

### Byte Encoding / การเข้ารหัสไบต์

| Byte Range | Encoding | Description |
|------------|----------|-------------|
| `0x01` | — | Newline character / ขึ้นบรรทัดใหม่ |
| `0x02` – `0xDF` | 1 byte | `char_table[byte - 2]` |
| `0xE0` – `0xFF` | 2 bytes | `char_table[(0xDF + second_byte) - 2]` |

### Language Impact / ผลกระทบต่อภาษา

The character table has **294 entries** arranged by frequency:

ตารางตัวอักษรมี **294 ตำแหน่ง** เรียงตามความถี่:

| Index Range | Languages | Encoding |
|-------------|-----------|----------|
| 0 – 221 | English, Latin, accented chars, Cyrillic, Japanese (basic) | 1 byte |
| 222 – 270 | **Thai** (all Thai characters) | 2 bytes |
| 271 – 293 | **Japanese** (katakana, kanji) | 2 bytes |

**Key insight**: English text uses 1 byte per character. Thai and Japanese text uses 2 bytes per character (for byte values >= 0xE0). This means:

**ข้อมูลสำคัญ**: ภาษาอังกฤษใช้ 1 ไบต์ต่อตัวอักษร ภาษาไทยและญี่ปุ่นใช้ 2 ไบต์ต่อตัวอักษร (สำหรับค่าไบต์ >= 0xE0) ซึ่งหมายความว่า:

- English-only strings decode correctly with simple 1-byte lookup / สตริงภาษาอังกฤษถอดรหัสด้วย 1 ไบต์ได้ถูกต้อง
- Thai/Japanese strings **require** the 2-byte decoding logic / สตริงภาษาไทย/ญี่ปุ่น **ต้องใช้** logic ถอดรหัส 2 ไบต์
- Mixed-language strings (e.g., Thai + English) use both encodings in the same value / สตริงผสมภาษาใช้ทั้ง 2 รูปแบบในค่าเดียวกัน

---

## File Formats / รูปแบบไฟล์

### `content.vfx` — Master Index

```
Header: 32 bytes
  [0x00] uint32  version = 1
  [0x04] uint32  unused = 1
  [0x08] bytes16 GUID
  [0x10] uint32  count
  [0x14] uint32  extra

Entry (repeating):
  uint32   crc32
  cstring  vfs_file     (e.g. "content00.vfs0")
  uint32   level_count
  cstring  level[N]     (e.g. "2033\l00_intro")
```

### `contentNN.vfs0` — Data Archive

```
Header: 8 bytes
  [0x00] uint32  = 0 (padding)
  [0x04] uint32  data_size

Body: data_size bytes

Footer: 16 bytes (GUID — matches .vfx)
```

### `.upk9` — UPK Archive

```
Section 1 — Data block:
  uint32  num0
  uint32  data_size
  bytes   raw data

Section 2 — File list:
  uint32  num1
  uint32  list_size
  Entry (repeating):
    byte    xor_key
    byte[3] padding
    uint32  offset
    uint32  size_unpacked
    uint32  size_packed
    uint32  name_len
    bytes   name_encrypted (XOR with xor_key)
    byte    null
```

---

## File Structure / โครงสร้างไฟล์

```
Metro Redux Tools/
  metro_redux_tools.py  ← main tool (single file)
  README.md             ← this file
```

---

## Credits / เครดิต

### Tools & Libraries

| Source | Usage | License |
|--------|-------|---------|
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | Modern dark-themed GUI framework | MIT |
| [QuickBMS](https://aluigi.altervista.org/quickbms.htm) | BMS script execution for compressed file extraction | Freeware |
| [MetroEX](https://github.com/ShokerStlk/MetroEX) | Reference for Metro file formats, LZ4 compression, texture export (DDS/TGA/PNG) | MIT |
| [lz4](https://github.com/python-lz4/python-lz4) | LZ4 block compression/decompression for VFS0 archives | BSD |
| [texture2ddecoder](https://github.com/TomSchimansky/texture2ddecoder) | BC7/BC3/BC1 texture decoding for GPU-compressed textures | MIT |
| [Pillow](https://python-pillow.org/) | PNG image encoding/decoding | MIT-CMU |

### BMS Scripts

| Script | Author | Version |
|--------|--------|---------|
| `vfs_unpack.bms` | hhrhhr@gmail.com | v1.3, 2010 |
| `unpack_upk.bms` | hhrhhr@gmail.com | v1.3, 2010 |
| `make_upk.bms` | hhrhhr@gmail.com | v1.3, 2010 |
| `1_file_unpack.bms` | hhrhhr@gmail.com | v1.3, 2010 |

### Reference Projects

| Project | What we used |
|---------|-------------|
| [MetroEX](https://github.com/ShokerStlk/MetroEX) | VFX file format, LZ4 DecompressBlob/DecompressStream, texture export (DDS DX10/DX9, TGA, PNG), BC7 handling |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | Modern Python GUI framework with dark mode, rounded widgets, tabbed interface |
| [OpenXRay](https://github.com/OpenXRay/xray-16) | General X-Ray engine file format understanding |

### .lng Format

The `.lng` file format was reverse-engineered from the binary data. The variable-length encoding scheme (1-byte for indices < 222, 2-byte for indices >= 222) was discovered through systematic analysis of the character substitution table.

รูปแบบไฟล์ `.lng` ถูกถอดรหัสจากข้อมูลไบนารี รูปแบบการเข้ารหัสความยาวแปรผัน (1 ไบต์สำหรับ index < 222, 2 ไบต์สำหรับ index >= 222) ถูกค้นพบผ่านการวิเคราะห์ตารางแทนที่ตัวอักษรอย่างเป็นระบบ

---

## License / สัญญาอนุญาต

MIT License
