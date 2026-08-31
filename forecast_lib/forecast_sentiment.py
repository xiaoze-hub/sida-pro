# 情绪面: LLM 打分 + 公告 + 板块共振
import os, json, time
from datetime import datetime, timedelta



def _detect_panwatch_url() -> str:
    """自动探测 PanWatch 地址(容器网关 IP)。

    引擎跑在主机,PanWatch 在 Docker 容器内,需通过主机→容器网关访问。
    网关 IP 可能随 Docker 网络变化,不能写死。自动从 /proc/net/route 探测,
    多候选尝试连通性。
    """
    import socket as _socket

    candidates = []

    # 1. 环境变量优先
    import os as _os
    env = _os.getenv("PANWATCH_URL", "")
    if env:
        candidates.append(env.rstrip("/"))

    # 2. 默认网关(主机→Docker 网桥)
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    ip_int = int(parts[2], 16)
                    gw = f"{(ip_int & 0xFF)}.{(ip_int >> 8 & 0xFF)}.{(ip_int >> 16 & 0xFF)}.{(ip_int >> 24 & 0xFF)}"
                    candidates.append(f"http://{gw}:8000")
                    break
    except Exception:
        pass

    # 3. 常见 Docker 网桥(兜底)
    for ip in ("172.17.0.1", "172.18.0.1", "10.8.0.1"):
        candidates.append(f"http://{ip}:8000")

    # 4. 去重 + 探测连通性
    seen, checked = set(), []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        checked.append(c)
        try:
            r = _req_get(f"{c}/api/health", timeout=3)
            if r and r.status_code < 500:
                return c
        except Exception:
            continue

    # 全部失败: 用第一个候选(可能是环境变量)
    return candidates[0] if candidates else "http://172.17.0.1:8000"



def _req_get(url: str, timeout: float = 8):
    """轻量 GET(避免顶层 import requests 的副作用)。"""
    import requests as _r
    return _r.get(url, timeout=timeout)


PANWATCH_URL = _detect_panwatch_url()
print(f"[forecast] PanWatch 地址: {PANWATCH_URL}")

PANWATCH_URL = _detect_panwatch_url()
print(f'[forecast] PanWatch 地址: {PANWATCH_URL}')



def _db_llm_config() -> dict | None:
    """从 PanWatch 设置页 DB(app_settings.forecast_llm_*)读 LLM 配置。

    设置页"接口 Key"区块维护,DB 优先于本地 env 文件。Docker
    Compose 通过 PANWATCH_DB 指向共享数据卷，配置保存后下次调用即生效。
    返回 {base_url, model, api_key} 或 None(未配置/不可读)。
    """
    import os as _os
    import sqlite3 as _sqlite

    db_paths = [
        _os.getenv("PANWATCH_DB", ""),
        "/var/lib/docker/volumes/panwatch_data/_data/panwatch.db",
        "/app/data/panwatch.db",
    ]
    for p in db_paths:
        if not p or not _os.path.exists(p):
            continue
        try:
            # 以只读 URI 打开，避免预测引擎误写 PanWatch 主数据库。
            conn = _sqlite.connect(f"file:{p}?mode=ro", uri=True, timeout=3)
            try:
                rows = dict(
                    conn.execute(
                        "SELECT key, value FROM app_settings WHERE key IN "
                        "('forecast_llm_base_url','forecast_llm_model','forecast_llm_api_key')"
                    ).fetchall()
                )
            finally:
                conn.close()
            if not rows:
                return None
            cfg: dict = {}
            if rows.get("forecast_llm_base_url"):
                cfg["base_url"] = rows["forecast_llm_base_url"]
            if rows.get("forecast_llm_model"):
                cfg["model"] = rows["forecast_llm_model"]
            if rows.get("forecast_llm_api_key"):
                cfg["api_key"] = rows["forecast_llm_api_key"]
            return cfg or None
        except Exception:
            continue
    return None


def _load_llm_config() -> dict:
    """加载 LLM 情绪打分配置。

    优先级: 设置页 DB(app_settings.forecast_llm_*) > 本地配置(~/.panwatch_forecast.env)
    > PanWatch 默认 AI 模型(动态) > 硬编码兜底。
    """
    import os as _os
    import json as _json

    cfg = {"base_url": "https://api.agnes-ai.cn/v1", "model": "agnes-2.5-flash", "api_key": ""}

    # 0. 设置页 DB 优先(接口 Key 区块维护,免重启)
    db_cfg = _db_llm_config()
    if db_cfg:
        cfg.update({k: v for k, v in db_cfg.items() if v})
        return cfg

    # 1. 本地配置覆盖
    env_path = _os.path.expanduser("~/.panwatch_forecast.env")
    if _os.path.exists(env_path):
        try:
            for line in open(env_path):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "LLM_BASE_URL":
                    cfg["base_url"] = v
                elif k == "LLM_MODEL":
                    cfg["model"] = v
                elif k == "LLM_API_KEY":
                    cfg["api_key"] = v
        except Exception:
            pass

    # 2. 从 PanWatch 拉默认 AI 模型(设置页配置,动态跟随)
    if not cfg["api_key"]:
        try:
            import requests as _req
            r = _req.get(f"{PANWATCH_URL}/api/providers/services", timeout=8)
            if r.status_code == 200:
                services = r.json()
                # providers API 无鉴权返回? 若 401 需带 token,则跳过
                if isinstance(services, list):
                    for svc in services:
                        for m in svc.get("models", []):
                            if m.get("is_default"):
                                cfg["base_url"] = svc.get("base_url", cfg["base_url"])
                                cfg["model"] = m.get("model", cfg["model"])
                                cfg["api_key"] = svc.get("api_key", cfg["api_key"])
                                return cfg
        except Exception:
            pass

    # 3. 兜底: agnes key 文件
    if not cfg["api_key"]:
        key_path = _os.path.expanduser("~/.agnes_key")
        if _os.path.exists(key_path):
            cfg["api_key"] = open(key_path).read().strip()
        if not cfg["api_key"]:
            cfg["api_key"] = _os.getenv("AGNES_API_KEY", "")
    return cfg



def _fetch_tencent_fundflow_5d(symbol: str) -> dict:
    """腾讯近5日主力资金流(2026-08-12 接入, 免key)。

    返回: {"list": [{date, mainNetIn 元}...], "total_5d": 累计, "text": 摘要文本}
    失败返回 {"list": [], "total_5d": 0, "text": ""}(调用方降级, 不阻断)。
    """
    import urllib.request as _ur
    sym = symbol.replace(".SZ", "").replace(".SH", "").replace(".BJ", "")
    code = f"sh{sym}" if sym.startswith(("6", "9")) else f"sz{sym}"
    try:
        url = f"https://proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab?code={code}&type=fiveDayFundFlow&klineNeedDay=20"
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://stockapp.finance.qq.com/"})
        with _ur.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
        j = json.loads(body)
        if j.get("code") != 0:
            return {"list": [], "total_5d": 0, "text": ""}
        ff = (j.get("data") or {}).get("fiveDayFundFlow") or {}
        day_list = ff.get("DayMainNetInList") or []
        rows = []
        for d in day_list:
            try:
                rows.append({"date": str(d.get("date", ""))[:10], "mainNetIn": float(d.get("mainNetIn") or 0)})
            except Exception:
                continue
        total = sum(r["mainNetIn"] for r in rows)
        # 摘要文本(单位换算万)
        parts = [f"{r['date'][5:]}:{r['mainNetIn']/1e4:+.0f}万" for r in rows]
        text = "近5日主力净流入: " + ", ".join(parts) + f" (合计 {total/1e4:+.0f}万)"
        return {"list": rows, "total_5d": total, "text": text}
    except Exception:
        return {"list": [], "total_5d": 0, "text": ""}


def llm_sentiment_score(events_text: str, _run_id: int = 0, fundflow_text: str = "") -> dict:
    """LLM 语义情绪打分(替代关键词规则)。

    调 agnes-ai chat completions,让 LLM 判断公告/新闻情绪:
    - score: -2(重大利空) ~ +2(重大利好), 0=中性
    - reason: 一句话理由
    fundflow_text: 可选, 腾讯近5日主力资金流摘要(并入 prompt 做资金面参考)。
    失败时返回 None(调用方降级到关键词规则)。
    _run_id > 0 时: 全程 prompt/response/latency 写到 prediction_sentiment_evals。
    """
    if not events_text.strip():
        return None
    import time as _time
    _t0 = _time.monotonic()
    _resp_text = ""
    _err = ""
    try:
        import requests as _req
        import os as _os

        # 动态加载配置(设置页改模型 → 引擎自动跟随)
        cfg = _load_llm_config()
        api_key = cfg.get("api_key", "")
        base_url = cfg.get("base_url", "https://api.agnes-ai.cn/v1").rstrip("/")
        model = cfg.get("model", "agnes-2.5-flash")

        fundflow_block = f"\n资金面参考(腾讯近5日主力净流入):\n{fundflow_text}\n" if fundflow_text else ""
        prompt = f"""你是A股短线情绪分析专家。以下是一只股票最近7天的公告/新闻标题:{fundflow_block}
{events_text[:800]}

请判断这些消息对股价的短期(1-5天)影响,只输出JSON:
{{"score": -2到+2的整数, "reason": "一句话理由"}}
规则: -2=重大利空(立案/退市/清仓减持/业绩暴雷), -1=利空(小幅减持/问询),
0=中性/无关, +1=利好(中标/回购/预增), +2=重大利好(重组/大额订单/政策利好)
注意: 若消息中性但资金面持续大幅净流入, 可给+1; 若消息中性但资金面持续大幅净流出, 可给-1。"""

        _payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你只输出JSON,不输出其他文字。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
        }
        r = _req.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=_payload,
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            _resp_text = content
            # 推理模型可能把思考放 reasoning_content,content 为空
            if not content.strip():
                content = msg.get("reasoning_content") or ""
            # 提取 JSON(容错:可能包在 ```json 里)
            import re as _re
            m = _re.search(r"\{[^}]*\"score\"[^}]*\}", content)
            if m:
                data2 = json.loads(m.group(0))
                score = int(data2.get("score", 0))
                score = max(-2, min(2, score))
                _latency = int((_time.monotonic() - _t0) * 1000)
                # 2026-08-12 修复埋点: adjustment = score*0.75 与 fetch_sentiment 换算口径一致
                adj_pct = round(score * 0.75, 2)
                _record_sentiment(_run_id, events_text, prompt, _resp_text, score, data2.get("reason", ""), adj_pct, _latency, "")
                return {"score": score, "reason": data2.get("reason", ""), "source": "llm"}
            _latency = int((_time.monotonic() - _t0) * 1000)
            _record_sentiment(_run_id, events_text, prompt, _resp_text, 0, f"LLM返回无法解析: {content[:80]}", 0.0, _latency, "parse_fail")
            return {"score": 0, "reason": f"LLM返回无法解析: {content[:80]}", "source": "llm-fallback"}
        _latency = int((_time.monotonic() - _t0) * 1000)
        _record_sentiment(_run_id, events_text, prompt, _resp_text, score, reason, 0.0, _latency, _err)
        return None
    except Exception as e:
        _latency = int((_time.monotonic() - _t0) * 1000)
        _err = str(e)
        _record_sentiment(_run_id, events_text, "", "", 0, "", 0.0, _latency, _err)
        print(f"LLM情绪打分失败: {e}")
        return None



def fetch_sentiment(symbol: str, _run_id: int = 0) -> dict:
    """消息情绪面: 个股公告/新闻 + 板块共振 + 市场情绪。

    复用 PanWatch 数据体系(wudao MCP + 东财涨停池),输出事件修正系数。
    方法论: a-share-multi-model-prediction skill Pitfall #9(隔夜事件)。
    - 重大利好事件 + 板块宽度≥4 → +0.5%~+1.5%
    - 重大利空事件 → 对称下修
    - 事件日 P10-P90 区间放宽 30%
    """
    result: dict = {
        "events": [], "board_peers": 0, "market_sentiment": None,
        "adjustment_pct": 0.0, "notes": [],
    }
    try:
        # 1. wudao 个股公告/事件(近7天) — 直接 HTTP 调 MCP(与 PanWatch 容器内同机制)
        import requests as _req
        import os as _os

        wu_token = _os.getenv("WUDAO_MCP_TOKEN", "")
        if wu_token:
            wu_url = _os.getenv("WUDAO_MCP_URL", "https://stock.quicktiny.cn/api/mcp")
            wu_headers = {
                "Authorization": f"Bearer {wu_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            _req.post(wu_url, headers=wu_headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "forecast", "version": "1.0"}},
            }, timeout=15)
            _req.post(wu_url, headers=wu_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
            ev_r = _req.post(wu_url, headers=wu_headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "official_announcements", "arguments": {"stockCode": symbol, "days": 7}},
            }, timeout=30)
            ev = ev_r.json()
            content = ((ev.get("result") or {}).get("content") or [])
            if content:
                txt = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
                result["events"].append({"source": "wudao", "text": txt[:400]})
    except Exception as e:
        result["notes"].append(f"wudao公告失败: {e}")

    # 1b. 同花顺快讯(2026-08-10 接入, 免key 7×24 新闻流, 替代 wudao 额度受限)
    # 拉最近50条快讯, 按股票名称/代码匹配相关新闻(覆盖 FCC 禁令类财经新闻, 公告源不含)
    try:
        import urllib.request as _ur
        sym_name = symbol.replace(".SZ", "").replace(".SH", "").replace(".BJ", "")
        # 股票名称(东财行情)
        name = ""
        try:
            secid = f"1.{sym_name}" if sym_name.startswith(("6", "5", "9")) else f"0.{sym_name}"
            nq = _ur.Request(
                f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f58",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            nd = json.loads(_ur.urlopen(nq, timeout=6).read().decode())
            name = (nd.get("data") or {}).get("f58") or ""
        except Exception:
            pass
        news_url = "https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize=50"
        n_req = _ur.Request(news_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://news.10jqka.com.cn/",
        })
        n_body = _ur.urlopen(n_req, timeout=10).read().decode("utf-8")
        n_j = json.loads(n_body)
        n_items = n_j.get("data", {}).get("list") or n_j.get("list") or []
        # 匹配: 标题含股票名 或 代码
        matched = []
        for it in n_items:
            title = it.get("title") or it.get("digest") or ""
            if (name and name in title) or (sym_name in title):
                matched.append({
                    "source": "ths_news",
                    "title": title,
                    "date": (it.get("ctime") or ""),
                    "text": (it.get("digest") or "")[:150],
                })
        for m in matched[:6]:
            result["events"].append(m)
        if matched:
            result["notes"].append(f"同花顺快讯命中 {len(matched)} 条(名称:{name or sym_name})")
    except Exception as e:
        result["notes"].append(f"同花顺快讯失败: {e}")

    try:
        # 2. 东财公告(备用)
        import requests as _req
        url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
        params = {"sr": "-1", "page_size": "5", "page_index": "1", "ann_type": "A", "client_source": "web", "stock_list": symbol}
        r = _req.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            anns = (data.get("data") or {}).get("list") or []
            for a in anns[:5]:
                result["events"].append({
                    "source": "eastmoney",
                    "title": a.get("title", ""),
                    "date": str(a.get("notice_date", ""))[:10],
                })
    except Exception:
        pass

    # 3. 板块共振(涨停池) + 市场情绪 — 直接 HTTP 调东财
    try:
        import requests as _req2
        url = "https://push2ex.eastmoney.com/getTopicZTPool"
        params = {
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
            "Pageindex": "0", "pagesize": "60", "sort": "fbt:asc",
            "date": datetime.now().strftime("%Y%m%d"),
        }
        r = _req2.get(url, params=params, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            pool = (d.get("data") or {}).get("pool") or []
            sector_dist = {}
            for item in pool:
                sec = item.get("hybk", "") or "其他"
                sector_dist[sec] = sector_dist.get(sec, 0) + 1
            top_sectors = sorted(sector_dist.items(), key=lambda x: x[1], reverse=True)[:5]
            result["market_sentiment"] = {
                "limit_up_count": len(pool),
                "top_sectors": [{"name": k, "count": v} for k, v in top_sectors],
            }
    except Exception:
        pass

    # 4. 情绪打分: 优先 LLM 语义判断,失败降级关键词规则
    events_text = " ".join(e.get("title", "") or str(e.get("text", ""))[:100] for e in result["events"])
    adjust = 0.0

    # 4a. 腾讯近5日主力资金流(2026-08-12 接入, 并入 LLM 参考)
    fundflow = _fetch_tencent_fundflow_5d(symbol)
    if fundflow.get("text"):
        result["fundflow_5d"] = fundflow
        result["notes"].append(fundflow["text"])

    # 4b. LLM 语义打分(带资金面参考)
    llm_res = llm_sentiment_score(events_text, _run_id=_run_id, fundflow_text=fundflow.get("text", ""))
    if llm_res:
        llm_score = llm_res.get("score", 0)
        # score -2~+2 → 修正 -1.5%~+1.5% (每档 0.75%)
        adjust += llm_score * 0.75
        result["notes"].append(
            f"LLM情绪判断: {llm_score:+d} ({llm_res.get('reason', '')}) → {adjust:+.2f}%"
        )
    else:
        # 4c. 关键词规则(降级)
        bearish_kw = ["减持", "亏损", "立案", "处罚", "警示", "问询", "终止", "退市", "风险提示", "诉讼", "冻结"]
        bullish_kw = ["中标", "签约", "增持", "回购", "业绩预增", "扭亏", "获批", "订单", "涨停", "合同", "战略合作", "产能", "涨价"]

        hit_bearish = [k for k in bearish_kw if k in events_text]
        hit_bullish = [k for k in bullish_kw if k in events_text]

        if hit_bullish:
            adjust += min(1.5, 0.5 + 0.5 * len(hit_bullish))
            result["notes"].append(f"利好事件: {', '.join(hit_bullish)} → +{adjust:.1f}%")
        if hit_bearish:
            adjust -= min(1.5, 0.5 + 0.5 * len(hit_bearish))
            result["notes"].append(f"利空事件: {', '.join(hit_bearish)} → {adjust:+.1f}%")
        result["notes"].append("(关键词规则,LLM不可用)")

    # 板块宽度(涨停池 top_sectors 中是否含该股所属板块)
    ms = result.get("market_sentiment") or {}
    top_sectors = ms.get("top_sectors", [])
    if top_sectors and len(top_sectors) >= 4:
        adjust += 0.5
        result["notes"].append("市场涨停板块≥4个(情绪偏热) → +0.5%")

    result["adjustment_pct"] = round(adjust, 2)
    return result


def _record_sentiment(run_id: int, events_text: str, prompt: str, response: str,
                     score: int, reason: str, adjustment_pct: float,
                     latency_ms: int, error: str = "") -> None:
    """把一次 LLM 情绪调用写到 prediction_sentiment_evals (埋点失败不抛)。"""
    if not run_id:
        return
    try:
        try:
            from .forecast_traces import record_sentiment_eval
        except ImportError:
            from forecast_traces import record_sentiment_eval
        record_sentiment_eval(
            run_id=run_id, source="llm",
            events_text=events_text[:2000],
            score=score, reason=reason,
            adjustment_pct=adjustment_pct,
            prompt=prompt[:2000], response=response[:2000],
            latency_ms=latency_ms, error=error,
        )
    except Exception:
        pass
