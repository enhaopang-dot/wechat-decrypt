#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从 path_dumps 中高吞吐量筛选 passphrase。
策略：先按熵过滤 32 字节窗口，再用多进程 PBKDF2 验证。
"""
import hashlib
import hmac as hmac_mod
import json
import math
import os
import re
import struct
from multiprocessing import Pool, cpu_count

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
KDF_ITER = 256000

DB_DIR = ""  # 见 config.example.json，按你的实际路径填
DUMP_DIR = "path_dumps"
ENTROPY_THRESHOLD = 4.0  # bits per byte


def shannon_entropy(data):
    if not data:
        return 0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    entropy = 0.0
    length = len(data)
    for c in counts:
        if c == 0:
            continue
        p = c / length
        entropy -= p * math.log2(p)
    return entropy


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


def collect_candidates(dump_dir):
    """收集所有高熵 32 字节候选和 hex64 候选。"""
    candidates = []
    seen = set()
    hex_re = re.compile(rb'[0-9a-fA-F]{64}')

    for name in sorted(os.listdir(dump_dir)):
        if not name.endswith(".bin"):
            continue
        path = os.path.join(dump_dir, name)
        with open(path, "rb") as f:
            data = f.read()

        # hex64 字符串
        for m in hex_re.finditer(data):
            try:
                cand = bytes.fromhex(m.group(0).decode('ascii'))
            except Exception:
                continue
            if cand not in seen:
                seen.add(cand)
                candidates.append(cand)

        # 高熵 32 字节窗口
        for i in range(len(data) - 32):
            cand = data[i:i+32]
            if cand in seen:
                continue
            # 跳过全零、全相同字节
            if len(set(cand)) < 4:
                continue
            if shannon_entropy(cand) >= ENTROPY_THRESHOLD:
                seen.add(cand)
                candidates.append(cand)

    return candidates


def worker_init(sample_page1_bytes):
    global g_sample_page1
    g_sample_page1 = sample_page1_bytes


def test_candidate(cand):
    if verify_passphrase_key(cand, g_sample_page1):
        return cand
    return None


def main():
    db_files = collect_db_files(DB_DIR)
    print(f"找到 {len(db_files)} 个数据库")
    sample_page1 = db_files[0][4]

    print("\n收集候选...")
    candidates = collect_candidates(DUMP_DIR)
    print(f"共 {len(candidates)} 个候选")

    print(f"\n开始多进程验证 (CPU={cpu_count()})...")
    found = []
    with Pool(processes=max(1, cpu_count() // 2), initializer=worker_init, initargs=(sample_page1,)) as pool:
        for i, result in enumerate(pool.imap_unordered(test_candidate, candidates, chunksize=10)):
            if result:
                print(f"\n[FOUND] 有效 passphrase: {result.hex()}")
                found.append(result)
                break
            if (i + 1) % 100 == 0:
                print(f"  已测试 {i+1}/{len(candidates)}...")

    if found:
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
        result["_db_dir"] = DB_DIR
        result["_mode"] = "passphrase"
        out_file = "all_keys.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n密钥已保存到: {out_file}")
    else:
        print("\n未能找到有效 passphrase")


if __name__ == "__main__":
    main()
