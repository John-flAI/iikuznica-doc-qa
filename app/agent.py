# -*- coding: utf-8 -*-
"""Агент: найти → решить → проверить → ответить, уточнить или передать в службу качества.

Агентность — в цикле: вопрос → поиск лучшего ответа в каждом регламенте → решение модели →
проверка кодом → при уточнении новый поиск уже с учётом ответа сотрудника.

Достоверность — не в вежливой просьбе к модели, а в проверке:
  • в ответ попадают только фрагменты из выданной модели подборки — дословные цитаты;
  • фрагмент должен быть достаточно похож на вопрос, иначе в ответ он не проходит;
  • если ответы пришли из разных регламентов, а сотрудник ещё не сказал, какой его —
    агент не выбирает сам, он спрашивает; названный регламент сужает следующий поиск;
  • числа в пояснении модели должны быть в цитатах, иначе пояснение не показывается.
"""
import re
from dataclasses import dataclass, field

from .llm import Usage, parse_json

MIN_SIMILARITY = 0.50     # ниже — фрагмент не считается относящимся к вопросу
CANDIDATE_FLOOR = 0.45    # ниже — фрагмент не показываем модели вовсе
PER_DOCUMENT = 3          # сколько лучших фрагментов берём из каждого регламента
MAX_CLARIFICATIONS = 3    # после этого агент прямо говорит, что однозначного ответа нет
NUMBER = re.compile(r'\d+(?:[.,]\d+)?')

SYSTEM = """Ты — внутренний агент компании. Сотрудник задаёт вопрос, а ты находишь ответ в регламентах.

ГЛАВНОЕ ПРАВИЛО: ответом считаются только фрагменты регламентов из списка. Ничего не добавляй от себя — ни сроков, ни правил, ни домыслов. Если ответа во фрагментах нет, его нет.

action = "answer" — фрагменты дают прямой ответ, и он верен для этого сотрудника.
action = "clarify" — фрагменты дают разные ответы в зависимости от условия, которого нет в вопросе: в разных регламентах разные сроки, ответственные или запреты; либо вопрос можно понять по-разному. Задай ОДИН короткий вопрос и предложи 2–4 варианта. Если различие — между регламентами, варианты должны быть названиями регламентов точно как в поле «Регламент» у фрагментов.
Не отвечай пунктом, который сам начинается с условия («если», «при», «в случае»), пока это условие не согласовано с сотрудником, — сначала уточни его.
action = "not_found" — ни один фрагмент не отвечает на вопрос. Если фрагменты лишь упоминают тему, но не дают конкретного правила, срока, числа или порядка действий — это тоже not_found.

Возвращай только JSON:
{"action": "answer" | "clarify" | "not_found",
 "cites": ["id фрагментов, которые дают ответ"],
 "why": "одно предложение: почему эти фрагменты отвечают на вопрос",
 "clarify": {"question": "…", "options": ["…", "…"]}}"""


@dataclass
class Decision:
    route: str                                  # answer · clarify · unresolved · not_found · degraded
    cites: list = field(default_factory=list)   # [(unit, similarity, coverage)]
    why: str = ''
    question: str = ''                          # уточняющий вопрос агента
    options: list = field(default_factory=list)  # варианты ответа сотрудника
    option_docs: dict = field(default_factory=dict)   # вариант → регламент, который он называет
    error: str = ''                             # почему модель не ответила (route = degraded)
    turns: int = 0                              # сколько было раундов «поиск → решение»
    usage: Usage = field(default_factory=Usage)

    @property
    def tokens(self):
        return self.usage.prompt_tokens + self.usage.completion_tokens


class Agent:
    def __init__(self, engine, llm, max_clarifications=MAX_CLARIFICATIONS, min_similarity=MIN_SIMILARITY):
        self.engine = engine
        self.llm = llm
        self.max_clarifications = max_clarifications
        self.min_similarity = min_similarity

    def ask(self, question, clarifications=()):
        """clarifications — [(вопрос агента, ответ сотрудника[, регламент])] из прошлых раундов."""
        history = [tuple(c) for c in clarifications]
        scope = next((c[2] for c in reversed(history) if len(c) > 2 and c[2]), None)
        # название выбранного регламента в запрос не добавляем: он уже выбран, а слова разбавят поиск
        query = ' '.join([question] + [c[1] for c in history if not (len(c) > 2 and c[2])])
        decision = Decision(route='not_found')
        if len(history) >= self.max_clarifications:
            decision.route, decision.cites = 'unresolved', self.best_of_each(query, scope)
            return decision
        candidates = self.candidates(query, scope)
        if not candidates:
            return decision
        reply = self.llm.chat(self.messages(question, history, candidates))
        decision.usage.add(reply, 'decide')
        decision.turns = 1
        if not reply.ok:
            # модель недоступна — не выдаём тишину: отдаём найденное без обработки модели
            decision.route, decision.cites, decision.error = 'degraded', self.best_of_each(query, scope), reply.error
            return decision
        choice = self.parse(reply.text)
        if choice.get('action') == 'answer':
            self.decide_answer(choice, decision, candidates, scope)
        elif choice.get('action') == 'clarify':
            self.decide_clarify(choice, decision, query, scope)
        return decision

    @staticmethod
    def parse(text):
        try:
            choice = parse_json(text)
        except ValueError:                      # не-JSON или обрыв — решения нет
            return {}
        return choice if isinstance(choice, dict) else {}

    def decide_answer(self, choice, decision, candidates, scope):
        wanted = choice.get('cites')
        chosen = [candidates[c] for c in dict.fromkeys(wanted if isinstance(wanted, list) else [])
                  if c in candidates]
        passing = [c for c in chosen if self.relevance(c[1], c[2]) >= self.min_similarity]
        if not passing:
            decision.route = 'not_found'
            return
        regulations = list(dict.fromkeys(c[0].doc for c in passing))
        if len(regulations) > 1 and not scope:
            # регламенты отвечают по-разному, а сотрудник ещё не сказал, какой его
            decision.route = 'clarify'
            decision.question = 'Ответ зависит от регламента. В каком вы работаете?'
            decision.options = [self.engine.corpus.title(doc) for doc in regulations]
            decision.option_docs = dict(zip(decision.options, regulations))
            return
        decision.route, decision.cites = 'answer', passing
        decision.why = self.safe_why(choice.get('why'), passing)

    def decide_clarify(self, choice, decision, query, scope):
        clarify = choice.get('clarify') or {}
        if not clarify.get('question'):
            decision.route, decision.cites = 'unresolved', self.best_of_each(query, scope)
            return
        decision.route = 'clarify'
        decision.question = str(clarify['question']).strip()[:300]
        options = [str(o).strip() for o in clarify.get('options') or [] if str(o).strip()][:6]
        decision.options = [o[:120] for o in options]
        # вариант, называющий регламент, запоминаем: он сузит следующий поиск
        decision.option_docs = {o[:120]: doc for o, doc in zip(options, self.regulation_of(options)) if doc}

    def regulation_of(self, options):
        """Какой регламент называет каждый вариант ответа."""
        titles = {doc: info['title'] for doc, info in self.engine.corpus.docs.items()}
        return [next((doc for doc, title in titles.items() if title.casefold() in o.casefold()), None)
                for o in options]

    def candidates(self, query, scope=None):
        """Лучшие фрагменты каждого регламента, достаточно близкие к вопросу: {uid: (unit, sim, cov)}."""
        groups = self.engine.search.per_document(query, k_each=PER_DOCUMENT if scope is None else PER_DOCUMENT + 2)
        found = {}
        for doc, hits in groups.items():
            if scope is not None and doc != scope:       # сотрудник назвал регламент — ищем только в нём
                continue
            for hit in hits:
                if self.relevance(hit.similarity, hit.coverage) >= CANDIDATE_FLOOR:
                    found[hit.uid] = (hit.unit, hit.similarity, hit.coverage)
        return found

    def best_of_each(self, query, scope=None):
        """По одному лучшему фрагменту каждого регламента — что показать при «неоднозначно»."""
        groups = self.engine.search.per_document(query, k_each=1)
        return [(hit.unit, hit.similarity, hit.coverage)
                for doc, hits in groups.items() for hit in hits
                if (scope is None or doc == scope) and self.relevance(hit.similarity, hit.coverage) >= self.min_similarity]

    def relevance(self, similarity, coverage):
        """Насколько фрагмент относится к вопросу.

        Словесного покрытия мало: у короткого вопроса остаётся один токен, и он найдётся
        с покрытием 100 %. Поэтому, когда есть эмбеддинги, судим по сходству.
        """
        return similarity if self.engine.search.vectors is not None else coverage

    def messages(self, question, history, candidates):
        lines = ['Вопрос сотрудника: %s' % question]
        for asked, answered, *rest in history:
            lines += ['Уточнение агента: %s' % asked, 'Ответ сотрудника: %s' % answered]
        lines.append('Фрагменты регламентов:')
        for uid, (unit, _, _) in candidates.items():
            lines.append('[%s] Регламент: «%s». Адрес: %s\n%s' % (
                uid, self.engine.corpus.title(unit.doc), unit.address, unit.quote))
        return [{'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': '\n'.join(lines)}]

    def safe_why(self, why, cites):
        """Пояснение модели показываем, только если все числа в нём есть в цитатах или их номерах."""
        why = ' '.join(str(why or '').split())[:400]
        cited = {n.replace(',', '.') for c in cites
                 for n in NUMBER.findall(c[0].text) + ([c[0].number] if c[0].number else [])}
        if not why or any(n.replace(',', '.') not in cited for n in NUMBER.findall(why)):
            return ''
        return why
