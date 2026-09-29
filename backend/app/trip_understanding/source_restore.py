"""Owner-scoped source fragment credentials and duplicate-safe restoration."""
import json
import re
from hashlib import sha256
from uuid import uuid5, NAMESPACE_URL
from cryptography.fernet import InvalidToken
from app.trip_understanding.candidates import _cipher
from app.trip_understanding.errors import CommandTargetChangedError


def source_fragments(text, resource, now, restored):
    version=sha256(text.encode()).hexdigest()
    day=None
    fragments=[]
    for match in re.finditer(r'[^\n]+', text):
        raw=match.group().strip()
        heading=re.match(r'^[#*\s]*Day\s*(\d+)', raw, re.I)
        body_start=match.start()
        if heading:
            day=int(heading[1])
            inline=re.match(r'^[#*\s]*Day\s*\d+\s*[：:]\s*',match.group(),re.I)
            if not inline:
                continue
            body_start=match.start()+inline.end()
            raw=text[body_start:match.end()]
        if not raw or raw.startswith('#') or re.match(r'^[*\s]*(主打|主题)[：:]',raw):
            continue
        for start in range(body_start, match.end(), 600):
            end=min(start+600, match.end())
            fragment_id=uuid5(NAMESPACE_URL,f'{version}:{start}:{end}').hex
            body={'resource':resource,'version':version,'start':start,'end':end,
                'text':text[start:end],'fragment_id':fragment_id,'expires':now.timestamp()+3600}
            token=_cipher().encrypt(json.dumps(body,ensure_ascii=False).encode()).decode()
            fragments.append({'fragment_id':fragment_id,'source_version':version,'start':start,'end':end,
                'day_index':day,'text':text[start:end],'source_token':token,'restored':fragment_id in restored})
    return fragments


def verify_fragment(token, *, resource, source_hash, now):
    try:
        body=json.loads(_cipher().decrypt(token.encode()))
        if body['resource']!=resource or body['version']!=source_hash or body['expires']<=now.timestamp():
            raise ValueError('source changed')
        if not 0<=body['start']<body['end'] or body['end']-body['start']!=len(body['text']):
            raise ValueError('invalid fragment')
        return body
    except (InvalidToken, ValueError, TypeError, KeyError) as exc:
        raise CommandTargetChangedError('source fragment expired or changed') from exc


def note_text(text):
    value=re.sub(r'^[\s*\-]*(?:上午|中午|下午|傍晚|晚上)[*\s]*[：:]', '', text).replace('**','')
    value=re.sub(r'\d+(?:\.\d+)?(?:\s*[-–~至]\s*\d+(?:\.\d+)?)?\s*(?:小时|分钟)', '', value)
    return value.strip()[:600] or '原文备注'
