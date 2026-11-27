# -*- coding: utf-8 -*-
"""Клиент языковой модели через OpenAI-совместимый API + телеметрия каждого вызова.

Работает и с локальным сервером (Ollama, vLLM), и с облаком. Облако бывает перегружено
(429, 529): запрос повторяется с паузой, затем уходит на запасную модель. Каждый вызов
возвращает число токенов — по ним считается стоимость запроса в форме организаторов.
"""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

RETRY_STATUS = {429, 500, 502, 503, 504, 529}
# Паузы перед повторами, секунды. Перегрузка бесплатной GLM-4.7-Flash в облаке — до половины вызовов,
# но отказ приходит мгновенно и короткими сериями (замер 27.11: серии до семи подряд): чаще — важнее, чем дольше.
PAUSES = (0.5, 1, 1.5, 2, 2, 3, 3, 4, 5)
# Есть запасная модель — основную не мучаем: короткая серия, и запрос уходит в резерв
PAUSES_WITH_FALLBACK = (0.4, 0.8, 1.2, 1.6, 2)
# Модели с «размышлением» тратят на него весь лимит и не успевают ответить. Выключаем тем
# переключателем, который понимает сервер: Ollama — reasoning_effort, облако — thinking.
NO_THINKING = {'reasoning_effort': 'none', 'thinking': {'type': 'disabled'}}


@dataclass
class Reply:
    text: str = ''
    model: str = ''
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    attempts: int = 0
    error: str = ''
    ok: bool = False
    truncated: bool = False
    retry_prompt_tokens: int = 0       # токены попыток, ушедших впустую
    retry_completion_tokens: int = 0


@dataclass
class Usage:
    """Счётчик по шагам: сколько раз звали модель и сколько токенов ушло."""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    by_step: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)      # [(шаг, Reply)] — по одному на вызов модели

    def add(self, reply, step):
        self.steps.append((step, reply))
        prompt = reply.prompt_tokens + reply.retry_prompt_tokens
        completion = reply.completion_tokens + reply.retry_completion_tokens
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.seconds += reply.seconds
        row = self.by_step.setdefault(step, {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0})
        row['calls'] += 1
        row['prompt_tokens'] += prompt
        row['completion_tokens'] += completion

    def as_dict(self):
        return {'calls': self.calls, 'prompt_tokens': self.prompt_tokens,
                'completion_tokens': self.completion_tokens, 'seconds': round(self.seconds, 2),
                'by_step': self.by_step,
                'models': list(dict.fromkeys(r.model for _, r in self.steps if r.model)),
                'attempts': sum(r.attempts for _, r in self.steps)}


class LLMClient:
    def __init__(self, base_url, model, api_key=None, fallback_model=None, timeout=180, extra_body=None):
        self.url = base_url.rstrip('/') + '/chat/completions'
        self.model = model
        self.api_key = api_key
        self.fallback_model = fallback_model
        self.timeout = timeout
        self.extra_body = dict(NO_THINKING)
        self.extra_body.update(extra_body or {})

    def _call(self, model, messages, max_tokens, temperature):
        body = {'model': model, 'messages': messages, 'max_tokens': max_tokens,
                'temperature': temperature, 'stream': False}
        body.update(self.extra_body)
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        req = urllib.request.Request(self.url, data=json.dumps(body).encode('utf-8'),
                                     headers=headers, method='POST')
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return Reply(error='HTTP %s' % e.code, seconds=time.perf_counter() - started), e.code in RETRY_STATUS
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return Reply(error='%s' % e.reason if getattr(e, 'reason', None) else type(e).__name__,
                         seconds=time.perf_counter() - started), True
        usage = data.get('usage') or {}
        choice = (data.get('choices') or [{}])[0]
        message = choice.get('message') or {}
        return Reply(text=message.get('content') or '', model=data.get('model') or model,
                     prompt_tokens=usage.get('prompt_tokens') or 0,
                     completion_tokens=usage.get('completion_tokens') or 0,
                     seconds=time.perf_counter() - started, ok=True,
                     truncated=choice.get('finish_reason') == 'length'), False

    def chat(self, messages, max_tokens=600, temperature=0):
        """Повторы при перегрузке, затем запасная модель. Reply.attempts — сколько было попыток."""
        models = [self.model] + ([self.fallback_model] if self.fallback_model and self.fallback_model != self.model else [])
        reply, attempts, budget, lost = Reply(model=self.model), 0, max_tokens, [0, 0]
        seconds = 0.0                          # серия целиком: неудачные попытки тоже шли по времени
        pauses = PAUSES_WITH_FALLBACK if len(models) > 1 else PAUSES
        for model in models:
            for pause in pauses:
                reply, failed_retryable = self._call(model, messages, budget, temperature)
                reply.attempts = attempts = attempts + 1
                seconds += reply.seconds               # паузы между повторами не считаем — только сами попытки
                retryable = failed_retryable
                if reply.ok and reply.truncated and budget < max_tokens * 4:
                    budget *= 2                 # ответ оборван — повторяем с большим лимитом
                    retryable = True
                if not retryable:
                    break
                lost[0] += reply.prompt_tokens      # неудачная попытка тоже стоила токенов
                lost[1] += reply.completion_tokens
                if pause is not pauses[-1]:
                    time.sleep(pause)
            if reply.ok:
                break
            reply.model, reply.ok, reply.error = model, False, reply.error or 'нет ответа'
        if not reply.ok:
            lost[0] -= reply.prompt_tokens     # последняя неудачная попытка уже в reply — из потерянных убираем
            lost[1] -= reply.completion_tokens
        reply.retry_prompt_tokens, reply.retry_completion_tokens = lost
        reply.seconds = seconds                # сумма всех попыток серии, а не только последней
        return reply


def parse_json(text):
    """Первый полный объект JSON в ответе модели — модели любят обрамлять его словами."""
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0]
    start = text.find('{')
    if start < 0:
        raise ValueError('в ответе нет JSON')
    depth, inside, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if inside:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                inside = False
            continue
        if ch == '"':
            inside = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError('незакрытый JSON в ответе модели')
