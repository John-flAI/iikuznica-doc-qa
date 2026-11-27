# -*- coding: utf-8 -*-
"""Настройки из окружения. Ключи — только из окружения, в коде и репозитории их нет.

Сервер модели — любой OpenAI-совместимый: локальный (Ollama, vLLM) или облачный API
официального провайдера открытой модели. Переключение — одной переменной.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('REGQA_DATA', ROOT / 'data'))     # корпус регламентов: только читается
STATE_DIR = Path(os.environ.get('REGQA_STATE', DATA_DIR))        # база, индекс, база знаний — переживает обновление
DB_PATH = Path(os.environ.get('REGQA_DB', STATE_DIR / 'state.db'))

# Модель эмбеддингов: BGE-M3, лицензия MIT — https://huggingface.co/BAAI/bge-m3
EMBED_BASE_URL = os.environ.get('EMBED_BASE_URL', 'http://127.0.0.1:11434/v1')
EMBED_MODEL = os.environ.get('EMBED_MODEL', 'bge-m3')
EMBED_API_KEY = os.environ.get('EMBED_API_KEY')

# Языковая модель — любая OpenAI-совместимая. На стенде: GLM-4.5-Air (MIT, 62 ГБ в Q4) с запасной
# GLM-4.7-Flash (MIT, 19 ГБ). По умолчанию — локальный сервер без ключей; значения для облака — .env.example
LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'http://127.0.0.1:11434/v1')
LLM_MODEL = os.environ.get('LLM_MODEL', 'glm-4.7-flash:latest')
LLM_API_KEY = os.environ.get('LLM_API_KEY')
LLM_FALLBACK_MODEL = os.environ.get('LLM_FALLBACK_MODEL')     # запасная, если основная перегружена

# Несколько моделей на выбор — переключатель в шапке чата. Записи через «;»:
#   id|подпись на стенде|адрес сервера|имя модели|имя переменной окружения с ключом.
# Первая запись — по умолчанию. Запись без ключа в окружении на стенд не попадает.
MODELS = ('zai-flash|GLM-5.3-Flash · Z.ai|https://api.z.ai/api/coding/paas/v4|GLM-5.3-Flash|LLM_API_KEY;'
          'comet-qwen|Qwen3.5-122B-A10B · CometAPI|https://api.cometapi.com/v1|qwen3.5-122b-a10b|COMETAPI_KEY;'
          'comet-oss|gpt-oss-120b · CometAPI|https://api.cometapi.com/v1|gpt-oss-120b|COMETAPI_KEY')
LLM_MODELS = os.environ.get('LLM_MODELS', MODELS)

# Стенд демонстрационный: сколько вопросов в час можно задать с одного адреса (0 — без лимита).
# Каждый вопрос — вызов платной модели, поэтому лимит написан на самом стенде.
ASK_LIMIT = int(os.environ.get('ASK_LIMIT', '20'))


def models():
    """Каталог моделей стенда: [{id, label, base_url, model, key_env, key}] — только записи, у которых есть ключ."""
    records = [[part.strip() for part in record.split('|')] for record in LLM_MODELS.split(';') if record.strip()]
    items = [dict(zip(('id', 'label', 'base_url', 'model', 'key_env'), record)) for record in records if len(record) == 5]
    if not items:
        items = [{'id': 'default', 'label': LLM_MODEL, 'base_url': LLM_BASE_URL, 'model': LLM_MODEL, 'key_env': 'LLM_API_KEY'}]
    for item in items:
        item['key'] = os.environ.get(item['key_env']) or None
    return [item for item in items if item['key']]      # без ключа запись не показывается: иначе звали бы облако без ключа (401)
