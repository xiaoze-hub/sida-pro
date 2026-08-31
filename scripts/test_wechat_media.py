"""PanWatch 微信媒体功能验证脚本(不依赖真实微信消息/网络)。

验证:
1. media_utils 能 import(容器依赖)
2. image_to_text: Pillow 画中文文字图片 → OCR(chi_sim) 提取文字, 并留档保存
3. file_to_text: .txt / .xlsx(pandas)/ .csv 解析
4. wechat_ilink: AES-128-ECB 加解密 roundtrip + key 解析 + CDN URL 拼装
5. wechat_bot_worker._extract_text: 图片/文件/语音 item 文本化(用 monkeypatch 绕开网络)
"""
import asyncio
import base64
import os
import sys
from io import BytesIO

os.environ.setdefault("DATA_DIR", "/tmp/PanWatch/data")
sys.path.insert(0, "/tmp/PanWatch")

from PIL import Image, ImageDraw, ImageFont

# ---- 0. import 检查(容器依赖) ----
import cryptography, pytesseract, pypdf, PIL, pandas  # noqa: F401
from src.core import media_utils, wechat_ilink, wechat_bot_worker

print("[0] import OK: cryptography=%s pypdf=%s Pillow=%s pytesseract=%s pandas=%s"
      % (cryptography.__version__, pypdf.__version__, PIL.__version__,
         pytesseract.get_tesseract_version(), pandas.__version__))

# ---- 1. image_to_text: 生成中文图片 → OCR ----
img = Image.new("RGB", (640, 200), "white")
draw = ImageDraw.Draw(img)
font = ImageFont.truetype("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 48)
draw.text((30, 60), "贵州茅台 今日涨停", fill="black", font=font)
buf = BytesIO()
img.save(buf, format="PNG")
png_bytes = buf.getvalue()

text, saved_path = media_utils.image_to_text(png_bytes)
print(f"[1] OCR 结果: {text!r}")
print(f"[1] 留档路径: {saved_path}")
assert text, "OCR 未提取到任何文字"
assert any(k in text for k in ("茅台", "涨停", "贵州")), f"OCR 文字不匹配: {text!r}"
assert saved_path and os.path.exists(saved_path), "图片未留档保存"
print("[1] PASS: OCR 提取出中文文字且图片已留档\n")

# ---- 2. file_to_text: txt / csv / xlsx ----
import csv

txt = "/tmp/PanWatch/data/wx_test.txt"
with open(txt, "w", encoding="utf-8") as f:
    f.write("测试持仓:\n贵州茅台 500股\n宁德时代 200股\n")
t = media_utils.file_to_text(txt)
assert "贵州茅台" in t and "宁德时代" in t
print(f"[2] txt 解析 OK: {t.splitlines()[0]!r}")

csv_path = "/tmp/PanWatch/data/wx_test.csv"
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["代码", "名称", "涨跌幅"])
    w.writerow(["600519", "贵州茅台", "3.21%"])
    w.writerow(["300750", "宁德时代", "-1.05%"])
t = media_utils.file_to_text(csv_path)
assert "600519" in t and "贵州茅台" in t
print(f"[2] csv 解析 OK: {t.splitlines()[1] if len(t.splitlines())>1 else t!r}")

xlsx_path = "/tmp/PanWatch/data/wx_test.xlsx"
try:
    import openpyxl  # noqa: F401
    df = pandas.DataFrame({"代码": ["600519", "300750"], "名称": ["贵州茅台", "宁德时代"], "涨跌幅": [3.21, -1.05]})
    df.to_excel(xlsx_path, index=False)
    t = media_utils.file_to_text(xlsx_path)
    assert "贵州茅台" in t and "600519" in t
    print(f"[2] xlsx 解析 OK: {t.splitlines()[1] if len(t.splitlines())>1 else t!r}")
except ImportError:
    print("[2] xlsx 跳过(openpyxl 不可用)")

# 不支持的扩展名 → 空
assert media_utils.file_to_text("/tmp/PanWatch/data/wx_test.zip") == ""
print("[2] 不支持扩展名返回空 OK\n")

# ---- 3. wechat_ilink AES + key 解析 + CDN URL ----
key16 = b"0123456789abcdef"
plain = "贵州茅台今日涨停".encode("utf-8")
# PKCS7 填充 + ECB 加密(模拟 CDN 密文)
pad_len = 16 - (len(plain) % 16)
padded = plain + bytes([pad_len]) * pad_len
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
cipher = Cipher(algorithms.AES(key16), modes.ECB())
enc = cipher.encryptor().update(padded) + cipher.encryptor().finalize()

assert wechat_ilink._aes128_ecb_decrypt(enc, key16) == plain
print("[3] AES-128-ECB(PKCS7) 解密 roundtrip OK")

# key 来源 1: image_item.aeskey hex → hex→bytes→base64 → 当 b64 key
aeskey_hex = key16.hex()
b64_from_hex = base64.b64encode(bytes.fromhex(aeskey_hex)).decode("ascii")
assert wechat_ilink._parse_aes_key(b64_from_hex) == key16
print("[3] aeskey hex→b64 解析 OK")

# key 来源 2: media.aes_key 直接 b64(16 字节)
assert wechat_ilink._parse_aes_key(base64.b64encode(key16).decode()) == key16
print("[3] media.aes_key b64(16B) 解析 OK")

# key 来源 3: 32 字节 hex 文本(b64 解码后)
hex32 = "ab" * 16
b64_hex32 = base64.b64encode(hex32.encode()).decode()
assert wechat_ilink._parse_aes_key(b64_hex32) == bytes.fromhex(hex32)
print("[3] 32B hex 文本 key 解析 OK")

# CDN URL 拼装(URL 编码)
url = wechat_ilink._cdn_download_url("https://novac2c.cdn.weixin.qq.com/c2c", "a/b+c%")
assert url == "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=a%2Fb%2Bc%25", url
print(f"[3] CDN URL 拼装 OK: {url}")

# full_url 域名白名单(防 SSRF)
try:
    wechat_ilink._assert_weixin_cdn_url("http://evil.example.com/x")
    raise AssertionError("白名单未拦截")
except ValueError:
    print("[3] full_url 域名白名单拦截 OK")

# _media_ref_and_key 按类型取 media + key
item_img = {"type": 2, "image_item": {"aeskey": aeskey_hex, "media": {"encrypt_query_param": "abc", "full_url": ""}}}
media, key = wechat_ilink._media_ref_and_key(item_img)
assert key and base64.b64decode(key) == key16 and media["encrypt_query_param"] == "abc"
item_file = {"type": 4, "file_item": {"file_name": "a.xlsx", "media": {"aes_key": base64.b64encode(key16).decode()}}}
media, key = wechat_ilink._media_ref_and_key(item_file)
assert key and media == item_file["file_item"]["media"]
item_voice = {"type": 3, "voice_item": {"media": {"encrypt_query_param": "v1"}}}
media, key = wechat_ilink._media_ref_and_key(item_voice)
assert key is None and media["encrypt_query_param"] == "v1"
print("[3] _media_ref_and_key 图片/文件/语音 提取 OK\n")

# ---- 4. download_media 端到端(monkeypatch _download_bytes, 不碰网络) ----
async def _test_download():
    calls = {}
    orig = wechat_ilink._download_bytes

    async def fake_download(url, timeout_seconds=60.0):
        calls["url"] = url
        return enc  # 返回"密文"

    wechat_ilink._download_bytes = fake_download
    try:
        account = {"token": "t", "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c"}
        out = await wechat_ilink.download_media(account, item_img)
        assert out == plain, "download_media 解密结果错误"
        assert "encrypted_query_param=" in calls["url"] and "download" in calls["url"]
        print(f"[4] download_media(encrypted_query_param 路径) OK: {calls['url']}")

        # full_url 路径 + media.aes_key
        item_f = {"type": 4, "file_item": {"file_name": "r.xlsx",
                   "media": {"aes_key": base64.b64encode(key16).decode(),
                             "full_url": "https://novac2c.cdn.weixin.qq.com/c2c/f"}}}
        out = await wechat_ilink.download_media(account, item_f)
        assert out == plain
        print("[4] download_media(full_url + media.aes_key 路径) OK")

        # 无 key 明文媒体原样返回
        item_plain = {"type": 4, "file_item": {"file_name": "p.txt", "media": {"full_url": "https://novac2c.cdn.weixin.qq.com/c2c/p"}}}
        async def fake_plain(url, timeout_seconds=60.0):
            return b"RAW"
        wechat_ilink._download_bytes = fake_plain
        out = await wechat_ilink.download_media(account, item_plain)
        assert out == b"RAW"
        print("[4] download_media(无 key 原样返回) OK")
    finally:
        wechat_ilink._download_bytes = orig

asyncio.run(_test_download())

# ---- 5. worker _extract_text: 图片/文件/语音 item 文本化 ----
async def _test_extract():
    calls = {}

    async def fake_download(account, item):
        calls.setdefault("n", 0)
        calls["n"] += 1
        if item.get("type") == 2:
            return png_bytes
        if item.get("type") == 4:
            return "代码,名称\n600519,贵州茅台\n300750,宁德时代\n".encode("utf-8")
        return b"\x89PNG-fake"

    orig = wechat_ilink.download_media
    wechat_ilink.download_media = fake_download
    try:
        msg = {"item_list": [
            {"type": 1, "text_item": {"text": "帮我看看这张图"}},
            {"type": 2, "image_item": {"aeskey": "aa", "media": {"encrypt_query_param": "x"}}},
            {"type": 4, "file_item": {"file_name": "持仓.csv", "media": {"encrypt_query_param": "y"}}},
            {"type": 3, "voice_item": {"media": {"encrypt_query_param": "z"}}},
        ]}
        out = await wechat_bot_worker._extract_text(msg, {"token": "t"})
        print("[5] _extract_text 输出:\n" + out)
        assert "[图片内容]" in out and ("茅台" in out or "涨停" in out), "图片 OCR 未拼进消息"
        assert "[文件: 持仓.csv]" in out and "内容摘要" in out, "文件摘要未拼进消息"
        assert "[语音消息]" in out and "暂不支持" in out, "语音提示未拼进消息"
        # 失败降级: 坏 item 不炸
        bad = {"item_list": [{"type": 2, "image_item": {"media": {"encrypt_query_param": "broken"}}}]}
        out_bad = await wechat_bot_worker._extract_text(bad, {"token": "t"})
        assert out_bad, "失败降级应返回提示文本"
        print(f"[5] 失败降级 OK: {out_bad!r}")
    finally:
        wechat_ilink.download_media = orig

asyncio.run(_test_extract())
print("\nALL CHECKS PASSED")
