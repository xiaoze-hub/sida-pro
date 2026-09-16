import sys, traceback, inspect
sys.path.insert(0, "/app")
from src.web.api import theme_mood as tm

print("=== get_board signature", inspect.signature(tm.get_board))
try:
    r = tm.get_board(window=20, top=15)
    print("board ok type", type(r), str(r)[:200])
except Exception:
    traceback.print_exc()

print("=== get_ladder signature", inspect.signature(tm.get_ladder))
try:
    r = tm.get_ladder(window=20)
    print("ladder ok type", type(r), str(r)[:200])
except Exception:
    traceback.print_exc()
