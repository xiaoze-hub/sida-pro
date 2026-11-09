def test_no_date_pool_excludes_stale_last_bars(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector
    import marketdata.vendors.tq as tq
    def rpc(method, params, timeout=None):
        if method == 'get_stock_list':
            return [{'Code':'600001.SH','Name':'旧股'}, {'Code':'600002.SH','Name':'新股'}]
        return {'600001.SH':{'Date':['20261029','20261030'],'Close':[10,11]}, '600002.SH':{'Date':['20261106','20261109'],'Close':[10,11]}}
    monkeypatch.setattr(tq, 'tq_rpc', rpc)
    monkeypatch.setattr(MarketSentimentCollector, '_enrich_pool_blocks', lambda self, pool, **kw: pool)
    rows=MarketSentimentCollector()._limit_up_pool_tq()
    assert [row['code'] for row in rows] == ['600002']
    assert rows[0]['as_of'] == '2026-11-09'


def test_breadth_list_includes_top_count_beyond_score_cutoff():
    from src.core.market_mainline import aggregate_mainline
    pool = [{'code': f'a{i}', 'days':1, 'concepts':['商业航天']} for i in range(13)]
    for j in range(20):
        pool.extend({'code':f'b{j}-{i}', 'days':3, 'concepts':[f'题材{j:02d}']} for i in range(3))
    r=aggregate_mainline(pool)
    assert any(g['name']=='商业航天' and g['limit_up_count']==13 for g in r['concept_breadth_leaders'])
