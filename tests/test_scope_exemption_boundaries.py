from scripts.check_scoped_queries import check_source


def test_exemption_only_applies_to_own_query_with_reason():
    source = '''\ndef unrelated(db):\n    audit()  # scoped-check: allow — 已完成无关审计\n    return db.query(Stock).all()\n'''
    assert check_source(source, {'Stock'})


def test_empty_exemption_does_not_authorize_query():
    source = '''\ndef bad(db):\n    return db.query(Stock).all()  # scoped-check: allow\n'''
    assert check_source(source, {'Stock'})


def test_string_literal_cannot_exempt_query():
    source = '''\ndef bad(db):\n    note = "scoped-check: allow — 已校验用户身份"\n    return db.query(Stock).all()\n'''
    assert check_source(source, {'Stock'})


def test_valid_inline_exemption_applies_to_that_query_only():
    source = '''\ndef okay(db):\n    return db.query(Stock).all()  # scoped-check: allow — 管理员全局视图\n'''
    assert not check_source(source, {'Stock'})
