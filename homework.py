from datetime import datetime, timedelta
import sqlite3
import threading
import time
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

import core
from core import (
    ADMIN_USERNAME,
    DAYS_RUS,
    LOCAL_TZ,
    SHORT_DAYS_RUS,
    bot,
    build_faculty_keyboard,
    can_manage_hw,
    clear_user_state,
    db_connection,
    enqueue_hw_media,
    enqueue_message,
    esc,
    find_course_and_faculty_by_group,
    find_course_by_group,
    get_bound_group_chats,
    get_current_week_and_day,
    get_managed_context,
    get_user_data,
    get_user_state,
    infer_faculty_by_group,
    is_admin_user,
    is_group_valid,
    is_user_starosta,
    now_local,
    parse_photo_ids,
    safe_edit_message_text,
    send_hw_media,
    send_long_html_message,
    serialize_photo_ids,
    set_user_state,
    short_text,
    start_onboarding,
    sync_username,
)
from schedule_search import get_all_group_subjects, get_day_lessons_for_date


def _build_upcoming_subjects_map(course, group_name, faculty_code=None, days_ahead=35):
    """Загружает расписание, отмены и замены группы за один коннект к БД и возвращает словарь {date_obj: [subjects]}."""
    if not faculty_code:
        _, faculty_code = find_course_and_faculty_by_group(group_name)
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    today = now_local()
    date_items = []
    weeks_needed = set()
    date_strs = []
    for offset in range(1, days_ahead + 1):
        dt = today + timedelta(days=offset)
        w_num, d_name = get_current_week_and_day(dt, faculty_code=faculty_code)
        d_str = dt.strftime('%d.%m.%Y')
        date_items.append((dt, w_num, d_name, d_str))
        weeks_needed.add(w_num)
        date_strs.append(d_str)

    if not date_items:
        return {}

    week_list = sorted(weeks_needed)
    w_placeholders = ','.join('?' * len(week_list))
    d_placeholders = ','.join('?' * len(date_strs))

    with db_connection() as conn:
        cursor = conn.cursor()
        if faculty_code:
            cursor.execute(
                f'SELECT sw.week_number, s.day_of_week, s.time_slot, s.subject_name '
                f'FROM schedules s '
                f'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                f'WHERE s.faculty = ? AND s.course_number = ? AND s.group_name = ? '
                f'AND sw.week_number IN ({w_placeholders}) '
                f'ORDER BY s.time_slot ASC, s.id ASC',
                [faculty_code, course, group_name] + week_list,
            )
        else:
            cursor.execute(
                f'SELECT sw.week_number, s.day_of_week, s.time_slot, s.subject_name '
                f'FROM schedules s '
                f'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                f'WHERE s.course_number = ? AND s.group_name = ? '
                f'AND sw.week_number IN ({w_placeholders}) '
                f'ORDER BY s.time_slot ASC, s.id ASC',
                [course, group_name] + week_list,
            )
        sched_rows = cursor.fetchall()

        cursor.execute(
            f'SELECT target_date, time_slot FROM cancelled_lessons '
            f'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date IN ({d_placeholders})',
            [eff_fac, group_name] + date_strs,
        )
        canc_by_date = {}
        for c_date, c_slot in cursor.fetchall():
            canc_by_date.setdefault(c_date, set()).add(c_slot)

        cursor.execute(
            f'SELECT target_date, time_slot, subject_info FROM replacements '
            f'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date IN ({d_placeholders}) '
            f'ORDER BY time_slot ASC, id ASC',
            [eff_fac, group_name] + date_strs,
        )
        repl_by_date = {}
        for r_date, r_slot, r_subj in cursor.fetchall():
            repl_by_date.setdefault(r_date, []).append((r_slot, r_subj))

    sched_by_wk_day = {}
    for w_num, d_name, t_slot, subj_name in sched_rows:
        sched_by_wk_day.setdefault((w_num, d_name), []).append((t_slot, subj_name))

    result = {}
    for dt, w_num, d_name, d_str in date_items:
        if w_num <= 0 or w_num > 52 or d_name == 'Воскресенье' or w_num in core.PRACTICE_WEEKS.get(group_name, set()):
            result[dt.date()] = []
            continue
        cancelled_slots = canc_by_date.get(d_str, set())
        repls = repl_by_date.get(d_str, [])
        replaced_slots = {r_slot for r_slot, _ in repls}
        subjects = []
        for t_slot, subj_name in sched_by_wk_day.get((w_num, d_name), []):
            if t_slot in cancelled_slots or t_slot in replaced_slots:
                continue
            if subj_name not in subjects:
                subjects.append(subj_name)
        for _, r_subj in repls:
            if r_subj not in subjects:
                subjects.append(r_subj)
        result[dt.date()] = subjects
    return result


def find_upcoming_dates_for_subject(course, group_name, subject_query, days_ahead=35, max_dates=4, faculty_code=None):
    """Находит ближайшие будущие даты (начиная с завтрашнего дня), когда у группы стоит указанный предмет."""
    q_low = str(subject_query or '').strip().lower()
    if not q_low:
        return []
    day_map = _build_upcoming_subjects_map(course, group_name, faculty_code=faculty_code, days_ahead=days_ahead)
    today = now_local()
    found = []
    for offset in range(1, days_ahead + 1):
        dt = today + timedelta(days=offset)
        subjects = day_map.get(dt.date(), [])
        matched = any(
            q_low == s.strip().lower() or q_low in s.strip().lower() or s.strip().lower() in q_low
            for s in subjects
        )
        if matched:
            found.append(dt)
            if len(found) >= max_dates:
                break
    return found


def open_add_homework(chat_id, user_obj, message_id=None):
    clear_user_state(chat_id)
    course, group_name, fac_code = get_managed_context(user_obj)
    if not group_name:
        bot.send_message(chat_id, '⚠️ Сначала выбери свою группу через /start.')
        return
    if not can_manage_hw(user_obj):
        bot.send_message(
            chat_id,
            '⚠️ Сначала выбери свой факультет и группу через /start.',
            parse_mode='HTML',
        )
        return

    subjects = get_all_group_subjects(course, group_name, faculty_code=fac_code)
    day_map = _build_upcoming_subjects_map(course, group_name, faculty_code=fac_code, days_ahead=35)
    today = now_local()
    ordered_dates = [today + timedelta(days=offset) for offset in range(1, 36)]

    subj_entries = []
    for subj in subjects:
        q_low = subj.strip().lower()
        up_dates = []
        for dt in ordered_dates:
            day_subjs = day_map.get(dt.date(), [])
            if any(
                q_low == s.strip().lower() or q_low in s.strip().lower() or s.strip().lower() in q_low
                for s in day_subjs
            ):
                up_dates.append(dt)
                if len(up_dates) >= 4:
                    break
        next_date_str = up_dates[0].strftime('%d.%m.%Y') if up_dates else ''
        next_badge = (
            f' ({SHORT_DAYS_RUS[up_dates[0].weekday()]} {up_dates[0].strftime("%d.%m")})'
            if up_dates
            else ''
        )
        subj_entries.append({
            'subject': subj,
            'next_date': next_date_str,
            'upcoming_dates': [d.strftime('%d.%m.%Y') for d in up_dates],
            'badge': next_badge,
        })

    set_user_state(chat_id, 'awaiting_hw_manual_subj', {
        'hw_group': group_name,
        'hw_course': course,
        'hw_subj_entries': subj_entries,
    })

    markup = InlineKeyboardMarkup(row_width=1)
    for idx, ent in enumerate(subj_entries):
        badge = ent['badge']
        max_s_len = max(16, 38 - len(badge))
        btn_label = f'📘 {short_text(ent["subject"], max_s_len)}{badge}'
        markup.add(
            InlineKeyboardButton(btn_label, callback_data=f'hwadd_picksubj_{idx}')
        )
    markup.add(InlineKeyboardButton('📅 Выбрать сначала дату', callback_data='hwadd_bydate_menu'))
    markup.add(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))

    text = (
        f'📚 <b>ДОБАВЛЕНИЕ ДОМАШНЕГО ЗАДАНИЯ</b>\n'
        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}</blockquote>\n\n'
        f'<b>Шаг 1 из 3.</b> Нажми на предмет из списка ниже <b>или просто напиши его название сообщением</b> — '
        f'бот сам прикрепит ДЗ к ближайшей следующей паре по расписанию:'
    )
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def open_add_homework_by_date(chat_id, user_obj, message_id=None):
    clear_user_state(chat_id)
    _, group_name, _ = get_managed_context(user_obj)
    if not group_name:
        bot.send_message(chat_id, '⚠️ Сначала выбери свою группу через /start.')
        return

    today = now_local()
    d1 = (today + timedelta(days=1)).strftime('%d.%m.%Y')
    d2 = (today + timedelta(days=2)).strftime('%d.%m.%Y')
    d3 = (today + timedelta(days=3)).strftime('%d.%m.%Y')
    d7 = (today + timedelta(days=7)).strftime('%d.%m.%Y')

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(f'⏭ На завтра ({d1})', callback_data=f'hwadd_date_{d1}'),
        InlineKeyboardButton(f'⏩ На послезавтра ({d2})', callback_data=f'hwadd_date_{d2}'),
        InlineKeyboardButton(f'📅 Через 3 дня ({d3})', callback_data=f'hwadd_date_{d3}'),
        InlineKeyboardButton(f'🗓 Через неделю ({d7})', callback_data=f'hwadd_date_{d7}'),
        InlineKeyboardButton('⌨️ Ввести дату вручную', callback_data='hwadd_datemanual'),
        InlineKeyboardButton('🔙 К выбору по предмету', callback_data='hw_open_add'),
    )

    text = (
        f'📚 <b>ДОБАВЛЕНИЕ ДЗ ПО ДАТЕ</b>\n'
        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}</blockquote>\n\n'
        f'Выбери дату сдачи (дедлайн):'
    )
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def start_add_homework(message):
    open_add_homework(message.chat.id, message.from_user)


def show_hw_subject_selection(chat_id, message_id, course, group_name, date_str, show_all=False):
    parsed_date = datetime.strptime(date_str, '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
    _, day_name = get_current_week_and_day(parsed_date)

    if show_all:
        subjects = get_all_group_subjects(course, group_name)
        header_note = 'Все предметы группы'
    else:
        _, subjects = get_day_lessons_for_date(parsed_date, course, group_name)
        header_note = f'Пары на {day_name}, {date_str}'

    set_user_state(chat_id, '', {
        'hw_date': date_str,
        'hw_subjects': subjects,
        'hw_group': group_name,
        'hw_course': course,
    })

    markup = InlineKeyboardMarkup(row_width=1)
    for idx, subj in enumerate(subjects):
        markup.add(
            InlineKeyboardButton(
                f'📘 {short_text(subj, 38)}',
                callback_data=f'hwadd_subj_{idx}',
            )
        )

    if not show_all:
        markup.add(InlineKeyboardButton('📖 Все предметы группы', callback_data='hwadd_allsubj'))
    markup.add(InlineKeyboardButton('✍️ Ввести предмет вручную', callback_data='hwadd_subjmanual'))
    markup.add(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))

    text = (
        f'📚 <b>ДОБАВЛЕНИЕ ДЗ — {date_str} ({day_name})</b>\n\n'
        f'Выбери предмет <i>({header_note})</i>:'
    )
    if not subjects and not show_all:
        text += '\n\n<i>На этот день пар по расписанию нет — выбери из всех предметов группы или введи вручную.</i>'

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def format_queue_slots_block(queue_id, viewer_user_id=None, header_title='📋 Распределение вопросов:'):
    if not queue_id:
        return ''
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT max_slots, is_open FROM queues WHERE id = ?', (queue_id,))
        q_row = cursor.fetchone()
        if not q_row:
            return ''
        max_slots, is_open = q_row
        cursor.execute(
            'SELECT slot_num, user_id, user_label FROM queue_slots WHERE queue_id = ? ORDER BY slot_num ASC',
            (queue_id,),
        )
        slots = cursor.fetchall()

    taken_by_num = {s_num: (u_id, u_lbl) for s_num, u_id, u_lbl in slots}
    lines = [f'\n\n<b>{esc(header_title)}</b> <i>({len(slots)}/{max_slots})</i>']
    for num in range(1, max_slots + 1):
        if num in taken_by_num:
            u_id, u_lbl = taken_by_num[num]
            me_mark = ' 👈 <b>(ты)</b>' if (viewer_user_id and u_id == viewer_user_id) else ''
            lines.append(f'<b>{num}.</b> {esc(u_lbl)}{me_mark}')
        else:
            lines.append(f'<b>{num}.</b> <i>— свободно —</i>')
    return '\n'.join(lines)


def build_queue_slot_buttons(
    queue_id,
    viewer_user_id=None,
    can_moderate=False,
    include_manage_buttons=None,
):
    if not queue_id:
        return include_manage_buttons

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT max_slots, is_open FROM queues WHERE id = ?', (queue_id,))
        q_row = cursor.fetchone()
        if not q_row:
            return include_manage_buttons
        max_slots, is_open = q_row
        cursor.execute(
            'SELECT slot_num, user_id, user_label FROM queue_slots WHERE queue_id = ? ORDER BY slot_num ASC',
            (queue_id,),
        )
        slots = cursor.fetchall()

    taken_map = {s_num: (u_id, u_lbl) for s_num, u_id, u_lbl in slots}
    my_slots = [s_num for s_num, u_id, _ in slots if viewer_user_id and u_id == viewer_user_id]

    markup = InlineKeyboardMarkup(row_width=5)
    if is_open:
        free_nums = [n for n in range(1, max_slots + 1) if n not in taken_map]
        if free_nums:
            slot_btns = [
                InlineKeyboardButton(f'➕ №{n}', callback_data=f'hwq_pick_{queue_id}_{n}')
                for n in free_nums[:60]
            ]
            for i in range(0, len(slot_btns), 5):
                markup.row(*slot_btns[i : i + 5])

        removable_nums = sorted(taken_map.keys()) if can_moderate else my_slots
        max_rem = max(0, min(25, 92 - min(len(free_nums), 60)))
        if removable_nums and max_rem > 0:
            rem_btns = []
            for n in removable_nums[:max_rem]:
                owner_id, owner_lbl = taken_map[n]
                if viewer_user_id and owner_id == viewer_user_id:
                    btn_txt = f'❌ Снять №{n}'
                else:
                    short_owner = short_text(owner_lbl.split('(')[0].strip(), 10)
                    btn_txt = f'🧹 №{n} ({short_owner})'
                rem_btns.append(
                    InlineKeyboardButton(btn_txt, callback_data=f'hwq_free_{queue_id}_{n}')
                )
            for i in range(0, len(rem_btns), 3):
                markup.row(*rem_btns[i : i + 3])

        markup.row(
            InlineKeyboardButton('🔄 Обновить список', callback_data=f'hwq_refresh_{queue_id}')
        )

    if include_manage_buttons:
        for row_btns in include_manage_buttons:
            markup.row(*row_btns)
    return markup


def ask_hw_content(chat_id, group_name, date_str, subject_name, upcoming_dates=None):
    up_list = list(upcoming_dates or [])
    if date_str and date_str not in up_list:
        up_list.insert(0, date_str)

    set_user_state(chat_id, 'awaiting_hw_content', {
        'hw_group': group_name,
        'hw_date': date_str,
        'hw_subject': subject_name,
        'hw_upcoming_dates': up_list,
    })

    try:
        dt_obj = datetime.strptime(date_str, '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
        day_label = f' ({DAYS_RUS[dt_obj.weekday()]})'
    except Exception:
        day_label = ''

    alt_dates = [d for d in up_list if d != date_str][:3]
    markup = InlineKeyboardMarkup(row_width=2)
    if alt_dates:
        alt_btns = []
        for d_alt in alt_dates:
            try:
                dt_a = datetime.strptime(d_alt, '%d.%m.%Y')
                lbl_a = f'📅 {SHORT_DAYS_RUS[dt_a.weekday()]} {d_alt[:5]}'
            except Exception:
                lbl_a = f'📅 {d_alt[:5]}'
            alt_btns.append(InlineKeyboardButton(lbl_a, callback_data=f'hwadd_setdate_{d_alt}'))
        markup.add(*alt_btns)
    markup.row(
        InlineKeyboardButton('⌨️ Другая дата', callback_data='hwadd_changedate_manual'),
        InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
    )

    bot.send_message(
        chat_id,
        f'📘 <b>Предмет:</b> {esc(subject_name)}\n'
        f'📅 <b>Дата пары (дедлайн):</b> <b>{date_str}</b>{day_label}\n\n'
        f'<b>Шаг 2 из 3.</b> Отправь текст домашнего задания (можно сразу с фото, альбомом или документом):\n'
        f'<i>Если нужно прикрепить к следующей паре по этому предмету или другой дате — нажми кнопку ниже.</i>',
        reply_markup=markup,
        parse_mode='HTML',
    )


def process_hw_manual_subject(message, payload):
    group_name = (payload or {}).get('hw_group', '')
    course = (payload or {}).get('hw_course') or find_course_by_group(group_name)
    fixed_date_str = (payload or {}).get('hw_date', '')
    clear_user_state(message.chat.id)
    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        bot.send_message(message.chat.id, '❌ Добавление ДЗ отменено.')
        return

    raw_subj = message.text.strip()
    all_subjs = get_all_group_subjects(course, group_name) if course else []
    matched_subj = raw_subj
    for s in all_subjs:
        if s.strip().lower() == raw_subj.lower():
            matched_subj = s
            break
    else:
        partial = [s for s in all_subjs if raw_subj.lower() in s.strip().lower()]
        if len(partial) == 1:
            matched_subj = partial[0]

    if fixed_date_str:
        ask_hw_content(message.chat.id, group_name, fixed_date_str, matched_subj)
        return

    up_dts = find_upcoming_dates_for_subject(course, group_name, matched_subj, days_ahead=35, max_dates=4) if course else []
    up_strs = [d.strftime('%d.%m.%Y') for d in up_dts]
    if up_strs:
        ask_hw_content(message.chat.id, group_name, up_strs[0], matched_subj, upcoming_dates=up_strs)
        return

    # Если предмет не найден в расписании на ближайшие 5 недель — предлагаем быстрый выбор даты
    today = now_local()
    d1 = (today + timedelta(days=1)).strftime('%d.%m.%Y')
    d2 = (today + timedelta(days=2)).strftime('%d.%m.%Y')
    d7 = (today + timedelta(days=7)).strftime('%d.%m.%Y')
    set_user_state(message.chat.id, '', {
        'hw_group': group_name,
        'hw_course': course,
        'hw_pending_custom_subj': matched_subj,
    })
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(f'⏭ На завтра ({d1})', callback_data=f'hwadd_custdate_{d1}'),
        InlineKeyboardButton(f'⏩ На послезавтра ({d2})', callback_data=f'hwadd_custdate_{d2}'),
        InlineKeyboardButton(f'🗓 Через неделю ({d7})', callback_data=f'hwadd_custdate_{d7}'),
        InlineKeyboardButton('⌨️ Ввести дату вручную', callback_data='hwadd_changedate_manual_new'),
        InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
    )
    bot.send_message(
        message.chat.id,
        f'📘 <b>Предмет:</b> {esc(matched_subj)}\n\n'
        f'⚠️ Этот предмет не нашёлся в ближайшем расписании группы. Выбери дату, к которой прикрепить ДЗ:',
        reply_markup=markup,
        parse_mode='HTML',
    )


def ask_hw_attach_queue(chat_id, author_id, author_label, group_name, date_str, subject_name, hw_text, photo_ids, doc_file_id='', doc_file_name=''):
    set_user_state(chat_id, '', {
        'pending_hw_draft': {
            'author_id': author_id,
            'author_label': author_label,
            'group_name': group_name,
            'date_str': date_str,
            'subject_name': subject_name,
            'hw_text': hw_text,
            'photo_ids': photo_ids,
            'doc_file_id': doc_file_id,
            'doc_file_name': doc_file_name,
        }
    })
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton('✅ Да, добавить слоты для разбора вопросов', callback_data='hw_queue_yes'),
        InlineKeyboardButton('⏭ Нет, опубликовать ДЗ без слотов', callback_data='hw_queue_no'),
        InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
    )
    bot.send_message(
        chat_id,
        f'📋 <b>Шаг 3 из 3. Слоты для разбора вопросов</b>\n\n'
        f'Нужно ли прикрепить к этому ДЗ по предмету <b>«{esc(subject_name)}»</b> нумерованные слоты (1..N), чтобы студенты могли разобрать вопросы (в том числе по несколько вопросов на человека)?',
        reply_markup=markup,
        parse_mode='HTML',
    )


def process_hw_queue_count_input(message, payload):
    clear_user_state(message.chat.id, keep_payload=True)

    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        clear_user_state(message.chat.id)
        bot.send_message(message.chat.id, '❌ Добавление ДЗ отменено.')
        return

    raw = message.text.strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 60):
        set_user_state(message.chat.id, 'awaiting_hw_slots_count', payload)
        bot.send_message(
            message.chat.id,
            '⚠️ Введи целое число от <b>1</b> до <b>60</b> (например, <code>12</code> или <code>20</code>), либо напиши «Отмена»:',
            parse_mode='HTML',
        )
        return

    slots_count = int(raw)
    draft = (payload or {}).get('pending_hw_draft')
    if not draft:
        clear_user_state(message.chat.id)
        bot.send_message(message.chat.id, '⚠️ Сессия создания ДЗ устарела. Начни заново.')
        return

    save_and_broadcast_homework(
        message.chat.id,
        draft['author_id'],
        draft['group_name'],
        draft['date_str'],
        draft['subject_name'],
        draft['hw_text'],
        draft['photo_ids'],
        doc_file_id=draft['doc_file_id'],
        doc_file_name=draft['doc_file_name'],
        queue_slots_count=slots_count,
        author_label=draft.get('author_label') or format_hw_author_label(message.from_user),
    )


def save_and_broadcast_homework(
    chat_id,
    author_id,
    group_name,
    date_str,
    subject_name,
    hw_text,
    photo_ids,
    doc_file_id='',
    doc_file_name='',
    queue_slots_count=None,
    author_label='',
):
    clear_user_state(chat_id)
    serialized_photos = serialize_photo_ids(photo_ids)
    created_stamp = now_local().strftime('%d.%m.%Y %H:%M')

    with db_connection() as conn:
        c_tmp = conn.cursor()
        c_tmp.execute('SELECT username, faculty FROM users WHERE user_id = ?', (author_id,))
        u_r = c_tmp.fetchone()
        if not author_label:
            author_label = f'@{u_r[0]}' if (u_r and u_r[0]) else f'ID {author_id}'
        _, struct_fac = find_course_and_faculty_by_group(group_name)
        eff_fac = struct_fac or (u_r[1] if u_r and u_r[1] else '') or infer_faculty_by_group(group_name)

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO homeworks '
            '(faculty, group_name, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, added_by, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                eff_fac,
                group_name,
                date_str,
                subject_name,
                hw_text,
                serialized_photos,
                doc_file_id,
                doc_file_name,
                author_id,
                created_stamp,
            ),
        )
        hw_id = cursor.lastrowid
        queue_id = None

        if queue_slots_count and queue_slots_count > 0:
            q_title = f'{subject_name} (ДЗ на {date_str})'
            cursor.execute(
                'INSERT INTO queues (faculty, group_name, title, max_slots, is_open, homework_id, created_by, created_at) '
                'VALUES (?, ?, ?, ?, 1, ?, ?, ?)',
                (eff_fac, group_name, q_title, queue_slots_count, hw_id, author_id, created_stamp),
            )
            queue_id = cursor.lastrowid
            cursor.execute(
                'UPDATE homeworks SET queue_id = ? WHERE id = ?',
                (queue_id, hw_id),
            )

        cursor.execute(
            'SELECT user_id, is_starosta, is_hw_manager, username FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1',
            (eff_fac, group_name),
        )
        group_users = cursor.fetchall()

    att_notes = []
    if photo_ids:
        att_notes.append(f'фото: {len(photo_ids)} шт.')
    if doc_file_id:
        att_notes.append(f'файл: {esc(doc_file_name or "документ")}')
    att_fallback = f'<i>(прикреплено: {", ".join(att_notes)})</i>' if att_notes else ''

    q_block_author = format_queue_slots_block(queue_id, viewer_user_id=author_id) if queue_id else ''
    author_markup = (
        build_queue_slot_buttons(queue_id, viewer_user_id=author_id, can_moderate=True)
        if queue_id
        else None
    )

    confirm_text = (
        f'✅ <b>Домашнее задание сохранено!</b>\n\n'
        f'📅 <b>Дедлайн:</b> {date_str}\n'
        f'📘 <b>Предмет:</b> {esc(subject_name)}\n'
        f'📝 <b>Задание:</b> {esc(hw_text) if hw_text else att_fallback}\n'
        f'👤 <b>Добавил:</b> {esc(author_label)}'
        f'{q_block_author}'
    )
    send_hw_media(
        chat_id,
        photo_ids,
        confirm_text,
        reply_markup=author_markup,
        doc_file_id=doc_file_id,
        doc_file_name=doc_file_name,
    )

    for u_id, u_st, u_hw, u_uname in group_users:
        if u_id != author_id:
            is_u_mod = bool(u_st == 1 or (u_uname or '').lower() == ADMIN_USERNAME.lower())
            q_block_user = format_queue_slots_block(queue_id, viewer_user_id=u_id) if queue_id else ''
            user_markup = (
                build_queue_slot_buttons(queue_id, viewer_user_id=u_id, can_moderate=is_u_mod)
                if queue_id
                else None
            )
            notify_text = (
                f'📚 <b>НОВОЕ ДОМАШНЕЕ ЗАДАНИЕ!</b>\n'
                f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}\n'
                f'📅 <b>Срок сдачи:</b> {date_str}\n'
                f'👤 <b>Добавил:</b> {esc(author_label)}</blockquote>\n\n'
                f'📘 <b>{esc(subject_name)}</b>\n'
                f'📝 {esc(hw_text) if hw_text else "<i>(см. прикреплённое вложение)</i>"}'
                f'{q_block_user}'
            )
            enqueue_hw_media(
                u_id,
                photo_ids,
                notify_text,
                reply_markup=user_markup,
                doc_file_id=doc_file_id,
                doc_file_name=doc_file_name,
            )

    group_notify_text = (
        f'📚 <b>НОВОЕ ДОМАШНЕЕ ЗАДАНИЕ!</b>\n'
        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}\n'
        f'📅 <b>Срок сдачи:</b> {date_str}\n'
        f'👤 <b>Добавил:</b> {esc(author_label)}</blockquote>\n\n'
        f'📘 <b>{esc(subject_name)}</b>\n'
        f'📝 {esc(hw_text) if hw_text else "<i>(см. прикреплённое вложение)</i>"}'
        f'{format_queue_slots_block(queue_id) if queue_id else ""}'
    )
    group_markup = build_queue_slot_buttons(queue_id, viewer_user_id=None, can_moderate=False) if queue_id else None
    for g_chat_id in get_bound_group_chats(eff_fac, group_name):
        if g_chat_id != chat_id:
            enqueue_hw_media(
                g_chat_id,
                photo_ids,
                group_notify_text,
                reply_markup=group_markup,
                doc_file_id=doc_file_id,
                doc_file_name=doc_file_name,
            )


def flush_media_group_hw(mg_id):
    try:
        with core.media_group_lock:
            entry = core.media_group_buffer.pop(mg_id, None)
        if not entry:
            return
        clear_user_state(entry['chat_id'])
        dedup_by_msg = {}
        for msg_id_item, p_id, cap_txt in entry['items']:
            dedup_by_msg[msg_id_item] = (msg_id_item, p_id, cap_txt)
        sorted_items = sorted(dedup_by_msg.values(), key=lambda x: x[0])
        photo_ids = [x[1] for x in sorted_items]
        captions = [x[2] for x in sorted_items if x[2]]
        hw_text = captions[0] if captions else ''

        ask_hw_attach_queue(
            entry['chat_id'],
            entry['author_id'],
            entry['author_label'],
            entry['group_name'],
            entry['date_str'],
            entry['subject_name'],
            hw_text,
            photo_ids,
        )
    except Exception as e:
        print(f'Ошибка при сборке альбома ДЗ ({mg_id}): {e}')


def _is_album_message(m):
    if not (m and m.photo and m.media_group_id):
        return False
    mg_id = str(m.media_group_id)
    with core.media_group_lock:
        if mg_id in core.media_group_buffer:
            return True
    if m.from_user:
        st_name, pl = get_user_state(m.from_user.id)
        if st_name == 'awaiting_hw_content':
            return True
        if (pl or {}).get('_active_hw_media_group') == mg_id:
            return True
    return False


@bot.message_handler(
    chat_types=['private'],
    content_types=['photo'],
    func=_is_album_message,
)
def collect_subsequent_album_photos(message):
    mg_id = str(message.media_group_id)
    with core.media_group_lock:
        entry = core.media_group_buffer.get(mg_id)
        if not entry:
            st_name, pl = get_user_state(message.from_user.id)
            if st_name != 'awaiting_hw_content' and (pl or {}).get('_active_hw_media_group') != mg_id:
                return
            entry = {
                '_created_ts': time.time(),
                'chat_id': message.chat.id,
                'author_id': message.from_user.id,
                'author_label': format_hw_author_label(message.from_user),
                'group_name': (pl or {}).get('hw_group', ''),
                'date_str': (pl or {}).get('hw_date', ''),
                'subject_name': (pl or {}).get('hw_subject', ''),
                'items': [],
                'timer': None,
            }
            core.media_group_buffer[mg_id] = entry
            set_user_state(message.from_user.id, '', dict(pl or {}, _active_hw_media_group=mg_id))
        entry['items'].append(
            (message.message_id, message.photo[-1].file_id, (message.caption or '').strip())
        )
        if entry.get('timer'):
            entry['timer'].cancel()
        t = threading.Timer(1.3, flush_media_group_hw, args=[mg_id])
        entry['timer'] = t
        t.start()


def process_hw_content(message, payload):
    group_name = (payload or {}).get('hw_group', '')
    date_str = (payload or {}).get('hw_date', '')
    subject_name = (payload or {}).get('hw_subject', '')
    if message.text and message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        clear_user_state(message.chat.id)
        bot.send_message(message.chat.id, '❌ Добавление ДЗ отменено.')
        return

    author_label = format_hw_author_label(message.from_user)

    if message.photo and message.media_group_id:
        mg_id = str(message.media_group_id)
        with core.media_group_lock:
            entry = core.media_group_buffer.get(mg_id)
            if not entry:
                entry = {
                    '_created_ts': time.time(),
                    'chat_id': message.chat.id,
                    'author_id': message.from_user.id,
                    'author_label': author_label,
                    'group_name': group_name,
                    'date_str': date_str,
                    'subject_name': subject_name,
                    'items': [],
                    'timer': None,
                }
                core.media_group_buffer[mg_id] = entry
                set_user_state(message.from_user.id, '', dict(payload or {}, _active_hw_media_group=mg_id))
            entry['items'].append(
                (message.message_id, message.photo[-1].file_id, (message.caption or '').strip())
            )
            if entry.get('timer'):
                entry['timer'].cancel()
            t = threading.Timer(1.3, flush_media_group_hw, args=[mg_id])
            entry['timer'] = t
            t.start()
        return

    clear_user_state(message.chat.id)

    photo_ids = [message.photo[-1].file_id] if message.photo else []
    doc_file_id = message.document.file_id if message.document else ''
    doc_file_name = (message.document.file_name or 'Документ') if message.document else ''
    has_media = bool(photo_ids or doc_file_id)
    hw_text = (message.caption or '').strip() if has_media else (message.text or '').strip()

    if not has_media and not hw_text:
        set_user_state(message.chat.id, 'awaiting_hw_content', {
            'hw_group': group_name,
            'hw_date': date_str,
            'hw_subject': subject_name,
        })
        bot.send_message(
            message.chat.id,
            '⚠️ Пожалуйста, отправь текст задания, фото или документ (можно с подписью). Для отмены напиши «Отмена»:',
        )
        return

    ask_hw_attach_queue(
        message.chat.id,
        message.from_user.id,
        author_label,
        group_name,
        date_str,
        subject_name,
        hw_text,
        photo_ids,
        doc_file_id=doc_file_id,
        doc_file_name=doc_file_name,
    )


def process_hw_edit_text(message, payload):
    hw_id = (payload or {}).get('hw_id')
    group_name = (payload or {}).get('group_name', '')
    clear_user_state(message.chat.id)
    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        bot.send_message(message.chat.id, '❌ Редактирование ДЗ отменено.')
        return

    new_text = message.text.strip()
    _, eff_fac = find_course_and_faculty_by_group(group_name)
    eff_fac = eff_fac or infer_faculty_by_group(group_name)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT target_date, subject_name FROM homeworks WHERE id = ? AND (faculty = ? OR faculty = "") AND group_name = ?',
            (hw_id, eff_fac, group_name),
        )
        row = cursor.fetchone()
        if not row:
            bot.send_message(message.chat.id, '⚠️ Запись не найдена (возможно, она уже была удалена).')
            return
        hw_date, hw_subj = row
        cursor.execute(
            'UPDATE homeworks SET hw_text = ? WHERE id = ? AND (faculty = ? OR faculty = "") AND group_name = ?',
            (new_text, hw_id, eff_fac, group_name),
        )
        cursor.execute(
            'SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1',
            (eff_fac, group_name),
        )
        group_users = cursor.fetchall()

    bot.send_message(
        message.chat.id,
        f'✅ <b>Текст ДЗ обновлён!</b>\n\n'
        f'📅 <b>Дедлайн:</b> {hw_date}\n'
        f'📘 <b>Предмет:</b> {esc(hw_subj)}\n'
        f'📝 <b>Новый текст:</b> {esc(new_text)}',
        parse_mode='HTML',
    )

    notify_text = (
        f'✏️ <b>ОБНОВЛЁН ТЕКСТ ДОМАШНЕГО ЗАДАНИЯ</b>\n'
        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}\n'
        f'📅 <b>Срок сдачи:</b> {hw_date}</blockquote>\n\n'
        f'📘 <b>{esc(hw_subj)}</b>\n'
        f'📝 {esc(new_text)}'
    )
    for (u_id,) in group_users:
        if u_id != message.from_user.id:
            enqueue_message(u_id, notify_text, parse_mode='HTML')
    for g_chat_id in get_bound_group_chats(eff_fac, group_name):
        enqueue_message(g_chat_id, notify_text, parse_mode='HTML')


def open_homework_menu(message):
    user = get_user_data(message.from_user.id)
    if not user or not is_group_valid(user[7], user[0], user[1]):
        start_onboarding(message)
        return

    tomorrow_str = (now_local() + timedelta(days=1)).strftime('%d.%m.%Y')
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(f'⏭ ДЗ на завтра ({tomorrow_str})', callback_data=f'hw_showdate_{tomorrow_str}'),
        InlineKeyboardButton('📆 ДЗ на ближайшую неделю', callback_data='hw_view_week'),
        InlineKeyboardButton('🗓 Все актуальные ДЗ', callback_data='hw_view_all'),
        InlineKeyboardButton('🔍 Выбрать конкретную дату', callback_data='hw_view_manual'),
    )
    if can_manage_hw(message.from_user):
        markup.add(InlineKeyboardButton('➕ Добавить новое ДЗ', callback_data='hw_open_add'))

    bot.send_message(
        message.chat.id,
        f'📚 <b>ДОМАШНИЕ ЗАДАНИЯ И ДЕДЛАЙНЫ</b>\n'
        f'<blockquote>🎓 <b>Группа:</b> {esc(user[1])}</blockquote>\n\n'
        f'Выбери период для просмотра:',
        reply_markup=markup,
        parse_mode='HTML',
    )


def format_user_queue_label(user_obj):
    full_name = ' '.join(
        p for p in [user_obj.first_name, user_obj.last_name] if p
    ).strip()
    if user_obj.username:
        return f'{full_name} (@{user_obj.username})' if full_name else f'@{user_obj.username}'
    return full_name or f'Студент ID {user_obj.id}'


def format_hw_author_label(user_obj):
    if user_obj and getattr(user_obj, 'username', None):
        return f'@{user_obj.username}'
    if user_obj:
        full_name = ' '.join(
            p for p in [user_obj.first_name, user_obj.last_name] if p
        ).strip()
        return full_name or f'ID {user_obj.id}'
    return ''


def can_edit_specific_hw(viewer_user_obj, target_group_name, hw_added_by=None):
    if is_admin_user(viewer_user_obj):
        return True
    if hw_added_by and int(hw_added_by) == int(viewer_user_obj.id):
        return True
    if is_user_starosta(viewer_user_obj):
        _, managed_grp, _ = get_managed_context(viewer_user_obj)
        return bool(target_group_name and managed_grp == target_group_name)
    return False


def get_author_display_by_id(author_id):
    if not author_id:
        return ''
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT username FROM users WHERE user_id = ?', (int(author_id),))
        row = cursor.fetchone()
    if row and row[0]:
        return f'@{row[0]}'
    return f'ID {author_id}'


def is_hw_done_by_user(user_id, hw_id):
    if not user_id or not hw_id:
        return False
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT 1 FROM hw_done WHERE user_id = ? AND hw_id = ?',
            (int(user_id), int(hw_id)),
        )
        return bool(cursor.fetchone())


def render_hw_card_payload(hw_row, viewer_user_obj, target_group_name=None, is_group_chat=False):
    hw_id, hw_date, hw_subj, hw_text, hw_photo, hw_doc_id, hw_doc_name, hw_queue_id = hw_row[:8]
    hw_added_by = hw_row[8] if len(hw_row) > 8 else None
    can_edit = (not is_group_chat) and can_edit_specific_hw(viewer_user_obj, target_group_name, hw_added_by)
    is_done = (not is_group_chat) and bool(viewer_user_obj) and is_hw_done_by_user(viewer_user_obj.id, hw_id)
    q_block = format_queue_slots_block(
        hw_queue_id,
        viewer_user_id=(None if is_group_chat else (viewer_user_obj.id if viewer_user_obj else None)),
    ) if hw_queue_id else ''
    author_disp = get_author_display_by_id(hw_added_by)
    author_line = f'\n👤 <i>Добавил: {esc(author_disp)}</i>' if author_disp else ''
    done_line = '\n✅ <b>Статус: Выполнено</b>' if is_done else ''
    item_text = (
        f'📅 <b>Дедлайн:</b> {hw_date}\n'
        f'📘 <b>{esc(hw_subj)}</b>\n'
        f'📝 {esc(hw_text) if hw_text else "<i>(см. вложение)</i>"}'
        f'{author_line}'
        f'{done_line}'
        f'{q_block}'
    )
    manage_rows = []
    if not is_group_chat:
        if is_done:
            manage_rows.append([
                InlineKeyboardButton('↩️ Снять отметку «Сделано»', callback_data=f'hw_undone_{hw_id}'),
            ])
        else:
            manage_rows.append([
                InlineKeyboardButton('✅ Сделал', callback_data=f'hw_done_{hw_id}'),
            ])
    if can_edit:
        manage_rows.append([
            InlineKeyboardButton('✏️ Изменить текст', callback_data=f'hw_edit_{hw_id}'),
            InlineKeyboardButton('🗑 Удалить ДЗ', callback_data=f'hw_del_{hw_id}'),
        ])

    if hw_queue_id:
        markup = build_queue_slot_buttons(
            hw_queue_id,
            viewer_user_id=(None if is_group_chat else (viewer_user_obj.id if viewer_user_obj else None)),
            can_moderate=can_edit,
            include_manage_buttons=manage_rows,
        )
    elif manage_rows:
        markup = InlineKeyboardMarkup(row_width=2)
        for r in manage_rows:
            markup.row(*r)
    else:
        markup = None
    return item_text, markup


def send_hw_items(chat_id, user_obj, items, title, target_group_name=None):
    if not items:
        bot.send_message(
            chat_id,
            f'📚 <b>{title}</b>\n\n✨ <i>Заданий на выбранный период пока нет!</i>',
            parse_mode='HTML',
        )
        return

    bot.send_message(chat_id, f'📚 <b>{title}</b> (найдено: {len(items)})', parse_mode='HTML')

    for hw_row in items:
        hw_id, hw_date, hw_subj, hw_text, hw_photo, hw_doc_id, hw_doc_name, hw_queue_id = hw_row[:8]
        photo_ids = parse_photo_ids(hw_photo)
        item_text, markup = render_hw_card_payload(
            hw_row, user_obj, target_group_name=target_group_name, is_group_chat=(int(chat_id) < 0)
        )

        try:
            send_hw_media(
                chat_id,
                photo_ids,
                item_text,
                reply_markup=markup,
                doc_file_id=hw_doc_id,
                doc_file_name=hw_doc_name,
            )
        except Exception:
            bot.send_message(
                chat_id,
                item_text + '\n<i>(не удалось загрузить вложение)</i>',
                reply_markup=markup,
                parse_mode='HTML',
            )


def send_homeworks_for_date_view(chat_id, user_obj, group_name, date_str, faculty_code=None):
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by '
            'FROM homeworks '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? ORDER BY id ASC',
            (eff_fac, group_name, date_str),
        )
        items = cursor.fetchall()
    send_hw_items(chat_id, user_obj, items, f'ДЗ на {date_str}', target_group_name=group_name)


def send_homeworks_for_period(chat_id, user_obj, group_name, days_limit=None, faculty_code=None):
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    today = now_local().date()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by '
            'FROM homeworks '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (eff_fac, group_name),
        )
        all_items = cursor.fetchall()

    filtered = []
    for item in all_items:
        try:
            d = datetime.strptime(item[1], '%d.%m.%Y').date()
        except ValueError:
            continue
        if d < today:
            continue
        if days_limit is not None and d > today + timedelta(days=days_limit):
            continue
        filtered.append((d, item))

    filtered.sort(key=lambda x: (x[0], x[1][0]))
    items = [x[1] for x in filtered]
    title = 'ДЗ на ближайшую неделю' if days_limit == 7 else 'Все актуальные ДЗ'
    send_hw_items(chat_id, user_obj, items, title, target_group_name=group_name)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith(('hw_', 'hwadd_', 'hwq_'))
)
def handle_homework_callbacks(call):
    sync_username(call.from_user)
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    if not data.startswith(('hwq_', 'hw_done_', 'hw_undone_')):
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

    if data == 'hw_queue_no':
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        draft = (state or {}).get('pending_hw_draft')
        if not draft:
            bot.send_message(chat_id, '⚠️ Сессия создания ДЗ устарела. Начни заново.')
            return
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        save_and_broadcast_homework(
            chat_id,
            draft['author_id'],
            draft['group_name'],
            draft['date_str'],
            draft['subject_name'],
            draft['hw_text'],
            draft['photo_ids'],
            doc_file_id=draft['doc_file_id'],
            doc_file_name=draft['doc_file_name'],
            queue_slots_count=None,
            author_label=draft.get('author_label') or format_hw_author_label(call.from_user),
        )
        return

    if data == 'hw_queue_yes':
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        draft = (state or {}).get('pending_hw_draft')
        if not draft:
            bot.send_message(chat_id, '⚠️ Сессия создания ДЗ устарела. Начни заново.')
            return
        set_user_state(user_id, 'awaiting_hw_slots_count', state)
        safe_edit_message_text(
            f'🔢 <b>Укажи количество вопросов / слотов для ДЗ по предмету «{esc(draft["subject_name"])}»</b>\n\n'
            f'Введи число от <b>1</b> до <b>60</b> (например, <code>12</code> или <code>20</code>):\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    # --- РАЗБОР И ОСВОБОЖДЕНИЕ ВОПРОСОВ В ДОМАШНЕМ ЗАДАНИИ ---
    if data.startswith('hwq_pick_') or data.startswith('hwq_free_') or data.startswith('hwq_refresh_'):
        user_label = format_user_queue_label(call.from_user)
        cb_alert_text = ''
        cb_show_alert = False
        if data.startswith('hwq_pick_'):
            parts = data.replace('hwq_pick_', '').split('_')
            q_id = int(parts[0])
            chosen_slot = int(parts[1])
            u_data = get_user_data(user_id)
            user_group = u_data[1] if u_data else ''
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT max_slots, is_open, group_name FROM queues WHERE id = ?', (q_id,))
                q_info = cursor.fetchone()
                if not q_info:
                    cb_alert_text = '⚠️ Очередь не найдена.'
                    cb_show_alert = True
                elif not is_admin_user(call.from_user) and user_group != q_info[2]:
                    cb_alert_text = '⛔ Занимать вопросы могут только студенты этой группы!'
                    cb_show_alert = True
                elif q_info[1] == 1 and 1 <= chosen_slot <= q_info[0]:
                    try:
                        cursor.execute(
                            'INSERT INTO queue_slots (queue_id, slot_num, user_id, user_label, joined_at) '
                            'VALUES (?, ?, ?, ?, ?)',
                            (q_id, chosen_slot, user_id, user_label, now_local().strftime('%d.%m.%Y %H:%M')),
                        )
                        cb_alert_text = f'✅ Вопрос №{chosen_slot} закреплён за тобой!'
                    except sqlite3.IntegrityError:
                        cb_alert_text = f'⚠️ Вопрос №{chosen_slot} только что занял кто-то другой!'
                        cb_show_alert = True

        elif data.startswith('hwq_free_'):
            parts = data.replace('hwq_free_', '').split('_')
            q_id = int(parts[0])
            target_slot = int(parts[1])
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT group_name, created_by FROM queues WHERE id = ?', (q_id,))
                q_row = cursor.fetchone()
                q_grp = q_row[0] if q_row else ''
                q_creator = q_row[1] if q_row else None
                is_mod = can_edit_specific_hw(call.from_user, q_grp, q_creator)
                if is_mod:
                    cursor.execute(
                        'DELETE FROM queue_slots WHERE queue_id = ? AND slot_num = ?',
                        (q_id, target_slot),
                    )
                else:
                    cursor.execute(
                        'DELETE FROM queue_slots WHERE queue_id = ? AND slot_num = ? AND user_id = ?',
                        (q_id, target_slot, user_id),
                    )
                if cursor.rowcount > 0:
                    cb_alert_text = f'Слот №{target_slot} освобождён'
                else:
                    cb_alert_text = f'⚠️ Слот №{target_slot} уже пуст или занят другим студентом.'
                    cb_show_alert = True
        else:
            q_id = int(data.replace('hwq_refresh_', ''))
            cb_alert_text = 'Список обновлён'

        try:
            bot.answer_callback_query(call.id, text=cb_alert_text or None, show_alert=cb_show_alert)
        except Exception:
            pass

        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by, group_name '
                'FROM homeworks WHERE queue_id = ?',
                (q_id,),
            )
            hw_full = cursor.fetchone()

        if hw_full:
            hw_row = hw_full[:9]
            grp_name = hw_full[9]
            new_text, new_markup = render_hw_card_payload(
                hw_row, call.from_user, target_group_name=grp_name, is_group_chat=(int(chat_id) < 0)
            )
            try:
                if call.message.content_type in ('photo', 'document'):
                    if len(new_text) <= 1024:
                        bot.edit_message_caption(
                            caption=new_text,
                            chat_id=chat_id,
                            message_id=msg_id,
                            reply_markup=new_markup,
                            parse_mode='HTML',
                        )
                    else:
                        try:
                            bot.edit_message_reply_markup(
                                chat_id=chat_id,
                                message_id=msg_id,
                                reply_markup=None,
                            )
                        except Exception:
                            pass
                        send_long_html_message(
                            chat_id,
                            new_text,
                            reply_markup=new_markup,
                            parse_mode='HTML',
                        )
                else:
                    if len(new_text) <= 4096:
                        bot.edit_message_text(
                            new_text,
                            chat_id=chat_id,
                            message_id=msg_id,
                            reply_markup=new_markup,
                            parse_mode='HTML',
                        )
                    else:
                        try:
                            bot.edit_message_reply_markup(
                                chat_id=chat_id,
                                message_id=msg_id,
                                reply_markup=None,
                            )
                        except Exception:
                            pass
                        send_long_html_message(
                            chat_id,
                            new_text,
                            reply_markup=new_markup,
                            parse_mode='HTML',
                        )
            except ApiTelegramException as e_edit:
                if 'message is not modified' not in str(e_edit).lower():
                    try:
                        send_long_html_message(
                            chat_id,
                            new_text,
                            reply_markup=new_markup,
                            parse_mode='HTML',
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        return

    if data == 'hw_open_add':
        open_add_homework(chat_id, call.from_user, msg_id)
        return

    if data == 'hwadd_bydate_menu':
        if not can_manage_hw(call.from_user):
            return
        open_add_homework_by_date(chat_id, call.from_user, msg_id)
        return

    if data.startswith('hwadd_picksubj_'):
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        entries = (state or {}).get('hw_subj_entries', [])
        idx = int(data.replace('hwadd_picksubj_', ''))
        if idx >= len(entries):
            return
        ent = entries[idx]
        group_name = state.get('hw_group', '')
        course = state.get('hw_course') or find_course_by_group(group_name)
        subj_name = ent['subject']
        up_dates = ent.get('upcoming_dates') or []
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        if up_dates:
            ask_hw_content(chat_id, group_name, up_dates[0], subj_name, upcoming_dates=up_dates)
        else:
            today = now_local()
            d1 = (today + timedelta(days=1)).strftime('%d.%m.%Y')
            d2 = (today + timedelta(days=2)).strftime('%d.%m.%Y')
            d7 = (today + timedelta(days=7)).strftime('%d.%m.%Y')
            set_user_state(user_id, '', {
                'hw_group': group_name,
                'hw_course': course,
                'hw_pending_custom_subj': subj_name,
            })
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(
                InlineKeyboardButton(f'⏭ На завтра ({d1})', callback_data=f'hwadd_custdate_{d1}'),
                InlineKeyboardButton(f'⏩ На послезавтра ({d2})', callback_data=f'hwadd_custdate_{d2}'),
                InlineKeyboardButton(f'🗓 Через неделю ({d7})', callback_data=f'hwadd_custdate_{d7}'),
                InlineKeyboardButton('⌨️ Ввести дату вручную', callback_data='hwadd_changedate_manual_new'),
                InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
            )
            bot.send_message(
                chat_id,
                f'📘 <b>Предмет:</b> {esc(subj_name)}\n\n'
                f'⚠️ Ближайших пар по этому предмету в расписании не нашлось. Выбери дату, к которой прикрепить ДЗ:',
                reply_markup=markup,
                parse_mode='HTML',
            )
        return

    if data.startswith('hwadd_setdate_'):
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not state or 'hw_subject' not in state:
            return
        new_date_str = data.replace('hwadd_setdate_', '')
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        ask_hw_content(
            chat_id,
            state['hw_group'],
            new_date_str,
            state['hw_subject'],
            upcoming_dates=state.get('hw_upcoming_dates', []),
        )
        return

    if data == 'hwadd_changedate_manual':
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not state or 'hw_subject' not in state:
            return
        group_name = state['hw_group']
        course = find_course_by_group(group_name)
        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_manual_date', {
            'action': 'hw_change_date',
            'course': course,
            'group_name': group_name,
            'hw_subject': state['hw_subject'],
            'hw_upcoming_dates': state.get('hw_upcoming_dates', []),
        })
        safe_edit_message_text(
            f'⌨️ Введи нужную дату для предмета <b>«{esc(state["hw_subject"])}»</b> в формате <code>ДД.ММ.ГГГГ</code> '
            f'(например, <code>{(now_local() + timedelta(days=1)).strftime("%d.%m.%Y")}</code>)\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data == 'hwadd_changedate_manual_new':
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not state or 'hw_pending_custom_subj' not in state:
            return
        group_name = state['hw_group']
        course = state.get('hw_course') or find_course_by_group(group_name)
        subj_name = state['hw_pending_custom_subj']
        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_manual_date', {
            'action': 'hw_change_date',
            'course': course,
            'group_name': group_name,
            'hw_subject': subj_name,
            'hw_upcoming_dates': [],
        })
        safe_edit_message_text(
            f'⌨️ Введи дату для предмета <b>«{esc(subj_name)}»</b> в формате <code>ДД.ММ.ГГГГ</code>:\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data.startswith('hwadd_custdate_'):
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not state or 'hw_pending_custom_subj' not in state:
            return
        date_str = data.replace('hwadd_custdate_', '')
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        ask_hw_content(chat_id, state['hw_group'], date_str, state['hw_pending_custom_subj'])
        return

    if data.startswith('hwadd_date_'):
        if not can_manage_hw(call.from_user):
            return
        course, group_name, _ = get_managed_context(call.from_user)
        if not group_name:
            return
        date_str = data.replace('hwadd_date_', '')
        show_hw_subject_selection(chat_id, msg_id, course, group_name, date_str)
        return

    if data == 'hwadd_datemanual':
        if not can_manage_hw(call.from_user):
            return
        course, group_name, _ = get_managed_context(call.from_user)
        if not group_name:
            return
        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_manual_date', {
            'action': 'hw_add',
            'course': course,
            'group_name': group_name,
        })
        safe_edit_message_text(
            f'⌨️ Введи дату дедлайна в формате <code>ДД.ММ.ГГГГ</code> (например, <code>{(now_local() + timedelta(days=1)).strftime("%d.%m.%Y")}</code>)\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data == 'hwadd_allsubj':
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not state:
            return
        show_hw_subject_selection(
            chat_id,
            msg_id,
            state['hw_course'],
            state['hw_group'],
            state['hw_date'],
            show_all=True,
        )
        return

    if data == 'hwadd_subjmanual':
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not state:
            return
        clear_user_state(user_id, keep_payload=True)
        set_user_state(user_id, 'awaiting_hw_manual_subj', state)
        safe_edit_message_text(
            f'✍️ Введи название предмета для ДЗ на <b>{state["hw_date"]}</b>:\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data.startswith('hwadd_subj_'):
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not state:
            return
        idx = int(data.replace('hwadd_subj_', ''))
        subjects = state.get('hw_subjects', [])
        if idx >= len(subjects):
            return
        subj_name = subjects[idx]
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        ask_hw_content(chat_id, state['hw_group'], state['hw_date'], subj_name)
        return

    if data.startswith('hw_showdate_'):
        user = get_user_data(user_id)
        if not user or not is_group_valid(user[7], user[0], user[1]):
            bot.send_message(chat_id, '👇 Сначала выбери свой <b>факультет</b> и группу:', reply_markup=build_faculty_keyboard(), parse_mode='HTML')
            return
        date_str = data.replace('hw_showdate_', '')
        send_homeworks_for_date_view(chat_id, call.from_user, user[1], date_str, faculty_code=user[7])
        return

    if data == 'hw_view_week':
        user = get_user_data(user_id)
        if not user or not is_group_valid(user[7], user[0], user[1]):
            bot.send_message(chat_id, '👇 Сначала выбери свой <b>факультет</b> и группу:', reply_markup=build_faculty_keyboard(), parse_mode='HTML')
            return
        send_homeworks_for_period(chat_id, call.from_user, user[1], days_limit=7, faculty_code=user[7])
        return

    if data == 'hw_view_all':
        user = get_user_data(user_id)
        if not user or not is_group_valid(user[7], user[0], user[1]):
            bot.send_message(chat_id, '👇 Сначала выбери свой <b>факультет</b> и группу:', reply_markup=build_faculty_keyboard(), parse_mode='HTML')
            return
        send_homeworks_for_period(chat_id, call.from_user, user[1], days_limit=None, faculty_code=user[7])
        return

    if data == 'hw_view_manual':
        user = get_user_data(user_id)
        if not user or not is_group_valid(user[7], user[0], user[1]):
            bot.send_message(chat_id, '👇 Сначала выбери свой <b>факультет</b> и группу:', reply_markup=build_faculty_keyboard(), parse_mode='HTML')
            return
        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_manual_date', {
            'action': 'hw_view',
            'course': user[0],
            'group_name': user[1],
        })
        bot.send_message(
            chat_id,
            f'🔍 Введи дату для просмотра ДЗ в формате <code>ДД.ММ.ГГГГ</code> (например, <code>{(now_local() + timedelta(days=1)).strftime("%d.%m.%Y")}</code>)\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            parse_mode='HTML',
        )
        return

    if data.startswith('hw_edit_'):
        if not can_manage_hw(call.from_user):
            return
        hw_id = int(data.replace('hw_edit_', ''))

        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT target_date, subject_name, hw_text, group_name, added_by FROM homeworks '
                'WHERE id = ?',
                (hw_id,),
            )
            hw_row_raw = cursor.fetchone()

        if not hw_row_raw:
            bot.send_message(chat_id, '⛔ Запись не найдена.')
            return
        hw_date, hw_subj, old_text, group_name, hw_added_by = hw_row_raw
        if not can_edit_specific_hw(call.from_user, group_name, hw_added_by):
            bot.send_message(chat_id, '⛔ Редактировать ДЗ может только его автор, староста группы или администратор.')
            return

        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_hw_edit', {'hw_id': hw_id, 'group_name': group_name})
        bot.send_message(
            chat_id,
            f'✏️ <b>РЕДАКТИРОВАНИЕ ТЕКСТА ДЗ</b>\n'
            f'<blockquote>📅 <b>Дедлайн:</b> {hw_date}\n'
            f'📘 <b>Предмет:</b> {esc(hw_subj)}</blockquote>\n\n'
            f'<b>Текущий текст:</b>\n<code>{esc(old_text or "(пусто)")}</code>\n\n'
            f'Отправь новый текст задания (все прикреплённые фото и документы сохранятся).\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            parse_mode='HTML',
        )
        return

    if data.startswith('hw_done_') or data.startswith('hw_undone_'):
        is_mark_done = data.startswith('hw_done_')
        hw_id = int(data.replace('hw_done_', '') if is_mark_done else data.replace('hw_undone_', ''))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by, group_name '
                'FROM homeworks WHERE id = ?',
                (hw_id,),
            )
            hw_full = cursor.fetchone()
            if not hw_full:
                try:
                    bot.answer_callback_query(call.id, '⚠️ Задание уже удалено', show_alert=True)
                except Exception:
                    pass
                return
            if is_mark_done:
                cursor.execute(
                    'INSERT OR REPLACE INTO hw_done (user_id, hw_id, done_at) VALUES (?, ?, ?)',
                    (user_id, hw_id, now_local().strftime('%d.%m.%Y %H:%M')),
                )
                alert_msg = '✅ Отмечено как выполненное!'
            else:
                cursor.execute(
                    'DELETE FROM hw_done WHERE user_id = ? AND hw_id = ?',
                    (user_id, hw_id),
                )
                alert_msg = '↩️ Отметка «Сделано» снята'

        try:
            bot.answer_callback_query(call.id, alert_msg)
        except Exception:
            pass

        hw_row = hw_full[:9]
        grp_name = hw_full[9]
        new_text, new_markup = render_hw_card_payload(
            hw_row, call.from_user, target_group_name=grp_name, is_group_chat=(int(chat_id) < 0)
        )
        try:
            if call.message.content_type in ('photo', 'document'):
                if len(new_text) <= 1024:
                    bot.edit_message_caption(
                        caption=new_text,
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=new_markup,
                        parse_mode='HTML',
                    )
            else:
                if len(new_text) <= 4096:
                    bot.edit_message_text(
                        new_text,
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=new_markup,
                        parse_mode='HTML',
                    )
        except Exception:
            pass
        return

    if data.startswith('hw_del_'):
        if not can_manage_hw(call.from_user):
            return
        hw_id = int(data.replace('hw_del_', ''))

        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT target_date, subject_name, hw_text, queue_id, group_name, added_by FROM homeworks '
                'WHERE id = ?',
                (hw_id,),
            )
            hw_row_raw = cursor.fetchone()
            if not hw_row_raw:
                bot.send_message(chat_id, '⛔ Запись не найдена.')
                return
            hw_date, hw_subj, hw_text, linked_q_id, group_name, hw_added_by = hw_row_raw
            if not can_edit_specific_hw(call.from_user, group_name, hw_added_by):
                bot.send_message(chat_id, '⛔ Удалить ДЗ может только его автор, староста группы или администратор.')
                return

            _, eff_del_fac = find_course_and_faculty_by_group(group_name)
            eff_del_fac = eff_del_fac or infer_faculty_by_group(group_name)
            if linked_q_id:
                cursor.execute('DELETE FROM queue_slots WHERE queue_id = ?', (linked_q_id,))
                cursor.execute('DELETE FROM queues WHERE id = ? AND (faculty = ? OR faculty = "")', (linked_q_id, eff_del_fac))

            cursor.execute('DELETE FROM hw_done WHERE hw_id = ?', (hw_id,))
            cursor.execute(
                'DELETE FROM homeworks WHERE id = ? AND (faculty = ? OR faculty = "") AND group_name = ?',
                (hw_id, eff_del_fac, group_name),
            )
            cursor.execute(
                'SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1',
                (eff_del_fac, group_name),
            )
            group_users = cursor.fetchall()

        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        bot.send_message(chat_id, '🗑 Домашнее задание удалено.')

        notify_text = (
            f'🗑 <b>ДОМАШНЕЕ ЗАДАНИЕ УДАЛЕНО</b>\n'
            f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}\n'
            f'📅 <b>Дедлайн был:</b> {hw_date}</blockquote>\n\n'
            f'📘 <b>{esc(hw_subj)}</b>\n'
            f'<s>{esc(hw_text) if hw_text else "(вложение)"}</s>'
        )
        for (u_id,) in group_users:
            if u_id != user_id:
                enqueue_message(u_id, notify_text, parse_mode='HTML')
        for g_chat_id in get_bound_group_chats(eff_del_fac, group_name):
            enqueue_message(g_chat_id, notify_text, parse_mode='HTML')
        return
