"""Unit tests for src.collectors.tdx_local_parser.

Uses real sample fixtures pulled from the TDX Windows client
(C:\\new_tdx64\\T0002\\signals\\). No fabricated data.
"""
import os
from pathlib import Path

import pytest

from src.collectors.tdx_local_parser import (
    load_datacfg_mapping,
    parse_concept_membership,
    parse_northbound_positions,
)

FIXTURES = Path(__file__).parent / "fixtures"

NORTHBOUND_FIXTURE = FIXTURES / "tdx_signals_sys_20001_sample.txt"  # 100 real lines
CONCEPT_FIXTURE = FIXTURES / "tdx_extern_sys_sample.txt"  # 100 real lines
DATACFG_FIXTURE = FIXTURES / "tdx_datacfg_sample.sys"  # real broker mapping


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


# ---------------------------------------------------------------------------
# parse_northbound_positions
# ---------------------------------------------------------------------------

def test_parse_northbound_real_file():
    """Parse the real signals_sys_20001.dat sample (100 lines)."""
    data = _read_bytes(NORTHBOUND_FIXTURE)
    result = parse_northbound_positions(data, "20001", "汇丰银行")

    # 100 real lines, all clean -> 100 records
    assert len(result) == 100

    first = result[0]
    assert first["code"] == "000001"
    assert first["date"] == "20230828"
    assert isinstance(first["amount"], float)
    assert first["amount"] == pytest.approx(35366.98)
    assert first["broker_id"] == "20001"
    assert first["broker_name"] == "汇丰银行"

    # Each record carries the broker metadata
    assert all(r["broker_id"] == "20001" for r in result)
    # Dates are 8-digit numeric
    assert all(len(r["date"]) == 8 and r["date"].isdigit() for r in result)


def test_parse_northbound_real_file_spot_check():
    """Spot-check known rows from the real sample (code 000001 chronology)."""
    data = _read_bytes(NORTHBOUND_FIXTURE)
    result = parse_northbound_positions(data, "20001", "汇丰银行")

    # 000001 appears repeatedly across trading dates; grab its last row
    rows_for_000001 = [r for r in result if r["code"] == "000001"]
    assert rows_for_000001  # must exist

    # Compare against raw fixture for the last 000001 row: raw line equals
    # 0|000001|<date>|<amount> for the most recent 000001 entry.
    raw_text = data.decode("utf-8").replace("\r\n", "\n")
    last = rows_for_000001[-1]
    expected_prefix = f"0|000001|{last['date']}|{last['amount']:.2f}"
    assert expected_prefix in raw_text


def test_parse_northbound_empty():
    assert parse_northbound_positions(b"", "20001", "汇丰银行") == []


def test_parse_northbound_skips_junk_lines():
    """Malformed lines must be tolerated and skipped, never crash."""
    sample = (
        b"0|000001|20230101|100.00\r\n"
        b"garbage-line-no-pipes\r\n"
        b"0|000002|notadate|50.00\r\n"
        b"0|000003|20230103|abc\r\n"
        b"9|000004|20230104|200.00\r\n"        # wrong prefix
        b"0|000004|20230104|200.00\r\n"        # valid
    )
    result = parse_northbound_positions(sample, "20099", "测试券商")
    assert len(result) == 2
    assert result[0]["code"] == "000001"
    assert result[0]["amount"] == 100.00
    assert result[1]["code"] == "000004"
    assert result[1]["amount"] == 200.00


# ---------------------------------------------------------------------------
# parse_concept_membership
# ---------------------------------------------------------------------------

def test_parse_concept_real_file():
    """Parse the real extern_sys.txt sample (100 lines, GBK decoded to str)."""
    raw = _read_bytes(CONCEPT_FIXTURE)
    text = raw.decode("utf-8")  # fixture already stored as UTF-8
    result = parse_concept_membership(text)

    assert result, "expected non-empty concept map from real sample"

    # 000001 must be present with at least its known concepts
    assert "000001" in result
    concepts = result["000001"]
    assert "跨境支付CIPS" in concepts
    assert "罗素大盘" in concepts
    assert "农业保险" in concepts

    # Every stock code maps to a non-empty concept list
    assert all(len(v) > 0 for v in result.values())


def test_parse_concept_real_file_concept_id_ignored():
    """Field 4 (0.00 constant) and concept ID must not leak into names."""
    raw = _read_bytes(CONCEPT_FIXTURE)
    result = parse_concept_membership(raw.decode("utf-8"))
    for code, concepts in result.items():
        assert all("0.00" not in c for c in concepts)


def test_parse_concept_gbk_input():
    """The on-disk file is GBK; decoding to str then parsing must work."""
    raw = _read_bytes(CONCEPT_FIXTURE)
    # Round-trip through GBK to mimic real on-disk encoding
    text = raw.decode("utf-8").encode("gbk", errors="ignore").decode("gbk")
    result = parse_concept_membership(text)
    assert result
    assert "跨境支付CIPS" in result.get("000001", [])


def test_parse_concept_empty():
    assert parse_concept_membership("") == {}


def test_parse_concept_skips_junk():
    sample = (
        "0|000001|10001|锂电池概念,华为概念|0.00\n"
        "bad line without pipes\n"
        "0|000002|10001||0.00\n"           # empty concept list -> empty
        "9|000003|10001|垃圾|0.00\n"         # wrong prefix -> skipped
        "0|000004|10001|量子计算,空 值,AI|0.00\n"
    )
    result = parse_concept_membership(sample)
    assert result["000001"] == ["锂电池概念", "华为概念"]
    assert result["000002"] == []
    # 000003 has wrong prefix -> absent
    assert "000003" not in result
    assert result["000004"] == ["量子计算", "空 值", "AI"]


def test_parse_concept_dedup_when_repeated():
    """Same code appearing on multiple lines merges without duplicating names."""
    sample = (
        "0|000001|10001|AI,芯片|0.00\n"
        "0|000001|10001|芯片,机器人|0.00\n"
    )
    result = parse_concept_membership(sample)
    assert result["000001"] == ["AI", "芯片", "机器人"]


# ---------------------------------------------------------------------------
# load_datacfg_mapping (broker mapping helper)
# ---------------------------------------------------------------------------

def test_datacfg_mapping_real_file():
    """Parse the real datacfg.sys fixture for broker-id -> name mapping."""
    mapping = load_datacfg_mapping(_read_bytes(DATACFG_FIXTURE))
    # Real content: type=1 broker entries
    assert mapping["20001"]  # 汇丰
    assert mapping["20011"]  # 中金
    # Concept entries (type=0) must NOT appear in the broker map
    assert "10001" not in mapping
    # All broker ids present
    expected = {f"2000{i}" for i in range(1, 10)} | {"20010", "20011"}
    assert set(mapping.keys()) == expected