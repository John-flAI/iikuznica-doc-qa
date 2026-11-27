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
from .smalltalk import ROUTE as SMALLTALK
from .smalltalk import smalltalk_reply

MIN_SIMILARITY = 0.50     # ниже — фрагмент не считается относящимся к вопросу
CANDIDATE_FLOOR = 0.45    # ниже — фрагмент не показываем модели вовсе
PER_DOCUMENT = 3          # сколько лучших фрагментов берём из каждого регламента
MAX_CLARIFICATIONS = 3    # после этого агент прямо говорит, что однозначного ответа нет
KNOWLEDGE = 'kb'             # «документ» с ответами службы качества
GIVE_UP_FLOOR = 0.60        # «нет ответа» при столь похожих фрагментах переспрашиваем у модели
NUMBER = re.compile(r'\d+(?:[.,]\d+)?')
MEASURE = re.compile(r'месяц|недел|дн|час|год|процент|руб|штук|раза')   # чем измеряют срок или норму
ABBREV = re.compile(r'[А-ЯA-Z]{2,}')                                   # аббревиатура в названии регламента

WRITER_SYSTEM = """Вы пишете ответ сотруднику по пунктам регламента.
1. Используйте только сведения из приведённых фрагментов. Ничего не добавляйте от себя: ни сроков, ни правил, ни причин, ни пояснений.
2. Числа, сроки, названия ролей и сокращения пишите точно как во фрагментах.
3. От одного до трёх предложений, по-русски, обратитесь к сотруднику на «вы». Номера пунктов не называйте — они будут показаны отдельно.
4. Если для разных случаев действуют разные правила, так и напишите: «если …, то …; если …, то …».
5. Регламент — утверждённый документ, он главнее. Ответ службы качества — консультация о том, как применять нормы регламента; если он расходится с регламентом, опирайтесь на регламент и скажите о расхождении.
Верните только JSON: {"text": "…"}"""

JUDGE_SYSTEM = """Вы проверяете ответ агента на опору в регламенте. Даны вопрос сотрудника, фрагменты и текст ответа. Найдите в тексте утверждения, которых нет во фрагментах: добавленные факты, числа, сроки, правила, причины, рекомендации. Перефразирование фрагмента и обращение к сотруднику ошибкой не считаются.
Отдельно проверьте предмет: фрагменты отвечают именно на вопрос сотрудника? Если они говорят о другом объекте, хотя слова совпадают (спрашивают о приоритетах заявок, а фрагмент — о приоритетах рисков), ответ не по адресу.
Верните только JSON: {"supported": true, "unsupported": []}; или {"supported": false, "unsupported": ["цитата из текста ответа", …]}; или, если фрагменты о другом предмете, {"supported": false, "offtopic": true, "unsupported": []}"""

GIVE_UP_CHECK = ('Среди фрагментов есть близкие к вопросу. Если ответ зависит от условия, которого нет '
                 'в вопросе, — задай уточняющий вопрос (clarify). Если прямой ответ во фрагментах есть — '
                 'приведи его (answer). not_found — только если фрагменты действительно о другом.')

SYSTEM = """Ты — внутренний агент компании. Сотрудник задаёт вопрос, а ты находишь ответ в регламентах.

ГЛАВНОЕ ПРАВИЛО: ответом считаются только фрагменты регламентов из списка. Ничего не добавляй от себя — ни сроков, ни правил, ни домыслов. Если ответа во фрагментах нет, его нет.

action = "answer" — фрагменты дают прямой ответ, и он верен для этого сотрудника.
action = "clarify" — фрагменты дают разные ответы в зависимости от условия, которого нет в вопросе: в разных регламентах разные сроки, ответственные или запреты; либо вопрос можно понять по-разному. Задай ОДИН короткий вопрос и предложи 2–4 варианта. Если различие — между регламентами, варианты должны быть названиями регламентов точно как в поле «Регламент» у фрагментов.
Один и тот же ответ есть сразу в нескольких регламентах и не расходится (те же сроки, те же ответственные, тот же запрет) — отвечай сразу: action = "answer", "same": true, а в cites перечисли по одному пункту каждого из этих регламентов. Если сроки, ответственные или запреты различаются — "same": false и clarify.
Уточняй только тогда, когда без ответа сотрудника нельзя выбрать между разными правилами. Не задавай вопросов, которые ответ не меняют («какой это проект», «вся ли информация предоставлена»), если пункт даёт срок или правило для общего случая.
Фрагменты из базы знаний — это ответы службы качества: консультации о том, как применять нормы регламента. Регламент — утверждённый документ, он главнее: если ответ службы качества расходится с регламентом, опирайтесь на регламент и укажи на расхождение.
Не отвечай пунктом, который сам начинается с условия («если», «при», «в случае»), пока это условие не согласовано с сотрудником, — сначала уточни его.
action = "not_found" — ни один фрагмент не отвечает на вопрос. Если фрагменты лишь упоминают тему, но не дают конкретного правила, срока, числа или порядка действий — это тоже not_found. Но если фрагменты о том, о чём спросили, и ответ зависит от условия — это не not_found, а clarify.
Следи за предметом вопроса: фрагмент отвечает, только если в нём речь о том же объекте. Спрашивают о приоритетах заявок — пункт о приоритетах рисков не подходит, даже если слова те же.
action = "smalltalk" — сообщение не является рабочим вопросом по регламентам: приветствие, благодарность, разговор не о деле, просьба поболтать или написать что-то не по работе, вопрос о тебе самом. В "reply" напиши одно-два вежливых предложения на русском, обращаясь к сотруднику на «вы»: ты внутренний агент и отвечаешь только по регламентам; предложи задать вопрос по регламентам. Без фактов, советов и правил. Рабочий вопрос, ответа на который во фрагментах нет, — не smalltalk, а not_found.

Возвращай только JSON:
{"action": "answer" | "clarify" | "not_found" | "smalltalk",
 "cites": ["id фрагментов, которые дают ответ"],
 "why": "одно предложение: почему эти фрагменты отвечают на вопрос",
 "same": true | false,
 "clarify": {"question": "…", "options": ["…", "…"]},
 "reply": "текст для action = smalltalk"}"""

TALK_SYSTEM = """Ты — внутренний агент компании. Сотрудникам отвечают строго текстом регламентов, а по этому сообщению в регламентах не нашлось ничего подходящего. Реши, что это за сообщение.

"smalltalk" — не рабочий вопрос: приветствие, благодарность, прощание, разговор не о деле, просьба поболтать, рассказать или написать что-то не по работе, вопрос о том, кто ты и что ты умеешь.
"question" — всё остальное: любой вопрос или просьба о работе, процессах, документах, правилах, сроках, полномочиях, людях, инструментах, отпусках, командировках, договорах — даже короткий и с опечатками. Сомневаешься — "question".

Верни только JSON: {"kind": "smalltalk" | "question", "reply": "…"}
Для "smalltalk" в reply напиши одно-два вежливых предложения на русском, обращаясь к сотруднику на «вы»: вы внутренний агент и отвечаете только по регламентам; предложите задать вопрос по регламентам. Никаких фактов, советов и правил."""


@dataclass
class Decision:
    route: str                                  # answer · clarify · unresolved · not_found · smalltalk · degraded
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
        named, question = self.split_regulation(question)
        scope = next((c[2] for c in reversed(history) if len(c) > 2 and c[2]), None) or named
        # название выбранного регламента в запрос не добавляем: он уже выбран, а слова разбавят поиск
        query = ' '.join([question] + [c[1] for c in history if not (len(c) > 2 and c[2])])
        decision = Decision(route='not_found')
        if len(history) >= self.max_clarifications:
            decision.route, decision.cites = 'unresolved', self.best_of_each(query, scope)
            return decision
        candidates = self.candidates(query, scope)
        if not candidates:
            self.no_fragments(question, history, decision)
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
        elif choice.get('action') == 'smalltalk':
            self.decide_smalltalk(choice, decision, candidates)
        return decision

    def no_fragments(self, question, history, decision):
        """Подходящих фрагментов нет. Это рабочий вопрос, которого в регламентах нет, — или разговор не по делу.

        Решает модель без фрагментов. Недоступна или ответ не разобрать — считаем рабочим вопросом:
        письмо в службу качества не должно потеряться из-за ошибки классификации.
        """
        reply = self.llm.chat([{'role': 'system', 'content': TALK_SYSTEM},
                               {'role': 'user', 'content': '\n'.join(self.thread(question, history))}])
        decision.replies.append(reply)
        decision.usage.add(reply, 'classify')
        decision.turns = 1
        choice = self.parse(reply.text) if reply.ok else {}
        if choice.get('kind') == 'smalltalk':
            decision.route, decision.text = SMALLTALK, self.talk(choice.get('reply'))

    def decide_smalltalk(self, choice, decision, candidates):
        """Модель увидела разговор не по делу. Рабочий вопрос она может принять за болтовню, только если
        в корпусе нет ничего близкого: есть близкие фрагменты — значит вопрос по делу, ответа просто нет."""
        if self.strongest(candidates) >= GIVE_UP_FLOOR:
            decision.route = 'not_found'
            return
        decision.route, decision.text = SMALLTALK, self.talk(choice.get('reply'))

    @staticmethod
    def talk(text):
        """Вежливый ответ модели — короткий и без перевода строк; пустой — заготовка с возвратом к теме."""
        text = ' '.join(str(text or '').split())
        return text[:400] if text else smalltalk_reply('steady')

    def named_regulation(self, text):
        """Регламент, который сотрудник назвал в самом вопросе (см. split_regulation)."""
        return self.split_regulation(text)[0]

    def split_regulation(self, text):
        """(Регламент, названный в вопросе, вопрос без слов названия).

        Узнаём полное название, аббревиатуру из него («ДБРЦТ») или три первых слова названия в любом падеже
        («в организации проектной деятельности»). Одна аббревиатура в названиях нескольких регламентов («ИБ») —
        не указывает ни на какой: агент уточнит. Слова названия уходят из поискового запроса: они уже выбрали
        регламент, а внутри него только разбавляют поиск.
        """
        text = ' '.join(text.split())
        lowered = text.lower().replace('ё', 'е')
        tokens = set(re.findall(r'[a-zа-яе0-9-]+', lowered))
        found, cut = set(), text
        for doc, info in self.engine.corpus.docs.items():
            title = ' '.join(info['title'].lower().replace('ё', 'е').split())
            others = ' '.join(' '.join(i['title'].lower().replace('ё', 'е').split())
                              for d, i in self.engine.corpus.docs.items() if d != doc)
            head = [w[:6] if len(w) >= 6 else w for w in re.findall(r'[a-zа-яе0-9-]+', title)[:3]]
            head_tokens = {w[:6] if len(w) >= 6 else w for w in tokens}
            if title in lowered:
                found.add(doc)
                at = lowered.index(title)
                cut = text[:at] + ' ' + text[at + len(title):]
            elif any(abbr.lower() in tokens for abbr in ABBREV.findall(info['title'])):
                found.add(doc)
            elif all(h in head_tokens for h in head) and not all(h in others for h in head):
                found.add(doc)
        if len(found) != 1:
            return None, text
        return found.pop(), ' '.join(cut.split())

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
        chosen = self.resolve(choice.get('cites'), candidates)
        passing = [c for c in chosen if self.relevance(c[1], c[2]) >= self.floor(scope, self.min_similarity)]
        if not passing:
            decision.route = 'not_found'
            return
        regulations = list(dict.fromkeys(c[0].doc for c in passing))
        if len(regulations) > 1 and not scope and not (choice.get('same') is True and self.consistent(passing)):
            # регламенты отвечают по-разному, а сотрудник ещё не сказал, какой его
            decision.route = 'clarify'
            decision.question = 'Ответ зависит от регламента. В каком вы работаете?'
            decision.options = [self.engine.corpus.title(doc) for doc in regulations]
            decision.option_docs = dict(zip(decision.options, regulations))
            return
        decision.route, decision.cites = 'answer', passing
        decision.text, decision.checks = self.compose(question, history, passing, decision)
        if decision.checks.get('judge') == 'offtopic':
            decision.route, decision.cites, decision.text = 'not_found', [], ''

    def resolve(self, wanted, candidates):
        """Адреса из ответа модели → фрагменты-кандидаты, в том же порядке.

        Модель иногда отбрасывает суффикс абзаца («f2:1.5~1» → «f2:1.5»): тогда берём самый близкий к вопросу
        абзац этого пункта. Иначе готовый ответ превращался бы в «нет ответа» из-за описки в адресе.
        """
        chosen = []
        for cite in dict.fromkeys(wanted if isinstance(wanted, list) else []):
            if not isinstance(cite, str):
                continue
            if cite in candidates:
                chosen.append(candidates[cite])
                continue
            children = [c for uid, c in candidates.items() if uid.startswith(cite + '~')]
            if children:
                chosen.append(max(children, key=lambda c: self.relevance(c[1], c[2])))
        return chosen

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

    @staticmethod
    def consistent(cites):
        """Пункты разных регламентов называют одни и те же сроки и нормы? Словам модели («same») не верим на слово:
        каждый регламент — набор чисел и мер (дни, недели, проценты…); наборы двух регламентов разошлись — отвечать рано."""
        facts = {}
        for unit, _, _ in cites:
            text = unit.text.lower().replace('ё', 'е')
            found = {n.replace(',', '.') for n in NUMBER.findall(text)} | set(MEASURE.findall(text))
            facts[unit.doc] = facts.get(unit.doc, set()) | found
        return all(a == b for a in facts.values() for b in facts.values() if a and b)

    def regulation_of(self, options):
        """Какой регламент называет каждый вариант ответа."""
        titles = {doc: info['title'] for doc, info in self.engine.corpus.docs.items()}
        return [next((doc for doc, title in titles.items() if title.casefold() in o.casefold()), None)
                for o in options]

    def floor(self, scope, base=CANDIDATE_FLOOR):
        """Порог близости фрагмента к вопросу. Регламент назван — ниже: сотрудник уже сказал, где искать,
        а внутри одного регламента словесного покрытия меньше (его фрагменты похожи друг на друга)."""
        return base if scope is None else base * 0.6

    def candidates(self, query, scope=None):
        """Лучшие фрагменты каждого регламента, достаточно близкие к вопросу: {uid: (unit, sim, cov)}."""
        groups = self.engine.search.per_document(query, k_each=PER_DOCUMENT if scope is None else PER_DOCUMENT + 2)
        found = {}
        for doc, hits in groups.items():
            if scope is not None and doc != scope:       # сотрудник назвал регламент — ищем только в нём
                continue
            for hit in hits:
                if self.relevance(hit.similarity, hit.coverage) >= self.floor(scope):
                    found[hit.uid] = (hit.unit, hit.similarity, hit.coverage)
        if scope is not None and not found:
            # регламент назван, а по словам не нашлось — берём лучшие его фрагменты: модель решит, есть ли ответ
            for hit in groups.get(scope, [])[:PER_DOCUMENT]:
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
                if ruling.get('offtopic') is True:
                    checks['judge'] = 'offtopic'        # пункт о другом предмете: показывать его ответом нельзя
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
