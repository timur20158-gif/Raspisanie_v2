import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id


# --- НАСТРОЙКИ ---
VK_TOKEN = os.getenv('VK_TOKEN', 'YOUR_VK_COMMUNITY_TOKEN_HERE')
VK_GROUP_ID = int(os.getenv('VK_GROUP_ID', '0') or '0')
TG_BOT_LINK = os.getenv('TG_BOT_LINK', '')  # опционально: https://t.me/твой_бот

LOCAL_TZ = ZoneInfo('Asia/Yekaterinburg')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv('DATA_DIR') or ('/data' if os.path.isdir('/data') else BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, 'schedule.db')
SEMESTER_META_PATH = os.path.join(DATA_DIR, 'semester_meta.json')

DAYS_RUS = [
    'Понедельник',
    'Вторник',
    'Среда',
    'Четверг',
    'Пятница',
    'Суббота',
    'Воскресенье',
]

PAIR_NUMBERS = {
    '08:00 - 09:20': 1,
    '09:35 - 10:55': 2,
    '11:10 - 12:30': 3,
    '13:30 - 14:50': 4,
    '15:05 - 16:25': 5,
    '16:40 - 18:00': 6,
}
NUM_TO_TIME_SLOT = {v: k for k, v in PAIR_NUMBERS.items()}
PAIR_EMOJIS = {1: '1️⃣', 2: '2️⃣', 3: '3️⃣', 4: '4️⃣', 5: '5️⃣', 6: '6️⃣'}

FACULTIES_SHORT = {
    'ЕМФ': 'ЕМФ',
    'ПФ': 'Педфак',
    'ФЭП': 'ФЭП',
    'ТФ': 'ТФ',
}
FACULTY_ORDER = ['ЕМФ', 'ПФ', 'ФЭП', 'ТФ']


def now_local():
    return datetime.now(LOCAL_TZ)


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    try:
        conn.execute('PRAGMA busy_timeout = 30000')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_vk_db():
    with db_connection() as conn:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute(
            'CREATE TABLE IF NOT EXISTS vk_users ('
            'vk_user_id INTEGER PRIMARY KEY, '
            'faculty TEXT DEFAULT "", '
            'course INTEGER DEFAULT 0, '
            'group_name TEXT DEFAULT "", '
            'state TEXT DEFAULT "", '
            'payload_json TEXT DEFAULT "{}", '
            'last_active REAL DEFAULT 0)'
        )


def load_semester_meta():
    ref = now_local()
    start_year = ref.year - 1 if ref.month <= 6 else ref.year
    start_dt = datetime(start_year, 9, 1, tzinfo=LOCAL_TZ)
    practice_weeks = {}
    if os.path.exists(SEMESTER_META_PATH):
        try:
            with open(SEMESTER_META_PATH, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            s_raw = meta.get('academic_year_start') or meta.get('start_date')
            if s_raw:
                start_dt = datetime.strptime(s_raw, '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
            if isinstance(meta.get('practice_weeks'), dict):
                practice_weeks = {k: set(v) for k, v in meta['practice_weeks'].items()}
        except Exception:
            pass
    return start_dt, practice_weeks


def get_week_and_day(target_date):
    start_dt, _ = load_semester_meta()
    start_year = start_dt.year - 1 if start_dt.month <= 6 else start_dt.year
    year_end = datetime(start_year + 1, 9, 1, tzinfo=LOCAL_TZ) - timedelta(days=1)
    t_date = target_date.date()
    s_date = start_dt.date()
    day_name = DAYS_RUS[target_date.weekday()]
    if t_date < s_date:
        return 0, day_name
    if t_date > year_end.date():
        return 53, day_name
    if s_date.weekday() == 6:
        if t_date == s_date:
            return 1, day_name
        week1_monday = s_date + timedelta(days=1)
    else:
        week1_monday = s_date - timedelta(days=s_date.weekday())
    return ((t_date - week1_monday).days // 7) + 1, day_name


def get_faculty_structure():
    struct = {f: {} for f in FACULTY_ORDER}
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT DISTINCT faculty, course_number, group_name FROM schedules '
                'ORDER BY faculty ASC, course_number ASC, group_name ASC'
            )
            for fac, crs, grp in cursor.fetchall():
                fac = fac or 'ЕМФ'
                struct.setdefault(fac, {}).setdefault(int(crs), []).append(grp)
        except Exception:
            pass
    return struct


def get_vk_user(vk_user_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT faculty, course, group_name, state, payload_json FROM vk_users WHERE vk_user_id = ?',
            (int(vk_user_id),),
        )
        row = cursor.fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[4]) if row[4] else {}
    except Exception:
        payload = {}
    return {
        'faculty': row[0] or '',
        'course': int(row[1] or 0),
        'group_name': row[2] or '',
        'state': row[3] or '',
        'payload': payload,
    }


def upsert_vk_user(vk_user_id, faculty=None, course=None, group_name=None, state=None, payload=None):
    cur = get_vk_user(vk_user_id) or {
        'faculty': '',
        'course': 0,
        'group_name': '',
        'state': '',
        'payload': {},
    }
    if faculty is not None:
        cur['faculty'] = faculty
    if course is not None:
        cur['course'] = int(course)
    if group_name is not None:
        cur['group_name'] = group_name
    if state is not None:
        cur['state'] = state
    if payload is not None:
        cur['payload'] = payload

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO vk_users (vk_user_id, faculty, course, group_name, state, payload_json, last_active) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                int(vk_user_id),
                cur['faculty'],
                cur['course'],
                cur['group_name'],
                cur['state'],
                json.dumps(cur['payload'], ensure_ascii=False),
                time.time(),
            ),
        )


def format_pair_prefix(time_slot):
    clean = str(time_slot or '').strip()
    num = PAIR_NUMBERS.get(clean)
    if not num:
        for slot, n in PAIR_NUMBERS.items():
            if clean.startswith(slot[:5]):
                num = n
                break
    if num:
        return f'{PAIR_EMOJIS.get(num, f"{num}.")} {num} пара ({clean})'
    return f'⏰ {clean}'


def get_day_schedule_plain(target_date, fac_code, course, group_name):
    start_dt, practice_weeks = load_semester_meta()
    week_num, day_name = get_week_and_day(target_date)
    date_str = target_date.strftime('%d.%m.%Y')
    fac_short = FACULTIES_SHORT.get(fac_code, fac_code or 'ЕМФ')

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT s.time_slot, s.subject_name, s.lesson_type, s.teacher, s.room '
            'FROM schedules s '
            'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
            'WHERE s.faculty = ? AND s.course_number = ? AND s.group_name = ? '
            'AND s.day_of_week = ? AND sw.week_number = ? '
            'ORDER BY s.time_slot ASC, s.id ASC',
            (fac_code, course, group_name, day_name, week_num),
        )
        all_rows = cursor.fetchall()

        cursor.execute(
            'SELECT time_slot FROM cancelled_lessons '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ?',
            (fac_code, group_name, date_str),
        )
        cancelled_slots = {r[0] for r in cursor.fetchall()}

        cursor.execute(
            'SELECT time_slot, subject_info FROM replacements '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? '
            'ORDER BY time_slot ASC, id ASC',
            (fac_code, group_name, date_str),
        )
        replacements = cursor.fetchall()
        replaced_slots = {r[0] for r in replacements}

        cursor.execute(
            'SELECT subject_name, hw_text, photo_file_id, doc_file_id FROM homeworks '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? '
            'ORDER BY id ASC',
            (fac_code, group_name, date_str),
        )
        hws = cursor.fetchall()

    rows = [r for r in all_rows if r[0] not in cancelled_slots and r[0] not in replaced_slots]
    week_disp = f'{week_num}-я' if week_num > 0 else 'ещё не началась'

    lines = [
        f'📅 {day_name.upper()}, {date_str}',
        f'🏛 {fac_short} • 🎓 {group_name} • {week_disp} нед.',
        '',
    ]

    if week_num <= 0 and not replacements:
        lines.append(f'⏳ Учебный год стартует с {start_dt.strftime("%d.%m.%Y")}.')
    elif day_name == 'Воскресенье' and not replacements:
        lines.append('🎉 Воскресенье — выходной!')
    elif week_num in practice_weeks.get(group_name, set()) and not replacements:
        lines.append('🛠 На этой неделе у группы проходит практика.')
    elif not rows and not replacements:
        lines.append('✨ Пар на этот день нет!')
    else:
        for time_slot, subj, l_type, teacher, room in rows:
            lines.append(f'{format_pair_prefix(time_slot)}')
            lines.append(f'📘 {subj} ({l_type})')
            lines.append(f'📍 {room} • 👤 {teacher}')
            lines.append('')

    if replacements:
        lines.append('⚡ ЗАМЕНЫ:')
        for r_time, r_subj in replacements:
            lines.append(f'🔄 {format_pair_prefix(r_time)}')
            lines.append(f'👉 {r_subj}')
            lines.append('')

    if hws:
        lines.append('📚 ДЗ НА ЭТОТ ДЕНЬ:')
        for subj, hw_text, p_id, d_id in hws:
            att = ' [есть вложение в TG]' if (p_id or d_id) else ''
            body = hw_text.strip() if hw_text else '(см. вложение в Telegram)'
            lines.append(f'• {subj}: {body}{att}')

    return '\n'.join(lines).strip()


def get_week_schedule_plain(target_date, fac_code, course, group_name):
    start_dt, practice_weeks = load_semester_meta()
    week_num, _ = get_week_and_day(target_date)
    monday = target_date - timedelta(days=target_date.weekday())
    saturday = monday + timedelta(days=5)
    fac_short = FACULTIES_SHORT.get(fac_code, fac_code or 'ЕМФ')

    lines = [
        f'🗓 РАСПИСАНИЕ НА {week_num}-Ю НЕДЕЛЮ ({monday.strftime("%d.%m")} — {saturday.strftime("%d.%m")})',
        f'🏛 {fac_short} • 🎓 {group_name}',
    ]
    if week_num <= 0:
        lines.append(f'\n⏳ Учебный год начинается с {start_dt.strftime("%d.%m.%Y")}.')
        return '\n'.join(lines)
    if week_num in practice_weeks.get(group_name, set()):
        lines.append('\n🛠 Всю эту неделю у группы проходит практика.')
        return '\n'.join(lines)

    valid_days = []
    for i in range(6):
        dt = monday + timedelta(days=i)
        if dt.date() >= start_dt.date():
            valid_days.append((dt, DAYS_RUS[i], dt.strftime('%d.%m.%Y')))
    if not valid_days:
        lines.append('\n✨ Пар на этой неделе нет.')
        return '\n'.join(lines)

    date_strs = [d[2] for d in valid_days]
    ph = ','.join('?' * len(date_strs))

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT s.day_of_week, s.time_slot, s.subject_name, s.lesson_type, s.room '
            'FROM schedules s '
            'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
            'WHERE s.faculty = ? AND s.course_number = ? AND s.group_name = ? '
            'AND sw.week_number = ? ORDER BY s.time_slot ASC, s.id ASC',
            (fac_code, course, group_name, week_num),
        )
        sched_by_day = {}
        for d_name, t_slot, subj, l_type, room in cursor.fetchall():
            sched_by_day.setdefault(d_name, []).append((t_slot, subj, l_type, room))

        cursor.execute(
            f'SELECT target_date, time_slot FROM cancelled_lessons '
            f'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date IN ({ph})',
            [fac_code, group_name] + date_strs,
        )
        canc_by_date = {}
        for c_d, c_t in cursor.fetchall():
            canc_by_date.setdefault(c_d, set()).add(c_t)

        cursor.execute(
            f'SELECT target_date, time_slot, subject_info FROM replacements '
            f'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date IN ({ph}) '
            f'ORDER BY time_slot ASC, id ASC',
            [fac_code, group_name] + date_strs,
        )
        repl_by_date = {}
        for r_d, r_t, r_s in cursor.fetchall():
            repl_by_date.setdefault(r_d, []).append((r_t, r_s))

    has_any = False
    for dt, d_name, d_str in valid_days:
        canc = canc_by_date.get(d_str, set())
        repls = repl_by_date.get(d_str, [])
        repl_slots = {r[0] for r in repls}
        rows = [r for r in sched_by_day.get(d_name, []) if r[0] not in canc and r[0] not in repl_slots]
        if rows or repls:
            has_any = True
            lines.append(f'\n🔹 {d_name.upper()} ({dt.strftime("%d.%m")})')
            for t_slot, subj, l_type, room in rows:
                lines.append(f'  {format_pair_prefix(t_slot)} — {subj} ({l_type}), {room}')
            for r_t, r_s in repls:
                lines.append(f'  🔄 [ЗАМЕНА] {format_pair_prefix(r_t)} — {r_s}')

    if not has_any:
        lines.append('\n✨ Пар на этой неделе нет.')
    return '\n'.join(lines)


def get_upcoming_hw_plain(fac_code, group_name):
    today = now_local().date()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT target_date, subject_name, hw_text, photo_file_id, doc_file_id, queue_id '
            'FROM homeworks WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (fac_code, group_name),
        )
        rows = cursor.fetchall()

    upcoming = []
    for d_str, subj, hw_text, p_id, d_id, q_id in rows:
        try:
            d_obj = datetime.strptime(d_str, '%d.%m.%Y').date()
        except ValueError:
            continue
        if d_obj >= today:
            upcoming.append((d_obj, d_str, subj, hw_text, p_id, d_id, q_id))
    upcoming.sort(key=lambda x: x[0])

    if not upcoming:
        return f'📚 АКТУАЛЬНЫЕ ДЗ ({group_name})\n\n✨ Актуальных заданий пока нет!'

    lines = [f'📚 АКТУАЛЬНЫЕ ДЗ ({group_name}) — {len(upcoming)} шт.\n']
    has_tg_only = False
    for _, d_str, subj, hw_text, p_id, d_id, q_id in upcoming[:15]:
        notes = []
        if p_id or d_id:
            notes.append('📎 вложение')
            has_tg_only = True
        if q_id:
            notes.append('📋 разбор вопросов')
            has_tg_only = True
        badge = f' [{" · ".join(notes)}]' if notes else ''
        body = hw_text.strip() if hw_text else '(см. вложение в Telegram)'
        lines.append(f'📅 {d_str[:5]} • {subj}{badge}\n📝 {body}\n')

    if has_tg_only:
        tg_hint = f' ({TG_BOT_LINK})' if TG_BOT_LINK else ''
        lines.append(f'💡 Фото, файлы и разбор вопросов доступны в основном Telegram-боте{tg_hint}.')
    return '\n'.join(lines).strip()


def search_teacher_today_plain(query):
    today = now_local()
    week_num, day_name = get_week_and_day(today)
    date_str = today.strftime('%d.%m.%Y')
    q_low = query.strip().lower()

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT s.time_slot, s.subject_name, s.lesson_type, s.teacher, s.room, s.group_name, s.faculty '
            'FROM schedules s '
            'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
            'WHERE s.day_of_week = ? AND sw.week_number = ? '
            'ORDER BY s.time_slot ASC',
            (day_name, week_num),
        )
        rows = cursor.fetchall()

    matches = []
    for t_slot, subj, l_type, teacher, room, grp, fac in rows:
        if q_low in str(teacher or '').lower():
            fac_s = FACULTIES_SHORT.get(fac, fac or '')
            matches.append(f'{format_pair_prefix(t_slot)}\n👤 {teacher}\n📍 {room} • 📘 {subj} ({l_type})\n🎓 {grp} [{fac_s}]\n')

    if not matches:
        return f'🔎 По запросу «{query}» на сегодня ({date_str}) пар не найдено.'
    return f'👨‍🏫 ПОИСК НА СЕГОДНЯ ({day_name}, {date_str}):\n\n' + '\n'.join(matches[:15]).strip()


def build_main_kb():
    kb = VkKeyboard(one_time=False)
    kb.add_button('📅 Сегодня', color=VkKeyboardColor.PRIMARY)
    kb.add_button('⏭ Завтра', color=VkKeyboardColor.PRIMARY)
    kb.add_button('🗓 Неделя', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button('📚 Домашка', color=VkKeyboardColor.POSITIVE)
    kb.add_button('👨‍🏫 Найти препода', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button('⚙️ Сменить группу', color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def build_faculties_kb():
    struct = get_faculty_structure()
    kb = VkKeyboard(one_time=True)
    added = 0
    for f_code in FACULTY_ORDER:
        if any(bool(g) for g in struct.get(f_code, {}).values()):
            if added > 0 and added % 2 == 0:
                kb.add_line()
            kb.add_button(f'🏛 {FACULTIES_SHORT.get(f_code, f_code)}', color=VkKeyboardColor.PRIMARY)
            added += 1
    if added == 0:
        kb.add_button('🔄 Обновить', color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def build_courses_kb(fac_code):
    struct = get_faculty_structure()
    courses = sorted(c for c, grps in struct.get(fac_code, {}).items() if grps)
    kb = VkKeyboard(one_time=True)
    for idx, c in enumerate(courses):
        if idx > 0 and idx % 2 == 0:
            kb.add_line()
        kb.add_button(f'🎓 {c} курс', color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button('🔙 Факультеты', color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def build_groups_kb(fac_code, course):
    struct = get_faculty_structure()
    groups = struct.get(fac_code, {}).get(course, [])
    kb = VkKeyboard(one_time=True)
    for idx, g_nm in enumerate(groups[:8]):
        if idx > 0:
            kb.add_line()
        label = g_nm if len(g_nm) <= 38 else g_nm[:37] + '…'
        kb.add_button(label, color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button('🔙 Курсы', color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def send_vk_msg(vk, peer_id, text, keyboard=None):
    chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or ['']
    for idx, ch in enumerate(chunks):
        params = {
            'peer_id': peer_id,
            'message': ch,
            'random_id': get_random_id(),
        }
        if keyboard and idx == len(chunks) - 1:
            params['keyboard'] = keyboard
        vk.messages.send(**params)


def handle_message(vk, user_id, peer_id, text):
    raw = (text or '').strip()
    low = raw.lower()
    user = get_vk_user(user_id)

    if low in ('начать', 'старт', '/start', '⚙️ сменить группу', '🔙 факультеты'):
        upsert_vk_user(user_id, state='pick_fac', payload={})
        send_vk_msg(
            vk,
            peer_id,
            '👋 Привет! Я бот расписания СИ УУНиТ (общая база с Telegram-ботом).\n\nВыбери свой факультет:',
            keyboard=build_faculties_kb(),
        )
        return

    if not user or not user['group_name']:
        state = user['state'] if user else ''
        payload = user['payload'] if user else {}
        struct = get_faculty_structure()

        if state == 'pick_fac':
            chosen_fac = None
            for f_code in FACULTY_ORDER:
                short_nm = FACULTIES_SHORT.get(f_code, f_code)
                if raw in (f'🏛 {short_nm}', short_nm, f_code):
                    chosen_fac = f_code
                    break
            if chosen_fac:
                upsert_vk_user(user_id, faculty=chosen_fac, state='pick_course', payload={'fac': chosen_fac})
                send_vk_msg(
                    vk,
                    peer_id,
                    f'🏛 Факультет: {FACULTIES_SHORT.get(chosen_fac, chosen_fac)}\nВыбери свой курс:',
                    keyboard=build_courses_kb(chosen_fac),
                )
                return

        elif state == 'pick_course':
            fac_code = payload.get('fac') or user['faculty']
            if raw == '🔙 Курсы':
                send_vk_msg(vk, peer_id, 'Выбери свой курс:', keyboard=build_courses_kb(fac_code))
                return
            m = re.search(r'(\d+)', raw)
            if m and int(m.group(1)) in struct.get(fac_code, {}):
                crs = int(m.group(1))
                upsert_vk_user(user_id, course=crs, state='pick_group', payload={'fac': fac_code, 'course': crs})
                send_vk_msg(
                    vk,
                    peer_id,
                    f'🎓 Выбран {crs} курс. Теперь выбери свою группу:',
                    keyboard=build_groups_kb(fac_code, crs),
                )
                return

        elif state == 'pick_group':
            fac_code = payload.get('fac') or user['faculty']
            crs = int(payload.get('course') or user['course'] or 0)
            if raw == '🔙 Курсы':
                upsert_vk_user(user_id, state='pick_course', payload={'fac': fac_code})
                send_vk_msg(vk, peer_id, 'Выбери свой курс:', keyboard=build_courses_kb(fac_code))
                return
            groups = struct.get(fac_code, {}).get(crs, [])
            matched_grp = None
            for g in groups:
                short_g = g if len(g) <= 38 else g[:37] + '…'
                if raw == g or raw == short_g or raw.rstrip('…') in g:
                    matched_grp = g
                    break
            if matched_grp:
                upsert_vk_user(user_id, faculty=fac_code, course=crs, group_name=matched_grp, state='', payload={})
                send_vk_msg(
                    vk,
                    peer_id,
                    f'✅ Профиль сохранён!\n🏛 {FACULTIES_SHORT.get(fac_code, fac_code)} • {crs} курс • {matched_grp}\n\nВыбери действие в меню:',
                    keyboard=build_main_kb(),
                )
                return

        upsert_vk_user(user_id, state='pick_fac', payload={})
        send_vk_msg(vk, peer_id, '👇 Сначала выбери свой факультет:', keyboard=build_faculties_kb())
        return

    # Пользователь уже зарегистрирован
    if user['state'] == 'awaiting_teacher':
        upsert_vk_user(user_id, state='', payload={})
        if low in ('отмена', 'назад'):
            send_vk_msg(vk, peer_id, '❌ Поиск отменён.', keyboard=build_main_kb())
            return
        res = search_teacher_today_plain(raw)
        send_vk_msg(vk, peer_id, res, keyboard=build_main_kb())
        return

    fac_code, course, group_name = user['faculty'], user['course'], user['group_name']

    if raw == '📅 Сегодня' or low == 'сегодня':
        send_vk_msg(vk, peer_id, get_day_schedule_plain(now_local(), fac_code, course, group_name), keyboard=build_main_kb())
    elif raw == '⏭ Завтра' or low == 'завтра':
        send_vk_msg(vk, peer_id, get_day_schedule_plain(now_local() + timedelta(days=1), fac_code, course, group_name), keyboard=build_main_kb())
    elif raw == '🗓 Неделя' or low == 'неделя':
        send_vk_msg(vk, peer_id, get_week_schedule_plain(now_local(), fac_code, course, group_name), keyboard=build_main_kb())
    elif raw == '📚 Домашка' or low in ('дз', 'домашка'):
        send_vk_msg(vk, peer_id, get_upcoming_hw_plain(fac_code, group_name), keyboard=build_main_kb())
    elif raw == '👨‍🏫 Найти препода' or low == 'поиск':
        upsert_vk_user(user_id, state='awaiting_teacher', payload={})
        send_vk_msg(vk, peer_id, '👨‍🏫 Введи фамилию преподавателя (поиск пар на сегодня):')
    else:
        send_vk_msg(vk, peer_id, '👇 Используй кнопки меню ниже:', keyboard=build_main_kb())


def run_vk_bot():
    if not VK_TOKEN or VK_TOKEN == 'YOUR_VK_COMMUNITY_TOKEN_HERE' or VK_GROUP_ID == 0:
        raise RuntimeError('Укажи переменные окружения VK_TOKEN и VK_GROUP_ID!')
    init_vk_db()
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID)
    print('VK-бот СИ УУНиТ (сыночек) запущен и подключён к общей БД!')
    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW and event.from_user:
                    msg = event.obj.message
                    handle_message(vk, msg['from_id'], msg['peer_id'], msg.get('text', ''))
        except Exception as e:
            print(f'Сбой VK LongPoll, перезапуск через 5 сек: {e}')
            time.sleep(5)


if __name__ == '__main__':
    run_vk_bot()
