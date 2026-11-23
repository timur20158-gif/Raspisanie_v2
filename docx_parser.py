import os
import re
import shutil
import subprocess
import tempfile

import docx

from core import detect_faculty_from_text, esc


W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

_WEEKS_EXPR = r'\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*'
_TYPE_TOKEN = r'(?:(?:\d+|[IІVХX]+)\s*(?:п/г|подгр(?:уппа)?\.?)|ЛК|ПЗ|ЛАБ|Лаб|Лвб|ЛР|ПР|Сем|поток)'

# Блок недель и типа занятия — поддерживает ВСЕ 3 формата факультетов СИ УУНиТ:
# 1) ЕМФ / ПФ / ФЭП: (2-6, 8-15 – пз), (1-9 ЛК поток ПФ+ТФ+ФЭП), (1-9 ЛК 423 ауд., 10-16 ПР 422 ауд.)
# 2) ТФ (тип ПЕРЕД неделями): (ЛАБ 3-6), (ЛК 3-6, 8-13), (ЛАБ, ПЗ 8-17), (ЛК 1-9, ПЗ 11-13)
# 3) ПФ (только недели без типа): (1-7), (9-16)
WEEK_BLOCK_RE = re.compile(
    r'\(\s*(?:'
    r'(?P<w1>' + _WEEKS_EXPR + r')\s*(?P<t1>[-–—]?\s*' + _TYPE_TOKEN + r'[^)]*)'
    r'|'
    r'(?P<t2>(?:' + _TYPE_TOKEN + r')(?:[\s.,/]+' + _TYPE_TOKEN + r')*)\s*[-–—]?\s*(?P<w2>' + _WEEKS_EXPR + r')(?P<rest2>[^)]*)'
    r'|'
    r'(?P<w3>' + _WEEKS_EXPR + r')'
    r')\s*\)',
    re.IGNORECASE,
)

# Аудитория:
# - "ауд. 206", "ауд. 206а", "ауд. 301-б", "ауд. 421, 423", "ауд. конф-зал" (НЕ захватывает "с" из "ст.пр."!)
# - "215 ауд.", "417а ауд", "365 ауд."
# - "(спортивный зал)", "спортивный зал"
# - ТФ формат через одиночный слэш: "/ 251", "/251", "/ Спортзал"
ROOM_RE = re.compile(
    r'(?:'
    r'ауд\.?\s*(?:конф[-\s]*[зэ]ал|спорт[-\s]*зал|акт(?:овый)?[-\s]*зал|[№#]?\s*\d+(?:[-/]?[а-яёa-z])?(?![а-яёa-z])(?:\s*,\s*\d+(?:[-/]?[а-яёa-z])?(?![а-яёa-z]))*)'
    r'|\b\d{1,4}(?:[-/]?[а-яёa-z])?\s*ауд\.?(?![а-яёa-z])'
    r'|\(\s*(?:спортивный\s+зал|спорт[-\s]*зал)\s*\)'
    r'|(?<!/)/(?!/)\s*(?:спортивный\s+зал|спорт[-\s]*зал|акт(?:овый)?[-\s]*зал|конф[-\s]*[зэ]ал|\d{1,4}(?:[-/]?[а-яёa-z])?(?![а-яёa-z]))'
    r')',
    re.IGNORECASE,
)

# Полное упоминание преподавателя: [Степень/должность] Фамилия И.О.
TEACHER_FULL_RE = re.compile(
    r'(?:'
    r'(?:(?:канд\.|к\.|д-р\.?|д\.)\s*(?:[а-яё\-]+\.?\s*)*н\.?,?\s*|доц\.?,?\s*|проф\.?,?\s*|ст\.?\s*преп\.?,?\s*|ст\.?\s*пр\.?,?\s*|преп\.?,?\s*|асс\.?,?\s*)+'
    r')?'
    r'[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.',
    re.IGNORECASE,
)

# Поддержка времени как с разделителем ('08:00 - 09:20', '08.00-09.20'), так и слитно ('0800 – 0920')
TIME_NORM_RE = re.compile(
    r'(\d{1,2})\s*[:.\-]?\s*(\d{2})\s*[-–—]\s*(\d{1,2})\s*[:.\-]?\s*(\d{2})'
)

DAY_MAP = {
    'понедельник': 'Понедельник',
    'вторник': 'Вторник',
    'среда': 'Среда',
    'четверг': 'Четверг',
    'пятница': 'Пятница',
    'суббота': 'Суббота',
    'воскресенье': 'Воскресенье',
    # Сокращения Техфака (ТФ)
    'пн': 'Понедельник',
    'вт': 'Вторник',
    'ср': 'Среда',
    'чт': 'Четверг',
    'пт': 'Пятница',
    'сб': 'Суббота',
    'вс': 'Воскресенье',
}


def normalize_room_str(raw_room):
    s = str(raw_room or '').strip()
    if not s:
        return ''
    s = s.replace('конф-эал', 'конф-зал')
    low = s.lower()
    if 'спорт' in low:
        return 'Спортзал'
    if s.startswith('/'):
        inner = s.lstrip('/').strip()
        if not inner:
            return ''
        if inner[0].isdigit():
            return f'ауд. {inner}'
        return inner
    m_rev = re.match(r'^(\d{1,4}(?:[-/]?[а-яёa-z])?)\s*ауд\.?$', s, re.IGNORECASE)
    if m_rev:
        return f'ауд. {m_rev.group(1)}'
    s = re.sub(r'^ауд\.?\s*', 'ауд. ', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*,\s*', ', ', s).strip()
    return s


def clean_teacher_str(raw_teacher):
    s = re.sub(r'\s+', ' ', str(raw_teacher or '')).strip(' ,;-/\\').lstrip('.')
    s = re.sub(r'^\d+\s*', '', s).strip(' ,;-/\\').lstrip('. ')
    # Убираем висячую точку в конце, только если это не инициал вида "И."
    if s.endswith('.') and not re.search(r'[А-ЯЁа-яё]\.$', s):
        s = s.rstrip('.')
    if not s or len(s) < 3 or s.isdigit():
        return ''
    return s


def get_tc_clean_text(tc):
    if tc is None:
        return ''
    paras = []
    t_tag = f'{W_NS}t'
    br_tag = f'{W_NS}br'
    cr_tag = f'{W_NS}cr'
    for p in tc.findall(f'{W_NS}p'):
        parts = []
        for elem in p.iter():
            if elem.tag == t_tag and elem.text:
                parts.append(elem.text)
            elif elem.tag in (br_tag, cr_tag):
                parts.append('\n')
        line = ''.join(parts).strip()
        if line:
            paras.append(line)
    return '\n'.join(paras)


def build_logical_grid(table):
    """Строит корректную 2D-сетку ячеек таблицы Word с учётом gridSpan и vMerge."""
    grid = []
    for tr in table.rows:
        row_tcs = []
        col_cursor = 0
        trPr = tr._tr.find(f'{W_NS}trPr')
        if trPr is not None:
            gb = trPr.find(f'{W_NS}gridBefore')
            if gb is not None:
                gb_val = int(gb.get(f'{W_NS}val', 0))
                for _ in range(gb_val):
                    row_tcs.append(None)
                    col_cursor += 1

        for tc in tr._tr.findall(f'{W_NS}tc'):
            tcPr = tc.find(f'{W_NS}tcPr')
            grid_span = 1
            v_merge = None
            if tcPr is not None:
                gs = tcPr.find(f'{W_NS}gridSpan')
                if gs is not None:
                    grid_span = int(gs.get(f'{W_NS}val', 1))
                vm = tcPr.find(f'{W_NS}vMerge')
                if vm is not None:
                    v_merge = vm.get(f'{W_NS}val', 'continue')

            for _ in range(grid_span):
                if v_merge == 'continue' and grid and col_cursor < len(grid[-1]):
                    row_tcs.append(grid[-1][col_cursor])
                else:
                    row_tcs.append(tc)
                col_cursor += 1
        grid.append(row_tcs)
    return grid


def normalize_time_slot(raw_time):
    m = TIME_NORM_RE.search(str(raw_time or ''))
    if not m:
        return None
    h1, m1, h2, m2 = m.groups()
    h1_i, m1_i, h2_i, m2_i = int(h1), int(m1), int(h2), int(m2)
    if not (0 <= h1_i <= 23 and 0 <= h2_i <= 23 and 0 <= m1_i <= 59 and 0 <= m2_i <= 59):
        return None
    return f'{h1_i:02d}:{m1} - {h2_i:02d}:{m2}'


def normalize_day_name(raw_day):
    cleaned = re.sub(r'[^а-яА-ЯёЁ]', '', str(raw_day or '')).lower()
    return DAY_MAP.get(cleaned)


def parse_column_header(header_text):
    """
    Универсально распознаёт курс, название направления/профиля и недели практики
    из заголовка столбца таблицы Word для любого факультета (ЕМФ, ПФ, ФЭП, ТФ).
    """
    flat = re.sub(r'\s+', ' ', str(header_text or '')).strip()
    if not flat:
        return None, None, None

    crs_m = re.search(r'(\d)\s*курс', flat, re.IGNORECASE)
    if not crs_m:
        return None, None, None
    course = int(crs_m.group(1))

    prac_m = re.search(r'практика\s*\((\d+)\s*[-–—]\s*(\d+)\)', flat, re.IGNORECASE)
    if not prac_m:
        prac_m = re.search(r'\((\d+)\s*[-–—]\s*(\d+)\s*нед\.?\s*[-–—]\s*практика\)', flat, re.IGNORECASE)
    practice_range = (int(prac_m.group(1)), int(prac_m.group(2))) if prac_m else None

    name = re.sub(r'\d\s*курс', '', flat, flags=re.IGNORECASE)
    name = re.sub(r'практика\s*\([^)]*\)', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\(\d+\s*[-–—]\s*\d+\s*нед\.?\s*[-–—]\s*практика\)', '', name, flags=re.IGNORECASE)
    name = re.sub(r'Направление\s*(?:подготовки)?\s*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\d{2}\.\d{2}\.\d{2}\s*', '', name)
    name = re.sub(r'\(с\s+двумя\s+профилями(?:\s+подготовки)?\)', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\b\d+\s*[-–—]\s*\d+\s*нед\.?\s*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'(?:ауд\.?\s*\d+|\d+\s*ауд\.?)', '', name, flags=re.IGNORECASE)
    # Убираем шифр группы Техфака вида "ТХОМ-102БС"
    name = re.sub(r'\b[А-ЯЁA-Z]{2,8}-\d{2,4}[А-ЯЁA-Z]*\b', '', name)
    name = re.sub(r'\s+', ' ', name).strip(' ,;.')

    # Сначала проверяем формат ПФ: "направленность (профиль) «...»"
    prof_m = re.search(r'направленность\s*\(профиль\)\s*(.+)$', name, re.IGNORECASE)
    if not prof_m:
        prof_m = re.search(r'\((?:направленность\s*)?(?:профиль\s+)?([^)]+)\)', name, re.IGNORECASE)
    profile_raw = prof_m.group(1).strip(' «»"') if prof_m else ''
    profile_raw = re.sub(r'^(?:направленность|профиль)\s+', '', profile_raw, flags=re.IGNORECASE).strip(' «»"')

    # Отделяем базовое направление до "направленность" / "(профиль" / "(общий профиль"
    base_dir = re.sub(r'(?:,?\s*направленность|\(\s*(?:направленность|профиль|общий\s+профиль)).*$', '', name, flags=re.IGNORECASE).strip(' ,;.')
    base_dir = re.sub(r'[«»"]', '', base_dir).strip()

    if not base_dir and not profile_raw:
        return None, None, None

    low_base = base_dir.lower()
    low_prof = profile_raw.lower()

    if 'прикладная математика' in low_base:
        group_title = f'Прикладная математика и информатика ({course} курс)'
    elif 'экология' in low_base:
        group_title = f'Экология и природопользование ({course} курс)'
    elif 'биология' in low_base and 'педагогическое' not in low_base:
        group_title = f'Биология ({course} курс)'
    elif 'экономика' in low_base:
        group_title = f'Экономика ({course} курс)'
    elif 'юриспруденция' in low_base:
        group_title = f'Юриспруденция ({course} курс)'
    elif 'технология художественной обработки' in low_base:
        group_title = f'Технология худ. обработки материалов ({course} курс)'
    elif 'психолого-педагогическое' in low_base:
        if 'психология и социальная' in low_prof:
            group_title = f'Психология и социальная педагогика ({course} курс)'
        elif profile_raw:
            clean_p = re.sub(r'[«»"]', '', profile_raw).strip()
            group_title = f'{clean_p} ({course} курс)'
        else:
            group_title = f'Психолого-педагогическое образование ({course} курс)'
    elif 'педагогическое' in low_base:
        if 'иностранный' in low_prof and 'родного языка' in low_prof:
            group_title = f'Иностранный язык и Родной язык и литература ({course} курс)'
        elif 'дошкольное' in low_prof and 'начальное' in low_prof:
            group_title = f'Дошкольное образование и Начальное образование ({course} курс)'
        elif 'физ' in low_prof and 'спорт' in low_prof:
            group_title = f'Пед. образование: Физ. культура и спорт ({course} курс)'
        elif 'физ' in low_prof:
            group_title = f'Педагогическое образование: Физ. культура ({course} курс)'
        elif 'биология' in low_prof and 'химия' in low_prof:
            group_title = f'Пед. образование: Биология и Химия ({course} курс)'
        elif profile_raw:
            clean_p = re.sub(r'[«»"]', '', profile_raw)
            clean_p = re.sub(r'\s*,\s*', ' и ', clean_p).strip()
            group_title = f'{clean_p} ({course} курс)'
        else:
            group_title = f'Педагогическое образование ({course} курс)'
    else:
        clean_label = (
            f'{base_dir}: {profile_raw}'
            if profile_raw and profile_raw.lower() not in low_base and 'общий профиль' not in low_prof
            else base_dir
        )
        clean_label = re.sub(r'[«»"]', '', clean_label).strip()
        group_title = f'{clean_label} ({course} курс)'

    return course, group_title, practice_range


def _clean_lesson_type(type_raw):
    t = re.sub(r'Лвб', 'Лаб', str(type_raw or ''), flags=re.IGNORECASE)
    t = re.sub(r'\bпоток(?:\s+(?:с\s+)?[А-Яа-яЁёA-Za-z0-9+]+(?:\s+[а-яё]+)?)?', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^[\s\-–—.,;]+', '', t)
    t = re.sub(r'\s*\.\s*', ', ', t)
    t = re.sub(r'\s+', ' ', t).strip(' ,;-–—.')
    return t or 'ЛК, ПЗ'


def split_sub_segments(weeks_str, type_raw, default_room=''):
    """Разбивает сложную скобку на список кортежей (weeks_str, lesson_type, sub_room)."""
    type_fixed = re.sub(r'Лвб', 'Лаб', str(type_raw or ''), flags=re.IGNORECASE)
    cleaned = re.sub(r'\bпоток(?:\s+(?:с\s+)?[А-Яа-яЁёA-Za-z0-9+]+(?:\s+[а-яё]+)?)?', '', type_fixed, flags=re.IGNORECASE)
    cleaned = re.sub(r'^[\s\-–—]+', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ,;-–—')

    # Вариант 1 (ФЭП / ЕМФ / ПФ): "(1-9 ЛК 423 ауд., 10-16 ПР 422 ауд.)" или "(1-8 1 п/г ЛК, 9-16 2 п/г ПЗ)"
    sub_m = re.search(
        r'^(.*?),\s*(\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*)\s*([-–—]?\s*(?:(?:\d+|[IІVХX]+)\s*(?:п/г|подгр(?:уппа)?\.?)|ЛК|ПЗ|Лаб|ЛАБ|ЛР|ПР|Сем).*)$',
        cleaned,
        re.IGNORECASE,
    )
    if sub_m:
        t1_raw = sub_m.group(1)
        w2 = re.sub(r'\s+', '', sub_m.group(2)).replace('–', '-').replace('—', '-')
        t2_raw = sub_m.group(3)
        w1 = re.sub(r'\s+', '', weeks_str).replace('–', '-').replace('—', '-')

        r1, r2 = default_room, default_room
        rm1 = ROOM_RE.search(t1_raw)
        if rm1:
            r1 = normalize_room_str(rm1.group(0))
            t1_raw = (t1_raw[:rm1.start()] + ' ' + t1_raw[rm1.end():]).strip()
        rm2 = ROOM_RE.search(t2_raw)
        if rm2:
            r2 = normalize_room_str(rm2.group(0))
            t2_raw = (t2_raw[:rm2.start()] + ' ' + t2_raw[rm2.end():]).strip()
        return [(w1, _clean_lesson_type(t1_raw), r1), (w2, _clean_lesson_type(t2_raw), r2)]

    # Вариант 2 (ЕМФ без запятой): "(2-4,9 ЛК поток 5-6,8,10-15 ПЗ)"
    sub_m2 = re.search(
        r'^(ЛК|ПЗ|Лаб|ЛАБ|ЛР|ПР|Сем)\s+(\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*)\s*([-–—]?\s*(?:ЛК|ПЗ|Лаб|ЛАБ|ЛР|ПР|Сем).*)$',
        cleaned,
        re.IGNORECASE,
    )
    if sub_m2:
        t1 = _clean_lesson_type(sub_m2.group(1))
        w2 = re.sub(r'\s+', '', sub_m2.group(2)).replace('–', '-').replace('—', '-')
        t2 = _clean_lesson_type(sub_m2.group(3))
        w1 = re.sub(r'\s+', '', weeks_str).replace('–', '-').replace('—', '-')
        return [(w1, t1, default_room), (w2, t2, default_room)]

    # Одиночный блок (проверяем, не осталась ли аудитория внутри скобки)
    sub_room = default_room
    rm = ROOM_RE.search(cleaned)
    if rm:
        sub_room = normalize_room_str(rm.group(0))
        cleaned = (cleaned[:rm.start()] + ' ' + cleaned[rm.end():]).strip()

    w_norm = re.sub(r'\s+', '', weeks_str).replace('–', '-').replace('—', '-')
    return [(w_norm, _clean_lesson_type(cleaned), sub_room)]


def _extract_subject_teacher_room(pre_text):
    """
    Разбирает фрагмент текста ДО скобки с неделями на (subject, teacher, room, trailing_type).
    Поддерживает все стили: ЕМФ, ПФ, ФЭП, ТФ.
    """
    txt = re.sub(r'\s+', ' ', str(pre_text or '')).strip(' ,;-–—/')
    # Убираем случайное время в начале ячейки (например "15.05 ОБЩАЯ ФИЗИЧЕСКАЯ ПОДГОТОВКА" в ПФ)
    txt = re.sub(r'^\d{1,2}\.\d{2}\s+', '', txt).strip()

    room_clean = ''
    teacher_clean = ''
    trailing_type = ''

    # Проверяем, не стоит ли тип занятия прямо перед скобкой с неделями (ТФ: "ПЗ (9-16)")
    m_trail_t = re.search(r'(?:^|\s)(ЛК|ПЗ|ЛАБ|Лаб|ЛР|ПР|Сем)\.?$', txt, re.IGNORECASE)
    if m_trail_t:
        trailing_type = m_trail_t.group(1)
        txt = txt[:m_trail_t.start()].strip(' ,;-–—/')

    room_m = ROOM_RE.search(txt)
    if room_m:
        room_clean = normalize_room_str(room_m.group(0))
        txt = (txt[: room_m.start()] + ' ' + txt[room_m.end() :]).strip(' ,;-–—/')
        txt = re.sub(r'\s+', ' ', txt).strip()

    t_matches = list(TEACHER_FULL_RE.finditer(txt))
    if t_matches:
        last_t = t_matches[-1]
        if last_t.end() >= len(txt) - 2 and last_t.start() > 0:
            candidate_subj = txt[: last_t.start()].strip(' ,;-–—/')
            if len(candidate_subj) >= 2:
                teacher_clean = clean_teacher_str(last_t.group(0))
                txt = candidate_subj

    txt = re.sub(r'\bк\s+анд\.', 'канд.', txt, flags=re.IGNORECASE)
    t_matches_retry = list(TEACHER_FULL_RE.finditer(txt))
    if not teacher_clean and t_matches_retry:
        last_t = t_matches_retry[-1]
        if last_t.end() >= len(txt) - 2 and last_t.start() > 0:
            candidate_subj = txt[: last_t.start()].strip(' ,;-–—/')
            if len(candidate_subj) >= 2:
                teacher_clean = clean_teacher_str(last_t.group(0))
                txt = candidate_subj

    if txt.isupper() and len(txt) > 5:
        words = txt.split(' ')
        formatted_words = []
        for idx_w, w in enumerate(words):
            if re.match(r'^(?:[IVX]+|Ч\.\d+|ВНД|ГТО|РФ|РБ|СССР|ОТКД)$', w):
                formatted_words.append(w)
            elif idx_w == 0:
                formatted_words.append(w.capitalize())
            else:
                formatted_words.append(w.lower())
        txt = ' '.join(formatted_words)

    return txt.strip(' ,;-–—/'), teacher_clean, room_clean, trailing_type


def _extract_week_block_parts(match_obj):
    """Извлекает список пар (weeks_str, type_raw) из одного совпадения WEEK_BLOCK_RE."""
    if match_obj.group('w1'):
        return [(match_obj.group('w1'), match_obj.group('t1') or '')]
    if match_obj.group('w2'):
        pairs = [(match_obj.group('w2'), match_obj.group('t2') or '')]
        rest2 = (match_obj.group('rest2') or '').strip(' ,;-–—')
        # Поддержка ТФ вида "(ЛК 1-9, ПЗ 11-13)"
        if rest2:
            for m_sub in re.finditer(
                r'(' + _TYPE_TOKEN + r'(?:[\s.,/]+' + _TYPE_TOKEN + r')*)\s*[-–—]?\s*(' + _WEEKS_EXPR + r')',
                rest2,
                re.IGNORECASE,
            ):
                pairs.append((m_sub.group(2), m_sub.group(1)))
        return pairs
    if match_obj.group('w3'):
        return [(match_obj.group('w3'), '')]
    return []


def parse_cell_lessons(cell_text):
    """
    Универсальный парсер ячейки таблицы расписания:
    поддерживает форматы ЕМФ, ПФ, ФЭП и ТФ (включая (ЛАБ 3-6), / 251, (спортивный зал),
    несколько скобок на один предмет, несколько пар через перенос строки или '//').
    """
    flat = re.sub(r'\s+', ' ', str(cell_text or '')).strip()
    if not flat or flat in ('-', '—', '–'):
        return []

    matches = list(WEEK_BLOCK_RE.finditer(flat))
    if not matches:
        # Фолбэк для редкого формата ТФ без скобок: "15-17 Машиноведение ЛАБ. Асылов А.Ф. / 250"
        m_plain = re.match(
            r'^(' + _WEEKS_EXPR + r')\s+(.+?)\s+(' + _TYPE_TOKEN + r'(?:[\s.,/]+' + _TYPE_TOKEN + r')*)\.?\s+(.+)$',
            flat,
            re.IGNORECASE,
        )
        if m_plain:
            w_raw, subj_raw, type_raw, tail_raw = m_plain.groups()
            room_clean = ''
            rm = ROOM_RE.search(tail_raw)
            if rm:
                room_clean = normalize_room_str(rm.group(0))
                tail_raw = (tail_raw[:rm.start()] + ' ' + tail_raw[rm.end():]).strip()
            teacher_clean = clean_teacher_str(tail_raw) or 'Не указан'
            w_norm = re.sub(r'\s+', '', w_raw).replace('–', '-').replace('—', '-')
            return [(subj_raw.strip(), _clean_lesson_type(type_raw), teacher_clean, room_clean or 'Не указана', w_norm)]
        return []

    segments = []
    for i, m in enumerate(matches):
        tail_start = m.end()
        tail_end = matches[i + 1].start() if i + 1 < len(matches) else len(flat)
        between = flat[tail_start:tail_end].strip()

        if i == len(matches) - 1:
            tail_text = between
            next_subj_prefix = ''
        else:
            if '//' in between:
                left_p, right_p = between.split('//', 1)
                tail_text = left_p.strip()
                next_subj_prefix = right_p.strip()
            else:
                teacher_m = list(TEACHER_FULL_RE.finditer(between))
                room_m = list(ROOM_RE.finditer(between))

                if teacher_m:
                    first_t = teacher_m[0]
                    before_t = between[: first_t.start()].strip(' ,;-–—')
                    before_t_no_room = ROOM_RE.sub('', before_t).strip(' ,;-–—')
                    if not before_t_no_room:
                        split_pos = first_t.end()
                        after_t = between[split_pos:]
                        imm_room = re.match(r'^\s*' + ROOM_RE.pattern, after_t, re.IGNORECASE)
                        if imm_room:
                            split_pos += imm_room.end()
                        tail_text = between[:split_pos].strip()
                        next_subj_prefix = between[split_pos:].strip()
                    else:
                        imm_room_start = re.match(r'^\s*' + ROOM_RE.pattern, between, re.IGNORECASE)
                        if imm_room_start:
                            tail_text = between[: imm_room_start.end()].strip()
                            next_subj_prefix = between[imm_room_start.end() :].strip()
                        else:
                            tail_text = ''
                            next_subj_prefix = between
                elif room_m:
                    imm_room_start = re.match(r'^\s*' + ROOM_RE.pattern, between, re.IGNORECASE)
                    if imm_room_start:
                        tail_text = between[: imm_room_start.end()].strip()
                        next_subj_prefix = between[imm_room_start.end() :].strip()
                    else:
                        split_pos = room_m[0].end()
                        tail_text = between[:split_pos].strip()
                        next_subj_prefix = between[split_pos:].strip()
                else:
                    tail_text = ''
                    next_subj_prefix = between

        segments.append({'match': m, 'tail': tail_text, 'next_subj': next_subj_prefix})

    raw_entries = []
    for i, seg in enumerate(segments):
        m = seg['match']
        pre_raw = flat[: m.start()] if i == 0 else segments[i - 1]['next_subj']
        subj, teacher_from_pre, room_from_pre, trailing_type = _extract_subject_teacher_room(pre_raw)

        tail = seg['tail']
        room_clean = room_from_pre
        room_m = ROOM_RE.search(tail)
        if room_m:
            room_clean = normalize_room_str(room_m.group(0))
            tail_no_room = (tail[: room_m.start()] + ' ' + tail[room_m.end() :]).strip(' ,;-–—/')
        else:
            tail_no_room = tail.strip(' ,;-–—/')

        teacher_from_tail = clean_teacher_str(tail_no_room)
        teacher_clean = teacher_from_tail or teacher_from_pre

        wb_parts = _extract_week_block_parts(m)
        for w_raw, t_raw in wb_parts:
            eff_type = t_raw or trailing_type
            raw_entries.append({
                'subject': subj,
                'weeks_raw': w_raw,
                'type_raw': eff_type,
                'teacher': teacher_clean,
                'room': room_clean,
            })

    # Наследование предмета вперёд (если у того же предмета 2 скобки подряд: "Философия (2-6 ЛК) 421 ауд. (12-17 ПР) 365 ауд." или "(ЛК 2-6) (ПЗ 9)")
    for i in range(1, len(raw_entries)):
        if not raw_entries[i]['subject'] and raw_entries[i - 1]['subject']:
            raw_entries[i]['subject'] = raw_entries[i - 1]['subject']

    # Наследование аудитории и преподавателя назад (если указаны только у последней пары в ячейке)
    for i in range(len(raw_entries) - 2, -1, -1):
        if not raw_entries[i]['room'] and raw_entries[i + 1]['room']:
            raw_entries[i]['room'] = raw_entries[i + 1]['room']
        if not raw_entries[i]['teacher'] and raw_entries[i + 1]['teacher']:
            raw_entries[i]['teacher'] = raw_entries[i + 1]['teacher']

    # Наследование аудитории вперёд (если у 2-й пары в той же ячейке кабинет не дублируется)
    for i in range(1, len(raw_entries)):
        if not raw_entries[i]['room'] and raw_entries[i - 1]['room']:
            raw_entries[i]['room'] = raw_entries[i - 1]['room']

    result = []
    for ent in raw_entries:
        if not ent['subject']:
            continue
        for w_str, l_type, sub_room in split_sub_segments(ent['weeks_raw'], ent['type_raw'], default_room=ent['room']):
            result.append((
                ent['subject'],
                l_type,
                ent['teacher'] or 'Не указан',
                sub_room or ent['room'] or 'Не указана',
                w_str,
            ))
    return result


def convert_doc_to_docx_if_needed(file_path):
    """Если передан старый .doc, пытается конвертировать его в .docx через LibreOffice/soffice."""
    if not file_path.lower().endswith('.doc') or file_path.lower().endswith('.docx'):
        return file_path, None

    office_bin = shutil.which('libreoffice') or shutil.which('soffice')
    if not office_bin:
        return None, (
            '⚠️ <b>Файл в старом формате .doc, а на сервере не установлен LibreOffice для автоконвертации.</b>\n\n'
            'Открой файл в Word / Google Документах, нажми <b>«Сохранить как → Документ Word (*.docx)»</b> '
            'и отправь полученный файл <code>.docx</code> сюда.'
        )

    out_dir = tempfile.mkdtemp()
    try:
        subprocess.run(
            [office_bin, '--headless', '--convert-to', 'docx', '--outdir', out_dir, file_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        base_name = os.path.splitext(os.path.basename(file_path))[0] + '.docx'
        converted_path = os.path.join(out_dir, base_name)
        if os.path.exists(converted_path):
            return converted_path, None
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, '❌ Не удалось сконвертировать .doc в .docx.'
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, f'❌ Ошибка конвертации .doc: <code>{esc(e)}</code>'


def parse_schedule_docx_file(docx_path, default_faculty='ЕМФ'):
    """
    Читает .docx файл расписания СИ УУНиТ (ЕМФ, ПФ, ФЭП, ТФ) и возвращает:
    (records_list, practice_dict, semester_title, detected_faculty)
    """
    doc = docx.Document(docx_path)

    semester_title = ''
    detected_faculty = None
    for p in doc.paragraphs:
        t = re.sub(r'\s+', ' ', p.text or '').strip()
        if not detected_faculty:
            detected_faculty = detect_faculty_from_text(t)
        if 'полугодие' in t.lower() or 'учебного года' in t.lower():
            if not semester_title:
                semester_title = t

    faculty_code = detected_faculty or default_faculty or 'ЕМФ'
    records = []
    practice_dict = {}

    for table in doc.tables:
        grid = build_logical_grid(table)
        if len(grid) < 2:
            continue

        header_row_idx = None
        col_groups = {}
        col_default_rooms = {}
        for r_idx in range(min(4, len(grid))):
            row_tcs = grid[r_idx]
            found_any = False
            temp_cols = {}
            for c_idx, tc in enumerate(row_tcs):
                txt = get_tc_clean_text(tc)
                crs, grp_title, prac_range = parse_column_header(txt)
                if crs and grp_title:
                    temp_cols[c_idx] = (crs, grp_title)
                    found_any = True
                    if prac_range:
                        practice_dict[grp_title] = list(range(prac_range[0], prac_range[1] + 1))
            if found_any:
                header_row_idx = r_idx
                col_groups = temp_cols
                if r_idx + 1 < len(grid):
                    next_row = grid[r_idx + 1]
                    for c_idx in col_groups:
                        if c_idx < len(next_row):
                            sub_txt = get_tc_clean_text(next_row[c_idx]).strip()
                            rm_m = re.search(r'(?:ауд\.?\s*(\d+)|(\d+)\s*ауд)', sub_txt, re.IGNORECASE)
                            if rm_m:
                                col_default_rooms[c_idx] = f'ауд. {rm_m.group(1) or rm_m.group(2)}'
                break

            # Поддержка двухстрочной шапки (как на ФЭП: 1-я строка — "2 курс", 2-я строка — "Направление ... 365 ауд.")
            if r_idx + 1 < len(grid):
                next_row_tcs = grid[r_idx + 1]
                found_two_row = False
                temp_cols_2 = {}
                temp_rooms_2 = {}
                for c_idx, tc in enumerate(row_tcs):
                    top_txt = get_tc_clean_text(tc)
                    bot_txt = get_tc_clean_text(next_row_tcs[c_idx]) if c_idx < len(next_row_tcs) else ''
                    combined_txt = f'{top_txt} {bot_txt}'.strip()
                    crs, grp_title, prac_range = parse_column_header(combined_txt)
                    if crs and grp_title:
                        temp_cols_2[c_idx] = (crs, grp_title)
                        found_two_row = True
                        if prac_range:
                            practice_dict[grp_title] = list(range(prac_range[0], prac_range[1] + 1))
                        rm_m = re.search(r'(?:ауд\.?\s*(\d+)|(\d+)\s*ауд)', bot_txt, re.IGNORECASE)
                        if rm_m:
                            room_num = rm_m.group(1) or rm_m.group(2)
                            temp_rooms_2[c_idx] = f'ауд. {room_num}'
                if found_two_row:
                    header_row_idx = r_idx + 1
                    col_groups = temp_cols_2
                    col_default_rooms = temp_rooms_2
                    break

        if header_row_idx is None or not col_groups:
            continue

        header_data_start_col = min(col_groups.keys())
        current_day = None
        current_time_slot = None
        last_col_offset = 0

        for r_idx in range(header_row_idx + 1, len(grid)):
            row_tcs = grid[r_idx]
            if len(row_tcs) < 3:
                continue

            # Проверяем столбцы метаданных (0..min(3, header_data_start_col)) на день недели и время пары
            meta_scan_limit = min(len(row_tcs), max(3, header_data_start_col))
            day_candidate = None
            for m_c in range(min(2, len(row_tcs))):
                cand = normalize_day_name(get_tc_clean_text(row_tcs[m_c]))
                if cand:
                    day_candidate = cand
                    break

            if day_candidate and day_candidate != current_day:
                current_day = day_candidate
                current_time_slot = None

            found_time_slot = None
            time_end_col = None
            for m_c in range(meta_scan_limit):
                ts = normalize_time_slot(get_tc_clean_text(row_tcs[m_c]))
                if ts:
                    found_time_slot = ts
                    tc_time = row_tcs[m_c]
                    end_c = m_c
                    while end_c + 1 < len(row_tcs) and row_tcs[end_c + 1] is tc_time:
                        end_c += 1
                    time_end_col = end_c
                    break

            if found_time_slot:
                current_time_slot = found_time_slot
                # Компенсация сдвига столбцов, если в строке данных ячейка времени имеет лишний gridSpan (как в ЕМФ Пед. обр.)
                col_offset = (time_end_col + 1) - header_data_start_col
                if col_offset < 0:
                    col_offset = 0
                last_col_offset = col_offset
            else:
                col_offset = last_col_offset

            if not current_day or not current_time_slot:
                continue

            seen_tcs = set()
            for c_idx, (crs, grp_title) in col_groups.items():
                actual_c_idx = c_idx + col_offset
                if actual_c_idx >= len(row_tcs):
                    continue
                tc = row_tcs[actual_c_idx]
                tc_key = (grp_title, id(tc))
                if tc is None or tc_key in seen_tcs:
                    continue
                seen_tcs.add(tc_key)

                cell_txt = get_tc_clean_text(tc)
                lessons = parse_cell_lessons(cell_txt)
                default_room = col_default_rooms.get(c_idx, 'Не указана')
                for subj, l_type, teacher, room, weeks_str in lessons:
                    final_room = room if room != 'Не указана' else default_room
                    records.append({
                        'faculty': faculty_code,
                        'course': crs,
                        'group': grp_title,
                        'day': current_day,
                        'time': current_time_slot,
                        'subject': subj,
                        'type': l_type,
                        'teacher': teacher,
                        'room': final_room,
                        'weeks': weeks_str,
                    })

    return records, practice_dict, semester_title, faculty_code
