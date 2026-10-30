#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mflac2mp3.py — QQ 音乐加密音频转 MP3 工具（单文件、无第三方依赖）

支持格式:
  - 普通 OGG / FLAC 音频 (直接转码, 无需 QQ 音乐登录)
  - QMCv1 静态加密 (.qmc0/.qmc3/.bkcmp3/.bkcflac 等, 旧文件)
  - QMCv2 内嵌 ekey (.mflac/.mgg 带 QTag 或 ekey 尾部, 旧版客户端下载)
  - QMCv2 musicex 尾部 (新版 PC 客户端 >= 19.57 下载, 文件内无 ekey,
    自动从本机运行中的 QQ 音乐客户端提取登录凭据并调用
    music.vkey.GetEVkey 接口获取 ekey)

解密后用 ffmpeg 转码为 MP3 (默认 320kbps CBR, 保留标签与封面)。

用法:
  python mflac2mp3.py <文件或目录...> [选项]
  python mflac2mp3.py --self-test

仅供个人将已购买/会员有效期内下载的音乐做格式转换, 请勿用于传播。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path


class UnsupportedFormat(Exception):
    pass


class OperationCancelled(Exception):
    """用户取消操作。"""


def app_dir() -> Path:
    """工具所在目录 (PyInstaller 打包后取 exe 所在目录)。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

# ============================================================
#  腾讯 TEA (TC-TEA) 与 ekey 密钥推导
# ============================================================

class TeaCipher:
    """标准 TEA 分组密码 (big-endian u32, 32 轮)。"""

    DELTA = 0x9E3779B9
    MASK = 0xFFFFFFFF

    def __init__(self, key: bytes, rounds: int = 32):
        if len(key) != 16:
            raise ValueError("TEA key 必须为 16 字节")
        if rounds % 2 != 0:
            raise ValueError("TEA 轮数必须为偶数")
        self.k0, self.k1, self.k2, self.k3 = struct.unpack(">IIII", key)
        self.rounds = rounds

    def decrypt_block(self, block: bytes) -> bytes:
        v0, v1 = struct.unpack(">II", block)
        M = self.MASK
        s = (self.DELTA * (self.rounds // 2)) & M
        for _ in range(self.rounds // 2):
            v1 = (v1 - (((((v0 << 4) & M) + self.k2) & M) ^ ((v0 + s) & M) ^ ((((v0 >> 5)) + self.k3) & M))) & M
            v0 = (v0 - (((((v1 << 4) & M) + self.k0) & M) ^ ((v1 + s) & M) ^ ((((v1 >> 5)) + self.k1) & M))) & M
            s = (s - self.DELTA) & M
        return struct.pack(">II", v0, v1)

    def encrypt_block(self, block: bytes) -> bytes:
        v0, v1 = struct.unpack(">II", block)
        M = self.MASK
        s = 0
        for _ in range(self.rounds // 2):
            s = (s + self.DELTA) & M
            v0 = (v0 + (((((v1 << 4) & M) + self.k0) & M) ^ ((v1 + s) & M) ^ ((((v1 >> 5)) + self.k1) & M))) & M
            v1 = (v1 + (((((v0 << 4) & M) + self.k2) & M) ^ ((v0 + s) & M) ^ ((((v0 >> 5)) + self.k3) & M))) & M
        return struct.pack(">II", v0, v1)


TC_SALT_LEN = 2
TC_ZERO_LEN = 7


def tc_tea_decrypt(in_buf: bytes, key: bytes) -> bytes:
    """QQ TEA CBC 解密。

    密文块链: p0 = D(c0); p_i = D(p_{i-1} ^ c_i) ^ c_{i-1}
    明文帧: [PadLen(1)][Pad(0-7)][Salt(2)][Body][Zero(7)]
    """
    n = len(in_buf)
    if n % 8 != 0:
        raise ValueError("TEA 密文长度不是 8 的倍数")
    if n < 16:
        raise ValueError("TEA 密文过短")
    blk = TeaCipher(key, 32)
    tmp = bytearray(blk.decrypt_block(in_buf[0:8]))
    pad_len = tmp[0] & 0x07
    out_len = n - 1 - pad_len - TC_SALT_LEN - TC_ZERO_LEN
    if out_len < 0:
        raise ValueError("TEA 帧长度异常")
    out = bytearray(out_len)

    iv_prev = bytes(8)
    iv_cur = in_buf[0:8]
    pos = 8
    idx = 1 + pad_len

    def crypt_block():
        nonlocal iv_prev, iv_cur, pos, idx
        iv_prev = iv_cur
        iv_cur = in_buf[pos:pos + 8]
        for j in range(8):
            tmp[j] ^= iv_cur[j]
        tmp[:] = blk.decrypt_block(bytes(tmp))
        pos += 8
        idx = 0

    i = 0
    while i < TC_SALT_LEN:
        if idx < 8:
            idx += 1
            i += 1
        else:
            crypt_block()

    o = 0
    while o < out_len:
        if idx < 8:
            out[o] = tmp[idx] ^ iv_prev[idx]
            o += 1
            idx += 1
        else:
            crypt_block()

    for _ in range(TC_ZERO_LEN):
        if idx >= 8:
            raise ValueError("TEA 帧尾校验失败")
        if tmp[idx] != iv_prev[idx]:
            raise ValueError("TEA zero 校验失败 (密钥错误或数据损坏)")
        idx += 1
    return bytes(out)


def tc_tea_encrypt(body: bytes, key: bytes) -> bytes:
    """QQ TEA CBC 加密 (仅用于自检与构造测试数据)。"""
    blk = TeaCipher(key, 32)
    pad_len = (8 - (10 + len(body)) % 8) % 8
    plain = bytearray()
    plain.append(pad_len)
    plain += bytes(random.randrange(256) for _ in range(pad_len))   # pad
    plain += bytes(random.randrange(256) for _ in range(TC_SALT_LEN))
    plain += body
    plain += bytes(TC_ZERO_LEN)
    assert len(plain) % 8 == 0

    # 加密链 (解密链的逆): T_0 = P_0, c_0 = E(P_0);
    #   T_i = P_i ^ c_{i-1},  c_i = E(T_i) ^ T_{i-1}
    blocks = [bytes(plain[i:i + 8]) for i in range(0, len(plain), 8)]
    out = bytearray()
    t_prev = bytes(8)
    c_prev = bytes(8)
    for i, p in enumerate(blocks):
        if i == 0:
            c = blk.encrypt_block(p)
            t_prev = p
        else:
            x = bytes(a ^ b for a, b in zip(p, c_prev))
            c = bytes(a ^ b for a, b in zip(blk.encrypt_block(x), t_prev))
            t_prev = x
        out += c
        c_prev = c
    return bytes(out)


def simple_make_key(seed: int, size: int) -> bytes:
    """QQ simpleMakeKey: tan 种子生成密钥字节 (与 JS/浮点实现逐位一致)。"""
    out = bytearray(size)
    for i in range(size):
        t = math.tan(seed + i * 0.1)
        out[i] = int(abs(t) * 100.0) & 0xFF
    return bytes(out)


ENC_V2_PREFIX = b"QQMusic EncV2,Key:"
ENC_V2_STAGE1_KEY = b"386ZJY!@#*$%^&)("
ENC_V2_STAGE2_KEY = b"**#!(#$%&^a1cZ,T"


def derive_tea_key(ekey_header: bytes) -> bytes:
    sk = simple_make_key(106, 8)
    return bytes(b for pair in zip(sk, ekey_header) for b in pair)


def parse_ekey(ekey: str) -> bytes:
    """ekey (base64) → QMC2 原始密钥。支持 EncV2 双层 TEA 与 EncV1, 失败时回退为裸密钥。"""
    ekey = ekey.strip().strip("\x00")
    blob = base64.b64decode("".join(ekey.split()))

    if blob.startswith(ENC_V2_PREFIX):
        stage1 = tc_tea_decrypt(blob[len(ENC_V2_PREFIX):], ENC_V2_STAGE1_KEY)
        stage2 = tc_tea_decrypt(stage1, ENC_V2_STAGE2_KEY)
        blob = base64.b64decode(stage2)

    if len(blob) < 8:
        raise ValueError("ekey 解码后过短")

    header, body = blob[:8], blob[8:]
    if not body:
        return header
    try:
        return header + tc_tea_decrypt(body, derive_tea_key(header))
    except ValueError:
        # TEA 解密失败: 可能是 API 返回的裸密钥, 直接使用
        return blob


# ============================================================
#  QMC 密码器
# ============================================================

def _reduce_offset(off: int) -> int:
    return off if off <= 0x7FFF else off % 0x7FFF


def _xor_in_place(buf, mask: bytes) -> None:
    n = len(buf)
    x = int.from_bytes(buf, "little") ^ int.from_bytes(mask, "little")
    buf[:] = x.to_bytes(n, "little")


class StaticCipher:
    """QMCv1 静态密钥表加密 (旧格式)。"""

    BOX = bytes([
        0x77, 0x48, 0x32, 0x73, 0xDE, 0xF2, 0xC0, 0xC8, 0x95, 0xEC, 0x30, 0xB2, 0x51, 0xC3, 0xE1, 0xA0,
        0x9E, 0xE6, 0x9D, 0xCF, 0xFA, 0x7F, 0x14, 0xD1, 0xCE, 0xB8, 0xDC, 0xC3, 0x4A, 0x67, 0x93, 0xD6,
        0x28, 0xC2, 0x91, 0x70, 0xCA, 0x8D, 0xA2, 0xA4, 0xF0, 0x08, 0x61, 0x90, 0x7E, 0x6F, 0xA2, 0xE0,
        0xEB, 0xAE, 0x3E, 0xB6, 0x67, 0xC7, 0x92, 0xF4, 0x91, 0xB5, 0xF6, 0x6C, 0x5E, 0x84, 0x40, 0xF7,
        0xF3, 0x1B, 0x02, 0x7F, 0xD5, 0xAB, 0x41, 0x89, 0x28, 0xF4, 0x25, 0xCC, 0x52, 0x11, 0xAD, 0x43,
        0x68, 0xA6, 0x41, 0x8B, 0x84, 0xB5, 0xFF, 0x2C, 0x92, 0x4A, 0x26, 0xD8, 0x47, 0x6A, 0x7C, 0x95,
        0x61, 0xCC, 0xE6, 0xCB, 0xBB, 0x3F, 0x47, 0x58, 0x89, 0x75, 0xC3, 0x75, 0xA1, 0xD9, 0xAF, 0xCC,
        0x08, 0x73, 0x17, 0xDC, 0xAA, 0x9A, 0xA2, 0x16, 0x41, 0xD8, 0xA2, 0x06, 0xC6, 0x8B, 0xFC, 0x66,
        0x34, 0x9F, 0xCF, 0x18, 0x23, 0xA0, 0x0A, 0x74, 0xE7, 0x2B, 0x27, 0x70, 0x92, 0xE9, 0xAF, 0x37,
        0xE6, 0x8C, 0xA7, 0xBC, 0x62, 0x65, 0x9C, 0xC2, 0x08, 0xC9, 0x88, 0xB3, 0xF3, 0x43, 0xAC, 0x74,
        0x2C, 0x0F, 0xD4, 0xAF, 0xA1, 0xC3, 0x01, 0x64, 0x95, 0x4E, 0x48, 0x9F, 0xF4, 0x35, 0x78, 0x95,
        0x7A, 0x39, 0xD6, 0x6A, 0xA0, 0x6D, 0x40, 0xE8, 0x4F, 0xA8, 0xEF, 0x11, 0x1D, 0xF3, 0x1B, 0x3F,
        0x3F, 0x07, 0xDD, 0x6F, 0x5B, 0x19, 0x30, 0x19, 0xFB, 0xEF, 0x0E, 0x37, 0xF0, 0x0E, 0xCD, 0x16,
        0x49, 0xFE, 0x53, 0x47, 0x13, 0x1A, 0xBD, 0xA4, 0xF1, 0x40, 0x19, 0x60, 0x0E, 0xED, 0x68, 0x09,
        0x06, 0x5F, 0x4D, 0xCF, 0x3D, 0x1A, 0xFE, 0x20, 0x77, 0xE4, 0xD9, 0xDA, 0xF9, 0xA4, 0x2B, 0x76,
        0x1C, 0x71, 0xDB, 0x00, 0xBC, 0xFD, 0x0C, 0x6C, 0xA5, 0x47, 0xF7, 0xF6, 0x00, 0x79, 0x4A, 0x11,
    ])

    def __init__(self):
        tbl = bytearray(0x8000)
        for r in range(0x8000):
            tbl[r] = self.BOX[(r * r + 27) & 0xFF]
        self._period0 = bytes(tbl)                  # 归约偏移 0..0x7FFF
        self._cycle = bytes(tbl[1:0x7FFF]) + tbl[:1]  # 之后循环的残差序列 1..0x7FFE,0

    def decrypt(self, buf, offset: int = 0) -> None:
        _xor_in_place(buf, self._build_mask(offset, len(buf)))

    def _build_mask(self, start: int, length: int) -> bytes:
        return _build_period_mask(self._period0, self._cycle, start, length)


class MapCipher:
    """QMCv2 Map 密码器 (密钥长度 <= 300)。"""

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("Map 密码器密钥为空")
        self.key = key
        n = len(key)
        tbl = bytearray(0x8000)
        for r in range(0x8000):
            idx = (r * r + 71214) % n
            v = key[idx]
            rot = (idx + 4) & 7
            tbl[r] = ((v << rot) | (v >> rot)) & 0xFF
        self._period0 = bytes(tbl)
        self._cycle = bytes(tbl[1:0x7FFF]) + tbl[:1]

    def get_mask(self, offset: int) -> int:
        r = _reduce_offset(offset)
        idx = (r * r + 71214) % len(self.key)
        v = self.key[idx]
        rot = (idx + 4) & 7
        return ((v << rot) | (v >> rot)) & 0xFF

    def decrypt(self, buf, offset: int = 0) -> None:
        _xor_in_place(buf, self._build_mask(offset, len(buf)))

    def _build_mask(self, start: int, length: int) -> bytes:
        return _build_period_mask(self._period0, self._cycle, start, length)


def _build_period_mask(period0: bytes, cycle: bytes, start: int, length: int) -> bytes:
    """按流位置构建 XOR 掩码: 位置 p<=0x7FFF 用原值, 之后按周期 0x7FFF 循环。"""
    parts = []
    p = start
    remain = length
    cl = len(cycle)
    while remain > 0:
        if p < 0x8000:
            seg = period0[p:p + remain]
        else:
            q = p % 0x7FFF
            entry = (cl - 1) if q == 0 else (q - 1)
            take = min(remain, cl - entry)
            seg = cycle[entry:entry + take]
        parts.append(seg)
        p += len(seg)
        remain -= len(seg)
    return b"".join(parts)


class RC4Cipher:
    """QMCv2 RC4 流密码 (密钥长度 > 300, 分段 0x80 / 0x1400)。"""

    FIRST_SEGMENT_SIZE = 0x80
    SEGMENT_SIZE = 5120

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("RC4 密码器密钥为空")
        self.key = key
        self.N = len(key)
        S = bytearray(self.N)
        for i in range(self.N):
            S[i] = i & 0xFF
        j = 0
        for i in range(self.N):
            j = (S[i] + j + key[i % self.N]) % self.N
            S[i], S[j] = S[j], S[i]
        self.S = S
        self.hash = 1
        for value in key:
            if value == 0:
                continue
            nxt = (self.hash * value) & 0xFFFFFFFF
            if nxt == 0 or nxt <= self.hash:
                break
            self.hash = nxt

    def decrypt(self, buf, offset: int = 0) -> None:
        mv = memoryview(buf)
        to_process = len(buf)
        processed = 0

        if offset < self.FIRST_SEGMENT_SIZE:
            seg_len = min(to_process, self.FIRST_SEGMENT_SIZE - offset)
            self._first_segment(mv[0:seg_len], offset)
            to_process -= seg_len
            processed += seg_len
            offset += seg_len
            if to_process == 0:
                return

        if offset % self.SEGMENT_SIZE != 0:
            seg_len = min(self.SEGMENT_SIZE - (offset % self.SEGMENT_SIZE), to_process)
            self._a_segment(mv[processed:processed + seg_len], offset)
            to_process -= seg_len
            processed += seg_len
            offset += seg_len
            if to_process == 0:
                return

        while to_process > self.SEGMENT_SIZE:
            self._a_segment(mv[processed:processed + self.SEGMENT_SIZE], offset)
            to_process -= self.SEGMENT_SIZE
            processed += self.SEGMENT_SIZE
            offset += self.SEGMENT_SIZE

        if to_process > 0:
            self._a_segment(mv[processed:], offset)

    def _first_segment(self, mv, offset: int) -> None:
        for i in range(len(mv)):
            mv[i] ^= self.key[self._segment_key(offset + i)]

    def _a_segment(self, mv, offset: int) -> None:
        S = self.S[:]
        N = self.N
        skip = (offset % self.SEGMENT_SIZE) + self._segment_key(offset // self.SEGMENT_SIZE)
        j = 0
        k = 0
        for i in range(-skip, len(mv)):
            j = (j + 1) % N
            k = (S[j] + k) % N
            S[k], S[j] = S[j], S[k]
            if i >= 0:
                mv[i] ^= S[(S[j] + S[k]) % N]

    def _segment_key(self, seg_id: int) -> int:
        seed = self.key[seg_id % self.N]
        if seed == 0:
            raise ValueError("RC4 密钥包含零字节种子")
        idx = math.floor(self.hash / ((seg_id + 1) * seed) * 100.0)
        return idx % self.N


def make_cipher(ekey: str | None):
    if ekey is None:
        return StaticCipher()
    key = parse_ekey(ekey)
    if len(key) > 300:
        return RC4Cipher(key)
    return MapCipher(key)


# ============================================================
#  文件尾部格式探测
# ============================================================

class Trailer:
    __slots__ = ("kind", "audio_size", "ekey", "song_id", "media_mid", "filename")

    def __init__(self, kind, audio_size, ekey=None, song_id=0, media_mid="", filename=""):
        self.kind = kind            # musicex / qtag / ekey / static
        self.audio_size = audio_size
        self.ekey = ekey
        self.song_id = song_id
        self.media_mid = media_mid
        self.filename = filename


def _read_utf16le(data: bytes, offset: int, max_len: int) -> str:
    chars = []
    end = min(offset + max_len, len(data))
    i = offset
    while i + 1 < end:
        code = data[i] | (data[i + 1] << 8)
        if code == 0:
            break
        chars.append(code)
        i += 2
    return "".join(chr(c) for c in chars)


def detect_trailer(data) -> Trailer:
    size = len(data)
    if size < 32:
        raise UnsupportedFormat("文件过小, 不是有效的加密音频")

    if data[-8:] == b"musicex\x00":
        version = struct.unpack("<I", data[-12:-8])[0]
        footer_size = struct.unpack("<I", data[-16:-12])[0]
        if version != 1 or not (16 < footer_size <= size):
            raise UnsupportedFormat(f"musicex 尾部无效 (version={version}, footer_size={footer_size})")
        meta = data[size - footer_size: size - 16]
        song_id = struct.unpack("<I", meta[0:4])[0] if len(meta) >= 4 else 0
        media_mid = _read_utf16le(meta, 0x0C, 60)
        filename = _read_utf16le(meta, 0x48, 68)
        if not media_mid or not filename:
            raise UnsupportedFormat("musicex 尾部缺少 media_mid / filename")
        return Trailer("musicex", size - footer_size, None, song_id, media_mid, filename)

    if data[-4:] == b"STag":
        raise UnsupportedFormat("STag 尾部 (Android 新版) 的 ekey 不在文件内且格式未公开, 暂不支持")

    if data[-4:] == b"QTag":
        key_size = struct.unpack(">I", data[-8:-4])[0]
        if not (0 < key_size <= size - 8):
            raise UnsupportedFormat(f"QTag 尾部 key_size 无效: {key_size}")
        audio_size = size - key_size - 8
        raw = data[audio_size: size - 8]
        comma = raw.find(b",")
        if comma < 0:
            raise UnsupportedFormat("QTag 尾部缺少 ekey")
        ekey = raw[:comma].decode("ascii", "ignore").strip()
        return Trailer("qtag", audio_size, ekey)

    key_size = struct.unpack("<I", data[-4:])[0]
    if 0 < key_size < 0x300 and key_size <= size - 4:
        audio_size = size - key_size - 4
        ekey = data[audio_size: size - 4].decode("ascii", "ignore").strip().strip("\x00")
        if ekey:
            return Trailer("ekey", audio_size, ekey)

    return Trailer("static", size)


# ============================================================
#  QQ 音乐客户端凭据提取 (Windows) 与 ekey 接口
# ============================================================

QQ_API_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
QQ_API_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
# 内存/文件中 JSON 常见格式: "authst":"x" / "authst" : "x" (值可能含 \/ 转义)
AUTHST_RE = re.compile(rb'"authst"\s*:\s*"((?:[^"\\]|\\.){10,}?)"')
AUTHST_CHARSET = re.compile(r"^[A-Za-z0-9+/_\-=]+$")


def extract_authst_tokens(data: bytes) -> list[str]:
    """从字节流中提取所有合法的 authst 候选 (去重, 保持顺序)。"""
    tokens: list[str] = []
    for m in AUTHST_RE.finditer(data):
        token = m.group(1).decode("ascii", "ignore").replace("\\", "")
        if len(token) >= 10 and AUTHST_CHARSET.match(token) and token not in tokens:
            tokens.append(token)
    return tokens


def read_uin_from_config() -> str | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    ini = Path(appdata) / "Tencent" / "QQMusic" / "QQMusicServiceConfig.ini"
    if not ini.is_file():
        return None
    try:
        for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
            t = line.strip()
            for prefix in ("Uin=", "uin=", "UIN="):
                if t.startswith(prefix):
                    v = t[len(prefix):].strip()
                    if v and v != "0":
                        return v
    except OSError:
        pass
    return None


def scan_cookie_files() -> list[str]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    base = Path(appdata) / "Tencent" / "QQMusic"
    found: list[str] = []
    for name in ("SetCookie.dat", "_SetCookie.dat"):
        p = base / name
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        for token in extract_authst_tokens(data):
            if token not in found:
                found.append(token)
    return found


def scan_process_memory_for_authst() -> list[str]:
    """枚举 QQMusic.exe / WeChatAppEx.exe / qmbrowser.exe 进程, 在其内存中查找 authst。"""
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        return []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return []

    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("PartitionId", wintypes.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    MEM_COMMIT = 0x1000
    MEM_PRIVATE = 0x20000
    MEM_IMAGE = 0x1000000
    PAGE_GUARD = 0x100
    PAGE_READABLE = 0x02 | 0x04 | 0x08 | 0x20 | 0x40
    CHUNK = 1024 * 1024
    TARGETS = {"qqmusic.exe", "wechatappex.exe", "qmbrowser.exe"}

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE

    # 枚举目标进程
    pids: list[tuple[int, str]] = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, -1, wintypes.HANDLE(-1).value):
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        name = entry.szExeFile
        if name.lower() in TARGETS:
            pids.append((entry.th32ProcessID, name))
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    if not pids:
        return []
    # 优先扫描 QQMusic.exe 主进程
    pids.sort(key=lambda item: 0 if item[1].lower() == "qqmusic.exe" else 1)

    tokens: list[str] = []

    for pid, name in pids:
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, 0, pid)
        if not h:
            continue
        addr = 0
        mbi = MBI()
        carry = b""
        while True:
            ret = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if ret == 0:
                break
            if (mbi.State == MEM_COMMIT and mbi.RegionSize > 0
                    and not (mbi.Protect & PAGE_GUARD)
                    and (mbi.Protect & PAGE_READABLE)
                    and (mbi.Type & (MEM_PRIVATE | MEM_IMAGE))):
                offset = 0
                while offset < mbi.RegionSize:
                    chunk = min(CHUNK, mbi.RegionSize - offset)
                    buf = ctypes.create_string_buffer(chunk)
                    nread = ctypes.c_size_t()
                    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr + offset), buf, chunk,
                                             ctypes.byref(nread)) and nread.value:
                        data = carry + buf.raw[:nread.value]
                        for token in extract_authst_tokens(data):
                            if token not in tokens:
                                tokens.append(token)
                        if tokens:
                            k32.CloseHandle(h)
                            return tokens
                        carry = data[-48:]
                    offset += chunk
            addr += mbi.RegionSize
            if addr <= 0 or addr > 0x7FFFFFFFFFFF:
                break
        k32.CloseHandle(h)
    return tokens


def get_authst_candidates(manual: str | None) -> list[str]:
    if manual:
        return [manual]
    cands = scan_cookie_files()
    if cands:
        return cands
    return scan_process_memory_for_authst()


def fetch_ekey_from_api(uin: str, authst: str, filename: str, songmid: str) -> str:
    """调用 music.vkey.GetEVkey 获取 ekey; 依次尝试微信/QQ 登录类型。"""
    last_err = ""
    for login_type in ("3", "1"):
        body = {
            "comm": {"authst": authst, "ct": "19", "cv": "1859", "uin": uin,
                     "tmeLoginType": login_type},
            "req_1": {
                "module": "music.vkey.GetEVkey",
                "method": "CgiGetEVkey",
                "param": {
                    "filename": [filename],
                    "guid": "10000",
                    "songmid": [songmid],
                    "songtype": [1],
                    "uin": uin,
                    "loginflag": 1,
                    "platform": "27" if os.name == "nt" else "20",
                    "ctx": 1,
                },
            },
        }
        req = urllib.request.Request(
            QQ_API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": QQ_API_UA,
                     "Referer": "https://y.qq.com/"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"请求 QQ 音乐接口失败: {e}")
        except json.JSONDecodeError:
            raise RuntimeError("QQ 音乐接口返回了无法解析的内容")

        req1 = payload.get("req_1") or {}
        code = req1.get("code", -1)
        if code != 0:
            last_err = f"接口 code={code}"
            continue
        info_list = ((req1.get("data") or {}).get("midurlinfo")) or []
        if not info_list:
            last_err = "接口未返回 midurlinfo"
            continue
        info = info_list[0]
        ekey = (info.get("ekey") or "").strip()
        if ekey:
            return ekey
        result = info.get("result", -1)
        if result == 104003:
            last_err = "无权限获取 ekey (result=104003): 该歌曲需要 VIP 且当前账号无权限"
        elif result == 104005:
            last_err = "ekey 为空 (result=104005): 登录凭据可能已过期, 请重启 QQ 音乐后重试"
        else:
            last_err = f"ekey 为空 (result={result})"
    raise RuntimeError(last_err or "获取 ekey 失败")


# ============================================================
#  ekey 缓存
# ============================================================

class EkeyCache:
    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, str] = {}
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def get(self, filename: str) -> str | None:
        return self.data.get(filename)

    def put(self, filename: str, ekey: str) -> None:
        self.data[filename] = ekey
        if self.path:
            try:
                self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
            except OSError:
                pass


# ============================================================
#  解密与转换
# ============================================================

def sniff_audio(head: bytes) -> str | None:
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"RIFF":
        return "wav"
    return None


def _read_vorbis_comments(body) -> dict[str, str]:
    """读取 FLAC / OGG 共用的 Vorbis comment 数据, 忽略不完整的标签。"""
    tags: dict[str, str] = {}
    if len(body) < 4:
        return tags
    pos = 4 + int.from_bytes(body[:4], "little")
    if pos + 4 > len(body):
        return tags
    count = int.from_bytes(body[pos:pos + 4], "little")
    pos += 4
    for _ in range(count):
        if pos + 4 > len(body):
            break
        length = int.from_bytes(body[pos:pos + 4], "little")
        pos += 4
        if pos + length > len(body):
            break
        comment = bytes(body[pos:pos + length]).decode("utf-8", "replace")
        key, sep, value = comment.partition("=")
        if key and sep:
            tags[key.upper()] = value
        pos += length
    return tags


def ogg_read_tags(audio) -> dict[str, str]:
    """读取 Ogg Vorbis / Opus 标签, 支持跨页的 comment packet。"""
    pos = 0
    packets: dict[int, bytearray] = {}
    while pos + 27 <= len(audio):
        if bytes(audio[pos:pos + 4]) != b"OggS" or audio[pos + 4] != 0:
            break
        segment_count = audio[pos + 26]
        body_pos = pos + 27 + segment_count
        if body_pos > len(audio):
            break
        segments = audio[pos + 27:body_pos]
        page_end = body_pos + sum(segments)
        if page_end > len(audio):
            break
        serial = int.from_bytes(audio[pos + 14:pos + 18], "little")
        packet = packets.setdefault(serial, bytearray())
        for length in segments:
            packet.extend(audio[body_pos:body_pos + length])
            body_pos += length
            if length < 255:
                if packet.startswith(b"\x03vorbis"):
                    return _read_vorbis_comments(packet[7:])
                if packet.startswith(b"OpusTags"):
                    return _read_vorbis_comments(packet[8:])
                packet.clear()
        pos = page_end
    return {}


def flac_read_tags(audio) -> dict[str, str]:
    """读取 FLAC VORBIS_COMMENT 标签 (返回大写 key -> value)。"""
    tags: dict[str, str] = {}
    try:
        if bytes(audio[:4]) != b"fLaC":
            return tags
        pos = 4
        while pos + 4 < len(audio):
            btype = audio[pos] & 0x7F
            last = bool(audio[pos] & 0x80)
            blen = int.from_bytes(bytes(audio[pos + 1:pos + 4]), "big")
            body = bytes(audio[pos + 4:pos + 4 + blen])
            if btype == 4:  # VORBIS_COMMENT
                tags = _read_vorbis_comments(body)
                break
            if last:
                break
            pos += 4 + blen
    except Exception:
        pass
    return tags


def guess_tags_from_filename(stem: str) -> dict[str, str]:
    """从 '歌手 - 歌名' 形式的文件名推测 ID3 标签。"""
    for sep in (" - ", "－", " — "):
        if sep in stem:
            artist, _, title = stem.partition(sep)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return {"artist": artist, "title": title}
    return {}


def find_ffmpeg(manual: str | None = None) -> str | None:
    if manual and Path(manual).is_file():
        return manual
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    candidates = [
        app_dir() / "ffmpeg.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def flac_duration_seconds(audio) -> float | None:
    """从 FLAC STREAMINFO 读取总时长 (秒), 用于转码进度。"""
    try:
        if bytes(audio[:4]) != b"fLaC":
            return None
        if (audio[4] & 0x7F) != 0:
            return None
        body = bytes(audio[8:42])
        if len(body) < 34:
            return None
        samplerate = (body[10] << 12) | (body[11] << 4) | (body[12] >> 4)
        total = ((body[13] & 0xF) << 32) | int.from_bytes(body[14:18], "big")
        if samplerate <= 0:
            return None
        return total / samplerate
    except Exception:
        return None


def run_ffmpeg(ffmpeg: str, src: Path, dst: Path, bitrate: int, vbr: bool,
               tags: dict[str, str] | None = None, total_seconds: float | None = None,
               progress=None, cancel_event: threading.Event | None = None,
               metadata_from_audio: bool = False) -> None:
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostats", "-loglevel", "error",
           "-progress", "pipe:1", "-i", str(src),
           "-map", "0:a", "-map", "0:v?",
           "-c:a", "libmp3lame"]
    if vbr:
        cmd += ["-q:a", "0"]
    else:
        cmd += ["-b:a", f"{bitrate}k"]
    if metadata_from_audio:
        # OGG 的 Vorbis comments 位于音轨, 需显式映射到 MP3 的全局 ID3 标签。
        cmd += ["-map_metadata", "0:s:a:0"]
    cmd += ["-c:v", "copy", "-id3v2_version", "3", "-write_id3v1", "1"]
    for k, v in (tags or {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd.append(str(dst))

    # Windows GUI 启动 ffmpeg 时不要为每个文件创建控制台窗口。
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=creationflags)
    stderr_lines: list[bytes] = []

    def drain_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    try:
        for raw in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                raise OperationCancelled()
            line = raw.decode("ascii", "ignore").strip()
            if total_seconds and progress and line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                    frac = min(max(us / 1_000_000 / total_seconds, 0.0), 1.0)
                    progress(0.2 + 0.78 * frac)
                except ValueError:
                    pass
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t.join(timeout=2)

    if proc.returncode != 0:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled()
        err = b"".join(stderr_lines).decode("utf-8", "replace").strip()[-500:]
        raise RuntimeError(f"ffmpeg 转码失败: {err}")


class ConvertOptions:
    """转换参数 (CLI 与 GUI 共用)。"""

    def __init__(self, output_dir=None, bitrate: int = 320, vbr: bool = False,
                 keep_flac: bool = False, flac_only: bool = False,
                 overwrite: bool = False, ffmpeg: str | None = None,
                 no_cache: bool = False, uin: str | None = None,
                 authst: str | None = None):
        self.output_dir = output_dir
        self.bitrate = bitrate
        self.vbr = vbr
        self.keep_flac = keep_flac
        self.flac_only = flac_only
        self.overwrite = overwrite
        self.ffmpeg = ffmpeg
        self.no_cache = no_cache
        self.uin = uin
        self.authst = authst


class ConvertContext:
    def __init__(self, opts: ConvertOptions):
        self.opts = opts
        self.out_dir = Path(opts.output_dir).resolve() if opts.output_dir else None
        self.bitrate = opts.bitrate
        self.vbr = opts.vbr
        self.keep_audio = opts.keep_flac
        self.flac_only = opts.flac_only
        self.overwrite = opts.overwrite
        self.ffmpeg = find_ffmpeg(opts.ffmpeg)
        self.uin = opts.uin
        self.authst = opts.authst
        self.no_cache = opts.no_cache
        cache_path = None if opts.no_cache else app_dir() / "ekey_cache.json"
        self.cache = EkeyCache(cache_path)
        self.cancel_event: threading.Event | None = None
        self._authst_candidates: list[str] | None = None

    def resolve_ekey(self, trailer: Trailer) -> str:
        assert trailer.kind == "musicex"
        if not self.no_cache:
            cached = self.cache.get(trailer.filename)
            if cached:
                return cached
        uin = self.uin or read_uin_from_config()
        if not uin:
            raise RuntimeError("无法读取 QQ 号: 请用 --uin 指定, 或确认本机已安装并登录 QQ 音乐")
        if self._authst_candidates is None:
            self._authst_candidates = get_authst_candidates(self.authst)
        if not self._authst_candidates:
            raise RuntimeError("未能获取登录凭据 authst: 请确认 QQ 音乐正在运行且已登录 "
                               "(或用 --authst 手动提供)")
        last_err = ""
        for token in self._authst_candidates[:4]:
            try:
                ekey = fetch_ekey_from_api(uin, token, trailer.filename, trailer.media_mid)
                if not self.no_cache:
                    self.cache.put(trailer.filename, ekey)
                return ekey
            except RuntimeError as e:
                last_err = str(e)
                continue
        raise RuntimeError(last_err or "获取 ekey 失败")


def _check_cancel(ctx: ConvertContext) -> None:
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise OperationCancelled()


def convert_one(path: Path, ctx: ConvertContext,
                progress=None, stage=None) -> tuple[bool, str]:
    """转换单个文件。

    progress: 可选回调, 参数为 0~1 的进度 (解密完成约 0.2, 转码完成 1.0)
    stage:    可选回调, 参数为当前阶段描述文本
    抛出 OperationCancelled 表示用户取消。
    """
    def _stage(text: str):
        if stage:
            stage(text)

    data = bytearray(path.read_bytes())
    _check_cancel(ctx)
    ext = sniff_audio(bytes(data[:16]))
    is_plain = ext in ("ogg", "flac")
    if is_plain:
        audio = memoryview(data)
        kind_desc = ext.upper()
    else:
        if path.suffix.lower() in (".ogg", ".flac"):
            return False, "不是有效的 OGG/FLAC 音频, 请检查文件是否损坏或仍处于加密状态"
        try:
            trailer = detect_trailer(data)
        except UnsupportedFormat as e:
            return False, f"不支持的文件格式: {e}"

        if trailer.kind == "musicex":
            _stage("获取 ekey")
            try:
                ekey = ctx.resolve_ekey(trailer)
            except RuntimeError as e:
                return False, f"获取 ekey 失败: {e}"
            kind_desc = f"musicex (song_id={trailer.song_id})"
        elif trailer.ekey:
            ekey = trailer.ekey
            kind_desc = trailer.kind
        else:
            ekey = None
            kind_desc = "static(v1)"

        _stage("解密中")
        try:
            cipher = make_cipher(ekey)
        except ValueError as e:
            return False, f"密钥无效: {e}"

        audio = memoryview(data)[:trailer.audio_size]
        cipher.decrypt(audio)
        _check_cancel(ctx)
        ext = sniff_audio(bytes(audio[:16]))
        if ext is None:
            head = bytes(audio[:8]).hex()
            return False, f"解密结果不是有效音频 (头部 {head}), 密钥可能错误或格式不受支持"
    if progress:
        progress(0.2)

    out_dir = ctx.out_dir or path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    dec_path = out_dir / f"{stem}.{ext}"

    if ctx.flac_only:
        if is_plain:
            if progress:
                progress(1.0)
            return True, f"[{kind_desc}] 无需解密, 已跳过: {path.name}"
        if dec_path.exists() and not ctx.overwrite:
            return True, f"已存在, 跳过: {dec_path.name}"
        dec_path.write_bytes(audio)
        if progress:
            progress(1.0)
        return True, f"[{kind_desc}] 解密完成 → {dec_path.name}"

    mp3_path = out_dir / f"{stem}.mp3"
    if mp3_path.exists() and not ctx.overwrite:
        return True, f"已存在, 跳过: {mp3_path.name}"

    if is_plain:
        # 普通音频直接作为转码输入, 不写回或清理源文件。
        tmp_dec = path
    elif ctx.keep_audio:
        dec_path.write_bytes(audio)
        tmp_dec = dec_path
    else:
        tmp_dec = out_dir / f".{stem}.tmp.{ext}"
        tmp_dec.write_bytes(audio)

    try:
        if not ctx.ffmpeg:
            if is_plain:
                return False, "未找到 ffmpeg, 无法转 MP3 (请安装 ffmpeg 或手动指定路径)"
            if not ctx.keep_audio:
                dec_path.write_bytes(audio)
                tmp_dec.unlink(missing_ok=True)
            return False, f"未找到 ffmpeg, 已保留解密文件 {dec_path.name} (请安装 ffmpeg 后重试转码)"
        _stage("转码 MP3")
        tags = None
        if ext in ("flac", "ogg"):
            existing = ogg_read_tags(audio) if ext == "ogg" else flac_read_tags(audio)
            guessed = guess_tags_from_filename(stem)
            tags = {k: v for k, v in guessed.items() if k.upper() not in existing}
        total = flac_duration_seconds(audio) if ext == "flac" else None
        run_ffmpeg(ctx.ffmpeg, tmp_dec, mp3_path, ctx.bitrate, ctx.vbr, tags,
                   total_seconds=total, progress=progress,
                   cancel_event=ctx.cancel_event, metadata_from_audio=(ext == "ogg"))
    finally:
        if not is_plain and not ctx.keep_audio:
            tmp_dec.unlink(missing_ok=True)

    if progress:
        progress(1.0)
    size_mb = mp3_path.stat().st_size / 1024 / 1024
    return True, f"[{kind_desc}] → {mp3_path.name} ({size_mb:.1f} MB)"


SUPPORTED_EXTS = {
    ".ogg", ".flac",
    ".mflac", ".mflac0", ".mflac1", ".mflac2", ".mflach",
    ".mgg", ".mgg0", ".mgg1", ".mggl", ".mggh",
    ".qmcflac", ".qmcogg", ".qmc0", ".qmc2", ".qmc3",
    ".bkcmp3", ".bkcflac",
}


def collect_inputs(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files.extend(sorted(q for q in p.rglob("*")
                                if q.suffix.lower() in SUPPORTED_EXTS and q.is_file()))
        elif p.is_file():
            files.append(p)
        else:
            print(f"警告: 路径不存在, 已忽略: {item}")
    # 去重保持顺序
    seen = set()
    uniq = []
    for f in files:
        key = str(f.resolve()).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


# ============================================================
#  自检
# ============================================================

def self_test() -> bool:
    ok = True

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {name}" + (f"  {detail}" if detail and not cond else ""))
        ok = ok and cond

    # 1. TEA 分组加解密互逆
    key = bytes(range(16))
    blk = TeaCipher(key, 32)
    plain = bytes([0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF])
    check("TEA 块加解密互逆", blk.encrypt_block(blk.decrypt_block(plain)) == plain)

    # 2. QQ TEA CBC 加解密互逆
    body = b"hello qmc tea cbc roundtrip test body!"
    check("TC-TEA CBC 加解密互逆",
          tc_tea_decrypt(tc_tea_encrypt(body, key), key) == body)

    # 3. simpleMakeKey 与 JS 实现一致
    check("simpleMakeKey(106,8) == 69 56 46 38 2b 20 15 0b",
          simple_make_key(106, 8).hex() == "69564638" + "2b20" + "150b")

    # 4. parse_ekey 官方向量 (来自 qmc-decoder 单元测试)
    ekey = "VGhpcyBpcyBHFWEh4cjZ1Vi7rJ56XeoPlqGM1sxBGPg7mt89umKclFBr9iqfmFdS"
    try:
        parsed = parse_ekey(ekey)
        check("parse_ekey 官方测试向量", parsed == b"This is a test key for test purpose :D")
    except Exception as e:
        check("parse_ekey 官方测试向量", False, repr(e))

    # 5. Map 密码器掩码向量 (来自 qmc-decoder 单元测试)
    map_key = bytes(range(0x41, 0x51))
    cipher = MapCipher(map_key)
    buf = bytearray(16)
    cipher.decrypt(buf, 0)
    expect0 = bytes([0x3F, 0x8A, 0xC1, 0x49, 0x3F, 0x49, 0xC1, 0x8A,
                     0x3F, 0x8A, 0xC1, 0x49, 0x3F, 0x49, 0xC1, 0x8A])
    check("Map 掩码向量 (offset=0)", bytes(buf) == expect0)

    buf = bytearray(16)
    cipher.decrypt(buf, 0x7FFF - 8)
    expect1 = bytes([0x8A, 0x3F, 0x8A, 0xC1, 0x49, 0x3F, 0x49, 0xC1,
                     0x8A, 0x8A, 0xC1, 0x49, 0x3F, 0x49, 0xC1, 0x8A])
    check("Map 掩码向量 (offset=0x7FFF-8)", bytes(buf) == expect1)

    # 6. 大缓冲区快速掩码路径与逐字节 get_mask 一致
    buf = bytearray(100000)
    cipher.decrypt(buf, 12345)
    ref = bytearray(cipher.get_mask(12345 + i) for i in range(100000))
    check("Map 快速路径 == 逐字节路径", bytes(buf) == bytes(ref))

    # 7. RC4 密码器对称性与分段一致性
    rc4_key = bytes((i * 7 + 3) & 0xFF for i in range(400))
    orig = bytes((i * 13 + 5) & 0xFF for i in range(20000))
    rc4 = RC4Cipher(rc4_key)
    work = bytearray(orig)
    rc4.decrypt(work)
    enc = bytes(work)                       # enc = 加密结果
    check("RC4 加密输出与原文不同", enc != orig)
    rc4.decrypt(work)
    check("RC4 对称性 (再解一次还原)", bytes(work) == orig)

    part = bytearray(orig[:9000])           # 只加密前 9000 字节
    rc4b = RC4Cipher(rc4_key)
    rc4b.decrypt(part)
    check("RC4 分段一致 (前缀独立加密 == 全量加密前缀)", bytes(part) == enc[:9000])

    # 8. 静态密码器边界
    sc = StaticCipher()
    check("Static 掩码 spot-check", sc.BOX[(27)] == 0xC3 and StaticCipher.BOX[0] == 0x77)

    # 9. 尾部探测: 构造 ekey 尾部与 QTag 尾部
    payload = b"fLaCfakeaudiobytes" * 4
    ekey_b = base64.b64encode(bytes(range(64)))
    blob = payload + ekey_b + struct.pack("<I", len(ekey_b))
    t = detect_trailer(blob)
    check("ekey 尾部探测", t.kind == "ekey" and t.audio_size == len(payload) and t.ekey == ekey_b.decode())

    return ok


# ============================================================
#  CLI
# ============================================================

def main(argv=None) -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
            sys.stderr.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        prog="mflac2mp3",
        description="OGG / FLAC / QQ 音乐加密音频 (.mflac/.mgg/.qmc*) 转 MP3 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python mflac2mp3.py "周杰伦 - 搁浅.mflac"
  python mflac2mp3.py D:\\Music\\下载 -o D:\\Music\\mp3
  python mflac2mp3.py *.mflac --vbr
  python mflac2mp3.py --self-test

说明:
  - 普通 OGG / FLAC 文件直接转 MP3, 无需解密或登录 QQ 音乐。
  - 旧版文件 (尾部内嵌 ekey / QMCv1) 完全离线解密。
  - 新版 musicex 文件需要本机 QQ 音乐客户端正在运行且已登录,
    工具会自动提取登录凭据并调用官方接口获取该文件的解密密钥。
""")
    ap.add_argument("inputs", nargs="*", help="要转换的文件或目录 (支持批量)")
    ap.add_argument("-o", "--output-dir", help="输出目录 (默认与输入文件同目录)")
    ap.add_argument("-b", "--bitrate", type=int, default=320, help="MP3 码率 kbps, 默认 320")
    ap.add_argument("--vbr", action="store_true", help="使用 VBR (lame -q:a 0) 代替 CBR")
    ap.add_argument("--keep-flac", action="store_true", help="保留解密后的原格式音频 (FLAC/OGG 等)")
    ap.add_argument("--flac-only", action="store_true", help="只解密, 不转 MP3")
    ap.add_argument("-y", "--overwrite", action="store_true", help="覆盖已存在的输出文件")
    ap.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    ap.add_argument("--no-cache", action="store_true", help="不使用 ekey 缓存")
    ap.add_argument("--uin", help="手动指定 QQ 号 (默认自动从客户端配置读取)")
    ap.add_argument("--authst", help="手动指定登录凭据 authst (默认自动提取)")
    ap.add_argument("--self-test", action="store_true", help="运行算法自检后退出")
    args = ap.parse_args(argv)

    if args.self_test:
        print("运行算法自检:")
        ok = self_test()
        print("自检结果: " + ("全部通过 ✓" if ok else "存在失败项 ✗"))
        return 0 if ok else 1

    if not args.inputs:
        ap.print_help()
        return 2

    files = collect_inputs(args.inputs)
    if not files:
        print("没有找到可处理的文件 (支持后缀: " + " ".join(sorted(SUPPORTED_EXTS)) + ")")
        return 2

    opts = ConvertOptions(
        output_dir=args.output_dir,
        bitrate=args.bitrate,
        vbr=args.vbr,
        keep_flac=args.keep_flac,
        flac_only=args.flac_only,
        overwrite=args.overwrite,
        ffmpeg=args.ffmpeg,
        no_cache=args.no_cache,
        uin=args.uin,
        authst=args.authst,
    )
    ctx = ConvertContext(opts)
    if not ctx.flac_only and not ctx.ffmpeg:
        print("警告: 未找到 ffmpeg, 将只解密保留音频原格式。"
              "请安装 ffmpeg (https://ffmpeg.org) 或用 --ffmpeg 指定路径。")

    print(f"共 {len(files)} 个文件待处理\n")
    success = 0
    for i, f in enumerate(files, 1):
        try:
            ok, msg = convert_one(f, ctx)
        except Exception as e:
            ok, msg = False, f"异常: {e.__class__.__name__}: {e}"
        status = "OK " if ok else "ERR"
        print(f"[{i}/{len(files)}] {status} {f.name}: {msg}")
        success += 1 if ok else 0

    failed = len(files) - success
    print(f"\n完成: 成功 {success}, 失败 {failed}")
    return 0 if failed == 0 else (1 if success > 0 else 2)


if __name__ == "__main__":
    sys.exit(main())
