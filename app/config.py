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
