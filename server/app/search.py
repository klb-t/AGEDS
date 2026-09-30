from __future__ import annotations
from .db import session

def search(q: str, limit: int = 100):
    if not q.strip():
        return []
    # Snippets contain untrusted source text. Keep them plain so HTML clients
    # can autoescape the entire fragment; source markup must never become UI.
    with session() as db:
        rows=db.execute("""SELECT object_kind,object_id,title,snippet(search_fts,3,'','','…',24) AS snippet,bm25(search_fts) AS rank
                           FROM search_fts WHERE search_fts MATCH ? ORDER BY rank LIMIT ?""",(q,limit)).fetchall()
        return [dict(r) for r in rows]
