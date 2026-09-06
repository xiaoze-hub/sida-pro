import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "marketdata" / "src"))

print("== TQ direct RPC ==", flush=True)
try:
    from marketdata.vendors.tq import _rpc

    r = _rpc("get_more_info", {"stock_code": "002361.SZ"})
    print("TQ_RPC_OK FCAmo=", r.get("FCAmo"), "BCancel=", r.get("BCancel"), "SCancel=", r.get("SCancel"), flush=True)
except Exception as e:
    print("TQ_RPC_FAIL:", repr(e), flush=True)

print("== Sina futures ==", flush=True)
try:
    import httpx

    resp = httpx.get(
        "https://hq.sinajs.cn/list=nf_SC0,nf_AU0",
        headers={"Referer": "https://finance.sina.com.cn"},
        timeout=8,
    )
    print("SINA_HTTP", resp.status_code, flush=True)
    print(resp.content.decode("gbk", errors="replace")[:200], flush=True)
except Exception as e:
    print("SINA_FAIL:", repr(e), flush=True)
