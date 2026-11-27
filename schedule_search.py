from datetime import datetime, timedelta
import re
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
)

import core
from core import (
    DAYS_RUS,
    FACULTY_ORDER,
    NUM_TO_TIME_SLOT,
    PAIR_EMOJIS,
    bot,
    build_faculty_keyboard,
    build_progress_bar,
    calc_week_number_for_start,
    clear_user_state,
    db_connection,
    esc,
    find_course_and_faculty_by_group,
    format_pair_prefix,
    get_bot_username,
    get_current_week_and_day,
    get_faculty_short,
    get_lesson_type_badge,
    get_pair_num,
    get_group_chat_binding,
    get_user_data,
    get_user_state,
    infer_faculty_by_group,
    is_group_valid,
    log_stat_event,
    now_local,
    parse_photo_ids,
    safe_edit_message_text,
    send_long_html_message,
    send_mascot_art,
    set_user_state,
    short_text,
    split_long_html_message,
    sync_username,
    truncate_html_safe,
)


def get_day_lessons_for_date(target_date, course, group_name, faculty_code=None):
    if not faculty_code:
        _, faculty_code = find_course_and_faculty_by_group(group_name)
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    week_number, day_name = get_current_week_and_day(target_date, faculty_code=faculty_code)
    date_str = target_date.strftime('%d.%m.%Y')

    with db_connection() as conn:
        cursor = conn.cursor()
        if faculty_code:
            cursor.execute(
                'SELECT s.time_slot, s.subject_name, s.lesson_type '
                'FROM schedules s '
                'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                'WHERE s.faculty = ? AND s.course_number = ? AND s.group_name = ? '
                'AND s.day_of_week = ? AND sw.week_number = ? '
                'ORDER BY s.time_slot ASC, s.id ASC',
                (faculty_code, course, group_name, day_name, week_number),
            )
        else:
            cursor.execute(
                'SELECT s.time_slot, s.subject_name, s.lesson_type '
                'FROM schedules s '
                'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                'WHERE s.course_number = ? AND s.group_name = ? '
                'AND s.day_of_week = ? AND sw.week_number = ? '
                'ORDER BY s.time_slot ASC, s.id ASC',
                (course, group_name, day_name, week_number),
            )
        all_rows = cursor.fetchall()

        cursor.execute(
            'SELECT time_slot FROM cancelled_lessons '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ?',
            (eff_fac, group_name, date_str),
        )
        cancelled_slots = {r[0] for r in cursor.fetchall()}

        cursor.execute(
            'SELECT time_slot, subject_info FROM replacements '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? '
            'ORDER BY time_slot ASC, id ASC',
            (eff_fac, group_name, date_str),
        )
        replacements = cursor.fetchall()

    replaced_slots = {r_time for r_time, _ in replacements}
    slot_map = {}
    subjects = []

    for time_slot, subj, l_type in all_rows:
        if time_slot in cancelled_slots or time_slot in replaced_slots:
            continue
        p_num = get_pair_num(time_slot)
        if p_num:
            slot_map.setdefault(p_num, []).append(subj)
        if subj not in subjects:
            subjects.append(subj)

    for r_time, r_subj in replacements:
        p_num = get_pair_num(r_time)
        if p_num:
            slot_map[p_num] = [f'[Замена] {r_subj}']
        if r_subj not in subjects:
            subjects.append(r_subj)

    return slot_map, subjects


def get_all_group_subjects(course, group_name, faculty_code=None):
    if not faculty_code:
        _, faculty_code = find_course_and_faculty_by_group(group_name)
    with db_connection() as conn:
        cursor = conn.cursor()
        if faculty_code:
            cursor.execute(
                'SELECT DISTINCT subject_name FROM schedules '
                'WHERE faculty = ? AND course_number = ? AND group_name = ? '
                'ORDER BY subject_name ASC',
                (faculty_code, course, group_name),
            )
        else:
            cursor.execute(
                'SELECT DISTINCT subject_name FROM schedules '
                'WHERE course_number = ? AND group_name = ? '
                'ORDER BY subject_name ASC',
                (course, group_name),
            )
        subjects = [r[0] for r in cursor.fetchall()]
    return subjects


def _parse_slot_end_time(time_slot):
    parts = str(time_slot or '').split('-')
    if len(parts) != 2:
        return None
    end_str = parts[1].strip()
    try:
        return datetime.strptime(end_str, '%H:%M').time()
    except ValueError:
        return None


def is_homework_passed_today(subject_name, today_slot_map, now_dt=None):
    """Возвращает True, если пара по этому предмету сегодня уже закончилась."""
    if not today_slot_map:
        return False
    if now_dt is None:
        now_dt = now_local()
    hw_key = str(subject_name or '').strip().lower()
    if not hw_key:
        return False

    matched_end_times = []
    for p_num, subj_list in today_slot_map.items():
        slot_str = NUM_TO_TIME_SLOT.get(p_num, '')
        end_t = _parse_slot_end_time(slot_str)
        if not end_t:
            continue
        for s in subj_list:
            s_clean = str(s or '').replace('[Замена]', '').strip().lower()
            if s_clean and (hw_key == s_clean or hw_key in s_clean or s_clean in hw_key):
                matched_end_times.append(end_t)
                break

    if not matched_end_times:
        return False
    # Если предмет стоит несколько раз в день, ДЗ уходит в неактивные после окончания последней такой пары
    return now_dt.time() >= max(matched_end_times)


def get_homeworks_for_date(group_name, date_str, faculty_code=None):
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name FROM homeworks '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? '
            'ORDER BY id ASC',
            (eff_fac, group_name, date_str),
        )
        rows = cursor.fetchall()
    return rows


def _build_today_live_status_line(rows, replacements, now_dt):
    """Возвращает строку живого статуса для сегодняшнего расписания (прогресс-бар текущей пары или отсчёт перемены)."""
    slots_info = []
    for time_slot, subject, l_type, teacher, room, _ in rows:
        parts = str(time_slot or '').split('-')
        if len(parts) != 2:
            continue
        try:
            st_t = datetime.strptime(parts[0].strip(), '%H:%M').time()
            en_t = datetime.strptime(parts[1].strip(), '%H:%M').time()
        except ValueError:
            continue
        p_num = get_pair_num(time_slot)
        slots_info.append((st_t, en_t, p_num, room, subject))
    for r_time, r_subj in replacements:
        parts = str(r_time or '').split('-')
        if len(parts) != 2:
            continue
        try:
            st_t = datetime.strptime(parts[0].strip(), '%H:%M').time()
            en_t = datetime.strptime(parts[1].strip(), '%H:%M').time()
        except ValueError:
            continue
        p_num = get_pair_num(r_time)
        slots_info.append((st_t, en_t, p_num, '', r_subj))

    if not slots_info:
        return ''
    slots_info.sort(key=lambda x: x[0])
    now_t = now_dt.time()
    now_min = now_t.hour * 60 + now_t.minute

    for st_t, en_t, p_num, room, subj in slots_info:
        st_m = st_t.hour * 60 + st_t.minute
        en_m = en_t.hour * 60 + en_t.minute
        if st_m <= now_min < en_m:
            elapsed = now_min - st_m
            total = max(1, en_m - st_m)
            left_m = en_m - now_min
            bar = build_progress_bar(elapsed, total, width=10)
            p_lbl = f'{p_num}-я пара' if p_num else 'пара'
            return f'🐾 <b>Идёт {p_lbl}:</b> <code>{bar}</code> <i>(осталось {left_m} мин)</i>\n\n'

    for st_t, en_t, p_num, room, subj in slots_info:
        st_m = st_t.hour * 60 + st_t.minute
        if now_min < st_m:
            wait_m = st_m - now_min
            p_lbl = f'{p_num}-й пары' if p_num else 'пары'
            room_p = f' ({esc(room)})' if room else ''
            if wait_m <= 65:
                return f'🐾 <b>Перемена</b> • до {p_lbl}{room_p}: <b>{wait_m} мин</b>\n\n'
            hrs, mins = divmod(wait_m, 60)
            return f'🐾 <b>До {p_lbl}{room_p}:</b> {hrs} ч {mins} мин\n\n'

    return '🐾 <i>Все пары на сегодня уже закончились — можно выдохнуть!</i>\n\n'


def get_schedule_text(target_date, course, group_name, faculty_code=None):
    if not faculty_code:
        _, faculty_code = find_course_and_faculty_by_group(group_name)
    week_number, day_name = get_current_week_and_day(target_date, faculty_code=faculty_code)
    date_str = target_date.strftime('%d.%m.%Y')
    fac_short = get_faculty_short(faculty_code or 'ЕМФ')

    with db_connection() as conn:
        cursor = conn.cursor()
        fac_filter = faculty_code or infer_faculty_by_group(group_name)
        cursor.execute(
            'SELECT s.time_slot, s.subject_name, s.lesson_type, s.teacher, s.room, s.weeks_raw '
            'FROM schedules s '
            'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
            'WHERE s.faculty = ? AND s.course_number = ? AND s.group_name = ? '
            'AND s.day_of_week = ? AND sw.week_number = ? '
            'ORDER BY s.time_slot ASC, s.id ASC',
            (fac_filter, course, group_name, day_name, week_number),
        )
        all_rows = cursor.fetchall()

        cursor.execute(
            'SELECT time_slot FROM cancelled_lessons '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ?',
            (fac_filter, group_name, date_str),
        )
        cancelled_slots = {r[0] for r in cursor.fetchall()}

        cursor.execute(
            'SELECT time_slot, subject_info FROM replacements '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? '
            'ORDER BY time_slot ASC, id ASC',
            (fac_filter, group_name, date_str),
        )
        replacements = cursor.fetchall()
        replaced_slots = {r[0] for r in replacements}

        rows = [r for r in all_rows if r[0] not in cancelled_slots and r[0] not in replaced_slots]

    hw_list = get_homeworks_for_date(group_name, date_str, faculty_code=fac_filter)
    hw_by_subj = {}
    for hw_id, hw_subj, hw_text, hw_photo, hw_doc_id, hw_doc_name in hw_list:
        hw_by_subj.setdefault(hw_subj.strip().lower(), []).append(
            (hw_id, hw_subj, hw_text, hw_photo, hw_doc_id, hw_doc_name)
        )
    matched_hw_ids = set()

    week_disp = f'{week_number}-я' if week_number > 0 else 'ещё не началась'
    header = (
        f'📅 <b>{day_name.upper()}, {date_str}</b>\n'
        f'<blockquote>🏛 <b>Факультет:</b> {esc(fac_short)}\n'
        f'🎓 <b>Группа:</b> {esc(group_name)}\n'
        f'📆 <b>Учебная неделя:</b> {week_disp}</blockquote>\n\n'
    )

    body = ''
    if week_number <= 0 and not replacements:
        body = (
            f'⏳ <i>Новый учебный год стартует с <b>{core.ACADEMIC_YEAR_START_DATE.strftime("%d.%m.%Y")}</b> — '
            f'пока можно отдохнуть перед началом занятий!</i>\n\n'
        )
    elif week_number > 52 and not replacements:
        body = (
            '⏳ <i>Расписание на новый учебный год ещё не загружено администратором. '
            'Как только оно появится, бот уведомит об этом!</i>\n\n'
        )
    elif day_name == 'Воскресенье' and not replacements:
        body = '🐾 <i>Воскресенье — Куница свернулась калачиком и отдыхает! Набирайся сил перед новой учебной неделей.</i>\n\n'
    elif week_number in core.PRACTICE_WEEKS.get(group_name, set()) and not replacements:
        body = '🛠 <i>На этой неделе аудиторных пар нет — у группы проходит учебная или производственная практика.</i>\n\n'
    elif not rows and not replacements:
        if cancelled_slots:
            body = '🐾 <i>Все пары на этот день отменены — Куница одобряет внезапный выходной!</i>\n\n'
        else:
            body = '🐾 <i>Куница проверила расписание — пар на этот день нет, можно спокойно отдохнуть!</i>\n\n'
    else:
        now_dt = now_local()
        if target_date.date() == now_dt.date():
            body += _build_today_live_status_line(rows, replacements, now_dt)
        for row in rows:
            time_slot, subject, l_type, teacher, room, weeks_raw = row
            pair_prefix = format_pair_prefix(time_slot)
            l_badge = get_lesson_type_badge(l_type)
            body += (
                f'{pair_prefix}\n'
                f'{l_badge} <b>{esc(subject)}</b> <i>({esc(l_type)})</i>\n'
                f'📍 <u>{esc(room)}</u>  •  👤 <i>{esc(teacher)}</i>\n'
                f'🗓 <code>Недели: {esc(weeks_raw)}</code>\n'
            )
            subj_key = subject.strip().lower()
            attached_hws = []
            for k, items in hw_by_subj.items():
                if k == subj_key or k in subj_key or subj_key in k:
                    for item in items:
                        if item[0] not in matched_hw_ids:
                            attached_hws.append(item)
                            matched_hw_ids.add(item[0])
            for _, _, hw_text, hw_photo, hw_doc_id, hw_doc_name in attached_hws:
                p_list = parse_photo_ids(hw_photo)
                badges = []
                if p_list:
                    badges.append(f'📷 фото: {len(p_list)} шт.')
                if hw_doc_id:
                    badges.append(f'📎 {esc(hw_doc_name or "файл")}')
                badge_str = f' <i>[{", ".join(badges)}]</i>' if badges else ''
                hw_display = esc(hw_text) if hw_text else '<i>(см. вложение)</i>'
                body += f'📝 <b>ДЗ:</b> {hw_display}{badge_str}\n'
            body += '\n'

    if replacements:
        body += '⚡ <b>ИЗМЕНЕНИЯ / ЗАМЕНЫ НА ЭТОТ ДЕНЬ:</b>\n'
        for r_time, r_subj in replacements:
            r_prefix = format_pair_prefix(r_time)
            body += (
                f'🔄 {r_prefix}\n'
                f'👉 <b>{esc(r_subj)}</b>\n'
            )
            r_key = r_subj.strip().lower()
            for k, items in hw_by_subj.items():
                if k == r_key or k in r_key or r_key in k:
                    for item in items:
                        if item[0] not in matched_hw_ids:
                            matched_hw_ids.add(item[0])
                            p_list = parse_photo_ids(item[3])
                            badges = []
                            if p_list:
                                badges.append(f'📷 фото: {len(p_list)} шт.')
                            if item[4]:
                                badges.append(f'📎 {esc(item[5] or "файл")}')
                            badge_str = f' <i>[{", ".join(badges)}]</i>' if badges else ''
                            hw_display = esc(item[2]) if item[2] else '<i>(см. вложение)</i>'
                            body += f'📝 <b>ДЗ:</b> {hw_display}{badge_str}\n'
            body += '\n'

    unmatched_hws = [h for h in hw_list if h[0] not in matched_hw_ids]
    if unmatched_hws:
        body += '📚 <b>ДОМАШНИЕ ЗАДАНИЯ НА ЭТОТ ДЕНЬ:</b>\n'
        for _, hw_subj, hw_text, hw_photo, hw_doc_id, hw_doc_name in unmatched_hws:
            p_list = parse_photo_ids(hw_photo)
            badges = []
            if p_list:
                badges.append(f'📷 фото: {len(p_list)} шт.')
            if hw_doc_id:
                badges.append(f'📎 {esc(hw_doc_name or "файл")}')
            badge_str = f' <i>[{", ".join(badges)}]</i>' if badges else ''
            hw_display = esc(hw_text) if hw_text else '<i>(см. вложение)</i>'
            body += f'• <b>{esc(hw_subj)}:</b> {hw_display}{badge_str}\n'
        body += '\n'

    return (header + body).strip()


def get_week_schedule_text(target_date, course, group_name, faculty_code=None):
    if not faculty_code:
        _, faculty_code = find_course_and_faculty_by_group(group_name)
    week_number, _ = get_current_week_and_day(target_date, faculty_code=faculty_code)
    monday = target_date - timedelta(days=target_date.weekday())
    saturday = monday + timedelta(days=5)
    fac_short = get_faculty_short(faculty_code or 'ЕМФ')

    week_title = f'НА {week_number}-Ю НЕДЕЛЮ' if week_number > 0 else '(ДО СТАРТА УЧЕБНОГО ГОДА)'
    text = (
        f'🗓 <b>РАСПИСАНИЕ {week_title}</b>\n'
        f'<blockquote>📆 <b>Период:</b> {monday.strftime("%d.%m")} — {saturday.strftime("%d.%m.%Y")}\n'
        f'🏛 <b>Факультет:</b> {esc(fac_short)}  •  🎓 <b>Группа:</b> {esc(group_name)}</blockquote>\n'
    )

    if week_number <= 0:
        return text + f'\n⏳ <i>Учебный год начинается с <b>{core.ACADEMIC_YEAR_START_DATE.strftime("%d.%m.%Y")}</b>.</i>'
    if week_number > 52:
        return text + '\n⏳ <i>Расписание на новый учебный год ещё не загружено администратором.</i>'
    if week_number in core.PRACTICE_WEEKS.get(group_name, set()):
        return text + '\n🛠 <i>Всю эту неделю у вашей группы проходит практика по учебному плану.</i>'

    fac_filter = faculty_code or infer_faculty_by_group(group_name)
    valid_days = []
    for day_idx in range(6):
        day_date = monday + timedelta(days=day_idx)
        if day_date.date() < core.ACADEMIC_YEAR_START_DATE.date():
            continue
        valid_days.append((day_date, DAYS_RUS[day_idx], day_date.strftime('%d.%m.%Y')))

    if not valid_days:
        return text + '\n✨ <i>На этой неделе учебных пар в расписании не нашлось.</i>'

    date_strs = [d[2] for d in valid_days]
    d_placeholders = ','.join('?' * len(date_strs))

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT s.day_of_week, s.time_slot, s.subject_name, s.lesson_type, s.teacher, s.room '
            'FROM schedules s '
            'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
            'WHERE s.faculty = ? AND s.course_number = ? AND s.group_name = ? '
            'AND sw.week_number = ? '
            'ORDER BY s.time_slot ASC, s.id ASC',
            (fac_filter, course, group_name, week_number),
        )
        sched_by_day = {}
        for d_name, t_slot, subj, l_type, teacher, room in cursor.fetchall():
            sched_by_day.setdefault(d_name, []).append((t_slot, subj, l_type, teacher, room))

        cursor.execute(
            f'SELECT target_date, time_slot FROM cancelled_lessons '
            f'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date IN ({d_placeholders})',
            [fac_filter, group_name] + date_strs,
        )
        canc_by_date = {}
        for c_date, c_slot in cursor.fetchall():
            canc_by_date.setdefault(c_date, set()).add(c_slot)

        cursor.execute(
            f'SELECT target_date, time_slot, subject_info FROM replacements '
            f'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date IN ({d_placeholders}) '
            f'ORDER BY time_slot ASC, id ASC',
            [fac_filter, group_name] + date_strs,
        )
        repl_by_date = {}
        for r_date, r_slot, r_subj in cursor.fetchall():
            repl_by_date.setdefault(r_date, []).append((r_slot, r_subj))

        cursor.execute(
            f'SELECT target_date, COUNT(*) FROM homeworks '
            f'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date IN ({d_placeholders}) '
            f'GROUP BY target_date',
            [fac_filter, group_name] + date_strs,
        )
        hw_cnt_by_date = dict(cursor.fetchall())

    has_any = False
    for day_date, day_name, date_str in valid_days:
        cancelled_slots = canc_by_date.get(date_str, set())
        replacements = repl_by_date.get(date_str, [])
        replaced_slots = {r[0] for r in replacements}
        rows = [
            r for r in sched_by_day.get(day_name, [])
            if r[0] not in cancelled_slots and r[0] not in replaced_slots
        ]
        hw_count = hw_cnt_by_date.get(date_str, 0)

        if rows or replacements or hw_count:
            has_any = True
            hw_mark = f'  📝 ДЗ: {hw_count}' if hw_count else ''
            text += f'\n🔹 <b><u>{day_name.upper()} ({day_date.strftime("%d.%m")})</u></b>{hw_mark}\n'
            for time_slot, subject, l_type, teacher, room in rows:
                pair_prefix = format_pair_prefix(time_slot)
                text += (
                    f'{pair_prefix} {get_lesson_type_badge(l_type)} <b>{esc(subject)}</b> <i>({esc(l_type)})</i>\n'
                    f'   📍 <u>{esc(room)}</u> · 👤 <i>{esc(teacher)}</i>\n'
                )
            for r_time, r_subj in replacements:
                r_prefix = format_pair_prefix(r_time)
                text += f'🔄 <b>[ЗАМЕНА]</b> {r_prefix} — <b>{esc(r_subj)}</b>\n'

    if not has_any:
        text += '\n✨ <i>На этой неделе учебных пар в расписании не нашлось.</i>'
    return text


def send_week_schedule_message(chat_id, target_date, course, group_name, week_offset=0, edit_msg_id=None, faculty_code=None):
    full_text = get_week_schedule_text(target_date, course, group_name, faculty_code=faculty_code)
    chunks = split_long_html_message(full_text, max_len=3900)
    markup = get_week_pagination_keyboard(week_offset)

    if len(chunks) == 1:
        if edit_msg_id:
            try:
                bot.edit_message_text(
                    chunks[0],
                    chat_id,
                    edit_msg_id,
                    reply_markup=markup,
                    parse_mode='HTML',
                )
                return
            except Exception:
                pass
        send_mascot_art(chat_id, 'marten_week', chunks[0], reply_markup=markup)
    else:
        if edit_msg_id:
            try:
                bot.delete_message(chat_id, edit_msg_id)
            except Exception:
                pass
        for idx, chunk in enumerate(chunks):
            kb = markup if idx == len(chunks) - 1 else None
            bot.send_message(chat_id, chunk, reply_markup=kb, parse_mode='HTML')


def get_pagination_keyboard(offset=0, group_name=None):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton('⬅️', callback_data=f'date_{offset - 1}'),
        InlineKeyboardButton('📍 Сегодня', callback_data='date_0'),
        InlineKeyboardButton('➡️', callback_data=f'date_{offset + 1}'),
    )
    if group_name:
        target_date_str = (now_local() + timedelta(days=offset)).strftime('%d.%m.%Y')
        hws = get_homeworks_for_date(group_name, target_date_str)
        if hws:
            has_media = any(parse_photo_ids(h[3]) or h[4] for h in hws)
            btn_label = (
                f'📎 ДЗ с вложениями ({len(hws)})'
                if has_media
                else f'📚 ДЗ на этот день ({len(hws)})'
            )
            markup.row(
                InlineKeyboardButton(
                    btn_label,
                    callback_data=f'hw_showdate_{target_date_str}',
                )
            )
    return markup


def get_week_pagination_keyboard(week_offset=0):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton('⬅️ Пред. неделя', callback_data=f'week_{week_offset - 1}'),
        InlineKeyboardButton('📍 Текущая', callback_data='week_0'),
        InlineKeyboardButton('След. неделя ➡️', callback_data=f'week_{week_offset + 1}'),
    )
    return markup


TEACHER_SURNAME_INIT_RE = re.compile(
    r'([А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]{2,})?)\s+([А-ЯЁ])\.\s*([А-ЯЁ])\.?'
)

TEACHER_RANK_PREFIX_RE = re.compile(
    r'^(?:(?:канд|к|д-р|д)\.?[а-яё\-.\s]*?н\.?(?=[\s,.-]|$)|доц\.?(?=[\s,.-]|$)|доцент(?=[\s,.-]|$)|проф\.?(?=[\s,.-]|$)|профессор(?=[\s,.-]|$)|ст\.?\s*преп\.?(?=[\s,.-]|$)|ст\.?\s*пр\.?(?=[\s,.-]|$)|старший\s+преподаватель(?=[\s,.-]|$)|преп\.?(?=[\s,.-]|$)|преподаватель(?=[\s,.-]|$)|асс\.?(?=[\s,.-]|$)|ассистент(?=[\s,.-]|$))[\s,.-]*',
    re.IGNORECASE,
)

TEACHER_RANK_SUFFIX_RE = re.compile(
    r'(?:[,;\s]+(?:канд|к|д-р|д)\.?[а-яё\-.\s]*?н\.?|[,;\s]+(?:доц|доцент|проф|профессор|ст\.?\s*преп|ст\.?\s*пр|преп|преподаватель|асс|ассистент)\.?\s*)+$',
    re.IGNORECASE,
)

_LATIN_TO_CYR_MAP = str.maketrans({
    'A': 'А', 'B': 'В', 'C': 'С', 'E': 'Е', 'H': 'Н', 'K': 'К',
    'M': 'М', 'O': 'О', 'P': 'Р', 'T': 'Т', 'X': 'Х',
    'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 'x': 'х',
})

_TEACHER_STOPWORDS = {
    'не указан', 'вакансия', 'доц', 'доцент', 'проф', 'профессор',
    'преп', 'преподаватель', 'асс', 'ассистент', 'кафедра', 'каф',
    'ауд', 'зал', 'спортзал', 'поток', 'нет',
}


def _format_surname_part(surname_raw):
    s = re.sub(r'[^а-яёА-ЯЁ-]', '', str(surname_raw or '')).strip('-')
    if not s or len(s) < 3:
        return ''
    if s.lower() in _TEACHER_STOPWORDS:
        return ''
    return '-'.join(part.capitalize() for part in s.split('-') if part)


def extract_teacher_surname_initials(raw_teacher):
    txt = re.sub(r'\s+', ' ', str(raw_teacher or '')).translate(_LATIN_TO_CYR_MAP).strip()
    if not txt or txt.lower() in ('не указан', '-', '—', 'нет', 'вакансия'):
        return ''

    if '\n' in str(raw_teacher or '') or '/' in txt or ';' in txt:
        parts_split = re.split(r'[\n/;]+', str(raw_teacher) if '\n' in str(raw_teacher or '') else txt)
        norm_parts = []
        seen_low = set()
        for p in parts_split:
            sub_norm = extract_teacher_surname_initials(p)
            if sub_norm and sub_norm.lower() not in seen_low:
                seen_low.add(sub_norm.lower())
                norm_parts.append(sub_norm)
        return ', '.join(norm_parts)

    m = TEACHER_SURNAME_INIT_RE.search(txt)
    if m:
        surname, i1, i2 = m.groups()
        clean_surname = _format_surname_part(surname)
        if clean_surname:
            return f'{clean_surname} {i1.upper()}.{i2.upper()}.'

    cleaned = txt
    while True:
        next_c = TEACHER_RANK_PREFIX_RE.sub('', cleaned).strip(' ,;-–—/.')
        next_c = TEACHER_RANK_SUFFIX_RE.sub('', next_c).strip(' ,;-–—/.')
        if next_c == cleaned:
            break
        cleaned = next_c

    if not cleaned:
        return ''

    m_no_dot = re.search(
        r'([А-ЯЁа-яё]{3,}(?:-[А-ЯЁа-яё]{2,})?)\s+([А-ЯЁа-яё])\.?\s*([А-ЯЁа-яё])\.?$',
        cleaned,
    )
    if m_no_dot:
        s_part, i1, i2 = m_no_dot.groups()
        f_sur = _format_surname_part(s_part)
        if f_sur:
            return f'{f_sur} {i1.upper()}.{i2.upper()}.'

    words = [w.strip(' ,;-–—/.()') for w in cleaned.split() if w.strip(' ,;-–—/.()')]
    if len(words) == 1 and re.fullmatch(r'[а-яёА-ЯЁ-]{3,}', words[0]):
        return _format_surname_part(words[0])
    if words and re.fullmatch(r'[а-яёА-ЯЁ-]{3,}', words[0]):
        f_sur = _format_surname_part(words[0])
        if not f_sur:
            return ''
        if len(words) >= 2 and re.fullmatch(r'[а-яёА-ЯЁ]\.?$', words[1]):
            init1 = words[1][0].upper()
            init2 = words[2][0].upper() if len(words) >= 3 and re.fullmatch(r'[а-яёА-ЯЁ]\.?$', words[2]) else ''
            if init2:
                return f'{f_sur} {init1}.{init2}.'
            return f'{f_sur} {init1}.'
        return f_sur

    return ''


def _teacher_surname_only(teacher_norm):
    """Возвращает только фамилию в нижнем регистре из нормализованной строки вида 'Суюндуков Я.Т.' или 'Бускунова'."""
    first = str(teacher_norm or '').split(',')[0].strip()
    if not first:
        return ''
    return first.split()[0].lower()


_TEACHERS_CACHE = {'ver': -1, 'items': []}


def get_all_unique_teachers():
    cur_ver = getattr(core, 'SCHEDULE_CACHE_VERSION', 0)
    if _TEACHERS_CACHE['ver'] == cur_ver and _TEACHERS_CACHE['items']:
        return list(_TEACHERS_CACHE['items'])
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT teacher FROM schedules')
        raw_teachers = [r[0] for r in cursor.fetchall() if r[0]]

    by_lower = {}
    for t in raw_teachers:
        norm = extract_teacher_surname_initials(t)
        if not norm:
            continue
        for single in norm.split(', '):
            s_clean = single.strip()
            if not s_clean:
                continue
            low_key = s_clean.lower()
            if low_key not in by_lower:
                by_lower[low_key] = s_clean

    all_vals = list(by_lower.values())
    surnames_with_initials = set()
    for val in all_vals:
        if ' ' in val and '.' in val:
            surnames_with_initials.add(val.split()[0].lower())

    deduped = []
    for val in sorted(all_vals):
        low_v = val.lower()
        # Не удаляем фамилию без инициалов, если по этой фамилии есть ещё и запись без инициалов,
        # но если в базе есть "Суюндуков Я.Т." и "Суюндуков И.В.", оба останутся.
        if low_v in surnames_with_initials and ' ' not in val:
            continue
        deduped.append(val)

    result = sorted(deduped)
    _TEACHERS_CACHE['ver'] = cur_ver
    _TEACHERS_CACHE['items'] = result
    return list(result)


def open_search_menu(chat_id, message_id=None):
    clear_user_state(chat_id, keep_payload=True)
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton('👨‍🏫 Где преподаватель?', callback_data='search_teacher'),
        InlineKeyboardButton('🚪 Свободные аудитории', callback_data='search_rooms'),
    )
    text = (
        '🐾 <b>ПОИСК ПО ИНСТИТУТУ</b>\n\n'
        'Куница поможет быстро найти:\n'
        '• <b>Где преподаватель?</b> — по твоей группе, факультету или всему институту на <i>сегодня</i>, <i>завтра</i> или <i>неделю</i>.\n'
        '• <b>Свободные аудитории</b> — пустые кабинеты на нужный день и пару.'
    )
    if message_id:
        safe_edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
        return
    send_mascot_art(chat_id, 'marten_search', text, reply_markup=markup)


def show_teacher_period_choice(chat_id, msg_id=None):
    today = now_local()
    tomorrow = today + timedelta(days=1)
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton(f'📍 Сегодня ({today.strftime("%d.%m")})', callback_data='tsearch_period_today'),
        InlineKeyboardButton(f'⏭ Завтра ({tomorrow.strftime("%d.%m")})', callback_data='tsearch_period_tomorrow'),
    )
    markup.row(
        InlineKeyboardButton('🗓 На всю текущую неделю', callback_data='tsearch_period_week'),
    )
    markup.row(
        InlineKeyboardButton('🔙 Назад', callback_data='search_back'),
    )
    text = (
        '👨‍🏫 <b>ПОИСК ПРЕПОДАВАТЕЛЯ</b>\n\n'
        '<b>Шаг 1 из 2.</b> Выбери период, за который нужно найти пары преподавателя:'
    )
    if msg_id:
        safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        return
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')



def get_teachers_by_scope(scope='all', course=None, group_name=None, faculty_code=None):
    """Возвращает отсортированный список уникальных преподавателей: по группе, факультету или по всем факультетам."""
    with db_connection() as conn:
        cursor = conn.cursor()
        if scope == 'group' and group_name and course:
            if faculty_code:
                cursor.execute(
                    'SELECT DISTINCT teacher FROM schedules WHERE faculty = ? AND course_number = ? AND group_name = ?',
                    (faculty_code, int(course), group_name),
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
    result = []
    for (t_raw,) in rows:
        t_norm = extract_teacher_surname_initials(t_raw)
        if not t_norm or 'не указан' in t_norm.lower():
            continue
        for single in t_norm.split(', '):
            s_c = single.strip()
            if s_c and s_c.lower() not in seen:
                seen.add(s_c.lower())
                result.append(s_c)
    result.sort()
    return result


def show_teacher_buttons_menu(chat_id, msg_id, user_id, period, scope=None, page=0):
    u_data = get_user_data(user_id)
    u_course = u_data[0] if u_data else None
    u_group = u_data[1] if u_data else None
    u_fac = u_data[7] if (u_data and u_data[7]) else None
    if scope is None:
        scope = 'group' if (u_group and u_course) else (u_fac or 'all')

    period_labels = {
        'today': 'на сегодня',
        'tomorrow': 'на завтра',
        'week': 'на всю текущую неделю',
    }
    teachers = get_teachers_by_scope(scope, course=u_course, group_name=u_group, faculty_code=u_fac)
    per_page = 12
    total_pages = max(1, (len(teachers) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = teachers[page * per_page : (page + 1) * per_page]

    set_user_state(user_id, 'awaiting_teacher_query', {
        'teacher_period': period,
        'teacher_scope': scope,
        'teacher_page_list': chunk,
    })

    markup = InlineKeyboardMarkup(row_width=2)
    # Кнопки переключения: Моя группа / Все факультеты + факультеты
    top_row = []
    if u_group and u_course:
        grp_mark = '✅ ' if scope == 'group' else ''
        top_row.append(InlineKeyboardButton(f'{grp_mark}🎓 Моя группа', callback_data=f'tsearch_list_{period}_group_0'))
    all_mark = '✅ ' if scope == 'all' else ''
    top_row.append(InlineKeyboardButton(f'{all_mark}🌐 Все факультеты', callback_data=f'tsearch_list_{period}_all_0'))
    markup.row(*top_row)

    fac_btns = []
    for f_code in FACULTY_ORDER:
        mark = '✅ ' if scope == f_code else ''
        fac_btns.append(
            InlineKeyboardButton(
                f'{mark}{get_faculty_short(f_code)}',
                callback_data=f'tsearch_list_{period}_{f_code}_0',
            )
        )
    markup.row(*fac_btns[:2])
    if len(fac_btns) > 2:
        markup.row(*fac_btns[2:4])

    # Кнопки преподавателей (по 2 в ряд)
    t_btns = [
        InlineKeyboardButton(short_text(t_name, 22), callback_data=f'tsearch_btn_{idx}')
        for idx, t_name in enumerate(chunk)
    ]
    for i in range(0, len(t_btns), 2):
        markup.row(*t_btns[i : i + 2])

    # Пагинация
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton('⬅️ Назад', callback_data=f'tsearch_list_{period}_{scope}_{page - 1}'))
        nav_row.append(InlineKeyboardButton(f'{page + 1}/{total_pages}', callback_data='tsearch_noop'))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton('Вперёд ➡️', callback_data=f'tsearch_list_{period}_{scope}_{page + 1}'))
        markup.row(*nav_row)

    markup.row(InlineKeyboardButton('🔙 К выбору периода', callback_data='search_teacher'))

    if scope == 'group':
        scope_title = f'🎓 Моя группа ({short_text(u_group, 28)})'
    elif scope == 'all':
        scope_title = '🌐 Все факультеты'
    else:
        scope_title = f'🏛 {get_faculty_short(scope)}'
    text = (
        f'👨‍🏫 <b>Поиск преподавателя ({period_labels.get(period, "")})</b>\n'
        f'<blockquote>{scope_title} • Найдено: {len(teachers)} (стр. {page + 1}/{total_pages})</blockquote>\n\n'
        f'<b>Шаг 2 из 2.</b> Нажми на преподавателя ниже <b>или введи фамилию вручную</b>:\n'
        f'<i>Для отмены напиши «Отмена».</i>'
    )
    safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')


def execute_teacher_search_for_period(chat_id, query, period):
    today = now_local()
    q_lower = query.strip().lower()

    if period == 'today':
        target_dates = [today]
        period_title = f'Сегодня ({DAYS_RUS[today.weekday()]}, {today.strftime("%d.%m.%Y")})'
    elif period == 'tomorrow':
        tmr = today + timedelta(days=1)
        target_dates = [tmr]
        period_title = f'Завтра ({DAYS_RUS[tmr.weekday()]}, {tmr.strftime("%d.%m.%Y")})'
    else:
        monday = today - timedelta(days=today.weekday())
        target_dates = [monday + timedelta(days=i) for i in range(6)]
        w_num, _ = get_current_week_and_day(today)
        period_title = f'Вся {w_num}-я неделя ({monday.strftime("%d.%m")} — {target_dates[-1].strftime("%d.%m")})'

    all_day_results = []
    with db_connection() as conn:
        cursor = conn.cursor()
        for dt_obj in target_dates:
            week_num, day_name = get_current_week_and_day(dt_obj)
            date_str = dt_obj.strftime('%d.%m.%Y')

            cursor.execute(
                'SELECT s.time_slot, s.subject_name, s.lesson_type, s.teacher, s.room, s.group_name, s.faculty '
                'FROM schedules s '
                'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                'WHERE s.day_of_week = ? AND sw.week_number = ? '
                'ORDER BY s.time_slot ASC, s.group_name ASC',
                (day_name, week_num),
            )
            rows = cursor.fetchall()

            cursor.execute(
                'SELECT COALESCE(NULLIF(faculty, ""), ""), group_name, time_slot FROM cancelled_lessons WHERE target_date = ?',
                (date_str,),
            )
            cancelled_rows_raw = cursor.fetchall()
            cancelled_set = {(r[0], r[1], r[2]) for r in cancelled_rows_raw}
            cancelled_grp_slot = {(r[1], r[2]) for r in cancelled_rows_raw if not r[0]}

            cursor.execute(
                'SELECT COALESCE(NULLIF(faculty, ""), ""), group_name, time_slot, subject_info FROM replacements WHERE target_date = ?',
                (date_str,),
            )
            repl_rows = cursor.fetchall()
            replaced_set = {(r[0], r[1], r[2]) for r in repl_rows}
            replaced_grp_slot = {(r[1], r[2]) for r in repl_rows if not r[0]}

            day_matches = []
            for time_slot, subj, l_type, teacher, room, grp, fac in rows:
                eff_fac = fac or infer_faculty_by_group(grp)
                if (eff_fac, grp, time_slot) in cancelled_set or (grp, time_slot) in cancelled_grp_slot:
                    continue
                if (eff_fac, grp, time_slot) in replaced_set or (grp, time_slot) in replaced_grp_slot:
                    continue
                if week_num in core.PRACTICE_WEEKS.get(grp, set()):
                    continue
                teacher_norm = extract_teacher_surname_initials(teacher)
                t_norm_low = (teacher_norm or '').lower()
                t_sur_low = _teacher_surname_only(teacher_norm)
                q_compact = re.sub(r'[.\s]+', '', q_lower)
                t_compact = re.sub(r'[.\s]+', '', t_norm_low)
                if teacher_norm and (
                    q_lower in t_norm_low
                    or (len(q_compact) >= 2 and q_compact in t_compact)
                    or (q_lower == t_sur_low)
                ):
                    fac_tag = get_faculty_short(eff_fac)
                    day_matches.append((time_slot, teacher_norm, room, subj, l_type, f'{grp} [{fac_tag}]', False))

            q_surname = q_lower.split()[0] if q_lower.split() else q_lower
            for r_fac, grp, time_slot, subj_info in repl_rows:
                subj_low = subj_info.lower()
                if q_lower in subj_low or (len(q_surname) >= 3 and q_surname in subj_low):
                    eff_fac = r_fac or infer_faculty_by_group(grp)
                    fac_tag = get_faculty_short(eff_fac)
                    day_matches.append((time_slot, 'Замена', '', subj_info, 'Замена', f'{grp} [{fac_tag}]', True))

            if day_matches:
                all_day_results.append((dt_obj, day_name, date_str, week_num, day_matches))

    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton('📍 Сегодня', callback_data='tsearch_re_today'),
        InlineKeyboardButton('⏭ Завтра', callback_data='tsearch_re_tomorrow'),
        InlineKeyboardButton('🗓 Неделя', callback_data='tsearch_re_week'),
    )
    markup.row(InlineKeyboardButton('🔍 Новый поиск', callback_data='search_back'))

    _, prev_st_payload = get_user_state(chat_id)
    merged_st = dict(prev_st_payload or {})
    merged_st['last_teacher_query'] = query
    set_user_state(chat_id, '', merged_st)

    if not all_day_results:
        bot.send_message(
            chat_id,
            f'🔎 По запросу <b>«{esc(query)}»</b> за период <b>{period_title}</b> пар не найдено.\n\n'
            f'<i>Попробуй переключить период кнопками ниже:</i>',
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    text = f'👨‍🏫 <b>ПОИСК: «{esc(query)}»</b>\n📅 <i>{period_title}</i>\n\n'
    for dt_obj, day_name, date_str, week_num, matches in all_day_results:
        if len(target_dates) > 1:
            text += f'🔹 <b><u>{day_name.upper()} ({dt_obj.strftime("%d.%m")})</u></b>\n'
        grouped = {}
        for time_slot, teacher, room, subj, l_type, grp, is_repl in matches:
            key = (time_slot, teacher, room, subj, l_type, is_repl)
            grouped.setdefault(key, []).append(grp)

        for (time_slot, teacher, room, subj, l_type, is_repl), grps in sorted(grouped.items(), key=lambda x: x[0][0]):
            pair_prefix = format_pair_prefix(time_slot)
            grps_str = ', '.join(grps)
            if is_repl:
                text += (
                    f'🔄 {pair_prefix}\n'
                    f'👉 <b>{esc(subj)}</b>\n'
                    f'🎓 <i>Группы: {esc(grps_str)}</i>\n\n'
                )
            else:
                text += (
                    f'{pair_prefix}\n'
                    f'👤 <b>{esc(teacher)}</b>\n'
                    f'📍 <u>{esc(room)}</u>  •  📘 {esc(subj)} <i>({esc(l_type)})</i>\n'
                    f'🎓 <i>Группы: {esc(grps_str)}</i>\n\n'
                )

    chunks = split_long_html_message(text.strip(), max_len=3900)
    for idx, ch in enumerate(chunks):
        kb = markup if idx == len(chunks) - 1 else None
        bot.send_message(chat_id, ch, reply_markup=kb, parse_mode='HTML')


def process_teacher_search(message, payload):
    period = (payload or {}).get('teacher_period', 'today')
    clear_user_state(message.chat.id, keep_payload=True)
    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        bot.send_message(message.chat.id, '❌ Поиск отменён.')
        return

    query = message.text.strip()
    if len(query) < 2:
        set_user_state(message.chat.id, 'awaiting_teacher_query', {'teacher_period': period})
        bot.send_message(
            message.chat.id,
            '⚠️ Введи хотя бы 2 буквы фамилии преподавателя (или «Отмена»):',
        )
        return

    all_teachers = get_all_unique_teachers()
    q_low = re.sub(r'\s+', ' ', query.lower()).strip()
    q_no_dots = re.sub(r'[.\s]+', '', q_low)

    def _matches_teacher(t_str):
        t_low = t_str.lower()
        t_no_dots = re.sub(r'[.\s]+', '', t_low)
        t_sur = _teacher_surname_only(t_str)
        return (
            q_low in t_low
            or (len(q_no_dots) >= 2 and q_no_dots in t_no_dots)
            or (q_low == t_sur)
        )

    matched_teachers = [t for t in all_teachers if _matches_teacher(t)]

    # Если запрос совпадает с фамилией нескольких преподавателей (например, "Суюндуков" -> "Суюндуков И.В.", "Суюндуков Я.Т.")
    # или найдено несколько вариантов — всегда показываем выбор конкретных преподавателей
    if len(matched_teachers) > 1:
        suggestions = matched_teachers[:8]
        set_user_state(message.chat.id, '', {
            'teacher_suggestions': suggestions,
            'teacher_period': period,
            'teacher_raw_query': query,
        })
        markup = InlineKeyboardMarkup(row_width=1)
        for idx, t_name in enumerate(suggestions):
            markup.add(
                InlineKeyboardButton(
                    f'👤 {t_name}',
                    callback_data=f'tsearch_pick_{idx}',
                )
            )
        markup.add(
            InlineKeyboardButton(
                f'🔎 Искать по всем совпадениям («{short_text(query, 18)}»)',
                callback_data='tsearch_pick_all',
            )
        )
        bot.send_message(
            message.chat.id,
            f'💡 Найдено несколько преподавателей по запросу <b>«{esc(query)}»</b>.\n'
            f'Выбери нужного из списка или выполни общий поиск:',
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    final_query = matched_teachers[0] if len(matched_teachers) == 1 else query
    execute_teacher_search_for_period(message.chat.id, final_query, period)


def show_free_rooms_day_choice(chat_id, msg_id=None):
    today = now_local()
    tomorrow = today + timedelta(days=1)
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(
            f'📍 Сегодня ({DAYS_RUS[today.weekday()]}, {today.strftime("%d.%m")})',
            callback_data='freerooms_day_0',
        ),
        InlineKeyboardButton(
            f'⏭ Завтра ({DAYS_RUS[tomorrow.weekday()]}, {tomorrow.strftime("%d.%m")})',
            callback_data='freerooms_day_1',
        ),
        InlineKeyboardButton('🔙 Назад', callback_data='search_back'),
    )
    text = (
        '🚪 <b>СВОБОДНЫЕ АУДИТОРИИ</b>\n\n'
        '<b>Шаг 1 из 2.</b> Выбери день недели:'
    )
    if msg_id:
        safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        return
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def show_free_rooms_pair_choice(chat_id, msg_id=None, day_offset=0):
    target_dt = now_local() + timedelta(days=day_offset)
    _, day_name = get_current_week_and_day(target_dt)
    date_str = target_dt.strftime('%d.%m.%Y')
    day_label = 'Сегодня' if day_offset == 0 else 'Завтра'

    markup = InlineKeyboardMarkup(row_width=2)
    btns = []
    for p_num in range(1, 7):
        emoji = PAIR_EMOJIS.get(p_num, f'{p_num}.')
        slot = NUM_TO_TIME_SLOT[p_num]
        btns.append(
            InlineKeyboardButton(
                f'{emoji} {p_num} пара ({slot[:5]})',
                callback_data=f'freerooms_pair_{day_offset}_{p_num}',
            )
        )
    markup.add(*btns)
    markup.add(InlineKeyboardButton('🔙 Сменить день', callback_data='search_rooms'))

    text = (
        f'🚪 <b>СВОБОДНЫЕ АУДИТОРИИ</b>\n'
        f'📅 <i>{day_label}: {day_name}, {date_str}</i>\n\n'
        f'<b>Шаг 2 из 2.</b> Выбери номер пары:'
    )
    if msg_id:
        safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        return
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def extract_rooms_from_text(room_str):
    raw = str(room_str or '').strip()
    if not raw:
        return []
    if raw.lower().startswith('ауд.'):
        rest = raw[4:].strip()
        parts = [p.strip() for p in rest.split(',') if p.strip()]
        return [f'ауд. {p}' for p in parts]
    return [raw]


def show_free_rooms_result(chat_id, msg_id, pair_num, day_offset=0, user_id=None, scope='fac'):
    target_dt = now_local() + timedelta(days=day_offset)
    week_num, day_name = get_current_week_and_day(target_dt)
    date_str = target_dt.strftime('%d.%m.%Y')
    time_slot = NUM_TO_TIME_SLOT.get(pair_num, '')

    user_fac = None
    if user_id is not None:
        u_data = get_user_data(user_id)
        if u_data and u_data[7]:
            user_fac = u_data[7]

    use_fac_filter = (scope == 'fac' and bool(user_fac))

    with db_connection() as conn:
        cursor = conn.cursor()

        if use_fac_filter:
            cursor.execute('SELECT DISTINCT room FROM schedules WHERE faculty = ?', (user_fac,))
        else:
            cursor.execute('SELECT DISTINCT room FROM schedules')
        all_raw_rooms = [r[0] for r in cursor.fetchall()]

        all_rooms = set()
        for r_str in all_raw_rooms:
            for single in extract_rooms_from_text(r_str):
                if 'конф-зал' not in single.lower() and 'не указана' not in single.lower():
                    all_rooms.add(single)

        cursor.execute(
            'SELECT s.room, s.group_name, s.faculty FROM schedules s '
            'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
            'WHERE s.day_of_week = ? AND sw.week_number = ? AND s.time_slot = ?',
            (day_name, week_num, time_slot),
        )
        busy_rows = cursor.fetchall()

        cursor.execute(
            'SELECT COALESCE(NULLIF(faculty, ""), ""), group_name FROM cancelled_lessons WHERE target_date = ? AND time_slot = ?',
            (date_str, time_slot),
        )
        canc_raw = cursor.fetchall()
        cancelled_keys = {(r[0], r[1]) for r in canc_raw}
        cancelled_unscoped = {r[1] for r in canc_raw if not r[0]}

        cursor.execute(
            'SELECT COALESCE(NULLIF(faculty, ""), ""), group_name, subject_info FROM replacements WHERE target_date = ? AND time_slot = ?',
            (date_str, time_slot),
        )
        repl_rows_rooms = cursor.fetchall()
        replaced_keys = {(r[0], r[1]) for r in repl_rows_rooms}
        replaced_unscoped = {r[1] for r in repl_rows_rooms if not r[0]}
        repl_texts = [r[2] for r in repl_rows_rooms]

    busy_rooms = set()
    for r_str, grp, r_fac in busy_rows:
        eff_fac = r_fac or infer_faculty_by_group(grp)
        if (eff_fac, grp) in cancelled_keys or grp in cancelled_unscoped:
            continue
        if (eff_fac, grp) in replaced_keys or grp in replaced_unscoped:
            continue
        if week_num in core.PRACTICE_WEEKS.get(grp, set()):
            continue
        for single in extract_rooms_from_text(r_str):
            busy_rooms.add(single)

    for r_txt in repl_texts:
        for room in all_rooms:
            num_part = room.replace('ауд.', '').strip()
            if re.search(rf'(?<!\d){re.escape(num_part)}(?!\d)', r_txt):
                busy_rooms.add(room)

    free_rooms = sorted(all_rooms - busy_rooms)
    busy_sorted = sorted(busy_rooms & all_rooms) if use_fac_filter else sorted(busy_rooms)

    scope_label = f'🏛 Факультет: {esc(get_faculty_short(user_fac))}' if use_fac_filter else '🌐 Все факультеты'
    pair_prefix = format_pair_prefix(time_slot)
    text = (
        f'🚪 <b>СВОБОДНЫЕ АУДИТОРИИ</b>\n'
        f'📅 <i>{day_name}, {date_str} ({week_num}-я нед.)</i>\n'
        f'{pair_prefix}  •  {scope_label}\n\n'
    )

    if day_name == 'Воскресенье':
        text += '🎉 <i>Воскресенье — все аудитории закрыты на выходной.</i>'
    else:
        if free_rooms:
            text += '✅ <b>Свободны по расписанию:</b>\n' + ', '.join(f'<code>{esc(r)}</code>' for r in free_rooms) + '\n\n'
        else:
            text += '⚠️ <i>Свободных аудиторий из списка на эту пару не найдено.</i>\n\n'

        if busy_sorted:
            text += '🔴 <b>Заняты парами:</b>\n' + ', '.join(esc(r) for r in busy_sorted)

    markup = InlineKeyboardMarkup(row_width=1)
    if user_fac:
        if use_fac_filter:
            markup.add(
                InlineKeyboardButton(
                    '🌐 Искать по всем факультетам',
                    callback_data=f'freerooms_pair_{day_offset}_{pair_num}_all',
                )
            )
        else:
            markup.add(
                InlineKeyboardButton(
                    f'🏛 Только мой факультет ({get_faculty_short(user_fac)})',
                    callback_data=f'freerooms_pair_{day_offset}_{pair_num}_fac',
                )
            )
    markup.add(
        InlineKeyboardButton('🔄 Выбрать другую пару', callback_data=f'freerooms_day_{day_offset}'),
        InlineKeyboardButton('🔙 В меню поиска', callback_data='search_back'),
    )
    safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')


@bot.inline_handler(func=lambda query: True)
def inline_schedule_handler(inline_query):
    log_stat_event('inline')
    user_id = inline_query.from_user.id
    user = get_user_data(user_id)
    results = []

    if not user or not is_group_valid(user[7], user[0], user[1]):
        results.append(
            InlineQueryResultArticle(
                id='need_reg',
                title='⚠️ Сначала выбери факультет и группу в боте',
                description='Открой бота и нажми /start, чтобы привязать свою группу',
                input_message_content=InputTextMessageContent(
                    '⚠️ Чтобы делиться расписанием и ДЗ, сначала нужно выбрать свой факультет и группу в боте (/start).'
                ),
            )
        )
        try:
            bot.answer_inline_query(inline_query.id, results, cache_time=5, is_personal=True)
        except Exception:
            pass
        return

    course, group_name, fac_code = user[0], user[1], user[7]
    eff_fac = fac_code or infer_faculty_by_group(group_name)
    q = (inline_query.query or '').strip().lower()
    today = now_local()

    if not q or any(w in q for w in ['сегодня', 'today', 'пары', 'расп']):
        sched_today = truncate_html_safe(
            get_schedule_text(today, course, group_name, faculty_code=fac_code), 4096
        )
        results.append(
            InlineQueryResultArticle(
                id=f'today_{today.strftime("%Y%m%d")}',
                title=f'📅 Расписание на сегодня ({today.strftime("%d.%m")})',
                description=f'{get_faculty_short(eff_fac)} • {group_name}',
                input_message_content=InputTextMessageContent(sched_today, parse_mode='HTML'),
            )
        )

    if not q or any(w in q for w in ['завтра', 'tomorrow', 'пары', 'расп']):
        tomorrow = today + timedelta(days=1)
        sched_tomorrow = truncate_html_safe(
            get_schedule_text(tomorrow, course, group_name, faculty_code=fac_code), 4096
        )
        results.append(
            InlineQueryResultArticle(
                id=f'tomorrow_{tomorrow.strftime("%Y%m%d")}',
                title=f'⏭ Расписание на завтра ({tomorrow.strftime("%d.%m")})',
                description=f'{get_faculty_short(eff_fac)} • {group_name}',
                input_message_content=InputTextMessageContent(sched_tomorrow, parse_mode='HTML'),
            )
        )

    if not q or any(w in q for w in ['дз', 'домаш', 'hw', 'задан']):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name '
                'FROM homeworks WHERE (faculty = ? OR faculty = "") AND group_name = ?',
                (eff_fac, group_name),
            )
            all_hws = cursor.fetchall()

        today_d = today.date()
        today_slot_map, _ = get_day_lessons_for_date(today, course, group_name, faculty_code=eff_fac)
        upcoming = []
        for hw_id, h_date, h_subj, h_text, h_photo, h_doc_id, h_doc_name in all_hws:
            try:
                d_obj = datetime.strptime(h_date, '%d.%m.%Y').date()
                if d_obj < today_d:
                    continue
                if d_obj == today_d and is_homework_passed_today(h_subj, today_slot_map, now_dt=today):
                    continue
                upcoming.append((d_obj, hw_id, h_date, h_subj, h_text, h_photo, h_doc_id, h_doc_name))
            except ValueError:
                pass
        upcoming.sort(key=lambda x: (x[0], x[1]))

        bot_uname = get_bot_username()

        if upcoming:
            hw_msg = f'📚 <b>АКТУАЛЬНЫЕ ДЗ — {esc(group_name)}</b>\n\n'
            has_any_media = False
            for _, hw_id, h_date, h_subj, h_text, h_photo, h_doc_id, h_doc_name in upcoming[:10]:
                p_list = parse_photo_ids(h_photo)
                p_cnt = len(p_list)
                badges = []
                if p_cnt:
                    badges.append(f'📷 {p_cnt} фото')
                    has_any_media = True
                if h_doc_id:
                    badges.append(f'📎 {esc(h_doc_name or "файл")}')
                    has_any_media = True
                p_badge = f' [{", ".join(badges)}]' if badges else ''
                desc = esc(h_text) if h_text else '<i>(см. вложение ниже или в боте)</i>'
                hw_msg += f'📅 <b>{h_date}</b> — <b>{esc(h_subj)}</b>{p_badge}\n📝 {desc}\n\n'
            hw_preview = f'Заданий: {len(upcoming)} (ближайшее: {upcoming[0][3]})'
            summary_kb = None
            if has_any_media and bot_uname:
                summary_kb = InlineKeyboardMarkup()
                summary_kb.add(
                    InlineKeyboardButton(
                        '📎 Открыть все вложения в боте',
                        url=f'https://t.me/{bot_uname}?start=hw_all',
                    )
                )
        else:
            hw_msg = f'📚 <b>ДОМАШНИЕ ЗАДАНИЯ — {esc(group_name)}</b>\n\n✨ Актуальных заданий пока нет!'
            hw_preview = 'Актуальных заданий пока нет'
            summary_kb = None

        results.append(
            InlineQueryResultArticle(
                id=f'hw_summary_{today.strftime("%Y%m%d")}',
                title='📚 Все актуальные ДЗ (списком)',
                description=hw_preview,
                input_message_content=InputTextMessageContent(
                    truncate_html_safe(hw_msg.strip(), 4096), parse_mode='HTML'
                ),
                reply_markup=summary_kb,
            )
        )

        # Отдельные карточки по каждому ДЗ (с реальным фото / документом, если есть вложение!)
        for _, hw_id, h_date, h_subj, h_text, h_photo, h_doc_id, h_doc_name in upcoming[:15]:
            p_list = parse_photo_ids(h_photo)
            extra_notes = []
            if len(p_list) > 1:
                extra_notes.append(f'📷 Прикреплено фото: {len(p_list)} шт. (в чате отображается 1-е)')
            if p_list and h_doc_id:
                extra_notes.append(f'📎 Также прикреплён файл: {esc(h_doc_name or "документ")}')
            extra_footer = ('\n\n<i>' + '\n'.join(extra_notes) + '</i>') if extra_notes else ''

            card_caption = (
                f'📚 <b>ДОМАШНЕЕ ЗАДАНИЕ ({esc(group_name)})</b>\n'
                f'📅 <b>Дедлайн:</b> {h_date}\n'
                f'📘 <b>{esc(h_subj)}</b>\n\n'
                f'📝 {esc(h_text) if h_text else "<i>(см. прикреплённое вложение)</i>"}'
                f'{extra_footer}'
            )

            item_kb = None
            if (len(p_list) > 1 or (p_list and h_doc_id)) and bot_uname:
                item_kb = InlineKeyboardMarkup()
                item_kb.add(
                    InlineKeyboardButton(
                        '📎 Все фото и файлы в боте',
                        url=f'https://t.me/{bot_uname}?start=hw_item_{hw_id}',
                    )
                )

            short_desc = (h_text[:70] + '…' if len(h_text) > 70 else h_text) if h_text else 'Вложение к заданию'

            if p_list:
                results.append(
                    InlineQueryResultCachedPhoto(
                        id=f'hw_photo_{hw_id}',
                        photo_file_id=p_list[0],
                        title=f'📷 ДЗ ({h_date}): {h_subj}',
                        description=short_desc,
                        caption=truncate_html_safe(card_caption, 1024),
                        parse_mode='HTML',
                        reply_markup=item_kb,
                    )
                )
            elif h_doc_id:
                results.append(
                    InlineQueryResultCachedDocument(
                        id=f'hw_doc_{hw_id}',
                        title=f'📎 ДЗ ({h_date}): {h_subj}',
                        document_file_id=h_doc_id,
                        description=f'{h_doc_name or "Файл"} • {short_desc}',
                        caption=truncate_html_safe(card_caption, 1024),
                        parse_mode='HTML',
                        reply_markup=item_kb,
                    )
                )
            else:
                results.append(
                    InlineQueryResultArticle(
                        id=f'hw_text_{hw_id}',
                        title=f'📝 ДЗ ({h_date}): {h_subj}',
                        description=short_desc,
                        input_message_content=InputTextMessageContent(
                            truncate_html_safe(card_caption, 4096), parse_mode='HTML'
                        ),
                    )
                )

    try:
        bot.answer_inline_query(inline_query.id, results, cache_time=10, is_personal=True)
    except Exception:
        pass


@bot.callback_query_handler(
    func=lambda call: call.data in ('search_back', 'search_teacher', 'search_rooms', 'tsearch_pick_all')
    or call.data.startswith(('date_', 'week_', 'tsearch_', 'freerooms_'))
)
def handle_schedule_and_search_callbacks(call):
    sync_username(call.from_user)
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    if not (data == 'tsearch_pick_all' or data.startswith('tsearch_pick_')):
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

    if data == 'search_back':
        open_search_menu(chat_id, msg_id)
        return

    if data == 'search_teacher':
        clear_user_state(user_id, keep_payload=True)
        show_teacher_period_choice(chat_id, msg_id)
        return

    if data == 'tsearch_noop':
        return

    if data.startswith('tsearch_period_'):
        clear_user_state(user_id, keep_payload=True)
        period = data.replace('tsearch_period_', '')
        show_teacher_buttons_menu(chat_id, msg_id, user_id, period, scope=None, page=0)
        return

    if data.startswith('tsearch_list_'):
        parts = data.replace('tsearch_list_', '').split('_')
        if len(parts) >= 3:
            period, scope, page_num = parts[0], parts[1], int(parts[2])
            show_teacher_buttons_menu(chat_id, msg_id, user_id, period, scope=scope, page=page_num)
        return

    if data.startswith('tsearch_btn_'):
        _, state = get_user_state(user_id)
        page_list = (state or {}).get('teacher_page_list') or []
        idx = int(data.replace('tsearch_btn_', ''))
        if 0 <= idx < len(page_list):
            period = (state or {}).get('teacher_period', 'today')
            clear_user_state(user_id, keep_payload=True)
            execute_teacher_search_for_period(chat_id, page_list[idx], period)
        return

    if data == 'tsearch_pick_all':
        _, state = get_user_state(user_id)
        if not state or 'teacher_raw_query' not in state:
            try:
                bot.answer_callback_query(call.id, '⚠️ Список устарел, повтори поиск')
            except Exception:
                pass
            return
        try:
            bot.answer_callback_query(call.id)
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        execute_teacher_search_for_period(
            chat_id, state['teacher_raw_query'], state.get('teacher_period', 'today')
        )
        return

    if data.startswith('tsearch_pick_'):
        _, state = get_user_state(user_id)
        if not state or 'teacher_suggestions' not in state:
            try:
                bot.answer_callback_query(call.id, '⚠️ Список устарел, повтори поиск')
            except Exception:
                pass
            return
        idx = int(data.replace('tsearch_pick_', ''))
        suggestions = state['teacher_suggestions']
        if idx >= len(suggestions):
            return
        try:
            bot.answer_callback_query(call.id)
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        execute_teacher_search_for_period(
            chat_id, suggestions[idx], state.get('teacher_period', 'today')
        )
        return

    if data.startswith('tsearch_re_'):
        period = data.replace('tsearch_re_', '')
        _, state = get_user_state(user_id)
        if not state or 'last_teacher_query' not in state:
            show_teacher_period_choice(chat_id, msg_id)
            return
        execute_teacher_search_for_period(chat_id, state['last_teacher_query'], period)
        return

    if data == 'search_rooms':
        clear_user_state(user_id, keep_payload=True)
        show_free_rooms_day_choice(chat_id, msg_id)
        return

    if data.startswith('freerooms_day_'):
        day_offset = int(data.replace('freerooms_day_', ''))
        show_free_rooms_pair_choice(chat_id, msg_id, day_offset=day_offset)
        return

    if data.startswith('freerooms_pair_'):
        parts = data.replace('freerooms_pair_', '').split('_')
        day_offset = int(parts[0])
        pair_num = int(parts[1])
        scope = parts[2] if len(parts) > 2 else 'fac'
        show_free_rooms_result(chat_id, msg_id, pair_num, day_offset=day_offset, user_id=user_id, scope=scope)
        return

    if data.startswith('date_'):
        offset = int(data.split('_')[1])
        if int(chat_id) < 0:
            binding = get_group_chat_binding(chat_id)
            if not binding or not is_group_valid(binding[0], binding[1], binding[2]):
                return
            fac_code, course, group_name = binding
        else:
            user = get_user_data(user_id)
            if not user or not is_group_valid(user[7], user[0], user[1]):
                bot.send_message(
                    chat_id,
                    '👇 Сначала выбери свой <b>факультет</b> и группу:',
                    reply_markup=build_faculty_keyboard(),
                    parse_mode='HTML',
                )
                return
            course, group_name, fac_code = user[0], user[1], user[7]
        text = get_schedule_text(
            now_local() + timedelta(days=offset), course, group_name, faculty_code=fac_code
        )
        kb = get_pagination_keyboard(offset, group_name)
        if len(text) <= 4000:
            safe_edit_message_text(text, chat_id, msg_id, reply_markup=kb, parse_mode='HTML')
            return
        else:
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            send_long_html_message(chat_id, text, reply_markup=kb, parse_mode='HTML')
        return

    if data.startswith('week_'):
        week_offset = int(data.split('_')[1])
        if int(chat_id) < 0:
            binding = get_group_chat_binding(chat_id)
            if not binding or not is_group_valid(binding[0], binding[1], binding[2]):
                return
            fac_code, course, group_name = binding
        else:
            user = get_user_data(user_id)
            if not user or not is_group_valid(user[7], user[0], user[1]):
                bot.send_message(
                    chat_id,
                    '👇 Сначала выбери свой <b>факультет</b> и группу:',
                    reply_markup=build_faculty_keyboard(),
                    parse_mode='HTML',
                )
                return
            course, group_name, fac_code = user[0], user[1], user[7]
        target_date = now_local() + timedelta(days=week_offset * 7)
        send_week_schedule_message(
            chat_id, target_date, course, group_name, week_offset=week_offset, edit_msg_id=msg_id, faculty_code=fac_code
        )
        return
