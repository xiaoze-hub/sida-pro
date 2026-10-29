def test_propose_action_describes_date_only_marks_and_limitations():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    function = CHAT_TOOL_REGISTRY['propose_action'].schema['function']
    price = function['parameters']['properties']['price']['description']
    ts = function['parameters']['properties']['ts']['description']
    assert 'price 或 ts' in price
    assert 'YYYY-MM-DD' in ts
    assert '日线' in ts
    assert '单独给 ts 会被拒绝' not in ts
