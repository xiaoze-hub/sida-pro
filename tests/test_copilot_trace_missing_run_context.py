"""缺失轨迹不能继承上一轮的 run 归因。"""
from types import SimpleNamespace

from src.core import llm_context
from src.harness.trace import bind_llm_context


def test_trace_unavailable_clears_previous_run_id():
    tokens = llm_context.bind(user_id='old-user', run_id='old-run', scene='chat')
    try:
        bind_llm_context(SimpleNamespace(id='new-user'), None)
        assert llm_context.current()['user_id'] == 'new-user'
        assert llm_context.current()['run_id'] is None
    finally:
        llm_context.unbind(tokens)


def test_no_user_and_no_trace_clears_both_previous_fields():
    tokens = llm_context.bind(user_id='old-user', run_id='old-run', scene='chat')
    try:
        bind_llm_context(None, None)
        assert llm_context.current() == {'user_id': None, 'run_id': None, 'scene': 'chat'}
    finally:
        llm_context.unbind(tokens)


def test_real_trace_replaces_previous_run():
    tokens = llm_context.bind(user_id='old-user', run_id='old-run')
    try:
        bind_llm_context(SimpleNamespace(id='new-user'), SimpleNamespace(run_id='new-run'))
        assert llm_context.current() == {'user_id': 'new-user', 'run_id': 'new-run', 'scene': 'chat'}
    finally:
        llm_context.unbind(tokens)
