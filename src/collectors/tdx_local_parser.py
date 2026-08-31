"""
TDX (TongDaXin) local file parser for:
  1. Northbound (陆股通) broker-level position data (signals_sys_*.dat)
  2. Concept / theme membership data (extern_sys.txt)

Both files are plain-text pipe-delimited formats sourced from
C:\\new_tdx64\\T0002\\signals\\ on the TDX Windows client.
"""

from __future__ import annotations

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


def parse_northbound_positions(
    file_bytes: bytes,
    broker_id: str,
    broker_name: str,
) -> List[Dict[str, str | float]]:
    """Parse a single TDX signals_sys_*.dat file for northbound broker positions.

    The file is UTF-8 encoded, pipe-delimited, with one record per line:
        0|stock_code|date_YYYYMMDD|amount

    Args:
        file_bytes: Raw bytes of the signals_sys_*.dat file.
        broker_id: The broker number (e.g. "20001") for inclusion in output.
        broker_name: The broker name (e.g. "汇丰银行") for inclusion in output.

    Returns:
        A list of dicts with keys: code, date, amount, broker_id, broker_name.
        Invalid lines (wrong field count, non-numeric amount, etc.) are
        skipped with a warning.
    """
    results: List[Dict[str, str | float]] = []

    if not file_bytes:
        logger.warning("parse_northbound_positions: empty input bytes")
        return results

    # Normalise line endings before splitting
    text = file_bytes.decode("utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        parts = stripped.split("|")
        if len(parts) != 4:
            logger.debug(
                "Skipping line %d: expected 4 pipe-delimited fields, got %d",
                lineno,
                len(parts),
            )
            continue

        prefix, code, date_str, amount_str = parts
        if prefix != "0":
            logger.debug(
                "Skipping line %d: expected prefix '0', got '%s'",
                lineno,
                prefix,
            )
            continue

        # Basic date format sanity (YYYYMMDD)
        if not (len(date_str) == 8 and date_str.isdigit()):
            logger.debug(
                "Skipping line %d: invalid date '%s'",
                lineno,
                date_str,
            )
            continue

        try:
            amount = float(amount_str)
        except (ValueError, TypeError):
            logger.debug(
                "Skipping line %d: non-numeric amount '%s'",
                lineno,
                amount_str,
            )
            continue

        results.append(
            {
                "code": code,
                "date": date_str,
                "amount": amount,
                "broker_id": broker_id,
                "broker_name": broker_name,
            }
        )

    return results


def parse_concept_membership(text: str) -> Dict[str, List[str]]:
    """Parse the TDX extern_sys.txt concept-membership file.

    The file is pipe-delimited (GBK-encoded on disk), with one record per line:
        0|stock_code|concept_id|concept_name1,concept_name2,...|0.00

    The |0.00 trailer is a constant placeholder and is ignored.

    Args:
        text: The full text content of extern_sys.txt, decoded from GBK to str.

    Returns:
        A dict mapping stock code (e.g. "000001") to a list of concept names.
        Lines that cannot be parsed are skipped with a warning.
    """
    result: Dict[str, List[str]] = {}

    if not text:
        logger.warning("parse_concept_membership: empty input text")
        return result

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        parts = stripped.split("|")
        # Expected: 5 fields — 0, code, concept_id, concepts_csv, 0.00
        if len(parts) < 4:
            logger.debug(
                "Skipping line %d: expected ≥4 pipe-delimited fields, got %d",
                lineno,
                len(parts),
            )
            continue

        prefix = parts[0]
        if prefix != "0":
            logger.debug(
                "Skipping line %d: expected prefix '0', got '%s'",
                lineno,
                prefix,
            )
            continue

        code = parts[1]
        if not code:
            logger.debug("Skipping line %d: empty stock code", lineno)
            continue

        # The concept names are in field index 3 (0-indexed)
        concepts_raw = parts[3] if len(parts) > 3 else ""
        concept_names = [
            c.strip() for c in concepts_raw.split(",") if c.strip()
        ]

        if code in result:
            # Merge: add any concepts not already seen for this code
            existing = set(result[code])
            for name in concept_names:
                if name not in existing:
                    result[code].append(name)
        else:
            result[code] = concept_names

    return result


def load_datacfg_mapping(file_bytes: bytes) -> Dict[str, str]:
    """Parse datacfg.sys to extract broker-id → broker-name mapping.

    The file is GBK-encoded, pipe-delimited:
        broker_id|type|date|broker_name
    where type=1 indicates a broker northbound position entry.

    Args:
        file_bytes: Raw bytes of datacfg.sys.

    Returns:
        A dict mapping broker_id -> broker_name (e.g. {"20001": "汇丰银行陆股通持股(万股)"}).
    """
    mapping: Dict[str, str] = {}
    if not file_bytes:
        return mapping

    text = file_bytes.decode("gbk", errors="replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split("|")
        if len(parts) >= 4 and parts[1] == "1":
            mapping[parts[0]] = parts[3]

    return mapping