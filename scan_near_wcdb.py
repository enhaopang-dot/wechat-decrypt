#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
基于 WCDB.Config.Cipher 字符串定位，扫描附近内存寻找 passphrase。
"""
import ctypes
import hashlib
import hmac as hmac_mod
import json
import os
import re
import struct
import sys
from ctypes import wintypes

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
KDF_ITER = 256000

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
OpenProcess = kernel32.OpenProcess
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenProcess.restype = wintypes.HANDLE
ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
ReadProcessMemory.restype = wintypes.BOOL
CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wintypes.DWORD), ("_pad1", wintypes.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD), ("_pad2", wintypes.DWORD),
    ]


def get_pids():
    import subprocess
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    pids = []
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pid = int(p[1])
            mem = int(p[4].replace(',', '').replace(' K', '').strip() or '0')
            pids.append((pid, mem))
    if not pids:
        raise RuntimeError("Weixin.exe 未运行")
    pids.sort(key=lambda x: x[1], reverse=True)
    return pids


def enum_regions(h):
    regs = []
    addr = 0
    mbi = MBI()
    while kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) != 0:
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    if ReadProcessMemory(h, ctypes.c_void_p(addr), buf, sz, ctypes.byref(n)):
        return buf.raw[:n.value]
    return None


def read_bytes_from_pid(pid, addr, size):
    h = OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return b''
    try:
        return read_mem(h, addr, size) or b''
    finally:
        CloseHandle(h)


def verify_passphrase_key(passphrase, db_page1):
    salt = db_page1[:SALT_SZ]
    key_material = hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, dklen=KEY_SZ * 2)
    enc_key = key_material[:KEY_SZ]
    hmac_key = key_material[KEY_SZ:]
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(hmac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def collect_db_files(db_dir):
    db_files = []
    for root, dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            if size < PAGE_SZ:
                continue
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
            rel = os.path.relpath(path, db_dir)
            salt = page1[:SALT_SZ].hex()
            db_files.append((rel, path, size, salt, page1))
    return db_files


def find_string_matches(pid, regions, needle):
    """在内存中搜索 needle 字符串，返回匹配地址列表。"""
    h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        return []
    matches = []
    try:
        for base, size in regions:
            data = read_mem(h, base, size)
            if not data:
                continue
            start = 0
            while True:
                idx = data.find(needle, start)
                if idx == -1:
                    break
                matches.append(base + idx)
                start = idx + 1
    finally:
        CloseHandle(h)
    return matches


def test_candidate(passphrase, db_files):
    """测试候选 passphrase 对所有数据库是否有效。"""
    ok = 0
    for rel, path, sz, salt_hex, page1 in db_files:
        if verify_passphrase_key(passphrase, page1):
            ok += 1
    return ok


def extract_32byte_candidates(data, window_start):
    """从 data 中提取所有可能为 32 字节 passphrase 的候选。
    同时返回原始 32 字节值和 64 位 hex 字符串解码值。
    """
    candidates = []
    # 1. 任意连续的 32 字节
    for i in range(len(data) - 32):
        candidates.append(data[i:i+32])
    # 2. 64 位 hex 字符串解码为 32 字节
    hex_re = re.compile(rb'[0-9a-fA-F]{64}')
    for m in hex_re.finditer(data):
        try:
            candidates.append(bytes.fromhex(m.group(0).decode('ascii')))
        except Exception:
            pass
    return candidates


def main():
    db_dir = ""  # 见 config.example.json，按你的实际路径填
    print("=" * 60)
    print("  WCDB Config.Cipher 附近 passphrase 扫描")
    print("=" * 60)

    db_files = collect_db_files(db_dir)
    print(f"\n找到 {len(db_files)} 个数据库")

    pids = get_pids()
    print(f"\n找到 {len(pids)} 个 Weixin.exe 进程")

    needles = [
        b"com.Tencent.WCDB.Config.Cipher",
        b"WCDB.Config.Cipher",
        b"Config.Cipher",
        b"WCDB",
        b"xwechat_files",
        b"db_storage",
    ]

    found_passphrases = set()
    sample_page1 = db_files[0][4]

    for pid, mem in pids:
        print(f"\n[*] 扫描 PID={pid}...")
        h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not h:
            continue
        regions = enum_regions(h)
        CloseHandle(h)

        for needle in needles:
            addrs = find_string_matches(pid, regions, needle)
            print(f"  字符串 '{needle.decode()}' 匹配数: {len(addrs)}")
            if not addrs:
                continue

            # 对每个匹配地址，读取前后 8KB 内存
            for addr in addrs[:50]:  # 限制前50个匹配
                chunk = read_bytes_from_pid(pid, max(0, addr - 4096), 8192 + len(needle))
                if len(chunk) < 32:
                    continue
                candidates = extract_32byte_candidates(chunk, addr - 4096)
                for cand in candidates:
                    if cand in found_passphrases:
                        continue
                    if verify_passphrase_key(cand, sample_page1):
                        print(f"\n[FOUND] 找到有效 passphrase @ PID={pid}, 地址=0x{addr:016X}")
                        print(f"  passphrase (hex): {cand.hex()}")
                        found_passphrases.add(cand)

    if found_passphrases:
        passphrase = list(found_passphrases)[0]
        result = {}
        for rel, path, sz, salt_hex, page1 in db_files:
            salt = bytes.fromhex(salt_hex)
            key_material = hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, dklen=KEY_SZ * 2)
            enc_key = key_material[:KEY_SZ]
            hmac_key = key_material[KEY_SZ:]
            if verify_passphrase_key(passphrase, page1):
                result[rel] = {
                    "enc_key": enc_key.hex(),
                    "hmac_key": hmac_key.hex(),
                    "salt": salt_hex,
                    "size_mb": round(sz / 1024 / 1024, 1)
                }
                print(f"  OK: {rel}")
        result["_db_dir"] = db_dir
        result["_mode"] = "passphrase"
        out_file = "all_keys.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n密钥已保存到: {out_file}")
    else:
        print("\n未能找到有效 passphrase")


if __name__ == "__main__":
    main()
