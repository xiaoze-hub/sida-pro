import sys, traceback
sys.path.insert(0, "/app")
try:
    from src.web.api import theme_mood as tm
    print("module ok", tm)
    # find route handlers
    for name in dir(tm):
        if name.startswith("get_") or name in ("board", "ladder"):
            print("attr", name, getattr(tm, name))
except Exception:
    traceback.print_exc()
    sys.exit(1)

# try call board endpoint function if exists
try:
    import inspect
    src = inspect.getsource(tm)
    print("has ladder", "def " in src)
    # list router routes
    if hasattr(tm, "router"):
        for r in tm.router.routes:
            print("route", getattr(r, "path", r), getattr(r, "methods", None))
except Exception:
    traceback.print_exc()
