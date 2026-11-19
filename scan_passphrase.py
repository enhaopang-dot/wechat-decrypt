#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信 4.1.x passphrase 内存扫描器
基于 wxManager/wx_info_v4.py 的思路，扫描 Weixin.exe 内存寻找 passphrase。
"""
import ctypes
import hashlib
import hmac as hmac_mod
import json
import os
import struct
import sys
import time
from ctypes import wintypes

import yara

# 常量
PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_READWRITE = 0x04
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


def scan_with_yara(pid, regions, rules_source, progress_every=50):
    """用 yara 扫描所有内存区域，返回匹配列表。"""
    h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        print(f"  [WARN] 无法打开进程 PID={pid}")
        return []
    rules = yara.compile(source=rules_source)
    matches_all = []
    try:
        total = len(regions)
        for idx, (base, size) in enumerate(regions):
            data = read_mem(h, base, size)
            if not data:
                continue
            matches = rules.match(data=data)
            if matches:
                for match in matches:
                    for string in match.strings:
                        for instance in string.instances:
                            matches_all.append((base + instance.offset, instance.matched_data, match.rule, string.identifier))
            if (idx + 1) % progress_every == 0:
                print(f"  扫描进度 {idx+1}/{total}...")
    finally:
        CloseHandle(h)
    return matches_all


def main():
    db_dir = ""  # 见 config.example.json，按你的实际路径填
    print("=" * 60)
    print("  微信 4.1.x passphrase 内存扫描")
    print("=" * 60)

    db_files = collect_db_files(db_dir)
    print(f"\n找到 {len(db_files)} 个数据库")
    # 用第一个数据库的 page1 作为校验样本
    sample_page1 = db_files[0][4]
    sample_salt = db_files[0][3]
    print(f"校验样本: {db_files[0][0]} (salt={sample_salt})")

    pids = get_pids()
    print(f"\n找到 {len(pids)} 个 Weixin.exe 进程")
    for pid, mem in pids:
        print(f"  PID={pid} ({mem//1024}MB)")

    # 原始 wx_info_v4.py 的 stub 规则
    rules_stub = r'''
    rule GetKeyAddrStub {
        strings:
            $a = /.{6}\x00{2}\x00{8}\x20\x00{7}\x2f\x00{7}/
        condition:
            all of them
    }
    '''

    # 更宽松的规则：寻找可能指向 32 字节数据的指针结构
    rules_relaxed = r'''
    rule KeyPtr32 {
        strings:
            $a = /.{8}\x20\x00{7}.{8}\x20\x00{7}/
        condition:
            all of them
    }
    '''

    candidates = []
    seen_keys = set()

    for pid, mem in pids:
        print(f"\n[*] 扫描 PID={pid}...")
        h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not h:
            continue
        regions = enum_regions(h)
        CloseHandle(h)
        print(f"  共 {len(regions)} 个内存区域")

        # 先用原始 stub 规则
        print("  -> 使用原始 stub 规则扫描...")
        matches = scan_with_yara(pid, regions, rules_stub, progress_every=200)
        print(f"  原始 stub 匹配数: {len(matches)}")
        for addr, data, rule, sid in matches:
            # 从匹配数据中提取指针地址（前8字节）
            ptr = struct.unpack('<Q', data[:8])[0]
            key = read_bytes_from_pid(pid, ptr, 32)
            if key and len(key) == 32 and key not in seen_keys:
                seen_keys.add(key)
                candidates.append((pid, addr, ptr, key, "stub"))
                print(f"    候选 key @ 0x{ptr:016X} (来自 0x{addr:016X})")

        # 再用宽松规则
        print("  -> 使用宽松规则扫描...")
        matches = scan_with_yara(pid, regions, rules_relaxed, progress_every=200)
        print(f"  宽松规则匹配数: {len(matches)}")
        for addr, data, rule, sid in matches:
            # 尝试 data 中每个 8 字节作为指针
            for i in range(0, len(data) - 8, 8):
                ptr = struct.unpack('<Q', data[i:i+8])[0]
                if ptr == 0 or ptr > 0x7FFFFFFFFFFF:
                    continue
                key = read_bytes_from_pid(pid, ptr, 32)
                if key and len(key) == 32 and key not in seen_keys:
                    seen_keys.add(key)
                    candidates.append((pid, addr, ptr, key, "relaxed"))
                    print(f"    候选 key @ 0x{ptr:016X} (来自 0x{addr:016X})")

    print(f"\n共收集 {len(candidates)} 个候选 passphrase")

    # 验证候选 passphrase
    found = []
    for pid, addr, ptr, key, src in candidates:
        if verify_passphrase_key(key, sample_page1):
            print(f"\n[FOUND] 找到有效 passphrase!")
            print(f"  来源: PID={pid}, 地址=0x{ptr:016X}, 规则={src}")
            print(f"  passphrase (hex): {key.hex()}")
            found.append(key)
        else:
            print(f"  无效候选: 0x{ptr:016X} ({src})")

    if found:
        print(f"\n成功找到 {len(found)} 个有效 passphrase")
        # 派生所有数据库密钥
        passphrase = found[0]
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
        print("建议：尝试其他内存扫描规则或等待 wcdb-key-tool 等工具更新")


if __name__ == "__main__":
    main()
