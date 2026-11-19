"""
微信 4.1.x passphrase 模式密钥派生器

微信 4.1.x 不再把 raw key 长期放在内存里，而是使用 SQLCipher 4 passphrase 模式：
  key_material = PBKDF2-HMAC-SHA512(passphrase, salt, 256000, 64)
  enc_key  = key_material[:32]
  hmac_key = key_material[32:]

本脚本从 wx_key 等工具获取的 64 位 hex passphrase 出发，
为每个数据库派生对应的 enc_key / hmac_key，并生成 all_keys.json。

使用方法：
  1. 用 wx_key 提取 passphrase（64 位 hex 字符串）
  2. 在 config.json 中加入："passphrase": "aabbcc..."
  3. python derive_keys_from_passphrase.py
  4. 运行 monitor_web.py / monitor.py 开始实时监控
"""
import hashlib
import hmac as hmac_mod
import json
import os
import struct
import sys

from key_scan_common import collect_db_files
from key_utils import strip_key_metadata
from config import load_config

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
KDF_ITER = 256000


def verify_passphrase_key(enc_key, hmac_key, db_page1):
    """用 SQLCipher 4 passphrase 模式校验 page 1。"""
    salt = db_page1[:SALT_SZ]
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(hmac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def derive_keys_from_passphrase(passphrase_hex, db_dir):
    """派生所有数据库密钥，返回 {rel_path: {enc_key, hmac_key, salt, size_mb}}。"""
    try:
        passphrase = bytes.fromhex(passphrase_hex.strip())
    except ValueError:
        raise ValueError("passphrase 必须是 64 位 hex 字符串")

    if len(passphrase) != 32:
        print(f"[WARN] passphrase 长度为 {len(passphrase)} 字节，标准应为 32 字节")

    db_files, salt_to_dbs = collect_db_files(db_dir)
    print(f"找到 {len(db_files)} 个数据库")

    result = {}
    ok_count = 0

    for rel, path, sz, salt_hex, page1 in db_files:
        salt = bytes.fromhex(salt_hex)
        key_material = hashlib.pbkdf2_hmac(
            "sha512", passphrase, salt, KDF_ITER, dklen=KEY_SZ * 2
        )
        enc_key = key_material[:KEY_SZ]
        hmac_key = key_material[KEY_SZ:]

        if verify_passphrase_key(enc_key, hmac_key, page1):
            result[rel] = {
                "enc_key": enc_key.hex(),
                "hmac_key": hmac_key.hex(),
                "salt": salt_hex,
                "size_mb": round(sz / 1024 / 1024, 1)
            }
            ok_count += 1
            print(f"  OK: {rel}")
        else:
            print(f"  FAIL: {rel} (passphrase 校验失败)")

    print(f"\n结果: {ok_count}/{len(db_files)} 个数据库密钥派生成功")
    return result


def main():
    print("=" * 60)
    print("  微信 4.1.x passphrase 密钥派生")
    print("=" * 60)

    cfg = load_config()
    db_dir = cfg["db_dir"]
    out_file = cfg.get("keys_file", "all_keys.json")

    passphrase = cfg.get("passphrase", "")
    if not passphrase:
        print("[ERROR] config.json 中未配置 passphrase")
        print("        请先用 wx_key 等工具提取 passphrase，然后写入 config.json:")
        print('        "passphrase": "aabbccdd..."')
        sys.exit(1)

    print(f"\n数据库目录: {db_dir}")
    print(f"输出文件: {out_file}")

    result = derive_keys_from_passphrase(passphrase, db_dir)

    if not result:
        print("[ERROR] 未能为任何数据库派生有效密钥")
        sys.exit(1)

    result["_db_dir"] = db_dir
    result["_mode"] = "passphrase"

    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n密钥已保存到: {out_file}")
    print("现在可以运行: python monitor_web.py")


if __name__ == "__main__":
    main()
