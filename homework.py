from datetime import datetime, timedelta
import re
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
    get_group_chat_binding,
    get_managed_context,
    get_user_data,
    get_user_state,
    infer_faculty_by_group,
    is_admin_user,
    is_extra_hw_lists_enabled,
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
from schedule_search import get_all_group_subjects, get_day_lessons_for_date, is_homework_passed_today


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


def format_queue_slots_block(queue_id, viewer_user_id=None, header_title=None, is_secondary=False):
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

    disp_name = _resolve_queue_display_name(raw_title, is_secondary=is_secondary)
    if header_title is None:
        icon = '📑' if is_secondary else '📋'
        header_title = f'{icon} {disp_name}:'

    taken_by_num = {s_num: (u_id, u_lbl) for s_num, u_id, u_lbl in slots}
    free_nums = [num for num in range(1, max_slots + 1) if num not in taken_by_num]

    header = f'\n\n<b>{esc(header_title)}</b> <i>({len(slots)}/{max_slots})</i>'
    taken_lines = []
    for num in sorted(taken_by_num.keys()):
        u_id, u_lbl = taken_by_num[num]
        me_mark = ' ★' if (viewer_user_id and u_id == viewer_user_id) else ''
        taken_lines.append(f'<b>{num}.</b> {_format_user_link_html(u_lbl)}{me_mark}')

    parts = [header]
    if taken_lines:
        body_str = '\n'.join(taken_lines)
        if len(taken_lines) > 6:
            parts.append(f'<blockquote expandable>{body_str}</blockquote>')
        else:
            parts.append(body_str)

    if free_nums:
        free_str = ', '.join(str(n) for n in free_nums)
        parts.append(f'<i>Свободно: {free_str}</i>')
    else:
        parts.append('<i>Все вопросы разобраны!</i>')
    return '\n'.join(parts)


def _resolve_queue_display_name(raw_title, is_secondary=False):
    t = str(raw_title or '').strip()
    if not t or '(ДЗ на ' in t:
        if 'Аннотации' in t or is_secondary:
            return 'Аннотации'
        return 'Вопросы'
    return t


def _queue_button_prefix(display_name):
    for ch in str(display_name or '').strip():
        if ch.isalpha():
            return ch.upper()
    return 'Б'


def _load_queue_state(q_id, is_secondary=False):
    if not q_id:
        return None
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT max_slots, is_open, title FROM queues WHERE id = ?', (q_id,))
        q_row = cursor.fetchone()
        if not q_row:
            return None
        max_slots, is_open, raw_title = q_row
        cursor.execute(
            'SELECT slot_num, user_id, user_label FROM queue_slots WHERE queue_id = ? ORDER BY slot_num ASC',
            (q_id,),
        )
        slots = cursor.fetchall()
    taken_map = {s_num: (u_id, u_lbl) for s_num, u_id, u_lbl in slots}
    disp_name = _resolve_queue_display_name(raw_title, is_secondary=is_secondary)
    return {
        'id': q_id,
        'max_slots': max_slots,
        'is_open': is_open,
        'title': disp_name,
        'prefix': _queue_button_prefix(disp_name),
        'taken_map': taken_map,
    }


def build_queue_slot_buttons(
    queue_id,
    viewer_user_id=None,
    can_moderate=False,
    include_manage_buttons=None,
    mod_mode=False,
    hw_id=None,
    is_done=None,
    annot_queue_id=None,
):
    q_state = _load_queue_state(queue_id, is_secondary=False)
    a_state = _load_queue_state(annot_queue_id, is_secondary=True)
    if not q_state and not a_state:
        return include_manage_buttons

    primary_q_id = queue_id or annot_queue_id
    markup = InlineKeyboardMarkup(row_width=6)

    if mod_mode and can_moderate:
        mod_btns = []
        if q_state:
            for n in sorted(q_state['taken_map'].keys())[:30]:
                _, owner_lbl = q_state['taken_map'][n]
                short_owner = short_text(_clean_slot_display_label(owner_lbl), 11)
                mod_btns.append(
                    InlineKeyboardButton(f'🧹 №{n} ({short_owner})', callback_data=f'hwq_free_{q_state["id"]}_{n}')
                )
        if a_state:
            for n in sorted(a_state['taken_map'].keys())[:30]:
                _, owner_lbl = a_state['taken_map'][n]
                short_owner = short_text(_clean_slot_display_label(owner_lbl), 11)
                mod_btns.append(
                    InlineKeyboardButton(f'🧹 {a_state["prefix"]}{n} ({short_owner})', callback_data=f'hwq_free_{a_state["id"]}_{n}')
                )
        for i in range(0, len(mod_btns), 2):
            markup.row(*mod_btns[i : i + 2])
        markup.row(InlineKeyboardButton('🔙 Назад', callback_data=f'hwq_refresh_{primary_q_id}'))
        return markup

    any_taken = False
    if q_state and q_state['is_open']:
        taken_map = q_state['taken_map']
        if taken_map:
            any_taken = True
        free_nums = [n for n in range(1, q_state['max_slots'] + 1) if n not in taken_map]
        if free_nums:
            slot_btns = [
                InlineKeyboardButton(f'{n}', callback_data=f'hwq_pick_{q_state["id"]}_{n}')
                for n in free_nums[:36]
            ]
            for i in range(0, len(slot_btns), 6):
                markup.row(*slot_btns[i : i + 6])

    if a_state and a_state['is_open']:
        a_taken = a_state['taken_map']
        if a_taken:
            any_taken = True
        a_free = [n for n in range(1, a_state['max_slots'] + 1) if n not in a_taken]
        if a_free:
            a_btns = [
                InlineKeyboardButton(f'{a_state["prefix"]}{n}', callback_data=f'hwq_pick_{a_state["id"]}_{n}')
                for n in a_free[:36]
            ]
            for i in range(0, len(a_btns), 6):
                markup.row(*a_btns[i : i + 6])

    # Личные кнопки снятия
    rem_btns = []
    if q_state and viewer_user_id:
        for n, (u_id, _) in sorted(q_state['taken_map'].items()):
            if u_id == viewer_user_id:
                rem_btns.append(InlineKeyboardButton(f'❌ №{n}', callback_data=f'hwq_free_{q_state["id"]}_{n}'))
    if a_state and viewer_user_id:
        for n, (u_id, _) in sorted(a_state['taken_map'].items()):
            if u_id == viewer_user_id:
                rem_btns.append(InlineKeyboardButton(f'❌ {a_state["prefix"]}{n}', callback_data=f'hwq_free_{a_state["id"]}_{n}'))
    for i in range(0, len(rem_btns[:16]), 4):
        markup.row(*rem_btns[i : i + 4])

    # Компактная нижняя панель управления в 1 строку
    ctrl_row = []
    if hw_id is not None and is_done is not None:
        if is_done:
            ctrl_row.append(InlineKeyboardButton('↩️ Не сделано', callback_data=f'hw_undone_{hw_id}'))
        else:
            ctrl_row.append(InlineKeyboardButton('✅ Сделал', callback_data=f'hw_done_{hw_id}'))
    ctrl_row.append(InlineKeyboardButton('🔄', callback_data=f'hwq_refresh_{primary_q_id}'))
    if can_moderate and hw_id is not None:
        ctrl_row.append(InlineKeyboardButton('⚙️ Упр.', callback_data=f'hw_manage_{hw_id}'))
    elif can_moderate and any_taken:
        ctrl_row.append(InlineKeyboardButton('🧹 Слоты', callback_data=f'hwq_modmenu_{primary_q_id}'))
    markup.row(*ctrl_row)

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
    _, fac_code = find_course_and_faculty_by_group(group_name)
    extra_on = is_extra_hw_lists_enabled(fac_code, group_name)
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
        },
        'hw_slot_lists': [],
        'extra_lists_enabled': extra_on,
    })
    markup = InlineKeyboardMarkup(row_width=2)
    if extra_on:
        markup.row(
            InlineKeyboardButton('📋 Вопросы', callback_data='hw_list_preset_Вопросы'),
            InlineKeyboardButton('✍️ Свой список', callback_data='hw_list_custom'),
        )
    else:
        markup.row(InlineKeyboardButton('📋 Добавить вопросы', callback_data='hw_list_preset_Вопросы'))
    markup.row(
        InlineKeyboardButton('⏭ Без слотов', callback_data='hw_queue_no'),
        InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
    )
    bot.send_message(
        chat_id,
        f'📋 <b>Шаг 3/3 • Распределение</b>\n'
        f'📘 <b>{esc(subject_name)}</b>\n\n'
        f'Добавить разбор вопросов?' if not extra_on else
        f'📋 <b>Шаг 3/3 • Распределение</b>\n'
        f'📘 <b>{esc(subject_name)}</b>\n\n'
        f'Добавить список для разбора?',
        reply_markup=markup,
        parse_mode='HTML',
    )


def _prompt_hw_slot_name(chat_id, msg_id=None, list_num=1):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton('📑 Аннотации', callback_data='hw_list_preset_Аннотации'),
        InlineKeyboardButton('🛠 Практика', callback_data='hw_list_preset_Практика'),
    )
    markup.row(
        InlineKeyboardButton('👥 Команды', callback_data='hw_list_preset_Команды'),
        InlineKeyboardButton('🎤 Доклады', callback_data='hw_list_preset_Доклады'),
    )
    markup.row(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))
    txt = (
        f'✍️ <b>Список №{list_num} — название</b>\n\n'
        f'Выбери готовый вариант или напиши своё название <i>(до 25 символов)</i>:'
    )
    if msg_id:
        safe_edit_message_text(txt, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, txt, reply_markup=markup, parse_mode='HTML')


def process_hw_slot_name_input(message, payload):
    clear_user_state(message.chat.id, keep_payload=True)
    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        clear_user_state(message.chat.id)
        bot.send_message(message.chat.id, '❌ Добавление ДЗ отменено.')
        return

    name = short_text(message.text.strip(), 25)
    new_state = dict(payload or {})
    new_state['current_list_name'] = name
    set_user_state(message.chat.id, 'awaiting_hw_slots_count', new_state)
    bot.send_message(
        message.chat.id,
        f'📌 <b>{esc(name)}</b> — введи количество слотов (1–60):',
        parse_mode='HTML',
    )


def _finalize_hw_draft(chat_id, user_obj, state):
    draft = (state or {}).get('pending_hw_draft')
    if not draft:
        clear_user_state(chat_id)
        bot.send_message(chat_id, '⚠️ Сессия создания ДЗ устарела. Начни заново.')
        return
    slot_lists = (state or {}).get('hw_slot_lists') or []
    q1 = slot_lists[0] if len(slot_lists) >= 1 else None
    q2 = slot_lists[1] if len(slot_lists) >= 2 else None
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
        queue_slots_count=(q1['count'] if q1 else None),
        queue_list_name=(q1['name'] if q1 else 'Вопросы'),
        annot_slots_count=(q2['count'] if q2 else None),
        annot_list_name=(q2['name'] if q2 else 'Аннотации'),
        author_label=draft.get('author_label') or format_hw_author_label(user_obj),
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
            '⚠️ Введи целое число от <b>1</b> до <b>60</b> (например, <code>10</code>):',
            parse_mode='HTML',
        )
        return

    cnt = int(raw)
    new_state = dict(payload or {})
    slot_lists = list(new_state.get('hw_slot_lists') or [])
    list_name = new_state.get('current_list_name') or 'Вопросы'
    slot_lists.append({'name': list_name, 'count': cnt})
    new_state['hw_slot_lists'] = slot_lists
    new_state.pop('current_list_name', None)

    if len(slot_lists) >= 2 or not new_state.get('extra_lists_enabled'):
        _finalize_hw_draft(message.chat.id, message.from_user, new_state)
        return

    set_user_state(message.chat.id, '', new_state)
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton('🚀 Опубликовать ДЗ', callback_data='hw_publish_now'),
        InlineKeyboardButton('➕ Добавить 2-й список (аннотации, практика…)', callback_data='hw_list_custom'),
        InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
    )
    bot.send_message(
        message.chat.id,
        f'✅ <b>Список 1:</b> {esc(list_name)} ({cnt} шт.)\n\n'
        f'Опубликовать сейчас или добавить второй список?',
        reply_markup=markup,
        parse_mode='HTML',
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
    queue_list_name='Вопросы',
    annot_slots_count=None,
    annot_list_name='Аннотации',
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
        annot_queue_id = None

        if queue_slots_count and queue_slots_count > 0:
            q_title = str(queue_list_name or 'Вопросы').strip()
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

        if annot_slots_count and annot_slots_count > 0:
            a_title = str(annot_list_name or 'Аннотации').strip()
            cursor.execute(
                'INSERT INTO queues (faculty, group_name, title, max_slots, is_open, homework_id, created_by, created_at) '
                'VALUES (?, ?, ?, ?, 1, ?, ?, ?)',
                (eff_fac, group_name, a_title, annot_slots_count, hw_id, author_id, created_stamp),
            )
            annot_queue_id = cursor.lastrowid
            cursor.execute(
                'UPDATE homeworks SET annot_queue_id = ? WHERE id = ?',
                (annot_queue_id, hw_id),
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

    q_block_author = (
        (format_queue_slots_block(queue_id, viewer_user_id=author_id, is_secondary=False) if queue_id else '')
        + (format_queue_slots_block(annot_queue_id, viewer_user_id=author_id, is_secondary=True) if annot_queue_id else '')
    )
    author_markup = (
        build_queue_slot_buttons(queue_id, viewer_user_id=author_id, can_moderate=True, hw_id=hw_id, annot_queue_id=annot_queue_id)
        if (queue_id or annot_queue_id)
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
            q_block_user = (
                (format_queue_slots_block(queue_id, viewer_user_id=u_id, is_secondary=False) if queue_id else '')
                + (format_queue_slots_block(annot_queue_id, viewer_user_id=u_id, is_secondary=True) if annot_queue_id else '')
            )
            user_markup = (
                build_queue_slot_buttons(queue_id, viewer_user_id=u_id, can_moderate=is_u_mod, hw_id=hw_id, annot_queue_id=annot_queue_id)
                if (queue_id or annot_queue_id)
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
        f'👤 <b>Добавил:</b> {_format_user_link_html(author_label)}</blockquote>\n\n'
        f'📘 <b>{esc(subject_name)}</b>\n'
        f'📝 {esc(hw_text) if hw_text else "<i>(см. прикреплённое вложение)</i>"}'
        f'{format_queue_slots_block(queue_id, is_secondary=False) if queue_id else ""}'
        f'{format_queue_slots_block(annot_queue_id, is_secondary=True) if annot_queue_id else ""}'
    )
    group_markup = (
        build_queue_slot_buttons(queue_id, viewer_user_id=None, can_moderate=False, annot_queue_id=annot_queue_id)
        if (queue_id or annot_queue_id)
        else None
    )
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

    vk_hw_plain = (
        f'📚 НОВОЕ ДОМАШНЕЕ ЗАДАНИЕ!\n'
        f'🎓 Группа: {group_name} • Дедлайн: {date_str}\n'
        f'📘 {subject_name}\n'
        f'📝 {hw_text if hw_text else "(см. вложение в разделе «📚 Домашка»)"}'
    )
    core.notify_vk_group(eff_fac, group_name, vk_hw_plain)


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
    core.notify_vk_group(
        eff_fac,
        group_name,
        f'✏️ ОБНОВЛЁН ТЕКСТ ДЗ\n🎓 Группа: {group_name} • Срок сдачи: {hw_date}\n📘 {hw_subj}\n📝 {new_text}',
    )


def _build_homework_menu_payload(user_obj, group_name):
    today_dt = now_local()
    today_str = today_dt.strftime('%d.%m.%Y')
    tomorrow_str = (today_dt + timedelta(days=1)).strftime('%d.%m.%Y')

    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton('📍 Сегодня', callback_data=f'hw_showdate_{today_str}'),
        InlineKeyboardButton('⏭ Завтра', callback_data=f'hw_showdate_{tomorrow_str}'),
    )
    markup.row(
        InlineKeyboardButton('📆 Неделя', callback_data='hw_view_week'),
        InlineKeyboardButton('🗓 Все ДЗ', callback_data='hw_view_all'),
    )
    if can_manage_hw(user_obj):
        markup.row(
            InlineKeyboardButton('🔍 По дате', callback_data='hw_view_manual'),
            InlineKeyboardButton('➕ Добавить ДЗ', callback_data='hw_open_add'),
        )
    else:
        markup.row(InlineKeyboardButton('🔍 Выбрать дату', callback_data='hw_view_manual'))

    text = f'📚 <b>Домашка</b> • <i>{esc(group_name)}</i>'
    return text, markup


def open_homework_menu(message):
    user = get_user_data(message.from_user.id)
    if not user or not is_group_valid(user[7], user[0], user[1]):
        start_onboarding(message)
        return

    text, markup = _build_homework_menu_payload(message.from_user, user[1])
    bot.send_message(
        message.chat.id,
        text,
        reply_markup=markup,
        parse_mode='HTML',
    )


def _clean_slot_display_label(raw_label):
    lbl = str(raw_label or '').strip()
    m = re.search(r'@([A-Za-z0-9_]{3,})', lbl)
    if m:
        return m.group(1)
    return short_text(lbl, 20)


def _format_user_link_html(raw_label):
    lbl = str(raw_label or '').strip()
    m = re.search(r'@([A-Za-z0-9_]{3,})', lbl)
    if m:
        uname = m.group(1)
        return f'<a href="https://t.me/{esc(uname)}">{esc(uname)}</a>'
    return esc(short_text(lbl, 20))


def format_user_queue_label(user_obj):
    if user_obj.username:
        return f'@{user_obj.username}'
    full_name = ' '.join(
        p for p in [user_obj.first_name, user_obj.last_name] if p
    ).strip()
    return short_text(full_name, 20) if full_name else f'Студент ID {user_obj.id}'


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
    aid = int(author_id)
    with db_connection() as conn:
        cursor = conn.cursor()
        if aid < 0:
            vk_id = abs(aid)
            cursor.execute('SELECT full_name FROM vk_users WHERE vk_user_id = ?', (vk_id,))
            vrow = cursor.fetchone()
            if vrow and vrow[0]:
                return f'{vrow[0]} (VK)'
            return f'id{vk_id} (VK)'
        cursor.execute('SELECT username FROM users WHERE user_id = ?', (aid,))
        row = cursor.fetchone()
    if row and row[0]:
        return f'@{row[0]}'
    return f'ID {aid}'


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
    hw_annot_queue_id = hw_row[9] if len(hw_row) > 9 else None
    can_edit = (not is_group_chat) and can_edit_specific_hw(viewer_user_obj, target_group_name, hw_added_by)
    is_done = (not is_group_chat) and bool(viewer_user_obj) and is_hw_done_by_user(viewer_user_obj.id, hw_id)
    v_uid = None if is_group_chat else (viewer_user_obj.id if viewer_user_obj else None)
    q_block = (
        (format_queue_slots_block(hw_queue_id, viewer_user_id=v_uid, is_secondary=False) if hw_queue_id else '')
        + (format_queue_slots_block(hw_annot_queue_id, viewer_user_id=v_uid, is_secondary=True) if hw_annot_queue_id else '')
    )
    short_date = hw_date[:5] if len(str(hw_date or '')) >= 5 else hw_date
    done_badge = ' ✅' if is_done else ''
    author_disp = get_author_display_by_id(hw_added_by)
    author_line = f'\n👤 <i>{_format_user_link_html(author_disp)}</i>' if author_disp else ''
    hw_body_line = f'\n📝 {esc(hw_text)}' if hw_text else ''
    item_text = (
        f'📘 <b>{esc(hw_subj)}</b> • <code>{esc(short_date)}</code>{done_badge}'
        f'{hw_body_line}'
        f'{author_line}'
        f'{q_block}'
    )
    if hw_queue_id or hw_annot_queue_id:
        markup = build_queue_slot_buttons(
            hw_queue_id,
            viewer_user_id=v_uid,
            can_moderate=can_edit,
            hw_id=hw_id,
            is_done=(None if is_group_chat else is_done),
            annot_queue_id=hw_annot_queue_id,
        )
    else:
        bottom_row = []
        if not is_group_chat:
            if is_done:
                bottom_row.append(InlineKeyboardButton('↩️ Не сделано', callback_data=f'hw_undone_{hw_id}'))
            else:
                bottom_row.append(InlineKeyboardButton('✅ Сделал', callback_data=f'hw_done_{hw_id}'))
        if can_edit:
            bottom_row.append(InlineKeyboardButton('⚙️ Упр.', callback_data=f'hw_manage_{hw_id}'))
        if bottom_row:
            markup = InlineKeyboardMarkup(row_width=2)
            markup.row(*bottom_row)
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
                hw_id=hw_id,
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
            'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by, annot_queue_id '
            'FROM homeworks '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? ORDER BY id ASC',
            (eff_fac, group_name, date_str),
        )
        items = cursor.fetchall()
    send_hw_items(chat_id, user_obj, items, f'ДЗ на {date_str}', target_group_name=group_name)


def get_user_done_hw_ids(user_id):
    if not user_id:
        return set()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT hw_id FROM hw_done WHERE user_id = ?', (int(user_id),))
        return {r[0] for r in cursor.fetchall()}


def send_single_hw_card(chat_id, user_obj, hw_row, target_group_name=None):
    photo_ids = parse_photo_ids(hw_row[4])
    hw_doc_id = hw_row[5]
    hw_doc_name = hw_row[6]
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
            hw_id=hw_id,
        )
    except Exception:
        bot.send_message(
            chat_id,
            item_text + '\n<i>(не удалось загрузить вложение)</i>',
            reply_markup=markup,
            parse_mode='HTML',
            disable_web_page_preview=True,
        )


def send_homeworks_for_period(chat_id, user_obj, group_name, days_limit=None, faculty_code=None, edit_msg_id=None):
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    now_dt = now_local()
    today = now_dt.date()
    course = find_course_by_group(group_name)
    today_slot_map = (
        get_day_lessons_for_date(now_dt, course, group_name, faculty_code=eff_fac)[0]
        if course is not None
        else {}
    )
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by, annot_queue_id '
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
        if d == today and is_homework_passed_today(item[2], today_slot_map, now_dt=now_dt):
            continue
        if days_limit is not None and d > today + timedelta(days=days_limit):
            continue
        filtered.append((d, item))

    filtered.sort(key=lambda x: (x[0], x[1][0]))
    items = [x[1] for x in filtered]
    title = 'ДЗ на ближайшую неделю' if days_limit == 7 else 'Все актуальные ДЗ'
    is_group_chat = int(chat_id) < 0
    period_cb = 'hw_view_week' if days_limit == 7 else 'hw_view_all'

    if not items:
        empty_text = f'📚 <b>{title}</b>\n\n✨ <i>Заданий на выбранный период пока нет!</i>'
        empty_kb = None
        if not is_group_chat:
            empty_kb = InlineKeyboardMarkup(row_width=1)
            empty_kb.add(InlineKeyboardButton('🔙 Назад', callback_data='hw_menu_back'))
        if edit_msg_id:
            safe_edit_message_text(empty_text, chat_id, edit_msg_id, reply_markup=empty_kb, parse_mode='HTML')
        else:
            bot.send_message(chat_id, empty_text, reply_markup=empty_kb, parse_mode='HTML')
        return

    done_ids = set() if is_group_chat else get_user_done_hw_ids(user_obj.id if user_obj else None)

    markup = InlineKeyboardMarkup(row_width=1)
    for hw_row in items[:40]:
        hw_id, hw_date, hw_subj = hw_row[0], hw_row[1], hw_row[2]
        short_date = hw_date[:5] if len(str(hw_date or '')) >= 5 else hw_date
        done_mark = ' ✅' if hw_id in done_ids else ''
        btn_label = f'📘 {short_text(hw_subj, 25)} • {short_date}{done_mark}'
        markup.add(InlineKeyboardButton(btn_label, callback_data=f'hw_card_{hw_id}'))

    if is_group_chat:
        markup.row(InlineKeyboardButton('🔄 Обновить список', callback_data=period_cb))
    else:
        markup.row(
            InlineKeyboardButton('🔄 Обновить', callback_data=period_cb),
            InlineKeyboardButton('🔙 Назад', callback_data='hw_menu_back'),
        )

    toc_text = (
        f'📚 <b>{title}</b> <i>({len(items)})</i>\n'
        f'<i>Выбери задание, чтобы открыть карточку с вложениями и вопросами:</i>'
    )
    if edit_msg_id:
        safe_edit_message_text(toc_text, chat_id, edit_msg_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, toc_text, reply_markup=markup, parse_mode='HTML')


@bot.callback_query_handler(
    func=lambda call: call.data.startswith(('hw_', 'hwadd_', 'hwq_'))
)
def handle_homework_callbacks(call):
    sync_username(call.from_user)
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    if not data.startswith(('hwq_', 'hw_done_', 'hw_undone_', 'hw_card_')):
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

    if data == 'hw_publish_now':
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        _finalize_hw_draft(chat_id, call.from_user, state)
        return

    if data == 'hw_list_custom':
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not (state or {}).get('pending_hw_draft'):
            bot.send_message(chat_id, '⚠️ Сессия создания ДЗ устарела. Начни заново.')
            return
        list_num = len((state or {}).get('hw_slot_lists') or []) + 1
        set_user_state(user_id, 'awaiting_hw_slot_name', state)
        _prompt_hw_slot_name(chat_id, msg_id=msg_id, list_num=list_num)
        return

    if data.startswith('hw_list_preset_'):
        if not can_manage_hw(call.from_user):
            return
        _, state = get_user_state(user_id)
        if not (state or {}).get('pending_hw_draft'):
            bot.send_message(chat_id, '⚠️ Сессия создания ДЗ устарела. Начни заново.')
            return
        preset_name = data.replace('hw_list_preset_', '')
        new_state = dict(state)
        new_state['current_list_name'] = preset_name
        set_user_state(user_id, 'awaiting_hw_slots_count', new_state)
        safe_edit_message_text(
            f'📌 <b>{esc(preset_name)}</b> — введи количество слотов (1–60):',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data.startswith('hw_manage_'):
        hw_id = int(data.replace('hw_manage_', ''))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT queue_id, annot_queue_id, group_name, added_by FROM homeworks WHERE id = ?',
                (hw_id,),
            )
            hw_meta = cursor.fetchone()
        if not hw_meta:
            return
        linked_q_id, linked_aq_id, grp_name, hw_added_by = hw_meta
        if not can_edit_specific_hw(call.from_user, grp_name, hw_added_by):
            return
        m_kb = InlineKeyboardMarkup(row_width=2)
        m_kb.row(
            InlineKeyboardButton('✏️ Изменить текст', callback_data=f'hw_edit_{hw_id}'),
            InlineKeyboardButton('🗑 Удалить ДЗ', callback_data=f'hw_del_{hw_id}'),
        )
        any_q_id = linked_q_id or linked_aq_id
        if any_q_id:
            m_kb.row(InlineKeyboardButton('🧹 Освободить слот', callback_data=f'hwq_modmenu_{any_q_id}'))
        m_kb.row(InlineKeyboardButton('🔙 Назад', callback_data=f'hw_backcard_{hw_id}'))
        try:
            bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=m_kb)
        except Exception:
            pass
        return

    if data.startswith('hw_backcard_'):
        hw_id = int(data.replace('hw_backcard_', ''))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by, annot_queue_id, group_name '
                'FROM homeworks WHERE id = ?',
                (hw_id,),
            )
            hw_full = cursor.fetchone()
        if hw_full:
            _, new_markup = render_hw_card_payload(
                hw_full[:10], call.from_user, target_group_name=hw_full[10], is_group_chat=(int(chat_id) < 0)
            )
            try:
                bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=new_markup)
            except Exception:
                pass
        return

    if data.startswith('hwq_modmenu_'):
        q_id = int(data.replace('hwq_modmenu_', ''))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT group_name, created_by FROM queues WHERE id = ?', (q_id,))
            q_row = cursor.fetchone()
        q_grp = q_row[0] if q_row else ''
        q_creator = q_row[1] if q_row else None
        if not can_edit_specific_hw(call.from_user, q_grp, q_creator):
            try:
                bot.answer_callback_query(call.id, '⛔ Только для автора ДЗ, старосты или админа', show_alert=True)
            except Exception:
                pass
            return
        try:
            bot.answer_callback_query(call.id, 'Выбери слот для освобождения')
        except Exception:
            pass
        with db_connection() as conn:
            c_m = conn.cursor()
            c_m.execute('SELECT queue_id, annot_queue_id FROM homeworks WHERE queue_id = ? OR annot_queue_id = ?', (q_id, q_id))
            hm_row = c_m.fetchone()
        q1_id = hm_row[0] if hm_row else q_id
        q2_id = hm_row[1] if hm_row else None
        mod_kb = build_queue_slot_buttons(q1_id, viewer_user_id=user_id, can_moderate=True, mod_mode=True, annot_queue_id=q2_id)
        try:
            bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=mod_kb)
        except Exception:
            pass
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
                'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by, annot_queue_id, group_name '
                'FROM homeworks WHERE queue_id = ? OR annot_queue_id = ?',
                (q_id, q_id),
            )
            hw_full = cursor.fetchone()

        if hw_full:
            hw_row = hw_full[:10]
            grp_name = hw_full[10]
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
                            disable_web_page_preview=True,
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

    if data == 'hw_menu_back':
        user = get_user_data(user_id)
        if not user or not is_group_valid(user[7], user[0], user[1]):
            return
        text, markup = _build_homework_menu_payload(call.from_user, user[1])
        safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        return

    if data.startswith('hw_card_'):
        hw_id = int(data.replace('hw_card_', ''))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by, annot_queue_id, group_name '
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

        grp_name = hw_full[10]
        if int(chat_id) < 0:
            binding = get_group_chat_binding(chat_id)
            if not binding or binding[2] != grp_name:
                try:
                    bot.answer_callback_query(call.id, '⛔ Это задание другой группы', show_alert=True)
                except Exception:
                    pass
                return
        else:
            user = get_user_data(user_id)
            if not is_admin_user(call.from_user) and (not user or user[1] != grp_name):
                try:
                    bot.answer_callback_query(call.id, '⛔ Это задание другой группы', show_alert=True)
                except Exception:
                    pass
                return

        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        send_single_hw_card(chat_id, call.from_user, hw_full[:10], target_group_name=grp_name)
        return

    if data in ('hw_view_week', 'hw_view_all'):
        d_lim = 7 if data == 'hw_view_week' else None
        if int(chat_id) < 0:
            binding = get_group_chat_binding(chat_id)
            if not binding or not is_group_valid(binding[0], binding[1], binding[2]):
                return
            send_homeworks_for_period(
                chat_id, call.from_user, binding[2], days_limit=d_lim, faculty_code=binding[0], edit_msg_id=msg_id
            )
            return
        user = get_user_data(user_id)
        if not user or not is_group_valid(user[7], user[0], user[1]):
            bot.send_message(chat_id, '👇 Сначала выбери свой <b>факультет</b> и группу:', reply_markup=build_faculty_keyboard(), parse_mode='HTML')
            return
        send_homeworks_for_period(
            chat_id, call.from_user, user[1], days_limit=d_lim, faculty_code=user[7], edit_msg_id=msg_id
        )
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
                'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by, annot_queue_id, group_name '
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

        hw_row = hw_full[:10]
        grp_name = hw_full[10]
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
                        disable_web_page_preview=True,
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
                'SELECT target_date, subject_name, hw_text, queue_id, annot_queue_id, group_name, added_by FROM homeworks '
                'WHERE id = ?',
                (hw_id,),
            )
            hw_row_raw = cursor.fetchone()
            if not hw_row_raw:
                bot.send_message(chat_id, '⛔ Запись не найдена.')
                return
            hw_date, hw_subj, hw_text, linked_q_id, linked_aq_id, group_name, hw_added_by = hw_row_raw
            if not can_edit_specific_hw(call.from_user, group_name, hw_added_by):
                bot.send_message(chat_id, '⛔ Удалить ДЗ может только его автор, староста группы или администратор.')
                return

            _, eff_del_fac = find_course_and_faculty_by_group(group_name)
            eff_del_fac = eff_del_fac or infer_faculty_by_group(group_name)
            for del_qid in (linked_q_id, linked_aq_id):
                if del_qid:
                    cursor.execute('DELETE FROM queue_slots WHERE queue_id = ?', (del_qid,))
                    cursor.execute('DELETE FROM queues WHERE id = ? AND (faculty = ? OR faculty = "")', (del_qid, eff_del_fac))

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
        core.notify_vk_group(
            eff_del_fac,
            group_name,
            f'🗑 ДОМАШНЕЕ ЗАДАНИЕ УДАЛЕНО\n🎓 Группа: {group_name} • Дедлайн был: {hw_date}\n📘 {hw_subj}',
        )
        return
