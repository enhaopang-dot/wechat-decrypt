#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
扫描 Weixin.exe 内存中的 WCDB Config.Cipher 相关字符串和 passphrase。
"""
import ctypes
import hashlib
import hmac as hmac_mod
import json
import os
import struct
import sys
from ctypes import wintypes

import yara

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


def find_wcdb_cipher(pid, regions):
    """扫描 WCDB.Config.Cipher 字符串，返回匹配地址列表。"""
    h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        return []
    rules = yara.compile(source=r'''
    rule WCDBCipher {
        strings:
            $a = "com.Tencent.WCDB.Config.Cipher"
            $b = "WCDB.Config.Cipher"
            $c = "Config.Cipher"
            $d = "WCDB"
        condition:
            any of them
    }
    ''')
    matches = []
    try:
        for base, size in regions:
            data = read_mem(h, base, size)
            if not data:
                continue
            res = rules.match(data=data)
            if res:
                for match in res:
                    for string in match.strings:
                        for instance in string.instances:
                            matches.append((base + instance.offset, string.identifier, instance.matched_data))
    finally:
        CloseHandle(h)
    return matches


def find_hex64_strings(pid, regions):
    """扫描内存中的 64 位 hex 字符串，作为 passphrase hex 候选。"""
    h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        return []
    hex64_re = yara.compile(source=r'''
    rule Hex64 {
        strings:
            $a = /[0-9a-fA-F]{64}/
        condition:
            any of them
    }
    ''')
    matches = []
    try:
        for base, size in regions:
            data = read_mem(h, base, size)
            if not data:
                continue
            res = hex64_re.match(data=data)
            if res:
                for match in res:
                    for string in match.strings:
                        for instance in string.instances:
                            matches.append(base + instance.offset)
    finally:
        CloseHandle(h)
    return matches


def main():
    db_dir = ""  # 见 config.example.json，按你的实际路径填
    print("=" * 60)
    print("  WCDB Config.Cipher / passphrase 扫描")
    print("=" * 60)

    db_files = collect_db_files(db_dir)
    print(f"\n找到 {len(db_files)} 个数据库")
    sample_page1 = db_files[0][4]

    pids = get_pids()
    print(f"\n找到 {len(pids)} 个 Weixin.exe 进程")

    found_passphrases = []

    for pid, mem in pids:
        print(f"\n[*] 扫描 PID={pid}...")
        h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not h:
            continue
        regions = enum_regions(h)
        CloseHandle(h)

        # 1. 找 WCDB 字符串
        wcdb_matches = find_wcdb_cipher(pid, regions)
        print(f"  WCDB 字符串匹配数: {len(wcdb_matches)}")
        for addr, sid, data in wcdb_matches[:20]:
            print(f"    {sid} @ 0x{addr:016X}: {data[:50]}")

        # 2. 找 64 位 hex 字符串并测试
        hex_addrs = find_hex64_strings(pid, regions)
        print(f"  64 位 hex 字符串数量: {len(hex_addrs)}")
        tested = 0
        for addr in hex_addrs:
            hex_str = read_bytes_from_pid(pid, addr, 64)
            if len(hex_str) != 64:
                continue
            try:
                passphrase = bytes.fromhex(hex_str.decode('ascii'))
            except Exception:
                continue
            if len(passphrase) != 32:
                continue
            tested += 1
            if verify_passphrase_key(passphrase, sample_page1):
                print(f"\n[FOUND] 找到有效 passphrase (hex) @ 0x{addr:016X}")
                print(f"  passphrase: {hex_str.decode('ascii')}")
                found_passphrases.append(passphrase)
        print(f"  已测试 {tested} 个 64 位 hex 候选")

    if found_passphrases:
        passphrase = found_passphrases[0]
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
