from src.harness.routing import route_for_request


def test_tail_session_market_screen_has_scan_tool():
    schemas = [{'type': 'function', 'function': {'name': x}} for x in ('get_market_scan_ranks', 'get_intraday_profile')]
    route = route_for_request('今天尾盘有适合建仓的股票吗，可以有创业板', schemas)
    assert route.recipe is not None and route.recipe.scope == 'market'
    assert 'get_market_scan_ranks' in [s['function']['name'] for s in route.tools]
