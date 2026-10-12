"""Bounded native SSE adapters. No retries, redirects, body logs or hidden thoughts."""
import json
import re
import threading
import urllib.parse
import urllib.request
import urllib.error
import http.client
from backend.contracts import ServiceError
from backend.gemini import GeminiClient, normalize_endpoint, model_name, http_failure, transport_failure


def openai_endpoint(value):
    try:
        p = urllib.parse.urlsplit(value.strip())
        if (p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment
                or p.port not in {None, 443} or len(value) > 1000 or not re.fullmatch(r'[/A-Za-z0-9._-]*', p.path)):
            raise ValueError()
        path = p.path.rstrip('/')
        if path.endswith('/chat/completions'): path = path[:-17]
        return urllib.parse.urlunsplit(('https', p.netloc.lower(), path, '', ''))
    except (ValueError, AttributeError, TypeError):
        raise ServiceError('invalid_model_address', '请输入不含密钥、账号或参数的 HTTPS 接口地址。') from None


def openai_model(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', value):
        raise ServiceError('invalid_model_name', '模型名称格式无效。')
    return value


class StreamControl:
    def __init__(self):
        self.event = threading.Event()
        self.response = None
        self.lock = threading.Lock()

    def cancel(self):
        self.event.set()
        # Do not block a Stop command on a response currently being read.
        def close():
            with self.lock: response = self.response
            if response:
                try: response.close()
                except Exception: pass
        threading.Thread(target=close, daemon=True).start()

    def check(self):
        if self.event.is_set():
            raise ServiceError('ask_stopped', '回答已停止；已发送请求仍可能计费。')


class AskProvider(GeminiClient):
    def stream(self, config, key, prompt, control):
        native = config['protocol'] == 'gemini'
        if native:
            base = normalize_endpoint(config['base_url'])
            url = base + ('' if base.endswith('/v1beta') else '/v1beta') + '/models/' + model_name(config['model']) + ':streamGenerateContent?alt=sse'
            payload = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}]}
            headers = {'x-goog-api-key': key}
        else:
            url = openai_endpoint(config['base_url']) + '/chat/completions'
            payload = {'model': config['model'], 'messages': [{'role': 'user', 'content': prompt}],
                       'stream': True, 'stream_options': {'include_usage': True}}
            headers = {'Authorization': 'Bearer ' + key}
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={**headers, 'Content-Type': 'application/json', 'Accept': 'text/event-stream'}, method='POST')
        control.check()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                with control.lock: control.response = response
                control.check()
                media = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                if media == 'application/json':
                    raw = response.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024: raise ValueError()
                    data = json.loads(raw)
                    text, finish, usage = self.parse(data, native, streaming=False)
                    control.check()
                    yield {'text': text.replace(key, '[访问密钥]')}
                    if finish not in {'STOP', 'stop'}: raise ValueError()
                    yield {'usage': usage, 'finished': True}
                    return
                if media != 'text/event-stream': raise ValueError()
                pending, total, finished, done, usage = [], 0, False, False, {}
                # Keep a small tail so a reflected secret split across chunks cannot leak.
                tail = ''
                for raw in response:
                    control.check()
                    total += len(raw)
                    if total > 4 * 1024 * 1024 or len(raw) > 512 * 1024: raise ValueError()
                    line = raw.decode('utf-8').rstrip('\r\n')
                    if line.startswith('data:'): pending.append(line[5:].lstrip())
                    if line or not pending: continue
                    data = '\n'.join(pending); pending = []
                    if data == '[DONE]': done = True; break
                    text, finish, counts = self.parse(json.loads(data), native)
                    if counts: usage = counts
                    if finish:
                        if finish not in {'STOP', 'stop'}: raise ValueError()
                        finished = True
                    tail = (tail + text).replace(key, '[访问密钥]')
                    if len(tail) > len(key):
                        chunk, tail = tail[:-len(key)], tail[-len(key):]
                        yield {'text': chunk}
                if pending or not finished or (not native and not done):
                    raise ServiceError('model_response_incomplete_unknown', '连接结束前未取得完整回答，可能已计费；不会自动重发。')
                if tail: yield {'text': tail.replace(key, '[访问密钥]')}
                yield {'usage': usage, 'finished': True}
        except urllib.error.HTTPError as e:
            media = e.headers.get('Content-Type', '') if e.headers else ''
            status = e.code; e.close()
            raise http_failure(status, media.split(';')[0]) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, EOFError) as e:
            control.check()
            raise transport_failure(e) from None
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            control.check()
            raise ServiceError('model_response_invalid', '服务未返回完整、可解析的回答；已收到的文字保留为部分回答。') from None
        finally:
            with control.lock: control.response = None

    @staticmethod
    def parse(data, native, streaming=True):
        if 'error' in data: raise ValueError()
        if native:
            candidates = data.get('candidates', [])
            if len(candidates) > 1: raise ValueError()
            c = candidates[0] if candidates else {}
            text = ''.join(p.get('text', '') for p in c.get('content', {}).get('parts', []) if not p.get('thought'))
            finish = c.get('finishReason')
            u = data.get('usageMetadata')
            fields = ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount')
        else:
            choices = data.get('choices', [])
            if len(choices) > 1: raise ValueError()
            c = choices[0] if choices else {}
            content = c.get('delta' if streaming else 'message', {})
            if content.get('tool_calls'): raise ValueError()
            text = content.get('content') or ''
            finish = c.get('finish_reason'); u = data.get('usage')
            fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
        counts = {name: u.get(field) if type(u.get(field)) is int and u.get(field) >= 0 else None
            for name, field in zip(('input_tokens', 'output_tokens', 'total_tokens'), fields)} if isinstance(u, dict) else {}
        return text, finish, counts
