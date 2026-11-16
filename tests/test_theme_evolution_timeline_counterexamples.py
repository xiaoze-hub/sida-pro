"""Independent provenance and cross-source counterexamples."""
import pytest
from src.core import theme_evolution_timeline as tl


def event(code):
    return {'date':'20261116','code':code,'labels':['测试'], 'source':'declared',
            'reference':{'file':'source.json','line':1,'sha256':'a'*64}}


def bar(code):
    return {'date':'20261116','code':code,'open':1,'high':1,'low':1,'close':1,'volume':1}


def test_unverified_reference_mapping_and_price_adjustment_are_marked_unknown():
    out=tl.build_timeline([event('a')],[bar('b')],verified_code_map={'a':'b'})
    p=out['provenance']
    assert p['reference_integrity']=='not_verified_by_timeline'
    assert p['identity_mapping_verification']=='caller_asserted_unverified'
    assert p['price_adjustment']=='unknown'
    assert '未复权' not in ''.join(out['limitations'])


def test_mapping_cannot_collide_with_direct_match():
    with pytest.raises(tl.TimelineInputError):
        tl.build_timeline([event('a'),event('b')],[bar('b')],verified_code_map={'a':'b'})
