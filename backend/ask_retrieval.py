"""Scope-first Chinese FTS retrieval with bounded, explicit coverage."""
import json
import re
from datetime import datetime, timedelta
from backend.contracts import ServiceError, parse_time, iso_time, stable_id
from backend.message_identity import identified
from backend.redaction import redact


def tokenize(text):
    # Chinese words/names need two-character matches; Unicode61 alone won't split them.
    terms = re.findall(r'[a-z0-9_]+', text.lower())
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        terms.extend(run[i:i+n] for n in (1, 2, 3) for i in range(len(run)-n+1))
    return ' '.join(terms)


class Retrieval:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def scope(self, raw=None):
        state = self.store.settings()
        if not state['source']: raise ServiceError('not_connected', '请先连接来源并同步需要查询的聊天。')
        defaults = [c['chat_id'] for c in self.store.chats() if c['analysis_enabled']]
        raw = raw or {}
        if not isinstance(raw, dict) or set(raw) - {'chat_ids', 'time_start', 'time_end', 'person', 'include_results'}:
            raise ServiceError('invalid_ask_scope', '查询范围格式无效。')
        ids = raw.get('chat_ids', defaults)
        available = {c['chat_id'] for c in self.store.chats()}
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or any(not isinstance(i, str) or i not in available for i in ids):
            raise ServiceError('invalid_ask_scope', '请选择当前账号中的聊天。')
        start, end = raw.get('time_start') or state['time_start'], raw.get('time_end') or iso_time(self.service.clock())
        if parse_time(start) >= parse_time(end) or parse_time(end) > self.service.clock()+60:
            raise ServiceError('invalid_ask_scope', '开始时间需早于结束时间，结束时间不能晚于现在。')
        person = raw.get('person', '')
        if not isinstance(person, str) or len(person) > 100 or type(raw.get('include_results', True)) is not bool:
            raise ServiceError('invalid_ask_scope', '人物或历史结果范围无效。')
        return {'account_id': state['source']['account_id'], 'source_hash': stable_id(state['source']),
            'chat_ids': sorted(set(ids)), 'time_start': iso_time(parse_time(start)), 'time_end': iso_time(parse_time(end)),
            'person': person.strip(), 'include_results': raw.get('include_results', True)}

    def index(self, scope, cancelled=lambda: False):
        last = ''
        # Transactions are kept short; checkpoints per message survive restarts.
        while not cancelled():
            with self.store.database() as db:
                rows = db.execute('SELECT m.message_id,m.content,m.sender_name,m.chat_id,COALESCE(c.display_name,c.chat_name,m.chat_id) chat_name FROM messages m LEFT JOIN chats c ON c.account_id=m.account_id AND c.chat_id=m.chat_id WHERE m.account_id=? AND m.message_id>? ORDER BY m.message_id LIMIT 250',
                    (scope['account_id'], last)).fetchall()
                if not rows: return
                for r in rows:
                    last = r['message_id']
                    text = (r['content'] or '') + ' ' + (r['sender_name'] or '') + ' ' + r['chat_name']
                    digest = stable_id(text)
                    old = db.execute('SELECT content_hash FROM ask_index WHERE message_id=?', (last,)).fetchone()
                    if old and old[0] == digest: continue
                    db.execute('DELETE FROM ask_fts WHERE message_id=?', (last,))
                    db.execute('INSERT INTO ask_fts(message_id,tokens) VALUES(?,?)', (last, tokenize(text)))
                    db.execute('INSERT OR REPLACE INTO ask_index VALUES(?,?)', (last, digest))

    def retrieve(self, scope, question, cancelled=lambda: False):
        self.index(scope, cancelled)
        ids = scope['chat_ids']
        where = 'm.account_id=? AND m.chat_id IN (' + ','.join('?' for _ in ids) + ') AND m.sent_epoch>=? AND m.sent_epoch<=?'
        args = [scope['account_id'], *ids, parse_time(scope['time_start']), parse_time(scope['time_end'])]
        # Relative dates intersect the user's authorized range, never expand it.
        now = datetime.fromtimestamp(self.service.clock()).astimezone()
        relative = None
        if '上周' in question:
            end = (now-timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            relative = (end-timedelta(days=7), end-timedelta(microseconds=1))
        elif '本周' in question: relative = ((now-timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0), now)
        elif '昨天' in question:
            end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            relative = (end-timedelta(days=1), end-timedelta(microseconds=1))
        if relative:
            args[-2], args[-1] = max(args[-2], relative[0].timestamp()), min(args[-1], relative[1].timestamp())
        with self.store.database() as db:
            if not scope['person']:
                names = db.execute('SELECT m.sender_name,COUNT(DISTINCT m.sender_id) count FROM messages m WHERE '+where+" AND m.sender_name IS NOT NULL GROUP BY m.sender_name", args).fetchall()
                for name in names:
                    if len(name['sender_name']) >= 2 and name['sender_name'] in question and name['count'] > 1:
                        raise ServiceError('ask_person_ambiguous', '问题中的“'+name['sender_name']+'”对应多个发送者，请在人物筛选中指定明确账号。')
            if scope['person']:
                people = db.execute('SELECT DISTINCT m.sender_id,m.sender_name FROM messages m WHERE '+where+
                    ' AND (m.sender_id=? OR m.sender_name=?)', (*args, scope['person'], scope['person'])).fetchall()
                matches = {r['sender_id'] for r in people if r['sender_id']}
                if len(matches) > 1:
                    raise ServiceError('ask_person_ambiguous', '这个名字对应多个账号，请在人物筛选中填写明确的发送者账号：' + '、'.join(sorted(matches)))
                if not people: raise ServiceError('ask_person_missing', '当前范围未找到该发送者，请核对备注、昵称或账号。')
                if matches: where += ' AND m.sender_id=?'; args.append(next(iter(matches)))
                else: where += ' AND m.sender_id IS NULL AND m.sender_name=?'; args.append(scope['person'])
            total = db.execute('SELECT COUNT(*) FROM messages m WHERE '+where, args).fetchone()[0]
            readable_where = where + " AND m.message_type='text' AND m.content_state='text_available' AND m.content IS NOT NULL"
            readable = db.execute('SELECT COUNT(*) FROM messages m WHERE '+readable_where, args).fetchone()[0]
            select = 'SELECT m.*,COALESCE(c.display_name,c.chat_name,m.chat_id) chat_name FROM messages m LEFT JOIN chats c ON c.account_id=m.account_id AND c.chat_id=m.chat_id '
            exhaustive = bool(re.search(r'全部|所有|统计|多少|数量|列出|汇总|有哪些|清单|列举', question))
            matched = readable
            if exhaustive:
                rows = db.execute(select+'WHERE '+readable_where+' ORDER BY m.sent_epoch,m.message_id LIMIT 301', args).fetchall()
            else:
                # Keep explicit short names and project names, drop grammatical stop words.
                cleaned = re.sub(r'上周|本周|昨天|今天|最近|请问|帮我|查找|关于|什么|哪些|如何|是否|一下|进展|讨论|情况|最终|结论|相关|消息|聊天', ' ', question)
                terms = sorted(set(tokenize(cleaned).split()), key=lambda x: (-len(x), x))
                terms = [t for t in terms if len(t) >= 2][:80]
                expr = ' OR '.join('"'+t+'"' for t in terms)
                if expr:
                    sub = 'm.message_id IN (SELECT message_id FROM ask_fts WHERE ask_fts MATCH ?)'
                    matched = db.execute('SELECT COUNT(*) FROM messages m WHERE '+readable_where+' AND '+sub, (*args, expr)).fetchone()[0]
                    rows = db.execute(select+'WHERE '+readable_where+' AND '+sub+' ORDER BY m.sent_epoch DESC LIMIT 60', (*args, expr)).fetchall()
                else:
                    rows = db.execute(select+'WHERE '+readable_where+' ORDER BY m.sent_epoch DESC LIMIT 60', args).fetchall()
                neighbors = {}
                for r in rows:
                    for n in db.execute(select+'WHERE '+readable_where+' AND m.chat_id=? ORDER BY ABS(m.sent_epoch-?),m.sent_epoch LIMIT 5', (*args, r['chat_id'], r['sent_epoch'])):
                        neighbors[n['message_id']] = n
                rows = sorted(neighbors.values(), key=lambda r: (r['sent_epoch'], r['message_id']))
            rows = identified([dict(r) for r in rows], self.store.settings()['source'])
            evidence, chars, clipped = [], 0, False
            for r in rows[:300]:
                body = redact(r['content'])[0]
                if chars + len(body) > 100000: clipped = True; break
                chars += len(body)
                evidence.append({'label': 'M'+str(len(evidence)+1), 'message_id': r['message_id'],
                    'chat_id': r['chat_id'], 'chat_name': redact(r['chat_name'])[0],
                    'sender_name': redact(r['sender_name'] or '发送者未知')[0], 'sent_at': r['sent_at'],
                    'speaker': '本人' if r['effective_is_self'] is True else '他人' if r['effective_is_self'] is False else '身份未确认', 'content': body})
            results = []; result_chars = 0
            if scope['include_results']:
                allowed = {e['message_id'] for e in evidence}
                for table, columns in [('tasks','task_id,title,status,source_message_ids'), ('attention_items','attention_id,title,summary,source_message_ids')]:
                    for r in db.execute('SELECT '+columns+' FROM '+table+' WHERE account_id=?', (scope['account_id'],)):
                        refs = json.loads(r['source_message_ids'])
                        if refs and set(refs) <= allowed:
                            item = {k: redact(v)[0] if isinstance(v, str) else v for k,v in dict(r).items()}
                            size = len(json.dumps(item, ensure_ascii=False))
                            if result_chars + size > 16000: continue
                            results.append(item); result_chars += size
                        if len(results) >= 100: break
            coverage = {'total_messages': total, 'readable_messages': readable, 'matched_messages': matched,
                'evidence_messages': len(evidence), 'exhaustive': exhaustive,
                'complete': exhaustive and len(evidence) == readable and readable == total and not clipped,
                'truncated': clipped or len(rows) > 300 or (exhaustive and readable > len(evidence)),
                'time_start': iso_time(args[1+len(ids)]), 'time_end': iso_time(args[2+len(ids)]),
                'result_count': len(results)}
            return evidence, results, coverage
