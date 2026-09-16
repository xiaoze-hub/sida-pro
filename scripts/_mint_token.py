import sys
sys.path.insert(0, "/app")
from src.web.database import SessionLocal
from src.web.api.auth import create_token
from src.web.models import User

db = SessionLocal()
u = db.query(User).filter(User.username == "admin").first()
if not u:
    print("NO_ADMIN")
    sys.exit(1)
tok, _ = create_token(u)
open("/tmp/.admin_tok", "w").write(tok)
print("TOKEN_OK len", len(tok))
