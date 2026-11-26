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
from .search import ABBR, WORD

MIN_SIMILARITY = 0.50     # ниже — фрагмент не считается относящимся к вопросу
CANDIDATE_FLOOR = 0.45    # ниже — фрагмент не показываем модели вовсе
PER_DOCUMENT = 3          # сколько лучших фрагментов берём из каждого регламента
MAX_CLARIFICATIONS = 3    # после этого агент прямо говорит, что однозначного ответа нет
KNOWLEDGE = 'kb'             # «документ» с ответами службы качества
GIVE_UP_FLOOR = 0.60        # «нет ответа» при столь похожих фрагментах переспрашиваем у модели
NUMBER = re.compile(r'\d+(?:[.,]\d+)?')

WRITER_SYSTEM = """Вы пишете ответ сотруднику по пунктам регламента.
1. Используйте только сведения из приведённых фрагментов. Ничего не добавляйте от себя: ни сроков, ни правил, ни причин, ни пояснений.
2. Числа, сроки, названия ролей и сокращения пишите точно как во фрагментах.
3. От одного до трёх предложений, по-русски, обратитесь к сотруднику на «вы». Номера пунктов не называйте — они будут показаны отдельно.
4. Если для разных случаев действуют разные правила, так и напишите: «если …, то …; если …, то …».
5. Если среди фрагментов есть ответ службы качества, он приоритетнее регламента; если он расходится с регламентом, скажите об этом.
Верните только JSON: {"text": "…"}"""

JUDGE_SYSTEM = """Вы проверяете ответ агента на опору в регламенте. Даны фрагменты и текст ответа. Найдите в тексте утверждения, которых нет во фрагментах: добавленные факты, числа, сроки, правила, причины, рекомендации. Перефразирование фрагмента и обращение к сотруднику ошибкой не считаются.
Верните только JSON: {"supported": true, "unsupported": []} или {"supported": false, "unsupported": ["цитата из текста ответа", …]}"""

GIVE_UP_CHECK = ('Среди фрагментов есть близкие к вопросу. Если ответ зависит от условия, которого нет '
                 'в вопросе, — задай уточняющий вопрос (clarify). Если прямой ответ во фрагментах есть — '
                 'приведи его (answer). not_found — только если фрагменты действительно о другом.')

SYSTEM = """Ты — внутренний агент компании. Сотрудник задаёт вопрос, а ты находишь ответ в регламентах.

ГЛАВНОЕ ПРАВИЛО: ответом считаются только фрагменты регламентов из списка. Ничего не добавляй от себя — ни сроков, ни правил, ни домыслов. Если ответа во фрагментах нет, его нет.

action = "answer" — фрагменты дают прямой ответ, и он верен для этого сотрудника.
action = "clarify" — фрагменты дают разные ответы в зависимости от условия, которого нет в вопросе: в разных регламентах разные сроки, ответственные или запреты; либо вопрос можно понять по-разному. Задай ОДИН короткий вопрос и предложи 2–4 варианта. Если различие — между регламентами, варианты должны быть названиями регламентов точно как в поле «Регламент» у фрагментов.
Фрагменты из базы знаний — это ответы службы качества. Они приоритетнее регламентов: если регламент говорит иначе, приведи ответ службы качества и укажи на расхождение.
Не отвечай пунктом, который сам начинается с условия («если», «при», «в случае»), пока это условие не согласовано с сотрудником, — сначала уточни его.
action = "not_found" — ни один фрагмент не отвечает на вопрос. Если фрагменты лишь упоминают тему, но не дают конкретного правила, срока, числа или порядка действий — это тоже not_found. Но если фрагменты о том, о чём спросили, и ответ зависит от условия — это не not_found, а clarify.

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
    text: str = ''                              # связный ответ модели по пунктам; пустой — пункты дословно
    checks: dict = field(default_factory=dict)   # чем подтверждён текст: attempts · judge · issues
    turns: int = 0                              # сколько было раундов «поиск → решение»
    replies: list = field(default_factory=list)  # сырые ответы модели — для журнала вызовов
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
        decision.replies.append(reply)
        decision.usage.add(reply, 'decide')
        decision.turns = 1
        if not reply.ok:
            # модель недоступна — не выдаём тишину: отдаём найденное без обработки модели
            decision.route, decision.cites, decision.error = 'degraded', self.best_of_each(query, scope), reply.error
            return decision
        choice = self.parse(reply.text)
        if choice.get('action') == 'not_found' and self.strongest(candidates) >= GIVE_UP_FLOOR:
            choice = self.parse(self.second_opinion(question, history, candidates, decision))
        if choice.get('action') == 'answer':
            self.decide_answer(choice, decision, candidates, scope, question, history)
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

    def second_opinion(self, question, history, candidates, decision):
        """Модель сдалась, а фрагменты близки к вопросу: один раз напоминаем про уточнение."""
        reply = self.llm.chat(self.messages(question, history, candidates)
                              + [{'role': 'user', 'content': GIVE_UP_CHECK}])
        decision.replies.append(reply)
        decision.usage.add(reply, 'decide-again')
        return reply.text

    def strongest(self, candidates):
        return max((self.relevance(c[1], c[2]) for c in candidates.values()), default=0.0)

    def decide_answer(self, choice, decision, candidates, scope, question, history):
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
        decision.text, decision.checks = self.compose(question, history, passing, decision)

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

    def fragments(self, cites, with_ids):
        lines = []
        for unit, _, _ in cites:
            source = 'База знаний' if unit.doc == KNOWLEDGE else 'Регламент'
            lines.append('%s%s: «%s». Адрес: %s\n%s' % (
                '[%s] ' % unit.uid if with_ids else '', source,
                self.engine.corpus.title(unit.doc), unit.address, unit.quote))
        return lines

    def thread(self, question, history):
        lines = ['Вопрос сотрудника: %s' % question]
        for asked, answered, *rest in history:
            lines += ['Уточнение агента: %s' % asked, 'Ответ сотрудника: %s' % answered]
        return lines

    def messages(self, question, history, candidates):
        lines = self.thread(question, history)
        lines.append('Фрагменты регламентов:')
        lines += self.fragments(list(candidates.values()), with_ids=True)
        return [{'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': '\n'.join(lines)}]

    def compose(self, question, history, cites, decision):
        """Связный текст по выбранным пунктам — и проверка, что в нём нет ничего от себя.

        Код сверяет числа и сокращения с пунктами, судья-модель ищет утверждения без опоры.
        Провал — одна переписка с замечаниями. Не прошло ни одной редакции — текст пустой,
        и сотруднику показывают пункты дословно.
        """
        issues, checks = [], {'attempts': 0, 'judge': 'passed', 'issues': []}
        for attempt in range(2):
            lines = self.thread(question, history)
            lines.append('Фрагменты регламентов:')
            lines += self.fragments(cites, with_ids=False)
            if issues:
                lines.append('Замечания проверки к предыдущей редакции — устраните их:')
            lines += ['— %s' % note for note in issues]
            reply = self.llm.chat([{'role': 'system', 'content': WRITER_SYSTEM},
                                   {'role': 'user', 'content': '\n'.join(lines)}])
            decision.replies.append(reply)
            decision.usage.add(reply, 'write')
            checks['attempts'] = attempt + 1
            if not reply.ok:
                checks['judge'] = 'unavailable'
                return '', checks
            text = self.written(reply.text)
            numbers, short = self.foreign_facts(text, cites)
            if text and not numbers:
                ruling = self.judge(question, history, cites, text, short, decision)
                if ruling is None:
                    checks['judge'] = 'unavailable'
                    return '', checks
                if ruling.get('supported') is not False:
                    return text, checks
                issues = [str(x) for x in ruling.get('unsupported') or []][:5] or ['есть утверждения без опоры в пунктах']
                checks['judge'] = 'rewritten'
            else:
                issues = numbers + short if text else ['ответ не в JSON']
                checks['judge'] = 'rewritten'
            checks['issues'] = issues
        checks['judge'] = 'failed'
        return '', checks

    @staticmethod
    def written(text):
        """Текст ответа из ответа модели: JSON с полем text либо просто текст."""
        try:
            choice = parse_json(text)
        except ValueError:
            choice = None
        if isinstance(choice, dict) and isinstance(choice.get('text'), str):
            text = choice['text']
        elif '{' in text:
            return ''
        return ' '.join(text.split())[:1200]

    def foreign_facts(self, text, cites):
        """([числа, которых нет в пунктах], [сокращения, которых нет в пунктах])."""
        # адрес не берём: номера разделов и пунктов в нём — не факты, и иначе «раздел 5» оправдает любое «5»
        known = ' '.join('%s %s %s' % (c[0].text, c[0].number, c[0].title) for c in cites)
        numbers = {n.replace(',', '.') for n in NUMBER.findall(known)}
        abbrs = {w for w in WORD.findall(known) if ABBR.match(w)}
        seen_numbers = dict.fromkeys(NUMBER.findall(text))
        seen_abbrs = dict.fromkeys(w for w in WORD.findall(text) if ABBR.match(w))
        return (['число «%s» есть в ответе, но его нет в пунктах' % n
                 for n in seen_numbers if n.replace(',', '.') not in numbers],
                ['сокращение «%s» не встречается в пунктах' % w for w in seen_abbrs if w not in abbrs])

    def judge(self, question, history, cites, text, short, decision):
        """Судья-модель: каждое ли утверждение ответа следует из пунктов. None — модель недоступна."""
        lines = self.thread(question, history)
        lines.append('Фрагменты регламентов:')
        lines += self.fragments(cites, with_ids=False)
        lines.append('Текст ответа:')
        lines.append(text)
        if short:
            lines.append('Отдельно проверьте эти сокращения: ' + '; '.join(short))
        reply = self.llm.chat([{'role': 'system', 'content': JUDGE_SYSTEM},
                               {'role': 'user', 'content': '\n'.join(lines)}])
        decision.replies.append(reply)
        decision.usage.add(reply, 'judge')
        return None if not reply.ok else self.parse(reply.text)
