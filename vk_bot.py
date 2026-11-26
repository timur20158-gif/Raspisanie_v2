import io
import json
import os
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.exceptions import ApiError
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.upload import VkUpload
from vk_api.utils import get_random_id

import core
from core import (
    ADMIN_USERNAME,
    DAYS_RUS,
    FACULTY_ORDER,
    LOCAL_TZ,
    NUM_TO_TIME_SLOT,
    PAIR_EMOJIS,
    PAIR_NUMBERS,
    db_connection,
    enqueue_hw_media,
    enqueue_message,
    esc,
    find_course_and_faculty_by_group,
    format_pair_prefix as tg_format_pair_prefix,
    get_bound_group_chats,
    get_current_week_and_day,
    get_faculty_short,
    infer_faculty_by_group,
    log_stat_event,
    now_local,
    parse_photo_ids,
    serialize_photo_ids,
    short_text,
)
from homework import find_upcoming_dates_for_subject
from schedule_search import (
    extract_rooms_from_text,
    extract_teacher_surname_initials,
    get_all_group_subjects,
    get_day_lessons_for_date,
    is_homework_passed_today,
)


# --- НАСТРОЙКИ ---
VK_TOKEN = os.getenv('VK_TOKEN', 'YOUR_VK_COMMUNITY_TOKEN_HERE')
VK_GROUP_ID = int(os.getenv('VK_GROUP_ID', '0') or '0')
VK_ADMIN_ID = int(os.getenv('VK_ADMIN_ID', '0') or '0')
TG_BOT_LINK = os.getenv('TG_BOT_LINK', '')

_VK_API = None
_VK_SESSION = None
_VK_SEND_QUEUE = queue.Queue()


def _vk_sender_worker():
    while True:
        task = _VK_SEND_QUEUE.get()
        if task is None:
            _VK_SEND_QUEUE.task_done()
            break
        try:
            if _VK_API is not None:
                peer_id, text, attachment, keyboard = task
                chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or ['']
                for idx, ch in enumerate(chunks):
                    params = {
                        'peer_id': peer_id,
                        'message': ch,
                        'random_id': get_random_id(),
                    }
                    if attachment and idx == len(chunks) - 1:
                        params['attachment'] = attachment
                    if keyboard and idx == len(chunks) - 1:
                        params['keyboard'] = keyboard
                    _VK_API.messages.send(**params)
        except ApiError as api_e:
            if getattr(api_e, 'code', None) in (901, 902):
                try:
                    with db_connection() as conn:
                        conn.execute(
                            'UPDATE vk_users SET sub_morning = 0, sub_evening = 0, notify_updates = 0, is_blocked = 1 WHERE vk_user_id = ?',
                            (int(peer_id),),
                        )
                except Exception:
                    pass
            else:
                print(f'Ошибка VK API ({getattr(api_e, "code", "")}): {api_e}')
        except Exception as e:
            print(f'Ошибка фоновой отправки VK: {e}')
        finally:
            _VK_SEND_QUEUE.task_done()
            time.sleep(0.06)


_vk_sender_thread = threading.Thread(target=_vk_sender_worker, name='VKSenderWorker', daemon=True)
_vk_sender_thread.start()


def enqueue_vk_msg(peer_id, text, attachment=None, keyboard=None):
    _VK_SEND_QUEUE.put((int(peer_id), str(text or ''), attachment, keyboard))


def notify_vk_group_users(faculty_code, group_name, plain_text, exclude_vk_id=None):
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT vk_user_id FROM vk_users '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND notify_updates = 1 AND COALESCE(is_blocked, 0) = 0',
            (eff_fac, group_name),
        )
        rows = cursor.fetchall()
    for (uid,) in rows:
        if exclude_vk_id and int(uid) == int(exclude_vk_id):
            continue
        enqueue_vk_msg(uid, plain_text)


def _fetch_vk_full_name(vk, vk_user_id):
    try:
        info = vk.users.get(user_ids=vk_user_id)
        if info:
            first = info[0].get('first_name', '').strip()
            last = info[0].get('last_name', '').strip()
            name = f'{first} {last}'.strip()
            if name:
                return name
    except Exception:
        pass
    return f'id{vk_user_id}'


def get_vk_user(vk_user_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT faculty, course, group_name, state, payload_json, '
            'COALESCE(is_starosta, 0), COALESCE(managed_group, ""), '
            'COALESCE(sub_morning, 0), COALESCE(sub_evening, 0), '
            'COALESCE(notify_updates, 0), COALESCE(full_name, "") '
            'FROM vk_users WHERE vk_user_id = ?',
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
        'is_starosta': int(row[5] or 0),
        'managed_group': row[6] or '',
        'sub_morning': int(row[7] or 0),
        'sub_evening': int(row[8] or 0),
        'notify_updates': int(row[9] or 0),
        'full_name': row[10] or '',
    }


def upsert_vk_user(vk_user_id, **kwargs):
    cur = get_vk_user(vk_user_id)
    now_ts = time.time()
    is_new = cur is None
    if is_new:
        cur = {
            'faculty': '',
            'course': 0,
            'group_name': '',
            'state': '',
            'payload': {},
            'is_starosta': 0,
            'managed_group': '',
            'sub_morning': 0,
            'sub_evening': 0,
            'notify_updates': 0,
            'full_name': '',
        }
    for k, v in kwargs.items():
        if k in cur and v is not None:
            cur[k] = v

    with db_connection() as conn:
        cursor = conn.cursor()
        if is_new:
            cursor.execute(
                'INSERT INTO vk_users '
                '(vk_user_id, faculty, course, group_name, state, payload_json, last_active, created_at, '
                'is_starosta, managed_group, sub_morning, sub_evening, notify_updates, full_name) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    int(vk_user_id),
                    cur['faculty'],
                    int(cur['course']),
                    cur['group_name'],
                    cur['state'],
                    json.dumps(cur['payload'], ensure_ascii=False),
                    now_ts,
                    now_ts,
                    int(cur['is_starosta']),
                    cur['managed_group'],
                    int(cur['sub_morning']),
                    int(cur['sub_evening']),
                    int(cur['notify_updates']),
                    cur['full_name'],
                ),
            )
        else:
            cursor.execute(
                'UPDATE vk_users SET faculty=?, course=?, group_name=?, state=?, payload_json=?, '
                'last_active=?, is_starosta=?, managed_group=?, sub_morning=?, sub_evening=?, '
                'notify_updates=?, full_name=?, is_blocked=0 WHERE vk_user_id=?',
                (
                    cur['faculty'],
                    int(cur['course']),
                    cur['group_name'],
                    cur['state'],
                    json.dumps(cur['payload'], ensure_ascii=False),
                    now_ts,
                    int(cur['is_starosta']),
                    cur['managed_group'],
                    int(cur['sub_morning']),
                    int(cur['sub_evening']),
                    int(cur['notify_updates']),
                    cur['full_name'],
                    int(vk_user_id),
                ),
            )
    return cur


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


# --- МЕДИА-МОСТ TG -> VK (С КЭШИРОВАНИЕМ И БЛОКИРОВКОЙ ПО hw_id) ---
_MEDIA_BRIDGE_MASTER_LOCK = threading.Lock()
_MEDIA_BRIDGE_LOCKS = {}


def _get_hw_bridge_lock(hw_id):
    with _MEDIA_BRIDGE_MASTER_LOCK:
        lock = _MEDIA_BRIDGE_LOCKS.get(int(hw_id))
        if lock is None:
            lock = threading.Lock()
            _MEDIA_BRIDGE_LOCKS[int(hw_id)] = lock
        return lock


def get_or_upload_vk_attachments(hw_id, photo_file_id, doc_file_id, doc_file_name, peer_id):
    if not (photo_file_id or doc_file_id) or _VK_SESSION is None:
        return ''
    with _get_hw_bridge_lock(hw_id):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COALESCE(vk_attachments, "") FROM homeworks WHERE id = ?', (int(hw_id),))
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]

        attachments = []
        try:
            upload = VkUpload(_VK_SESSION)
            photo_ids = parse_photo_ids(photo_file_id)
            for pid in photo_ids[:5]:
                if str(pid).startswith(('http://', 'https://')):
                    continue
                try:
                    f_info = core.bot.get_file(pid)
                    raw_bytes = core.bot.download_file(f_info.file_path)
                    bio = io.BytesIO(raw_bytes)
                    bio.name = 'photo.jpg'
                    res = upload.photo_messages(bio, peer_id=peer_id)
                    if res:
                        p = res[0]
                        attachments.append(f'photo{p["owner_id"]}_{p["id"]}')
                except Exception as e_p:
                    print(f'VK media bridge photo err (hw {hw_id}): {e_p}')

            if doc_file_id and not str(doc_file_id).startswith(('http://', 'https://')):
                try:
                    f_info = core.bot.get_file(doc_file_id)
                    raw_bytes = core.bot.download_file(f_info.file_path)
                    bio = io.BytesIO(raw_bytes)
                    bio.name = doc_file_name or 'document.bin'
                    res = upload.document_message(bio, title=(doc_file_name or 'Документ'), peer_id=peer_id)
                    if res and 'doc' in res:
                        d = res['doc']
                        attachments.append(f'doc{d["owner_id"]}_{d["id"]}')
                except Exception as e_d:
                    print(f'VK media bridge doc err (hw {hw_id}): {e_d}')
        except Exception as e:
            print(f'VK media bridge err: {e}')

        att_str = ','.join(attachments)
        if att_str:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE homeworks SET vk_attachments = ? WHERE id = ?', (att_str, int(hw_id)))
        return att_str


# --- ГЕНЕРАЦИЯ ТЕКСТОВ РАСПИСАНИЯ ---
def get_day_schedule_plain(target_date, fac_code, course, group_name):
    week_num, day_name = get_current_week_and_day(target_date, faculty_code=fac_code)
    date_str = target_date.strftime('%d.%m.%Y')
    fac_short = get_faculty_short(fac_code or 'ЕМФ')

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
        lines.append(f'⏳ Учебный год стартует с {core.ACADEMIC_YEAR_START_DATE.strftime("%d.%m.%Y")}.')
    elif day_name == 'Воскресенье' and not replacements:
        lines.append('🎉 Воскресенье — выходной!')
    elif week_num in core.PRACTICE_WEEKS.get(group_name, set()) and not replacements:
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
            att = ' [📎 вложение]' if (p_id or d_id) else ''
            body = hw_text.strip() if hw_text else '(см. вложение в «📚 Домашка»)'
            lines.append(f'• {subj}: {body}{att}')

    return '\n'.join(lines).strip()


def get_week_schedule_plain(target_date, fac_code, course, group_name):
    week_num, _ = get_current_week_and_day(target_date, faculty_code=fac_code)
    monday = target_date - timedelta(days=target_date.weekday())
    saturday = monday + timedelta(days=5)
    fac_short = get_faculty_short(fac_code or 'ЕМФ')

    lines = [
        f'🗓 РАСПИСАНИЕ НА {week_num}-Ю НЕДЕЛЮ ({monday.strftime("%d.%m")} — {saturday.strftime("%d.%m")})',
        f'🏛 {fac_short} • 🎓 {group_name}',
    ]
    if week_num <= 0:
        lines.append(f'\n⏳ Учебный год начинается с {core.ACADEMIC_YEAR_START_DATE.strftime("%d.%m.%Y")}.')
        return '\n'.join(lines)
    if week_num in core.PRACTICE_WEEKS.get(group_name, set()):
        lines.append('\n🛠 Всю эту неделю у группы проходит практика.')
        return '\n'.join(lines)

    valid_days = []
    for i in range(6):
        dt = monday + timedelta(days=i)
        if dt.date() >= core.ACADEMIC_YEAR_START_DATE.date():
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


# --- ДОМАШНИЕ ЗАДАНИЯ: ОГЛАВЛЕНИЕ, КАРТОЧКА, СЛОТЫ (VK), СДЕЛАНО ---
def get_active_hws(fac_code, course, group_name):
    eff_fac = fac_code or infer_faculty_by_group(group_name)
    now_dt = now_local()
    today = now_dt.date()
    today_slot_map = (
        get_day_lessons_for_date(now_dt, course, group_name, faculty_code=eff_fac)[0]
        if course
        else {}
    )
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, annot_queue_id '
            'FROM homeworks WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (eff_fac, group_name),
        )
        rows = cursor.fetchall()

    filtered = []
    for item in rows:
        try:
            d_obj = datetime.strptime(item[1], '%d.%m.%Y').date()
        except ValueError:
            continue
        if d_obj < today:
            continue
        if d_obj == today and is_homework_passed_today(item[2], today_slot_map, now_dt=now_dt):
            continue
        filtered.append((d_obj, item))
    filtered.sort(key=lambda x: (x[0], x[1][0]))
    return [x[1] for x in filtered]


def get_vk_done_hw_ids(vk_user_id):
    neg_uid = -abs(int(vk_user_id))
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT hw_id FROM hw_done WHERE user_id = ?', (neg_uid,))
        return {r[0] for r in cursor.fetchall()}


def format_queue_plain(queue_id, neg_uid):
    if not queue_id:
        return ''
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT max_slots, is_open, title FROM queues WHERE id = ?', (queue_id,))
        q_row = cursor.fetchone()
        if not q_row:
            return ''
        max_slots, is_open, raw_title = q_row
        cursor.execute(
            'SELECT slot_num, user_id, user_label FROM queue_slots WHERE queue_id = ? ORDER BY slot_num ASC',
            (queue_id,),
        )
        slots = cursor.fetchall()

    title = str(raw_title or 'Вопросы').strip()
    if '(ДЗ на ' in title:
        title = 'Аннотации' if 'Аннотации' in title else 'Вопросы'
    taken = {n: (u_id, lbl) for n, u_id, lbl in slots}
    free_nums = [n for n in range(1, max_slots + 1) if n not in taken]

    lines = [f'\n📋 {title} ({len(slots)}/{max_slots}):']
    for n in sorted(taken.keys()):
        u_id, lbl = taken[n]
        me_mark = ' ★' if u_id == neg_uid else ''
        lines.append(f'  {n}. {lbl}{me_mark}')
    if free_nums:
        lines.append(f'Свободно: {", ".join(str(n) for n in free_nums)}')
    else:
        lines.append('Все вопросы разобраны!')
    return '\n'.join(lines)


def send_hw_index(vk, peer_id, vk_user_id, user, page=0):
    items = get_active_hws(user['faculty'], user['course'], user['group_name'])
    if not items:
        kb_empty = VkKeyboard(inline=True)
        kb_empty.add_button('➕ Добавить ДЗ', color=VkKeyboardColor.POSITIVE, payload={'act': 'st_hw'})
        send_vk_msg(
            vk,
            peer_id,
            f'📚 Актуальные ДЗ ({user["group_name"]})\n\n✨ Заданий пока нет!',
            keyboard=kb_empty.get_keyboard(),
        )
        return

    done_ids = get_vk_done_hw_ids(vk_user_id)
    per_page = 4
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * per_page : (page + 1) * per_page]

    lines = [f'📚 АКТУАЛЬНЫЕ ДЗ ({user["group_name"]}) — {len(items)} шт. (стр. {page + 1}/{total_pages})\nНажми на кнопку, чтобы открыть карточку с вложениями и вопросами:']
    kb = VkKeyboard(inline=True)
    for idx, hw in enumerate(chunk):
        hw_id, hw_date, hw_subj = hw[0], hw[1], hw[2]
        done_mark = ' ✅' if hw_id in done_ids else ''
        att_mark = ' 📎' if (hw[4] or hw[5]) else ''
        q_mark = ' 📋' if (hw[7] or hw[8]) else ''
        btn_lbl = f'📘 {short_text(hw_subj, 22)} • {hw_date[:5]}{done_mark}'
        if idx > 0:
            kb.add_line()
        kb.add_button(btn_lbl[:40], color=VkKeyboardColor.PRIMARY, payload={'act': 'hw_open', 'id': hw_id})
        lines.append(f'• {hw_date[:5]} — {hw_subj}{att_mark}{q_mark}{done_mark}')

    if total_pages > 1:
        kb.add_line()
        if page > 0:
            kb.add_button('⬅️ Назад', color=VkKeyboardColor.SECONDARY, payload={'act': 'hw_page', 'p': page - 1})
        if page < total_pages - 1:
            kb.add_button('➡️ Ещё', color=VkKeyboardColor.SECONDARY, payload={'act': 'hw_page', 'p': page + 1})

    kb.add_line()
    kb.add_button('➕ Добавить ДЗ', color=VkKeyboardColor.POSITIVE, payload={'act': 'st_hw'})

    send_vk_msg(vk, peer_id, '\n'.join(lines), keyboard=kb.get_keyboard())


def send_hw_card(vk, peer_id, vk_user_id, hw_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, annot_queue_id, group_name, added_by '
            'FROM homeworks WHERE id = ?',
            (int(hw_id),),
        )
        hw = cursor.fetchone()
    if not hw:
        send_vk_msg(vk, peer_id, '⚠️ Задание не найдено или уже удалено.')
        return

    neg_uid = -abs(int(vk_user_id))
    done_ids = get_vk_done_hw_ids(vk_user_id)
    is_done = hw[0] in done_ids
    done_mark = ' ✅' if is_done else ''

    text = f'📘 {hw[2]} • {hw[1][:5]}{done_mark}\n'
    if hw[3]:
        text += f'\n📝 {hw[3]}\n'
    if hw[7]:
        text += format_queue_plain(hw[7], neg_uid)
    if hw[8]:
        text += format_queue_plain(hw[8], neg_uid)

    att_str = get_or_upload_vk_attachments(hw[0], hw[4], hw[5], hw[6], peer_id)

    u_cur = get_vk_user(vk_user_id) or {}
    can_del = (hw[10] is not None and int(hw[10]) == neg_uid) or (
        u_cur.get('is_starosta') == 1 and (u_cur.get('managed_group') or u_cur.get('group_name')) == hw[9]
    )
    kb = VkKeyboard(inline=True)
    if is_done:
        kb.add_button('↩️ Не сделано', color=VkKeyboardColor.SECONDARY, payload={'act': 'hw_done', 'id': hw[0], 'v': 0})
    else:
        kb.add_button('✅ Сделал', color=VkKeyboardColor.POSITIVE, payload={'act': 'hw_done', 'id': hw[0], 'v': 1})
    if can_del:
        kb.add_line()
        kb.add_button('✏️ Изменить', color=VkKeyboardColor.SECONDARY, payload={'act': 'hw_edit', 'id': hw[0]})
        kb.add_button('🗑 Удалить ДЗ', color=VkKeyboardColor.NEGATIVE, payload={'act': 'hw_del', 'id': hw[0]})

    if hw[7]:
        kb.add_line()
        kb.add_button('✋ Занять вопрос', color=VkKeyboardColor.PRIMARY, payload={'act': 'hwq_pick', 'qid': hw[7], 'hwid': hw[0]})
        kb.add_button('❌ Снять вопрос', color=VkKeyboardColor.NEGATIVE, payload={'act': 'hwq_free', 'qid': hw[7], 'hwid': hw[0]})
    if hw[8]:
        kb.add_line()
        kb.add_button('✋ Занять (список 2)', color=VkKeyboardColor.PRIMARY, payload={'act': 'hwq_pick', 'qid': hw[8], 'hwid': hw[0]})
        kb.add_button('❌ Снять (список 2)', color=VkKeyboardColor.NEGATIVE, payload={'act': 'hwq_free', 'qid': hw[8], 'hwid': hw[0]})

    send_vk_msg(vk, peer_id, text.strip(), keyboard=kb.get_keyboard(), attachment=att_str or None)


# --- ПОИСК ПРЕПОДАВАТЕЛЯ И СВОБОДНЫХ АУДИТОРИЙ ---
def search_teacher_plain(query, period):
    today = now_local()
    q_low = query.strip().lower()
    if period == 'today':
        dates = [today]
        p_title = f'Сегодня ({DAYS_RUS[today.weekday()]}, {today.strftime("%d.%m")})'
    elif period == 'tomorrow':
        tmr = today + timedelta(days=1)
        dates = [tmr]
        p_title = f'Завтра ({DAYS_RUS[tmr.weekday()]}, {tmr.strftime("%d.%m")})'
    else:
        mon = today - timedelta(days=today.weekday())
        dates = [mon + timedelta(days=i) for i in range(6)]
        w_num, _ = get_current_week_and_day(today)
        p_title = f'Вся {w_num}-я неделя'

    out = [f'👨‍🏫 ПОИСК: «{query}» ({p_title})\n']
    found_any = False

    with db_connection() as conn:
        cursor = conn.cursor()
        for dt_obj in dates:
            w_num, d_name = get_current_week_and_day(dt_obj)
            d_str = dt_obj.strftime('%d.%m.%Y')
            cursor.execute(
                'SELECT s.time_slot, s.subject_name, s.lesson_type, s.teacher, s.room, s.group_name, s.faculty '
                'FROM schedules s JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                'WHERE s.day_of_week = ? AND sw.week_number = ? ORDER BY s.time_slot ASC',
                (d_name, w_num),
            )
            rows = cursor.fetchall()
            cursor.execute('SELECT COALESCE(faculty, ""), group_name, time_slot FROM cancelled_lessons WHERE target_date = ?', (d_str,))
            canc_set = {(r[0], r[1], r[2]) for r in cursor.fetchall()}
            cursor.execute('SELECT COALESCE(faculty, ""), group_name, time_slot, subject_info FROM replacements WHERE target_date = ?', (d_str,))
            repl_rows = cursor.fetchall()
            repl_set = {(r[0], r[1], r[2]) for r in repl_rows}

            day_hits = []
            for t_slot, subj, l_type, teacher, room, grp, fac in rows:
                eff_fac = fac or infer_faculty_by_group(grp)
                if (eff_fac, grp, t_slot) in canc_set or ('', grp, t_slot) in canc_set:
                    continue
                if (eff_fac, grp, t_slot) in repl_set or ('', grp, t_slot) in repl_set:
                    continue
                if w_num in core.PRACTICE_WEEKS.get(grp, set()):
                    continue
                t_norm = extract_teacher_surname_initials(teacher) or str(teacher or '')
                if q_low in t_norm.lower() or q_low in str(teacher or '').lower():
                    day_hits.append(f'{format_pair_prefix(t_slot)} — {t_norm}\n📍 {room} • 📘 {subj} ({l_type}) • 🎓 {grp} [{get_faculty_short(eff_fac)}]')

            for r_fac, grp, t_slot, s_info in repl_rows:
                if q_low in s_info.lower():
                    day_hits.append(f'🔄 [ЗАМЕНА] {format_pair_prefix(t_slot)} — {s_info} (🎓 {grp})')

            if day_hits:
                found_any = True
                if len(dates) > 1:
                    out.append(f'🔹 {d_name.upper()} ({dt_obj.strftime("%d.%m")})')
                out.extend(day_hits[:12])
                out.append('')

    if not found_any:
        return f'🔎 По запросу «{query}» ({p_title}) пар не найдено.'
    return '\n'.join(out).strip()


def get_free_rooms_plain(day_offset, pair_num, user_fac, scope='fac'):
    target_dt = now_local() + timedelta(days=day_offset)
    w_num, d_name = get_current_week_and_day(target_dt)
    d_str = target_dt.strftime('%d.%m.%Y')
    time_slot = NUM_TO_TIME_SLOT.get(pair_num, '')
    use_fac = (scope == 'fac' and bool(user_fac))

    with db_connection() as conn:
        cursor = conn.cursor()
        if use_fac:
            cursor.execute('SELECT DISTINCT room FROM schedules WHERE faculty = ?', (user_fac,))
        else:
            cursor.execute('SELECT DISTINCT room FROM schedules')
        all_rooms = set()
        for (r_str,) in cursor.fetchall():
            for single in extract_rooms_from_text(r_str):
                if 'конф-зал' not in single.lower() and 'не указана' not in single.lower():
                    all_rooms.add(single)

        cursor.execute(
            'SELECT s.room, s.group_name, s.faculty FROM schedules s '
            'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
            'WHERE s.day_of_week = ? AND sw.week_number = ? AND s.time_slot = ?',
            (d_name, w_num, time_slot),
        )
        busy_rows = cursor.fetchall()
        cursor.execute('SELECT COALESCE(faculty, ""), group_name FROM cancelled_lessons WHERE target_date = ? AND time_slot = ?', (d_str, time_slot))
        canc_keys = {(r[0], r[1]) for r in cursor.fetchall()}
        cursor.execute('SELECT COALESCE(faculty, ""), group_name, subject_info FROM replacements WHERE target_date = ? AND time_slot = ?', (d_str, time_slot))
        repl_rows = cursor.fetchall()
        repl_keys = {(r[0], r[1]) for r in repl_rows}

    busy_rooms = set()
    for r_str, grp, r_fac in busy_rows:
        eff_fac = r_fac or infer_faculty_by_group(grp)
        if (eff_fac, grp) in canc_keys or ('', grp) in canc_keys:
            continue
        if (eff_fac, grp) in repl_keys or ('', grp) in repl_keys:
            continue
        if w_num in core.PRACTICE_WEEKS.get(grp, set()):
            continue
        for single in extract_rooms_from_text(r_str):
            busy_rooms.add(single)

    free_rooms = sorted(all_rooms - busy_rooms)
    scope_lbl = f'🏛 {get_faculty_short(user_fac)}' if use_fac else '🌐 Все факультеты'
    header = f'🚪 СВОБОДНЫЕ АУДИТОРИИ\n📅 {d_name}, {d_str} • {format_pair_prefix(time_slot)} • {scope_lbl}\n\n'
    if d_name == 'Воскресенье':
        return header + '🎉 Воскресенье — выходной.'
    if free_rooms:
        return header + '✅ Свободны:\n' + ', '.join(free_rooms)
    return header + '⚠️ Свободных аудиторий на эту пару не найдено.'


# --- КЛАВИАТУРЫ VK ---
def build_main_kb(is_starosta=0):
    kb = VkKeyboard(one_time=False)
    kb.add_button('📅 Сегодня', color=VkKeyboardColor.PRIMARY)
    kb.add_button('⏭ Завтра', color=VkKeyboardColor.PRIMARY)
    kb.add_button('🗓 Неделя', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button('📚 Домашка', color=VkKeyboardColor.POSITIVE)
    kb.add_button('🎓 Сессия', color=VkKeyboardColor.SECONDARY)
    kb.add_button('🔍 Поиск', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button('⚙️ Настройки', color=VkKeyboardColor.SECONDARY)
    if is_starosta:
        kb.add_button('🛠 Староста', color=VkKeyboardColor.PRIMARY)
    return kb.get_keyboard()


def build_day_nav_kb(offset):
    kb = VkKeyboard(inline=True)
    kb.add_button('⬅️ Пред. день', color=VkKeyboardColor.SECONDARY, payload={'act': 'day', 'off': offset - 1})
    kb.add_button('📍 Сегодня', color=VkKeyboardColor.PRIMARY, payload={'act': 'day', 'off': 0})
    kb.add_button('След. день ➡️', color=VkKeyboardColor.SECONDARY, payload={'act': 'day', 'off': offset + 1})
    return kb.get_keyboard()


def build_week_nav_kb(w_offset):
    kb = VkKeyboard(inline=True)
    kb.add_button('⬅️ Пред. нед.', color=VkKeyboardColor.SECONDARY, payload={'act': 'week', 'off': w_offset - 1})
    kb.add_button('📍 Текущая', color=VkKeyboardColor.PRIMARY, payload={'act': 'week', 'off': 0})
    kb.add_button('След. нед. ➡️', color=VkKeyboardColor.SECONDARY, payload={'act': 'week', 'off': w_offset + 1})
    return kb.get_keyboard()


def build_faculties_kb():
    struct = get_faculty_structure()
    kb = VkKeyboard(one_time=True)
    added = 0
    for f_code in FACULTY_ORDER:
        if any(bool(g) for g in struct.get(f_code, {}).values()):
            if added > 0 and added % 2 == 0:
                kb.add_line()
            kb.add_button(f'🏛 {get_faculty_short(f_code)}', color=VkKeyboardColor.PRIMARY)
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


def build_groups_kb(fac_code, course, page=0):
    struct = get_faculty_structure()
    groups = struct.get(fac_code, {}).get(course, [])
    per_page = 6
    total_pages = max(1, (len(groups) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = groups[page * per_page : (page + 1) * per_page]

    kb = VkKeyboard(one_time=True)
    for idx, g_nm in enumerate(chunk):
        if idx > 0:
            kb.add_line()
        label = g_nm if len(g_nm) <= 38 else g_nm[:37] + '…'
        kb.add_button(label, color=VkKeyboardColor.PRIMARY)
    if total_pages > 1:
        kb.add_line()
        if page > 0:
            kb.add_button('⬅️ Пред. группы', color=VkKeyboardColor.SECONDARY, payload={'act': 'grp_page', 'p': page - 1})
        if page < total_pages - 1:
            kb.add_button('➡️ Ещё группы', color=VkKeyboardColor.SECONDARY, payload={'act': 'grp_page', 'p': page + 1})
    kb.add_line()
    kb.add_button('🔙 Курсы', color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def send_settings_menu(vk, peer_id, user):
    m_lbl = '☀️ Утро (07:00): ВКЛ' if user['sub_morning'] else '☀️ Утро (07:00): ВЫКЛ'
    e_lbl = '🌙 Вечер (18:00): ВКЛ' if user['sub_evening'] else '🌙 Вечер (18:00): ВЫКЛ'
    n_lbl = '🔔 Пуши (пары/ДЗ): ВКЛ' if user['notify_updates'] else '🔕 Пуши (пары/ДЗ): ВЫКЛ'

    kb = VkKeyboard(inline=True)
    kb.add_button(m_lbl, color=VkKeyboardColor.PRIMARY if user['sub_morning'] else VkKeyboardColor.SECONDARY, payload={'act': 'set_m'})
    kb.add_line()
    kb.add_button(e_lbl, color=VkKeyboardColor.PRIMARY if user['sub_evening'] else VkKeyboardColor.SECONDARY, payload={'act': 'set_e'})
    kb.add_line()
    kb.add_button(n_lbl, color=VkKeyboardColor.POSITIVE if user['notify_updates'] else VkKeyboardColor.SECONDARY, payload={'act': 'set_n'})
    kb.add_line()
    kb.add_button('🏛 Сменить группу', color=VkKeyboardColor.SECONDARY, payload={'act': 'set_grp'})
    kb.add_button('📩 Ошибка', color=VkKeyboardColor.NEGATIVE, payload={'act': 'set_bug'})

    role_txt = '🌟 Староста' if user['is_starosta'] else '🎓 Студент'
    txt = (
        f'⚙️ НАСТРОЙКИ И ПРОФИЛЬ (VK)\n'
        f'🏛 {get_faculty_short(user["faculty"])} • {user["course"]} курс • {user["group_name"]}\n'
        f'👤 Статус: {role_txt}'
    )
    send_vk_msg(vk, peer_id, txt, keyboard=kb.get_keyboard())


def send_starosta_menu(vk, peer_id, user):
    if not user['is_starosta']:
        send_vk_msg(vk, peer_id, '⛔ Раздел доступен только старостам.', keyboard=build_main_kb(0))
        return
    kb = VkKeyboard(inline=True)
    kb.add_button('➕ Замена', color=VkKeyboardColor.PRIMARY, payload={'act': 'st_repl'})
    kb.add_button('🗑 Отмена пары', color=VkKeyboardColor.NEGATIVE, payload={'act': 'st_del'})
    kb.add_line()
    kb.add_button('📋 Активные правки', color=VkKeyboardColor.SECONDARY, payload={'act': 'st_changes'})
    kb.add_button('📝 Добавить ДЗ', color=VkKeyboardColor.POSITIVE, payload={'act': 'st_hw'})
    send_vk_msg(vk, peer_id, f'🛠 ПАНЕЛЬ СТАРОСТЫ (VK)\n🛡 Группа: {user["managed_group"] or user["group_name"]}', keyboard=kb.get_keyboard())


def send_vk_msg(vk, peer_id, text, keyboard=None, attachment=None):
    chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or ['']
    for idx, ch in enumerate(chunks):
        params = {
            'peer_id': peer_id,
            'message': ch,
            'random_id': get_random_id(),
        }
        if keyboard and idx == len(chunks) - 1:
            params['keyboard'] = keyboard
        if attachment and idx == len(chunks) - 1:
            params['attachment'] = attachment
        vk.messages.send(**params)



def _extract_vk_msg_attachments(attachments):
    vk_att_strs = []
    tg_photo_urls = []
    tg_doc_url = ''
    tg_doc_name = ''
    for att in (attachments or []):
        a_type = att.get('type')
        if a_type == 'photo':
            p = att.get('photo') or {}
            oid, pid = p.get('owner_id'), p.get('id')
            akey = p.get('access_key')
            if oid and pid:
                vk_att_strs.append(f'photo{oid}_{pid}_{akey}' if akey else f'photo{oid}_{pid}')
            sizes = p.get('sizes') or []
            if sizes:
                best = max(sizes, key=lambda s: int(s.get('width', 0)) * int(s.get('height', 0)))
                if best.get('url'):
                    tg_photo_urls.append(best['url'])
        elif a_type == 'doc' and not tg_doc_url:
            d = att.get('doc') or {}
            oid, did = d.get('owner_id'), d.get('id')
            akey = d.get('access_key')
            if oid and did:
                vk_att_strs.append(f'doc{oid}_{did}_{akey}' if akey else f'doc{oid}_{did}')
            if d.get('url'):
                tg_doc_url = d['url']
                tg_doc_name = d.get('title') or 'Документ'
    return ','.join(vk_att_strs), tg_photo_urls[:5], tg_doc_url, tg_doc_name


def send_hw_subject_picker(vk, peer_id, fac_code, course, group_name, page=0):
    subjects = get_all_group_subjects(course, group_name, faculty_code=fac_code)
    if not subjects:
        kb = VkKeyboard(inline=True)
        kb.add_button('📅 Выбрать дату', color=VkKeyboardColor.PRIMARY, payload={'act': 'st_hw_bydate'})
        send_vk_msg(vk, peer_id, '📝 Предметы группы не найдены в расписании. Выбери дату:', keyboard=kb.get_keyboard())
        return
    per_page = 4
    total_pages = max(1, (len(subjects) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = subjects[page * per_page : (page + 1) * per_page]

    kb = VkKeyboard(inline=True)
    for idx, subj in enumerate(chunk):
        if idx > 0:
            kb.add_line()
        global_idx = page * per_page + idx
        kb.add_button(f'📘 {short_text(subj, 32)}', color=VkKeyboardColor.PRIMARY, payload={'act': 'st_hw_subj', 'i': global_idx})

    if total_pages > 1:
        kb.add_line()
        if page > 0:
            kb.add_button('⬅️ Назад', color=VkKeyboardColor.SECONDARY, payload={'act': 'st_hw_spage', 'p': page - 1})
        if page < total_pages - 1:
            kb.add_button('➡️ Ещё', color=VkKeyboardColor.SECONDARY, payload={'act': 'st_hw_spage', 'p': page + 1})

    kb.add_line()
    kb.add_button('📅 Выбрать дату', color=VkKeyboardColor.SECONDARY, payload={'act': 'st_hw_bydate'})
    send_vk_msg(
        vk,
        peer_id,
        f'📝 ДОБАВЛЕНИЕ ДЗ ({group_name})\nВыбери предмет (бот сам найдёт ближайшую дату пары) или выбери дату вручную:',
        keyboard=kb.get_keyboard(),
    )



def get_teachers_by_scope_vk(scope='all', course=None, group_name=None, fac_code=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        if scope == 'group' and group_name and course:
            if fac_code:
                cursor.execute(
                    'SELECT DISTINCT teacher FROM schedules WHERE faculty = ? AND course_number = ? AND group_name = ?',
                    (fac_code, int(course), group_name),
                )
            else:
                cursor.execute(
                    'SELECT DISTINCT teacher FROM schedules WHERE course_number = ? AND group_name = ?',
                    (int(course), group_name),
                )
        elif scope and scope != 'all':
            cursor.execute('SELECT DISTINCT teacher FROM schedules WHERE faculty = ?', (scope,))
        else:
            cursor.execute('SELECT DISTINCT teacher FROM schedules')
        rows = cursor.fetchall()
    seen = set()
    res = []
    for (t_raw,) in rows:
        t_clean = (extract_teacher_surname_initials(t_raw) or str(t_raw or '')).strip()
        if not t_clean or 'не указан' in t_clean.lower():
            continue
        for single in t_clean.split(', '):
            s_c = single.strip()
            if s_c and s_c.lower() not in seen:
                seen.add(s_c.lower())
                res.append(s_c)
    res.sort()
    return res


def send_vk_teacher_picker(vk, peer_id, user_id, period, scope, page=0, user=None):
    u = user or get_vk_user(user_id) or {}
    teachers = get_teachers_by_scope_vk(
        scope,
        course=u.get('course'),
        group_name=u.get('group_name'),
        fac_code=u.get('faculty'),
    )
    per_page = 6
    total_pages = max(1, (len(teachers) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = teachers[page * per_page : (page + 1) * per_page]

    upsert_vk_user(user_id, state='awaiting_teacher', payload={'period': period})
    kb = VkKeyboard(inline=True)
    for idx, t_disp in enumerate(chunk):
        if idx > 0 and idx % 2 == 0:
            kb.add_line()
        kb.add_button(short_text(t_disp, 20), color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_tq', 'p': period, 'q': t_disp})

    if total_pages > 1:
        if chunk:
            kb.add_line()
        if page > 0:
            kb.add_button('⬅️ Назад', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_tl', 'p': period, 's': scope, 'pg': page - 1})
        if page < total_pages - 1:
            kb.add_button('➡️ Ещё', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_tl', 'p': period, 's': scope, 'pg': page + 1})

    if chunk or total_pages > 1:
        kb.add_line()
    if scope != 'group' and u.get('group_name'):
        kb.add_button('🎓 Моя группа', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_tl', 'p': period, 's': 'group', 'pg': 0})
    if scope != 'all':
        kb.add_button('🌐 Все', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_tl', 'p': period, 's': 'all', 'pg': 0})
    if scope in ('group', 'all'):
        kb.add_button('🏛 Факультет', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_tf', 'p': period})

    if scope == 'group':
        scope_lbl = '🎓 Моя группа'
    elif scope == 'all':
        scope_lbl = '🌐 Все факультеты'
    else:
        scope_lbl = f'🏛 {get_faculty_short(scope)}'
    send_vk_msg(
        vk,
        peer_id,
        f'👨‍🏫 ПРЕПОДАВАТЕЛИ ({scope_lbl}) — стр. {page + 1}/{total_pages}\nВыбери преподавателя ниже или напиши любую фамилию сообщением (или «Отмена»):',
        keyboard=kb.get_keyboard(),
    )


def send_vk_session_view(vk, peer_id, user):
    fac_code = user['faculty'] or infer_faculty_by_group(user['group_name'])
    group_name = user['group_name']
    today = now_local().date()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, exam_date, exam_time, subject_name, exam_type, teacher, room '
            'FROM session_exams WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (fac_code, group_name),
        )
        rows = cursor.fetchall()

    upcoming = []
    for r in rows:
        try:
            d_obj = datetime.strptime(r[1], '%d.%m.%Y').date()
        except ValueError:
            continue
        if d_obj >= today:
            upcoming.append((d_obj, r))
    upcoming.sort(key=lambda x: (x[0], x[1][2] or '', x[1][0]))

    can_edit = user['is_starosta'] == 1 and (user['managed_group'] or group_name) == group_name
    lines = [f'🎓 РАСПИСАНИЕ СЕССИИ ({group_name})', '']
    kb = VkKeyboard(inline=True)
    btn_rows = 0

    if not upcoming:
        lines.append('✨ Экзамены и зачёты пока не добавлены.')
    else:
        for d_obj, (ex_id, ex_date, ex_time, subj, ex_type, teacher, room) in upcoming:
            delta = (d_obj - today).days
            left_s = '🔴 СЕГОДНЯ' if delta == 0 else ('🟠 ЗАВТРА' if delta == 1 else f'через {delta} дн.')
            t_part = f' • {ex_time}' if ex_time else ''
            meta = ' · '.join(x for x in [room, teacher] if x)
            lines.append(f'🎯 {ex_date}{t_part} ({left_s})')
            lines.append(f'   📘 {subj} ({ex_type or "Экзамен"})' + (f' — {meta}' if meta else ''))
            if can_edit and btn_rows < 4:
                if btn_rows > 0:
                    kb.add_line()
                kb.add_button(f'🗑 {ex_date[:5]} {short_text(subj, 20)}'[:40], color=VkKeyboardColor.NEGATIVE, payload={'act': 'vk_sess_del', 'id': ex_id})
                btn_rows += 1

    if can_edit:
        if btn_rows > 0:
            kb.add_line()
        kb.add_button('➕ Добавить экзамен', color=VkKeyboardColor.POSITIVE, payload={'act': 'vk_sess_add'})
        send_vk_msg(vk, peer_id, '\n'.join(lines), keyboard=kb.get_keyboard())
    else:
        send_vk_msg(vk, peer_id, '\n'.join(lines))


def send_vk_active_changes(vk, peer_id, user):
    m_grp = user['managed_group'] or user['group_name']
    _, m_fac = find_course_and_faculty_by_group(m_grp)
    m_fac = m_fac or infer_faculty_by_group(m_grp) or user['faculty']
    today = now_local().date()
    valid_dates = {(today + timedelta(days=i)).strftime('%d.%m.%Y') for i in range(8)}
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, target_date, time_slot, subject_info FROM replacements '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (m_fac, m_grp),
        )
        repls = [r for r in cursor.fetchall() if r[1] in valid_dates]
        cursor.execute(
            'SELECT id, target_date, time_slot FROM cancelled_lessons '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (m_fac, m_grp),
        )
        cancs = [c for c in cursor.fetchall() if c[1] in valid_dates]

    if not repls and not cancs:
        send_vk_msg(vk, peer_id, f'📋 Активные правки ({m_grp})\n\n✨ На ближайшую неделю замен и отмен нет.')
        return

    lines = [f'📋 АКТИВНЫЕ ПРАВКИ ({m_grp}) — нажми, чтобы откатить:']
    kb = VkKeyboard(inline=True)
    btn_rows = 0
    for r_id, r_date, r_time, r_subj in repls[:3]:
        lines.append(f'🔄 {r_date[:5]} ({r_time}) — {r_subj}')
        if btn_rows > 0:
            kb.add_line()
        kb.add_button(f'❌ Замена {r_date[:5]} {short_text(r_subj, 18)}'[:40], color=VkKeyboardColor.NEGATIVE, payload={'act': 'vk_rev_r', 'id': r_id})
        btn_rows += 1
    for c_id, c_date, c_time in cancs[:3]:
        lines.append(f'🚫 {c_date[:5]} ({c_time}) — отмена')
        if btn_rows > 0:
            kb.add_line()
        kb.add_button(f'↩️ Вернуть {c_date[:5]} ({c_time[:5]})'[:40], color=VkKeyboardColor.PRIMARY, payload={'act': 'vk_rev_c', 'id': c_id})
        btn_rows += 1
    send_vk_msg(vk, peer_id, '\n'.join(lines), keyboard=kb.get_keyboard())


# --- ОБРАБОТЧИК СООБЩЕНИЙ И PAYLOAD ---
def handle_message(vk, user_id, peer_id, text, raw_payload=None, attachments=None):
    raw = (text or '').strip()
    low = raw.lower()
    pl = {}
    if raw_payload:
        try:
            pl = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except Exception:
            pl = {}

    user = get_vk_user(user_id)
    if user is None:
        full_name = _fetch_vk_full_name(vk, user_id)
        user = upsert_vk_user(user_id, full_name=full_name)
    else:
        if not user['full_name']:
            user = upsert_vk_user(user_id, full_name=_fetch_vk_full_name(vk, user_id))
        else:
            upsert_vk_user(user_id)  # обновляем last_active

    # Команды выдачи прав старосты прямо из VK (если задан VK_ADMIN_ID)
    if VK_ADMIN_ID and int(user_id) == VK_ADMIN_ID and (low.startswith('/starosta ') or low.startswith('/unstarosta ')):
        parts = raw.split()
        if len(parts) >= 2 and parts[1].isdigit():
            t_uid = int(parts[1])
            grant = low.startswith('/starosta ')
            t_u = get_vk_user(t_uid)
            if not t_u:
                send_vk_msg(vk, peer_id, f'⚠️ Пользователь VK {t_uid} ещё не запускал бота.')
            else:
                upsert_vk_user(t_uid, is_starosta=(1 if grant else 0), managed_group=(t_u['group_name'] if grant else ''))
                send_vk_msg(vk, peer_id, f'✅ Права старосты {"выданы" if grant else "сняты"} для VK ID {t_uid}.')
        return

    # Сброс/старт
    if low in ('начать', 'старт', '/start', '🔙 факультеты') or pl.get('act') == 'set_grp':
        upsert_vk_user(user_id, state='pick_fac', payload={})
        send_vk_msg(
            vk,
            peer_id,
            '👋 Привет! Я бот расписания СИ УУНиТ (общая база с Telegram).\n\nВыбери свой факультет:',
            keyboard=build_faculties_kb(),
        )
        return

    if low == 'меню':
        upsert_vk_user(user_id, state='', payload={})
        send_vk_msg(vk, peer_id, '👇 Главное меню:', keyboard=build_main_kb(user['is_starosta']))
        return

    # Онбординг (выбор факультета -> курса -> группы с пагинацией)
    if not user['group_name'] or user['state'] in ('pick_fac', 'pick_course', 'pick_group'):
        state = user['state']
        st_pl = user['payload']
        struct = get_faculty_structure()

        if state == 'pick_fac':
            chosen_fac = None
            for f_code in FACULTY_ORDER:
                short_nm = get_faculty_short(f_code)
                if raw in (f'🏛 {short_nm}', short_nm, f_code):
                    chosen_fac = f_code
                    break
            if chosen_fac:
                upsert_vk_user(user_id, faculty=chosen_fac, state='pick_course', payload={'fac': chosen_fac})
                send_vk_msg(vk, peer_id, f'🏛 Факультет: {get_faculty_short(chosen_fac)}\nВыбери свой курс:', keyboard=build_courses_kb(chosen_fac))
                return

        elif state == 'pick_course':
            fac_code = st_pl.get('fac') or user['faculty']
            m = re.search(r'(\d+)', raw)
            if m and int(m.group(1)) in struct.get(fac_code, {}):
                crs = int(m.group(1))
                upsert_vk_user(user_id, course=crs, state='pick_group', payload={'fac': fac_code, 'course': crs, 'page': 0})
                send_vk_msg(vk, peer_id, f'🎓 Выбран {crs} курс. Выбери свою группу:', keyboard=build_groups_kb(fac_code, crs, page=0))
                return

        elif state == 'pick_group':
            fac_code = st_pl.get('fac') or user['faculty']
            crs = int(st_pl.get('course') or user['course'] or 0)
            if raw == '🔙 Курсы':
                upsert_vk_user(user_id, state='pick_course', payload={'fac': fac_code})
                send_vk_msg(vk, peer_id, 'Выбери свой курс:', keyboard=build_courses_kb(fac_code))
                return
            if pl.get('act') == 'grp_page':
                p = int(pl.get('p', 0))
                upsert_vk_user(user_id, state='pick_group', payload={'fac': fac_code, 'course': crs, 'page': p})
                send_vk_msg(vk, peer_id, f'🎓 {crs} курс (стр. {p + 1}):', keyboard=build_groups_kb(fac_code, crs, page=p))
                return
            groups = struct.get(fac_code, {}).get(crs, [])
            matched_grp = None
            for g in groups:
                short_g = g if len(g) <= 38 else g[:37] + '…'
                if raw == g or raw == short_g or (len(raw) > 5 and raw.rstrip('…') in g):
                    matched_grp = g
                    break
            if matched_grp:
                m_grp = user['managed_group'] or (matched_grp if user['is_starosta'] else '')
                user = upsert_vk_user(user_id, faculty=fac_code, course=crs, group_name=matched_grp, managed_group=m_grp, state='', payload={})
                send_vk_msg(
                    vk,
                    peer_id,
                    f'✅ Профиль сохранён!\n🏛 {get_faculty_short(fac_code)} • {crs} курс • {matched_grp}\n\nВыбери действие в меню:',
                    keyboard=build_main_kb(user['is_starosta']),
                )
                return

        upsert_vk_user(user_id, state='pick_fac', payload={})
        send_vk_msg(vk, peer_id, '👇 Сначала выбери свой факультет:', keyboard=build_faculties_kb())
        return

    fac_code, course, group_name = user['faculty'], user['course'], user['group_name']
    act = pl.get('act', '')

    # --- ОБРАБОТКА INLINE-КНОПОК (PAYLOAD) ---
    if act == 'day':
        off = int(pl.get('off', 0))
        send_vk_msg(vk, peer_id, get_day_schedule_plain(now_local() + timedelta(days=off), fac_code, course, group_name), keyboard=build_day_nav_kb(off))
        return

    if act == 'week':
        off = int(pl.get('off', 0))
        send_vk_msg(vk, peer_id, get_week_schedule_plain(now_local() + timedelta(days=off * 7), fac_code, course, group_name), keyboard=build_week_nav_kb(off))
        return

    if act == 'hw_page':
        send_hw_index(vk, peer_id, user_id, user, page=int(pl.get('p', 0)))
        return

    if act == 'hw_open':
        send_hw_card(vk, peer_id, user_id, int(pl['id']))
        return

    if act == 'hw_edit':
        hw_id = int(pl['id'])
        neg_uid = -abs(int(user_id))
        with db_connection() as conn:
            hrow = conn.execute(
                'SELECT target_date, subject_name, hw_text, group_name, added_by, faculty FROM homeworks WHERE id = ?',
                (hw_id,),
            ).fetchone()
        if not hrow:
            send_vk_msg(vk, peer_id, '⚠️ Задание не найдено.')
            return
        can_edit = (hrow[4] is not None and int(hrow[4]) == neg_uid) or (
            user['is_starosta'] == 1 and (user['managed_group'] or group_name) == hrow[3]
        )
        if not can_edit:
            send_vk_msg(vk, peer_id, '⛔ Редактировать ДЗ может только его автор или староста группы.')
            return
        upsert_vk_user(user_id, state='st_await_hw_edit', payload={'hw_id': hw_id, 'grp': hrow[3], 'fac': hrow[5] or fac_code})
        send_vk_msg(vk, peer_id, f'✏️ Редактирование ДЗ ({hrow[1]}, {hrow[0]}).\nТекущий текст: {hrow[2] or "(пусто)"}\n\nОтправь новый текст задания (или «Отмена»):')
        return

    if act == 'vk_sess_add' and user['is_starosta']:
        m_grp = user['managed_group'] or group_name
        _, m_fac = find_course_and_faculty_by_group(m_grp)
        upsert_vk_user(user_id, state='st_await_sess_exam', payload={'grp': m_grp, 'fac': m_fac or fac_code})
        send_vk_msg(
            vk,
            peer_id,
            '➕ ДОБАВЛЕНИЕ ЭКЗАМЕНА / ЗАЧЁТА\nОтправь одной строкой через | :\n'
            'ДД.ММ.ГГГГ | Предмет | Тип | Время | Ауд., Преподаватель\n\n'
            'Пример: 25.12.2026 | Экология | Экзамен | 09:00 | ауд. 206, Иванов И.И.\n(или «Отмена»)',
        )
        return

    if act == 'vk_sess_del' and user['is_starosta']:
        ex_id = int(pl['id'])
        m_grp = user['managed_group'] or group_name
        with db_connection() as conn:
            conn.execute('DELETE FROM session_exams WHERE id = ? AND group_name = ?', (ex_id, m_grp))
        send_vk_msg(vk, peer_id, '🗑 Запись сессии удалена.')
        send_vk_session_view(vk, peer_id, user)
        return

    if act == 'hw_del':
        hw_id = int(pl['id'])
        neg_uid = -abs(int(user_id))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT target_date, subject_name, hw_text, queue_id, annot_queue_id, group_name, added_by, faculty FROM homeworks WHERE id = ?',
                (hw_id,),
            )
            hrow = cursor.fetchone()
            if not hrow:
                send_vk_msg(vk, peer_id, '⚠️ Задание уже удалено.')
                return
            hw_date, hw_subj, hw_text, q1, q2, h_grp, h_author, h_fac = hrow
            can_del = (h_author is not None and int(h_author) == neg_uid) or (
                user['is_starosta'] == 1 and (user['managed_group'] or group_name) == h_grp
            )
            if not can_del:
                send_vk_msg(vk, peer_id, '⛔ Удалить ДЗ может только его автор или староста группы.')
                return
            eff_f = h_fac or infer_faculty_by_group(h_grp)
            for qid_d in (q1, q2):
                if qid_d:
                    cursor.execute('DELETE FROM queue_slots WHERE queue_id = ?', (qid_d,))
                    cursor.execute('DELETE FROM queues WHERE id = ?', (qid_d,))
            cursor.execute('DELETE FROM hw_done WHERE hw_id = ?', (hw_id,))
            cursor.execute('DELETE FROM homeworks WHERE id = ?', (hw_id,))
            cursor.execute('SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1', (eff_f, h_grp))
            tg_users = [r[0] for r in cursor.fetchall()]

        tg_del_msg = (
            f'🗑 <b>ДОМАШНЕЕ ЗАДАНИЕ УДАЛЕНО (из VK)</b>\n'
            f'<blockquote>🎓 <b>Группа:</b> {esc(h_grp)}\n'
            f'📅 <b>Дедлайн был:</b> {hw_date}</blockquote>\n\n'
            f'📘 <b>{esc(hw_subj)}</b>'
        )
        for u_id in tg_users:
            enqueue_message(u_id, tg_del_msg, parse_mode='HTML')
        for g_cid in get_bound_group_chats(eff_f, h_grp):
            enqueue_message(g_cid, tg_del_msg, parse_mode='HTML')
        notify_vk_group_users(eff_f, h_grp, f'🗑 ДОМАШНЕЕ ЗАДАНИЕ УДАЛЕНО\n🎓 Группа: {h_grp} • Дедлайн был: {hw_date}\n📘 {hw_subj}', exclude_vk_id=user_id)
        send_vk_msg(vk, peer_id, f'🗑 ДЗ по предмету «{hw_subj}» удалено!', keyboard=build_main_kb(user['is_starosta']))
        return

    if act == 'st_changes' and user['is_starosta']:
        send_vk_active_changes(vk, peer_id, user)
        return

    if act in ('vk_rev_r', 'vk_rev_c') and user['is_starosta']:
        rec_id = int(pl['id'])
        m_grp = user['managed_group'] or group_name
        _, m_fac = find_course_and_faculty_by_group(m_grp)
        m_fac = m_fac or infer_faculty_by_group(m_grp) or fac_code
        tg_msg = None
        vk_msg = None
        with db_connection() as conn:
            cursor = conn.cursor()
            if act == 'vk_rev_r':
                cursor.execute('SELECT target_date, time_slot, subject_info FROM replacements WHERE id = ? AND group_name = ?', (rec_id, m_grp))
                row = cursor.fetchone()
                cursor.execute('DELETE FROM replacements WHERE id = ? AND group_name = ?', (rec_id, m_grp))
                if row:
                    tg_msg = f'↩️ <b>ЗАМЕНА УДАЛЕНА (из VK)</b>\n<blockquote>🎓 {esc(m_grp)} • {row[0]}</blockquote>\n{tg_format_pair_prefix(row[1])}\n<s>{esc(row[2])}</s>'
                    vk_msg = f'↩️ ЗАМЕНА УДАЛЕНА\n🎓 {m_grp} • {row[0]}\n{row[1]} — {row[2]}'
            else:
                cursor.execute('SELECT target_date, time_slot FROM cancelled_lessons WHERE id = ? AND group_name = ?', (rec_id, m_grp))
                row = cursor.fetchone()
                cursor.execute('DELETE FROM cancelled_lessons WHERE id = ? AND group_name = ?', (rec_id, m_grp))
                if row:
                    tg_msg = f'↩️ <b>ОТМЕНА ПАРЫ СНЯТА (из VK)</b>\n<blockquote>🎓 {esc(m_grp)} • {row[0]}</blockquote>\n{tg_format_pair_prefix(row[1])} — снова в расписании.'
                    vk_msg = f'↩️ ОТМЕНА ПАРЫ СНЯТА\n🎓 {m_grp} • {row[0]}\n{row[1]} — снова в расписании.'
            cursor.execute('SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1', (m_fac, m_grp))
            tg_users = [r[0] for r in cursor.fetchall()]

        if tg_msg:
            for u_id in tg_users:
                enqueue_message(u_id, tg_msg, parse_mode='HTML')
            for g_cid in get_bound_group_chats(m_fac, m_grp):
                enqueue_message(g_cid, tg_msg, parse_mode='HTML')
        if vk_msg:
            notify_vk_group_users(m_fac, m_grp, vk_msg, exclude_vk_id=user_id)
        send_vk_msg(vk, peer_id, '✅ Правка отменена! Уведомления отправлены в TG и VK.')
        send_vk_active_changes(vk, peer_id, user)
        return

    if act == 'hw_done':
        hw_id = int(pl['id'])
        neg_uid = -abs(int(user_id))
        with db_connection() as conn:
            cursor = conn.cursor()
            if int(pl.get('v', 1)) == 1:
                cursor.execute('INSERT OR REPLACE INTO hw_done (user_id, hw_id, done_at) VALUES (?, ?, ?)', (neg_uid, hw_id, now_local().strftime('%d.%m.%Y %H:%M')))
            else:
                cursor.execute('DELETE FROM hw_done WHERE user_id = ? AND hw_id = ?', (neg_uid, hw_id))
        send_hw_card(vk, peer_id, user_id, hw_id)
        return

    if act == 'hwq_pick':
        upsert_vk_user(user_id, state='awaiting_slot_pick', payload={'qid': int(pl['qid']), 'hwid': int(pl['hwid'])})
        send_vk_msg(vk, peer_id, '✋ Введи номер вопроса, который хочешь занять (числом), или напиши «Отмена»:')
        return

    if act == 'hwq_free':
        qid = int(pl['qid'])
        hwid = int(pl['hwid'])
        neg_uid = -abs(int(user_id))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM queue_slots WHERE queue_id = ? AND user_id = ?', (qid, neg_uid))
            removed = cursor.rowcount
        send_vk_msg(vk, peer_id, '✅ Твои вопросы освобождены.' if removed else 'ℹ️ За тобой не закреплено вопросов в этом ДЗ.')
        send_hw_card(vk, peer_id, user_id, hwid)
        return

    if act in ('set_m', 'set_e', 'set_n'):
        if act == 'set_m':
            user = upsert_vk_user(user_id, sub_morning=(0 if user['sub_morning'] else 1))
        elif act == 'set_e':
            user = upsert_vk_user(user_id, sub_evening=(0 if user['sub_evening'] else 1))
        else:
            user = upsert_vk_user(user_id, notify_updates=(0 if user['notify_updates'] else 1))
        send_settings_menu(vk, peer_id, user)
        return

    if act == 'set_bug':
        upsert_vk_user(user_id, state='awaiting_bug', payload={})
        send_vk_msg(vk, peer_id, '📩 Опиши ошибку или опечатку в расписании одним сообщением (или напиши «Отмена»):')
        return

    if act == 'srch_t':
        kb = VkKeyboard(inline=True)
        kb.add_button('📍 Сегодня', color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_tp', 'p': 'today'})
        kb.add_button('⏭ Завтра', color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_tp', 'p': 'tomorrow'})
        kb.add_button('🗓 Неделя', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_tp', 'p': 'week'})
        send_vk_msg(vk, peer_id, '👨‍🏫 Выбери период поиска преподавателя:', keyboard=kb.get_keyboard())
        return

    if act == 'srch_tp':
        period = pl.get('p', 'today')
        send_vk_teacher_picker(vk, peer_id, user_id, period, scope=('group' if group_name else (fac_code or 'all')), page=0, user=user)
        return

    if act == 'srch_tl':
        send_vk_teacher_picker(vk, peer_id, user_id, pl.get('p', 'today'), scope=pl.get('s', 'all'), page=int(pl.get('pg', 0)), user=user)
        return

    if act == 'srch_tf':
        period = pl.get('p', 'today')
        kb = VkKeyboard(inline=True)
        for idx, f_code in enumerate(FACULTY_ORDER):
            if idx > 0 and idx % 2 == 0:
                kb.add_line()
            kb.add_button(f'🏛 {get_faculty_short(f_code)}', color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_tl', 'p': period, 's': f_code, 'pg': 0})
        kb.add_line()
        kb.add_button('🌐 Все факультеты', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_tl', 'p': period, 's': 'all', 'pg': 0})
        send_vk_msg(vk, peer_id, '🏛 Выбери факультет для списка преподавателей:', keyboard=kb.get_keyboard())
        return

    if act == 'srch_tq':
        upsert_vk_user(user_id, state='', payload={})
        send_vk_msg(vk, peer_id, search_teacher_plain(pl.get('q', ''), pl.get('p', 'today')), keyboard=build_main_kb(user['is_starosta']))
        return

    if act == 'srch_r':
        kb = VkKeyboard(inline=True)
        kb.add_button('📍 Сегодня', color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_rd', 'd': 0})
        kb.add_button('⏭ Завтра', color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_rd', 'd': 1})
        send_vk_msg(vk, peer_id, '🚪 Выбери день для поиска свободных аудиторий:', keyboard=kb.get_keyboard())
        return

    if act == 'srch_rd':
        d_off = int(pl.get('d', 0))
        kb = VkKeyboard(inline=True)
        for p_num in range(1, 7):
            if p_num in (4,):
                kb.add_line()
            kb.add_button(f'{p_num} пара', color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_rp', 'd': d_off, 'p': p_num, 's': 'fac'})
        send_vk_msg(vk, peer_id, '🚪 Выбери номер пары:', keyboard=kb.get_keyboard())
        return

    if act == 'srch_rp':
        d_off = int(pl.get('d', 0))
        p_num = int(pl.get('p', 1))
        scope = pl.get('s', 'fac')
        res = get_free_rooms_plain(d_off, p_num, fac_code, scope=scope)
        kb = VkKeyboard(inline=True)
        if scope == 'fac':
            kb.add_button('🌐 Все факультеты', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_rp', 'd': d_off, 'p': p_num, 's': 'all'})
        else:
            kb.add_button(f'🏛 Только {get_faculty_short(fac_code)}', color=VkKeyboardColor.SECONDARY, payload={'act': 'srch_rp', 'd': d_off, 'p': p_num, 's': 'fac'})
        send_vk_msg(vk, peer_id, res, keyboard=kb.get_keyboard())
        return

    # --- СТАРОСТА (PAYLOAD) ---
    if act == 'st_hw' or (act in ('st_repl', 'st_del') and user['is_starosta']):
        today = now_local()
        d0 = today.strftime('%d.%m.%Y')
        d1 = (today + timedelta(days=1)).strftime('%d.%m.%Y')
        d2 = (today + timedelta(days=2)).strftime('%d.%m.%Y')
        d7 = (today + timedelta(days=7)).strftime('%d.%m.%Y')
        kb = VkKeyboard(inline=True)
        if act == 'st_hw':
            send_hw_subject_picker(vk, peer_id, fac_code, course, group_name, page=0)
            return
        else:
            next_act = 'st_rd' if act == 'st_repl' else 'st_dd'
            kb.add_button(f'Сегодня ({d0[:5]})', color=VkKeyboardColor.PRIMARY, payload={'act': next_act, 'd': d0})
            kb.add_button(f'Завтра ({d1[:5]})', color=VkKeyboardColor.PRIMARY, payload={'act': next_act, 'd': d1})
            kb.add_line()
            kb.add_button(f'Послезавтра ({d2[:5]})', color=VkKeyboardColor.SECONDARY, payload={'act': next_act, 'd': d2})
            send_vk_msg(vk, peer_id, '📅 Выбери дату:', keyboard=kb.get_keyboard())
        return

    if act in ('st_rd', 'st_dd') and user['is_starosta']:
        d_str = pl['d']
        next_act = 'st_rp' if act == 'st_rd' else 'st_dp'
        kb = VkKeyboard(inline=True)
        for p_num in range(1, 7):
            if p_num == 4:
                kb.add_line()
            kb.add_button(f'{p_num} пара', color=VkKeyboardColor.PRIMARY, payload={'act': next_act, 'd': d_str, 'p': p_num})
        send_vk_msg(vk, peer_id, f'📅 Дата: {d_str}\nВыбери номер пары:', keyboard=kb.get_keyboard())
        return

    if act == 'st_rp' and user['is_starosta']:
        upsert_vk_user(user_id, state='st_await_repl_text', payload={'d': pl['d'], 'p': int(pl['p'])})
        send_vk_msg(vk, peer_id, f'🔄 Замена на {pl["d"]} ({pl["p"]} пара).\nВведи предмет, аудиторию и преподавателя (или «Отмена»):')
        return

    if act == 'st_dp' and user['is_starosta']:
        d_str = pl['d']
        t_slot = NUM_TO_TIME_SLOT.get(int(pl['p']), '')
        m_grp = user['managed_group'] or group_name
        _, m_fac = find_course_and_faculty_by_group(m_grp)
        m_fac = m_fac or infer_faculty_by_group(m_grp) or fac_code
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM replacements WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? AND time_slot = ?', (m_fac, m_grp, d_str, t_slot))
            cursor.execute(
                'INSERT OR IGNORE INTO cancelled_lessons (faculty, group_name, target_date, time_slot, cancelled_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                (m_fac, m_grp, d_str, t_slot, -abs(int(user_id)), now_local().strftime('%d.%m.%Y %H:%M')),
            )
            cursor.execute('SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1', (m_fac, m_grp))
            tg_users = [r[0] for r in cursor.fetchall()]

        tg_msg = f'🚫 <b>ОТМЕНА ПАРЫ (из VK)</b>\n<blockquote>🎓 {esc(m_grp)} • {d_str}</blockquote>\n❌ {tg_format_pair_prefix(t_slot)} — <b>ПАРА ОТМЕНЕНА</b>'
        for u_id in tg_users:
            enqueue_message(u_id, tg_msg, parse_mode='HTML')
        for g_cid in get_bound_group_chats(m_fac, m_grp):
            enqueue_message(g_cid, tg_msg, parse_mode='HTML')
        notify_vk_group_users(m_fac, m_grp, f'🚫 ОТМЕНА ПАРЫ!\n🎓 {m_grp} • {d_str}\n❌ {format_pair_prefix(t_slot)} — ОТМЕНЕНА', exclude_vk_id=user_id)
        send_vk_msg(vk, peer_id, f'✅ Пара ({pl["p"]} пара, {d_str}) отменена! Уведомления отправлены в TG и VK.', keyboard=build_main_kb(user['is_starosta']))
        return

    if act == 'st_hw_spage':
        send_hw_subject_picker(vk, peer_id, fac_code, course, group_name, page=int(pl.get('p', 0)))
        return

    if act == 'st_hw_bydate':
        today = now_local()
        d1 = (today + timedelta(days=1)).strftime('%d.%m.%Y')
        d2 = (today + timedelta(days=2)).strftime('%d.%m.%Y')
        d7 = (today + timedelta(days=7)).strftime('%d.%m.%Y')
        kb = VkKeyboard(inline=True)
        kb.add_button(f'Завтра ({d1[:5]})', color=VkKeyboardColor.PRIMARY, payload={'act': 'st_hwd', 'd': d1})
        kb.add_button(f'Послезавтра ({d2[:5]})', color=VkKeyboardColor.PRIMARY, payload={'act': 'st_hwd', 'd': d2})
        kb.add_line()
        kb.add_button(f'Через неделю ({d7[:5]})', color=VkKeyboardColor.SECONDARY, payload={'act': 'st_hwd', 'd': d7})
        send_vk_msg(vk, peer_id, '📝 Выбери дату сдачи ДЗ:', keyboard=kb.get_keyboard())
        return

    if act == 'st_hw_subj':
        subjects = get_all_group_subjects(course, group_name, faculty_code=fac_code)
        idx = int(pl.get('i', -1))
        if 0 <= idx < len(subjects):
            subj_name = subjects[idx]
            up_dates = find_upcoming_dates_for_subject(course, group_name, subj_name, days_ahead=35, max_dates=4, faculty_code=fac_code)
            if up_dates:
                d_str = up_dates[0].strftime('%d.%m.%Y')
                upsert_vk_user(user_id, state='st_await_hw_text', payload={'d': d_str, 'subj': subj_name})
                send_vk_msg(
                    vk,
                    peer_id,
                    f'📘 Предмет: {subj_name}\n📅 Ближайшая пара: {d_str}\n\nОтправь текст задания (можно прикрепить фото или документ):',
                )
            else:
                today = now_local()
                d1 = (today + timedelta(days=1)).strftime('%d.%m.%Y')
                d7 = (today + timedelta(days=7)).strftime('%d.%m.%Y')
                upsert_vk_user(user_id, state='st_await_hw_text', payload={'d': d7, 'subj': subj_name})
                send_vk_msg(
                    vk,
                    peer_id,
                    f'📘 Предмет: {subj_name}\n📅 Ближайшая пара не найдена, ставлю дедлайн на {d7}.\n\nОтправь текст задания (можно прикрепить фото или документ):',
                )
        return

    if act == 'st_hwd':
        upsert_vk_user(user_id, state='st_await_hw_subj', payload={'d': pl['d']})
        send_vk_msg(vk, peer_id, f'📝 ДЗ на {pl["d"]}.\nШаг 1/3: Введи название предмета (или «Отмена»):')
        return

    # Кнопки главного меню имеют приоритет над активным FSM-состоянием
    if raw in ('📅 Сегодня', '⏭ Завтра', '🗓 Неделя', '📚 Домашка', '🎓 Сессия', '🔍 Поиск', '⚙️ Настройки', '🛠 Староста') and user['state']:
        user = upsert_vk_user(user_id, state='', payload={})

    # --- ОБРАБОТКА СОСТОЯНИЙ (FSM) ---
    st = user['state']
    st_pl = user['payload']
    if st:
        if low in ('отмена', '/cancel'):
            upsert_vk_user(user_id, state='', payload={})
            send_vk_msg(vk, peer_id, '❌ Действие отменено.', keyboard=build_main_kb(user['is_starosta']))
            return

        if st == 'awaiting_teacher':
            if len(raw) < 2:
                send_vk_msg(vk, peer_id, '⚠️ Введи хотя бы 2 буквы фамилии преподавателя текстом (или напиши «Отмена»):')
                return
            upsert_vk_user(user_id, state='', payload={})
            send_vk_msg(vk, peer_id, search_teacher_plain(raw, st_pl.get('period', 'today')), keyboard=build_main_kb(user['is_starosta']))
            return

        if st == 'st_await_hw_edit':
            if not raw:
                send_vk_msg(vk, peer_id, '⚠️ Введи новый текст задания (или напиши «Отмена»):')
                return
            upsert_vk_user(user_id, state='', payload={})
            hw_id = int(st_pl['hw_id'])
            h_grp = st_pl['grp']
            h_fac = st_pl['fac']
            with db_connection() as conn:
                cursor = conn.cursor()
                hrow = cursor.execute('SELECT target_date, subject_name FROM homeworks WHERE id = ?', (hw_id,)).fetchone()
                if not hrow:
                    send_vk_msg(vk, peer_id, '⚠️ Задание уже удалено.', keyboard=build_main_kb(user['is_starosta']))
                    return
                cursor.execute('UPDATE homeworks SET hw_text = ? WHERE id = ?', (raw, hw_id))
                tg_users = [r[0] for r in cursor.execute('SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1', (h_fac, h_grp)).fetchall()]
            tg_msg = f'✏️ <b>ОБНОВЛЁН ТЕКСТ ДЗ (из VK)</b>\n<blockquote>🎓 {esc(h_grp)} • Дедлайн: {hrow[0]}</blockquote>\n📘 <b>{esc(hrow[1])}</b>\n📝 {esc(raw)}'
            for u_id in tg_users:
                enqueue_message(u_id, tg_msg, parse_mode='HTML')
            for g_cid in get_bound_group_chats(h_fac, h_grp):
                enqueue_message(g_cid, tg_msg, parse_mode='HTML')
            notify_vk_group_users(h_fac, h_grp, f'✏️ ОБНОВЛЁН ТЕКСТ ДЗ\n🎓 {h_grp} • Дедлайн: {hrow[0]}\n📘 {hrow[1]}\n📝 {raw}', exclude_vk_id=user_id)
            send_vk_msg(vk, peer_id, '✅ Текст ДЗ обновлён! Уведомления отправлены в TG и VK.', keyboard=build_main_kb(user['is_starosta']))
            return

        if st == 'st_await_sess_exam' and user['is_starosta']:
            parts = [p.strip() for p in raw.split('|')]
            if len(parts) < 2:
                send_vk_msg(vk, peer_id, '⚠️ Используй разделитель | . Минимум: ДД.ММ.ГГГГ | Предмет (или «Отмена»):')
                return
            try:
                ex_date = datetime.strptime(parts[0], '%d.%m.%Y').strftime('%d.%m.%Y')
            except ValueError:
                send_vk_msg(vk, peer_id, '⚠️ Неверный формат даты. Пример: 25.12.2026 | Экология (или «Отмена»):')
                return
            upsert_vk_user(user_id, state='', payload={})
            subj = parts[1]
            ex_type = parts[2] if len(parts) > 2 and parts[2] else 'Экзамен'
            ex_time = parts[3] if len(parts) > 3 and parts[3] not in ('-', '—') else ''
            extra = parts[4] if len(parts) > 4 else ''
            room, teacher = ([x.strip() for x in extra.split(',', 1)] if ',' in extra else (extra, ''))
            m_grp = st_pl['grp']
            m_fac = st_pl['fac']
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT INTO session_exams (faculty, group_name, exam_date, exam_time, subject_name, exam_type, teacher, room, added_by, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (m_fac, m_grp, ex_date, ex_time, subj, ex_type, teacher, room, -abs(int(user_id)), now_local().strftime('%d.%m.%Y %H:%M')),
                )
                tg_users = [r[0] for r in cursor.execute('SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1', (m_fac, m_grp)).fetchall()]
            tg_msg = f'🎓 <b>СЕССИЯ: ДОБАВЛЕН {esc(ex_type.upper())} (из VK)</b>\n<blockquote>📚 {esc(m_grp)} • {ex_date} {esc(ex_time)}</blockquote>\n📘 <b>{esc(subj)}</b>'
            for u_id in tg_users:
                enqueue_message(u_id, tg_msg, parse_mode='HTML')
            for g_cid in get_bound_group_chats(m_fac, m_grp):
                enqueue_message(g_cid, tg_msg, parse_mode='HTML')
            notify_vk_group_users(m_fac, m_grp, f'🎓 СЕССИЯ: ДОБАВЛЕН {ex_type.upper()}\n📚 {m_grp} • {ex_date} {ex_time}\n📘 {subj}', exclude_vk_id=user_id)
            send_vk_msg(vk, peer_id, '✅ Запись сессии сохранена и разослана в TG и VK!', keyboard=build_main_kb(user['is_starosta']))
            return

        if st == 'awaiting_slot_pick':
            upsert_vk_user(user_id, state='', payload={})
            if not raw.isdigit():
                send_vk_msg(vk, peer_id, '⚠️ Нужно ввести номер вопроса числом.', keyboard=build_main_kb(user['is_starosta']))
                return
            slot_num = int(raw)
            qid = int(st_pl['qid'])
            hwid = int(st_pl['hwid'])
            neg_uid = -abs(int(user_id))
            vk_label = f'{user["full_name"] or f"id{user_id}"} (VK)'
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT max_slots, is_open, group_name FROM queues WHERE id = ?', (qid,))
                q_info = cursor.fetchone()
                if not q_info or q_info[1] != 1 or not (1 <= slot_num <= q_info[0]):
                    send_vk_msg(vk, peer_id, '⚠️ Некорректный номер вопроса или очередь закрыта.')
                    return
                if q_info[2] and q_info[2] != group_name:
                    send_vk_msg(vk, peer_id, '⛔ Занимать вопросы могут только студенты этой группы!', keyboard=build_main_kb(user['is_starosta']))
                    return
                try:
                    cursor.execute(
                        'INSERT INTO queue_slots (queue_id, slot_num, user_id, user_label, joined_at) VALUES (?, ?, ?, ?, ?)',
                        (qid, slot_num, neg_uid, vk_label, now_local().strftime('%d.%m.%Y %H:%M')),
                    )
                    send_vk_msg(vk, peer_id, f'✅ Вопрос №{slot_num} закреплён за тобой ({vk_label})!')
                except sqlite3.IntegrityError:
                    send_vk_msg(vk, peer_id, f'⚠️ Вопрос №{slot_num} уже занят!')
            send_hw_card(vk, peer_id, user_id, hwid)
            return

        if st == 'awaiting_bug':
            _, bug_photos, bug_doc, bug_doc_name = _extract_vk_msg_attachments(attachments)
            if not raw and not bug_photos and not bug_doc:
                send_vk_msg(vk, peer_id, '⚠️ Опиши ошибку текстом или прикрепи скриншот (или напиши «Отмена»):')
                return
            upsert_vk_user(user_id, state='', payload={})
            body_html = esc(raw) if raw else '<i>(скриншот/вложение без текста)</i>'
            report_html = (
                f'📩 <b>СООБЩЕНИЕ ОБ ОШИБКЕ (VK)</b>\n'
                f'<blockquote>👤 <b>От:</b> {esc(user["full_name"])} (<a href="https://vk.com/id{user_id}">vk.com/id{user_id}</a>)\n'
                f'🎓 <b>Группа:</b> [{esc(get_faculty_short(fac_code))}] {esc(group_name)} ({course} курс)</blockquote>\n\n'
                f'{body_html}'
            )
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT user_id FROM users WHERE LOWER(username) = ?', (ADMIN_USERNAME.lower(),))
                for (adm_id,) in cursor.fetchall():
                    if bug_photos or bug_doc:
                        enqueue_hw_media(adm_id, bug_photos, report_html, doc_file_id=bug_doc, doc_file_name=bug_doc_name)
                    else:
                        enqueue_message(adm_id, report_html, parse_mode='HTML')
            send_vk_msg(vk, peer_id, '✅ Спасибо! Сообщение об ошибке отправлено администратору в Telegram.', keyboard=build_main_kb(user['is_starosta']))
            return

        if st == 'st_await_repl_text' and user['is_starosta']:
            if not raw:
                send_vk_msg(vk, peer_id, '⚠️ Введи текст замены (предмет, аудитория, преподаватель) или напиши «Отмена»:')
                return
            upsert_vk_user(user_id, state='', payload={})
            d_str = st_pl['d']
            t_slot = NUM_TO_TIME_SLOT.get(int(st_pl['p']), '')
            m_grp = user['managed_group'] or group_name
            _, m_fac = find_course_and_faculty_by_group(m_grp)
            m_fac = m_fac or infer_faculty_by_group(m_grp) or fac_code
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM cancelled_lessons WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? AND time_slot = ?', (m_fac, m_grp, d_str, t_slot))
                cursor.execute('DELETE FROM replacements WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? AND time_slot = ?', (m_fac, m_grp, d_str, t_slot))
                cursor.execute(
                    'INSERT INTO replacements (faculty, group_name, target_date, time_slot, subject_info, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (m_fac, m_grp, d_str, t_slot, raw, -abs(int(user_id)), now_local().strftime('%d.%m.%Y %H:%M')),
                )
                cursor.execute('SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1', (m_fac, m_grp))
                tg_users = [r[0] for r in cursor.fetchall()]

            tg_msg = f'⚡ <b>НОВАЯ ЗАМЕНА (из VK)</b>\n<blockquote>🎓 {esc(m_grp)} • {d_str}</blockquote>\n🔄 {tg_format_pair_prefix(t_slot)}\n👉 <b>{esc(raw)}</b>'
            for u_id in tg_users:
                enqueue_message(u_id, tg_msg, parse_mode='HTML')
            for g_cid in get_bound_group_chats(m_fac, m_grp):
                enqueue_message(g_cid, tg_msg, parse_mode='HTML')
            notify_vk_group_users(m_fac, m_grp, f'⚡ НОВАЯ ЗАМЕНА!\n🎓 {m_grp} • {d_str}\n🔄 {format_pair_prefix(t_slot)}\n👉 {raw}', exclude_vk_id=user_id)
            send_vk_msg(vk, peer_id, '✅ Замена сохранена! Уведомления отправлены в TG и VK.', keyboard=build_main_kb(user['is_starosta']))
            return

        if st == 'st_await_hw_subj':
            if not raw:
                send_vk_msg(vk, peer_id, '⚠️ Введи название предмета текстом (или напиши «Отмена»):')
                return
            upsert_vk_user(user_id, state='st_await_hw_text', payload={'d': st_pl['d'], 'subj': raw})
            send_vk_msg(vk, peer_id, f'📘 Предмет: {raw}\nШаг 2/3: Введи текст задания:')
            return

        if st == 'st_await_hw_text':
            vk_att_str, tg_photo_urls, tg_doc_url, tg_doc_name = _extract_vk_msg_attachments(attachments)
            if not raw and not vk_att_str:
                send_vk_msg(vk, peer_id, '⚠️ Отправь текст задания или прикрепи фото/документ (или напиши «Отмена»):')
                return
            upsert_vk_user(
                user_id,
                state='st_await_hw_slots',
                payload={
                    'd': st_pl['d'],
                    'subj': st_pl['subj'],
                    'text': raw,
                    'vk_att': vk_att_str,
                    'tg_photos': tg_photo_urls,
                    'tg_doc': tg_doc_url,
                    'tg_doc_name': tg_doc_name,
                },
            )
            send_vk_msg(vk, peer_id, 'Сколько вопросов для разбора добавить? (введи число 1–60, или 0 если без вопросов):')
            return

        if st == 'st_await_hw_slots':
            upsert_vk_user(user_id, state='', payload={})
            slots_cnt = int(raw) if raw.isdigit() and 1 <= int(raw) <= 60 else 0
            d_str, subj, hw_text = st_pl['d'], st_pl['subj'], st_pl['text']
            vk_att_str = st_pl.get('vk_att', '')
            tg_photos = st_pl.get('tg_photos') or []
            tg_doc = st_pl.get('tg_doc', '')
            tg_doc_name = st_pl.get('tg_doc_name', '')
            serialized_photos = serialize_photo_ids(tg_photos)
            m_grp = group_name
            stamp = now_local().strftime('%d.%m.%Y %H:%M')
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT INTO homeworks (faculty, group_name, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, vk_attachments, added_by, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (fac_code, m_grp, d_str, subj, hw_text, serialized_photos, tg_doc, tg_doc_name, vk_att_str, -abs(int(user_id)), stamp),
                )
                hw_id = cursor.lastrowid
                if slots_cnt > 0:
                    cursor.execute(
                        'INSERT INTO queues (faculty, group_name, title, max_slots, is_open, homework_id, created_by, created_at) VALUES (?, ?, "Вопросы", ?, 1, ?, ?, ?)',
                        (fac_code, m_grp, slots_cnt, hw_id, -abs(int(user_id)), stamp),
                    )
                    q_id = cursor.lastrowid
                    cursor.execute('UPDATE homeworks SET queue_id = ? WHERE id = ?', (q_id, hw_id))
                cursor.execute('SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1', (fac_code, m_grp))
                tg_users = [r[0] for r in cursor.fetchall()]

            author_vk = user['full_name'] or f'id{user_id}'
            body_html = esc(hw_text) if hw_text else '<i>(см. вложение из VK)</i>'
            tg_msg = f'📚 <b>НОВОЕ ДЗ (из VK)</b>\n<blockquote>🎓 {esc(m_grp)} • Дедлайн: {d_str}\n👤 Добавил: {esc(author_vk)} (VK)</blockquote>\n📘 <b>{esc(subj)}</b>\n📝 {body_html}'
            for u_id in tg_users:
                enqueue_hw_media(u_id, tg_photos, tg_msg, doc_file_id=tg_doc, doc_file_name=tg_doc_name)
            for g_cid in get_bound_group_chats(fac_code, m_grp):
                enqueue_hw_media(g_cid, tg_photos, tg_msg, doc_file_id=tg_doc, doc_file_name=tg_doc_name)
            notify_vk_group_users(fac_code, m_grp, f'📚 НОВОЕ ДЗ!\n🎓 {m_grp} • Дедлайн: {d_str}\n👤 Добавил: {author_vk}\n📘 {subj}\n📝 {hw_text or "(см. вложение)"}', exclude_vk_id=user_id)
            send_vk_msg(vk, peer_id, '✅ ДЗ сохранено и разослано в TG и VK!', keyboard=build_main_kb(user['is_starosta']))
            return

    # --- КНОПКИ ГЛАВНОГО МЕНЮ ---
    if raw == '📅 Сегодня' or low == 'сегодня':
        log_stat_event('vk_today')
        send_vk_msg(vk, peer_id, get_day_schedule_plain(now_local(), fac_code, course, group_name), keyboard=build_day_nav_kb(0))
    elif raw == '⏭ Завтра' or low == 'завтра':
        log_stat_event('vk_tomorrow')
        send_vk_msg(vk, peer_id, get_day_schedule_plain(now_local() + timedelta(days=1), fac_code, course, group_name), keyboard=build_day_nav_kb(1))
    elif raw == '🗓 Неделя' or low == 'неделя':
        log_stat_event('vk_week')
        send_vk_msg(vk, peer_id, get_week_schedule_plain(now_local(), fac_code, course, group_name), keyboard=build_week_nav_kb(0))
    elif raw == '📚 Домашка' or low in ('дз', 'домашка'):
        log_stat_event('vk_homework')
        send_hw_index(vk, peer_id, user_id, user, page=0)
    elif raw == '🎓 Сессия' or low == 'сессия':
        log_stat_event('vk_session')
        send_vk_session_view(vk, peer_id, user)
    elif raw == '🔍 Поиск' or low == 'поиск':
        log_stat_event('vk_search')
        kb = VkKeyboard(inline=True)
        kb.add_button('👨‍🏫 Где преподаватель?', color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_t'})
        kb.add_line()
        kb.add_button('🚪 Свободные аудитории', color=VkKeyboardColor.PRIMARY, payload={'act': 'srch_r'})
        send_vk_msg(vk, peer_id, '🔍 УМНЫЙ ПОИСК (VK)\nВыбери, что нужно найти:', keyboard=kb.get_keyboard())
    elif raw == '⚙️ Настройки' or low == 'настройки':
        send_settings_menu(vk, peer_id, user)
    elif raw == '🛠 Староста' or low == 'староста':
        send_starosta_menu(vk, peer_id, user)
    else:
        send_vk_msg(vk, peer_id, '👇 Используй кнопки меню ниже:', keyboard=build_main_kb(user['is_starosta']))


def run_vk_bot():
    global _VK_API, _VK_SESSION
    if not VK_TOKEN or VK_TOKEN == 'YOUR_VK_COMMUNITY_TOKEN_HERE' or VK_GROUP_ID == 0:
        raise RuntimeError('Укажи переменные окружения VK_TOKEN и VK_GROUP_ID!')
    _VK_SESSION = vk_api.VkApi(token=VK_TOKEN)
    _VK_API = _VK_SESSION.get_api()
    core.VK_SEND_HOOK = enqueue_vk_msg
    core.VK_NOTIFY_GROUP_HOOK = notify_vk_group_users
    core.VK_QUEUE_SIZE_HOOK = _VK_SEND_QUEUE.qsize
    longpoll = VkBotLongPoll(_VK_SESSION, VK_GROUP_ID)
    print('VK-бот СИ УУНиТ (полноценный) запущен и синхронизирован с Telegram!')
    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW and event.from_user:
                    msg = event.obj.message
                    handle_message(_VK_API, msg['from_id'], msg['peer_id'], msg.get('text', ''), msg.get('payload'), msg.get('attachments'))
        except Exception as e:
            print(f'Сбой VK LongPoll, перезапуск через 5 сек: {e}')
            time.sleep(5)


if __name__ == '__main__':
    run_vk_bot()
