from __future__ import annotations
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from ..evidence import ensure_source, ingest_file, add_event

def ms_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value)/1000, tz=timezone.utc).isoformat()
    except Exception:
        return None

def import_sms_backup(path: Path) -> dict:
    source_id = ensure_source('android_backup','SMS Backup & Restore',str(path))
    artifact_id = ingest_file(path,source_id=source_id,source_locator=str(path),metadata={'importer':'sms_backup'})
    count = {'calls':0,'sms':0,'mms':0}
    root = ET.parse(path).getroot()
    tag = root.tag.lower()
    if tag == 'calls':
        for c in root.findall('call'):
            a=c.attrib
            typ={'1':'incoming','2':'outgoing','3':'missed','5':'rejected'}.get(a.get('type'),a.get('type'))
            start=ms_to_iso(a.get('date'))
            dur=int(a.get('duration','0') or 0)
            end=None
            if start:
                dt=datetime.fromisoformat(start)
                end=datetime.fromtimestamp(dt.timestamp()+dur,tz=timezone.utc).isoformat()
            add_event(source_id=source_id,artifact_id=artifact_id,event_type='call',ts_start=start,ts_end=end,direction=typ,contact_label=a.get('contact_name'),phone_or_address=a.get('number'),confidence=1.0,metadata=a)
            count['calls']+=1
    elif tag == 'smses':
        for child in root:
            a=child.attrib
            if child.tag.lower() == 'sms':
                direction={'1':'incoming','2':'outgoing'}.get(a.get('type'),a.get('type'))
                add_event(source_id=source_id,artifact_id=artifact_id,event_type='sms',ts_start=ms_to_iso(a.get('date')),direction=direction,contact_label=a.get('contact_name'),phone_or_address=a.get('address'),body=a.get('body'),confidence=1.0,metadata=a)
                count['sms']+=1
            elif child.tag.lower() == 'mms':
                text=[]
                for p in child.findall('./parts/part'):
                    if p.attrib.get('ct')=='text/plain' and p.attrib.get('text'):
                        text.append(p.attrib['text'])
                add_event(source_id=source_id,artifact_id=artifact_id,event_type='mms',ts_start=ms_to_iso(a.get('date')),phone_or_address=a.get('address'),body='\n'.join(text),confidence=1.0,metadata=a)
                count['mms']+=1
    else:
        raise ValueError(f'Unsupported SMS Backup root: {root.tag}')
    return {'artifact_id':artifact_id,**count}
