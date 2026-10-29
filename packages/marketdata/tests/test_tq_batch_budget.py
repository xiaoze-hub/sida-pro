"""TQ 批量快照：中途故障 / 静默网关 / 总预算（2026-10-29 复审实测）。

复现的缺陷（用户报告 + 代码核对）：
  - `TqQuoteVendor.fetch` 逐票 `get_market_snapshot`，每票用 `_TIMEOUT_S=4.0s`；
    5 只票遇到 502/超时就是 **5×4s=20s**，远超 Engine 的 `MARKETDATA_VENDOR_TIMEOUT=8s`
    ⇒ Engine 早就放弃了，TQ 线程还在继续打网关（线程杀不掉，白占池子）；
  - 预算早就花光之后，代码**仍然**接着取日线旁证、逐票 `get_more_info`（又是 5×4s），
    这些剩下的 RPC 对本次请求已经毫无意义；
  - 中途传输失败时 `break` 保留已取到的部分，但 Engine 默认 `min_count=1` 把这份
    "少几只"的结果当成功缓存 ⇒ 缺失票不会去腾讯补，也不会被标成 partial。

本文件用**真实 socket + 真实 httpx 超时**（不是 mock 时钟）量时延：
  - `_SilentGateway` 用 `http.server` 起在本机临时端口，按脚本 hang / 掉连接 / 回错误；
  - 断言的是**工程限额**（`QUOTE_SNAPSHOT_BUDGET_S` / `QUOTE_FETCH_BUDGET_S`，
    见 vendors/tq.py 注释：依据 = Engine 8s 超时 + 正常 5 只 ~0.15s），
    **不是**实测 P99 —— 生产 P99 需要真实网关采样，本机无不可达网关，不做假声明。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

pytest.importorskip("marketdata")

from marketdata.symbol import Symbol  # noqa: E402
from marketdata.vendors import tq as tqmod  # noqa: E402

CN_SYMBOLS = [Symbol.parse(c, "CN") for c in ("600519", "000001", "601318", "000651", "002361")]

_SNAP_TEMPLATE = {
    "ErrorId": "0",
    "Now": "1354.00",
    "LastClose": "1330.75",
    "Open": "1335.00",
    "Max": "1360.00",
    "Min": "1328.00",
    "Volume": "42992",
    "Amount": "182560.53",
    "Inside": "20000",
    "Outside": "22992",
}

# 真正上传到网关的调用序列（含 method + 代码），用于断言"没打没必要的 RPC"。
CALLS: list[tuple[str, str]] = []
# 实测时延（毫秒），运行后打印出来供报告引用；阈值仍是工程限额。
MEASURED_MS: dict[str, int] = {}


class _ScriptedGateway:
    """极简 TQ JSON-RPC 替身：真实 HTTP/1.1 socket，能 hang / 断连 / 回 ErrorId。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._script: list[dict] = []
        self._index = 0
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: D401  静音
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                method = body.get("method", "")
                params = body.get("params") or {}
                code = str(params.get("stock_code") or (params.get("stock_list") or [""])[0])
                with outer._lock:
                    CALLS.append((method, code))
                    step = outer._script[outer._index] if outer._index < len(outer._script) else {}
                    outer._index += 1
                action = step.get("action", "ok")
                if action == "hang":
                    time.sleep(step.get("sleep", 5.0))
                    return
                if action == "drop":
                    self.close_connection = True
                    return
                payload = {"id": 1, "result": step.get("result", {"ErrorId": "0", **(_SNAP_TEMPLATE)})}
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):  # 客户端已超时走人
                    pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/"

    def script(self, steps: list[dict]) -> None:
        with self._lock:
            self._script = steps
            self._index = 0

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=0.5)


@pytest.fixture(autouse=True)
def _fresh_state():
    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    CALLS.clear()
    MEASURED_MS.clear()
    yield
    tqmod._reset_daily_circuit()


@pytest.fixture
def gateway(monkeypatch):
    gw = _ScriptedGateway()
    monkeypatch.setattr(tqmod, "_resolve_tq_url", lambda: gw.url)
    # 预算压到"秒级以内"只为让测试快；断言的是**有界**，不是具体数值。
    monkeypatch.setattr(tqmod, "QUOTE_SNAPSHOT_BUDGET_S", 0.6)
    monkeypatch.setattr(tqmod, "QUOTE_FETCH_BUDGET_S", 1.2)
    try:
        yield gw
    finally:
        gw.close()


def _methods() -> list[str]:
    return [m for m, _ in CALLS]


def test_silent_gateway_aborts_within_budget_and_skips_daily_and_more_info(gateway):
    """第 2 只起静默：必须在快照预算内收手，且**不再**打日线/more_info。"""
    gateway.script([
        {"action": "ok"},
        *({"action": "hang", "sleep": 5.0} for _ in range(10)),
    ])
    t0 = time.monotonic()
    quotes = tqmod.TqQuoteVendor().fetch(CN_SYMBOLS, {})
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    MEASURED_MS["silent_mid_batch"] = elapsed_ms

    assert elapsed_ms < 1500, f"整批被拖成 {elapsed_ms}ms（旧实现 5×4s=20s）"
    assert [q.symbol for q in quotes] == ["600519"], "已取到的快照保留，后面没取到的不许编"
    assert quotes[0].quote_date is None and quotes[0].quote_date_basis == ""
    assert _methods() == ["get_market_snapshot", "get_market_snapshot"], (
        f"预算花光后还在打 RPC：{_methods()}"
    )
    stats = tqmod.quote_date_stats()
    assert stats.get("quote_snapshot_interrupted") == 1
    assert stats.get("quote_batch_partial") == 1


def test_budget_exhaustion_between_symbols_is_bounded(gateway):
    """每票都回但很慢：预算耗尽在**发起下一次 RPC 之前**收手，不发起 N 次。"""
    gateway.script([{"action": "ok", "sleep": 0.0}] * 10)  # 快，但把预算改小后仍可测
    tqmod.QUOTE_SNAPSHOT_BUDGET_S = 0.0  # 预算为 0 ⇒ 一次都不许打
    t0 = time.monotonic()
    quotes = tqmod.TqQuoteVendor().fetch(CN_SYMBOLS, {})
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    MEASURED_MS["zero_budget"] = elapsed_ms

    assert quotes == []
    assert _methods() == [], "预算为 0 还敢打网关"
    assert elapsed_ms < 200


def test_mid_batch_connection_drop_keeps_partial_without_daily_or_more_info(gateway):
    """中途掉连接（502/断链）：保留已取到的，但缺失票必须标缺，且不跑日线/more_info。"""
    gateway.script([{"action": "ok"}, {"action": "drop"}, {"action": "ok"}])
    quotes = tqmod.TqQuoteVendor().fetch(CN_SYMBOLS[:3], {})

    assert [q.symbol for q in quotes] == ["600519"]
    assert quotes[0].quote_date is None, "主快照没取全时不许再打日线旁证"
    assert "get_market_data" not in _methods()
    assert "get_more_info" not in _methods()
    assert tqmod.quote_date_stats().get("quote_snapshot_interrupted") == 1


def test_per_symbol_error_id_skips_only_that_symbol(gateway):
    """单标的 ErrorId 只丢该票（不是整源不可用），整批仍标 partial，且不跑逐票 more_info。"""
    gateway.script([
        {"action": "ok"},
        {"action": "ok", "result": {"ErrorId": "5", "Error": "code error"}},
        {"action": "ok", "result": {"ErrorId": "0", "Value": {}}},
    ])
    quotes = tqmod.TqQuoteVendor().fetch(CN_SYMBOLS[:2], {})

    assert [q.symbol for q in quotes] == ["600519"]
    assert tqmod.quote_date_stats().get("quote_symbol_error") == 1
    assert tqmod.quote_date_stats().get("quote_batch_partial") == 1
    assert _methods().count("get_market_data") == 1, "日线旁证只允许一次批量（固定代价）"
    assert "get_more_info" not in _methods(), "缺一只票就不该再打 N 次逐票 RPC"


def test_complete_batch_still_runs_daily_evidence_and_more_info(gateway, monkeypatch):
    """完整批次维持原有路径：日线旁证 + more_info 都照跑（别把预算改过头）。"""
    today = time.strftime("%Y%m%d")
    gateway.script([
        {"action": "ok"},
        {"action": "ok"},
        {"action": "ok", "result": {"ErrorId": "0", "Value": {
            "600519.SH": {"Date": [today], "Close": ["1354.00"], "Volume": ["4299231"]},
            "000001.SZ": {"Date": [today], "Close": ["1354.00"], "Volume": ["4299231"]},
        }}},
        {"action": "ok", "result": {"ErrorId": "0", "fHSL": "1.2"}},
        {"action": "ok", "result": {"ErrorId": "0", "fHSL": "2.3"}},
    ])
    quotes = tqmod.TqQuoteVendor().fetch(CN_SYMBOLS[:2], {})

    assert [q.symbol for q in quotes] == ["600519", "000001"]
    assert _methods().count("get_market_data") == 1, "日线必须一次批量"
    assert _methods().count("get_more_info") == 2
    assert quotes[0].quote_date is not None
    assert "quote_batch_partial" not in tqmod.quote_date_stats()
