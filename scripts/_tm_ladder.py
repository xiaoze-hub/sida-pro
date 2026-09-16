import sys, traceback, inspect
sys.path.insert(0, "/app")
from src.web.api import theme_mood as tm
print(inspect.signature(tm.get_ladder))
print(inspect.getsource(tm.get_ladder)[:1500])
try:
    r = tm.get_ladder(window=20, mode="auto")
    print("ok", type(r), str(r)[:300])
except Exception:
    traceback.print_exc()
