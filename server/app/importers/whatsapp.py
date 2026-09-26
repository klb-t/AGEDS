from __future__ import annotations
import re
from pathlib import Path
from dateutil import parser as dtparser
from ..evidence import ensure_source, ingest_file, add_event

LINE = re.compile(r'^[\[‎]?(?P<date>\d{1,2}[./-]\d{1,2}[./-]\d{2,4}),?\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)(?:\s?[APMapm.]{2,4})?[\]‎]?\s*[-–]\s*(?P<rest>.*)$')
MSG = re.compile(r'^(?P<sender>[^:]{1,120}):\s(?P<body>.*)$')

def import_whatsapp_txt(path: Path, chat_label: str | None = None) -> dict:
    source_id=ensure_source('whatsapp_export',chat_label or path.stem,str(path))
    artifact_id=ingest_file(path,source_id=source_id,source_locator=str(path),metadata={'importer':'whatsapp_txt'})
    current=None; count=0
    def flush(msg):
        nonlocal count
        if not msg:return
        add_event(source_id=source_id,artifact_id=artifact_id,event_type='whatsapp_message',ts_start=msg['ts'],sender=msg.get('sender'),thread_id=chat_label or path.stem,body=msg.get('body',''),confidence=.95,metadata={'raw_header':msg.get('header')})
        count+=1
    for raw in path.read_text(encoding='utf-8-sig',errors='replace').splitlines():
        m=LINE.match(raw)
        if m:
            flush(current)
            rest=m.group('rest'); mm=MSG.match(rest)
            sender=mm.group('sender') if mm else None; body=mm.group('body') if mm else rest
            try: ts=dtparser.parse(f"{m.group('date')} {m.group('time')}",dayfirst=True).isoformat()
            except Exception: ts=None
            current={'ts':ts,'sender':sender,'body':body,'header':raw[:200]}
        elif current:
            current['body']+='\n'+raw
    flush(current)
    return {'artifact_id':artifact_id,'messages':count}
