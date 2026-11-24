from datetime import datetime, timedelta
import json
import os
import tempfile
import time
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

import core
from core import (
    ADMIN_USERNAME,
    FACULTY_ORDER,
    LOCAL_TZ,
    MAILING_QUEUE,
    NUM_TO_TIME_SLOT,
    PAIR_EMOJIS,
    SCHEDULE_JSON_PATH,
    SEMESTER_META_PATH,
    atomic_write_json,
    bot,
    build_faculty_keyboard,
    calc_week_number_for_start,
    clear_user_state,
    db_connection,
    detect_faculty_from_text,
    enqueue_hw_media,
    enqueue_message,
    esc,
    extract_academic_start_from_title,
    find_course_and_faculty_by_group,
    format_pair_prefix,
    get_academic_year_end_date,
    get_academic_year_label,
    get_academic_year_start_by_year,
    get_bound_group_chats,
    get_current_week_and_day,
    get_default_academic_year_start,
    get_faculty_full,
    get_faculty_short,
    get_main_keyboard,
    get_managed_context,
    get_pair_num,
    get_pending_docx_import,
    get_role_label,
    get_user_data,
    get_user_state,
    infer_faculty_by_group,
    invalidate_user_cache,
    is_admin_user,
    is_extra_hw_lists_enabled,
    is_group_valid,
    is_user_starosta,
    now_local,
    reload_schedule_tables,
    resolve_target_user,
    safe_edit_message_text,
    save_pending_docx_import,
    save_semester_meta,
    send_db_backup,
    set_extra_hw_lists_enabled,
    set_user_state,
    short_text,
    split_long_html_message,
    start_onboarding,
    sync_username,
)
from docx_parser import convert_doc_to_docx_if_needed, parse_schedule_docx_file
from homework import (
    ask_hw_content,
    open_add_homework,
    send_homeworks_for_date_view,
    show_hw_subject_selection,
)
from schedule_search import get_day_lessons_for_date


def open_special_group_menu(chat_id, user_obj, message_id=None):
    if not is_user_starosta(user_obj):
        return
    _, target_group, target_fac = get_managed_context(user_obj)
    if not target_group:
        bot.send_message(chat_id, '⚠️ Сначала выбери свой факультет и группу через /start.')
        return

    extra_on = is_extra_hw_lists_enabled(target_fac, target_group)
    extra_lbl = '📑 Доп. списки: ВКЛ' if extra_on else '📑 Доп. списки: ВЫКЛ'

    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton('➕ Замена', callback_data='spec_add_repl'),
        InlineKeyboardButton('🗑 Отмена пары', callback_data='spec_del_pair'),
    )
    markup.row(
        InlineKeyboardButton('📋 Активные правки', callback_data='spec_active_changes'),
        InlineKeyboardButton('📝 Добавить ДЗ', callback_data='spec_add_hw'),
    )
    markup.row(InlineKeyboardButton(extra_lbl, callback_data='spec_toggle_extra_lists'))
    if is_admin_user(user_obj):
        markup.row(InlineKeyboardButton('🔙 Назад', callback_data='spec_back'))

    text = (
        f'🎓 <b>УПРАВЛЕНИЕ ГРУППОЙ</b>\n'
        f'<blockquote>🛡 <b>Группа:</b> {esc(target_group)}</blockquote>'
    )
    if message_id:
        safe_edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def open_special_system_menu(chat_id, user_obj, message_id=None):
    if not is_admin_user(user_obj):
        return
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton('📥 Обновить расписание (.docx/.doc)', callback_data='spec_upload_docx'),
        InlineKeyboardButton('💾 Бэкап БД сейчас', callback_data='spec_backup_now'),
        InlineKeyboardButton('🔄 Сбросить учебный год (обнулить всех)', callback_data='spec_reset_year_1'),
        InlineKeyboardButton('🔙 Назад', callback_data='spec_back'),
    )
    text = '⚙️ <b>СИСТЕМА И БАЗА ДАННЫХ</b>\n\n<i>Выбери действие:</i>'
    if message_id:
        safe_edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def open_special_menu(chat_id, user_obj, message_id=None):
    starosta_flag = is_user_starosta(user_obj)
    admin_flag = is_admin_user(user_obj)

    if not (starosta_flag or admin_flag):
        bot.send_message(
            chat_id,
            '⛔ Этот раздел доступен только старостам и администраторам. Добавить домашнее задание можно в разделе <b>«📚 Домашка»</b>.',
            parse_mode='HTML',
        )
        return

    user = get_user_data(user_obj.id)
    has_valid_group = bool(user and is_group_valid(user[7], user[0], user[1]))

    if not admin_flag:
        if not has_valid_group:
            bot.send_message(chat_id, '⚠️ Сначала выбери свой факультет и группу через /start.')
            return
        open_special_group_menu(chat_id, user_obj, message_id=message_id)
        return

    _, target_group, _ = get_managed_context(user_obj)
    group_display = esc(target_group) if (target_group and has_valid_group) else 'Не выбрана'
    text = (
        '🛠 <b>СПЕЦВОЗМОЖНОСТИ</b>\n'
        f'<blockquote>👑 <b>Администратор</b> · {group_display}</blockquote>'
    )

    markup = InlineKeyboardMarkup(row_width=2)
    if has_valid_group:
        markup.row(InlineKeyboardButton('🎓 Управление группой (пары / ДЗ)', callback_data='spec_sec_group'))
    markup.row(
        InlineKeyboardButton('📊 Статистика', callback_data='spec_stats'),
        InlineKeyboardButton('📣 Рассылка', callback_data='spec_broadcast'),
    )
    markup.row(InlineKeyboardButton('⚙️ Система (расписание, БД, сброс)', callback_data='spec_sec_system'))

    if message_id:
        safe_edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def notify_users_new_academic_year(user_ids, new_year_label, exclude_user_id=None):
    """Отправляет уведомление о новом учебном годе с актуальной клавиатурой факультетов."""
    kb = build_faculty_keyboard()
    msg_text = (
        f'🎓 <b>С НОВЫМ УЧЕБНЫМ ГОДОМ ({esc(new_year_label)})!</b>\n\n'
        f'Счётчик учебных недель обнулён, старое расписание очищено, а привязка к прошлому курсу и группе сброшена.\n'
        f'Как только появится актуальное расписание твоего факультета, выбери свой <b>факультет, новый курс и направление</b>:'
    )
    for u_id in user_ids:
        if exclude_user_id and int(u_id) == int(exclude_user_id):
            continue
        enqueue_message(u_id, msg_text, reply_markup=kb, parse_mode='HTML')


def reset_for_new_academic_year(
    new_start_dt,
    clear_schedule_file=True,
    force=False,
    manual_backup_chat_id=None,
    notify_users=True,
):
    """
    Полный переход на новый учебный год (по умолчанию срабатывает 1 раз на учебный год,
    при force=True — принудительно по подтверждённой команде администратора):
    - делает бэкап БД;
    - очищает schedule.json (если clear_schedule_file=True), таблицы расписания, замены, отмены, ДЗ и очереди;
    - сбрасывает курс, группу и роль старосты у всех пользователей (кроме главного админа);
    - обновляет core.ACADEMIC_YEAR_START_DATE и semester_meta.json на новый учебный год.
    Возвращает кортеж (reset_users_ids, cleared_starostas_count, new_year_label).
    """
    new_start_dt = new_start_dt.replace(tzinfo=LOCAL_TZ)
    new_year_label = get_academic_year_label(new_start_dt)

    if not force:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)')
            cursor.execute("SELECT value FROM app_meta WHERE key = 'last_global_reset_year'")
            row = cursor.fetchone()
            if row and row[0] == new_year_label:
                return [], 0, new_year_label

    # Обязательно делаем резервную копию базы перед очисткой
    send_db_backup(manual_chat_id=manual_backup_chat_id)

    # Обнуляем счётчик недель и сбрасываем метаданные прошлого года
    core.ACADEMIC_YEAR_START_DATE = new_start_dt
    core.PRACTICE_WEEKS = {}

    if clear_schedule_file:
        atomic_write_json(SCHEDULE_JSON_PATH, [])

    atomic_write_json(SEMESTER_META_PATH, {
        'academic_year': new_year_label,
        'academic_year_start': new_start_dt.strftime('%d.%m.%Y'),
        'semester_title': '',
        'practice_weeks': {},
        'reset_done_faculties': [],
        'updated_at': now_local().strftime('%d.%m.%Y %H:%M'),
    })

    with db_connection() as conn:
        cursor = conn.cursor()
        if clear_schedule_file:
            reload_schedule_tables(cursor, conn, force=True)

        # Полная очистка данных прошлого учебного года
        cursor.execute('DELETE FROM replacements')
        cursor.execute('DELETE FROM cancelled_lessons')
        cursor.execute('DELETE FROM queue_slots')
        cursor.execute('DELETE FROM queues')
        cursor.execute('DELETE FROM hw_done')
        cursor.execute('DELETE FROM homeworks')
        cursor.execute('DELETE FROM group_chats')
        cursor.execute('DELETE FROM group_settings')

        cursor.execute(
            'SELECT user_id FROM users '
            'WHERE course > 0 OR (group_name IS NOT NULL AND group_name != "") '
            'OR (managed_group IS NOT NULL AND managed_group != "") '
            'OR is_starosta = 1'
        )
        reset_users_ids = [r[0] for r in cursor.fetchall()]

        cursor.execute(
            'SELECT COUNT(*) FROM users '
            "WHERE is_starosta = 1 AND LOWER(COALESCE(username, '')) != ?",
            (ADMIN_USERNAME.lower(),),
        )
        cleared_starostas_count = cursor.fetchone()[0]

        # Снимаем курс, группу и роль старосты у всех, кроме главного админа
        cursor.execute(
            'UPDATE users SET course = 0, group_name = "", managed_group = "", is_starosta = 0, is_hw_manager = 0 '
            "WHERE LOWER(COALESCE(username, '')) != ?",
            (ADMIN_USERNAME.lower(),),
        )
        cursor.execute(
            'UPDATE users SET course = 0, group_name = "", managed_group = "" '
            "WHERE LOWER(COALESCE(username, '')) = ?",
            (ADMIN_USERNAME.lower(),),
        )

        cursor.execute('DELETE FROM user_states')
        cursor.execute('CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)')
        cursor.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('last_global_reset_year', ?)",
            (new_year_label,),
        )

    with core._USER_STATES_LOCK:
        core._USER_STATES_CACHE.clear()
    invalidate_user_cache()

    if notify_users and reset_users_ids:
        notify_users_new_academic_year(
            reset_users_ids,
            new_year_label,
            exclude_user_id=manual_backup_chat_id,
        )

    return reset_users_ids, cleared_starostas_count, new_year_label


def check_academic_year_rollover():
    """Автоматический переход на новый учебный год в день обнуления счётчика недель (на любые будущие годы)."""
    now_dt = now_local()
    year_end_dt = get_academic_year_end_date(core.ACADEMIC_YEAR_START_DATE)
    if now_dt.date() > year_end_dt.date():
        next_start_dt = get_default_academic_year_start(now_dt)
        reset_for_new_academic_year(next_start_dt, clear_schedule_file=True, force=False, notify_users=True)


def process_full_reset_confirm(message, payload):
    clear_user_state(message.chat.id)
    if not is_admin_user(message.from_user):
        return

    raw_text = (message.text or '').strip()
    if raw_text != 'УВЕРЕН':
        bot.send_message(
            message.chat.id,
            '❌ <b>Сброс учебного года отменён.</b>\n'
            'Для подтверждения нужно было написать строго <code>УВЕРЕН</code> (заглавными буквами).',
            parse_mode='HTML',
        )
        return

    now_dt = now_local()
    # Если ручной сброс запускается с мая по декабрь — сразу переводим счётчик на учебный год now_dt.year/(now_dt.year+1)
    # (чтобы после ручного сброса в июне не сработал повторный автосброс 30/31 августа).
    start_year = now_dt.year - 1 if now_dt.month <= 4 else now_dt.year
    target_start_dt = get_academic_year_start_by_year(start_year)

    reset_users_ids, starostas_count, yr_label = reset_for_new_academic_year(
        target_start_dt,
        clear_schedule_file=True,
        force=True,
        manual_backup_chat_id=message.chat.id,
        notify_users=True,
    )
    bot.send_message(
        message.chat.id,
        f'✅ <b>ПОЛНЫЙ СБРОС УЧЕБНОГО ГОДА ВЫПОЛНЕН!</b>\n\n'
        f'📅 <b>Новый учебный год:</b> {esc(yr_label)} (старт 1-й недели: <code>{target_start_dt.strftime("%d.%m.%Y")}</code>)\n'
        f'🗑 <b>База расписания (schedule.json):</b> полностью очищена\n'
        f'🧹 <b>Замены, отмены, ДЗ и очереди:</b> очищены\n'
        f'👥 <b>Сброшены курс и группа у пользователей:</b> {len(reset_users_ids)}\n'
        f'🌟 <b>Сняты роли старост:</b> {starostas_count}\n'
        f'💾 <b>Резервная копия старой БД:</b> отправлена выше\n\n'
        f'<i>Теперь можно загружать свежие файлы расписания (.docx/.doc) и назначать новых старост через /starosta.</i>',
        parse_mode='HTML',
    )


# --- ОБРАБОТЧИК ИМПОРТА РАСПИСАНИЯ ИЗ .DOCX / .DOC (ДЛЯ АДМИНА) ---
def apply_parsed_docx_schedule(chat_id, admin_user_id, import_token, year_start_dt, faculty_code=None):
    payload = get_pending_docx_import(import_token, pop=True)
    if not payload:
        bot.send_message(chat_id, '⚠️ Сессия импорта устарела. Отправь файл расписания заново.')
        return

    target_fac = faculty_code or payload.get('faculty_code') or 'ЕМФ'
    records = payload['records']
    for r in records:
        r['faculty'] = target_fac
    practice_dict = payload['practice_dict']
    semester_title = payload['semester_title']
    clear_old_changes = payload.get('clear_old_changes', True)

    existing_meta = {}
    if os.path.exists(SEMESTER_META_PATH):
        try:
            with open(SEMESTER_META_PATH, 'r', encoding='utf-8') as f:
                existing_meta = json.load(f)
        except Exception:
            existing_meta = {}

    new_year_label = get_academic_year_label(year_start_dt)
    saved_year_label = existing_meta.get('academic_year') or get_academic_year_label(core.ACADEMIC_YEAR_START_DATE)
    is_new_academic_year = new_year_label != saved_year_label

    reset_users_ids = []
    cleared_starostas = 0
    if is_new_academic_year:
        # Полный сброс старого года (уведомления отправим ПОСЛЕ обновления schedule.json и core.FACULTY_STRUCTURE)
        reset_users_ids, cleared_starostas, _ = reset_for_new_academic_year(
            year_start_dt,
            clear_schedule_file=False,
            force=False,
            manual_backup_chat_id=chat_id,
            notify_users=False,
        )
        kept_other_faculties = []
        reset_done_facs = [target_fac]
    else:
        send_db_backup(manual_chat_id=chat_id)
        existing_records = []
        if os.path.exists(SCHEDULE_JSON_PATH):
            try:
                with open(SCHEDULE_JSON_PATH, 'r', encoding='utf-8') as f:
                    existing_records = json.load(f)
            except Exception:
                existing_records = []
        kept_other_faculties = [
            r for r in existing_records
            if (r.get('faculty') or infer_faculty_by_group(r.get('group', ''))) != target_fac
        ]
        reset_done_facs = list(existing_meta.get('reset_done_faculties', []))
        if target_fac not in reset_done_facs:
            reset_done_facs.append(target_fac)

    merged_records = kept_other_faculties + records
    fac_groups = {r['group'] for r in records}

    atomic_write_json(SCHEDULE_JSON_PATH, merged_records)

    save_semester_meta(
        year_start_dt,
        practice_dict,
        semester_title,
        fac_groups=fac_groups,
        reset_done_faculties=reset_done_facs,
        academic_year_label=new_year_label,
    )

    with db_connection() as conn:
        cursor = conn.cursor()
        reload_schedule_tables(cursor, conn, force=True)

        if clear_old_changes and not is_new_academic_year:
            for grp in fac_groups:
                cursor.execute('DELETE FROM replacements WHERE (faculty = ? OR faculty = "") AND group_name = ?', (target_fac, grp))
                cursor.execute('DELETE FROM cancelled_lessons WHERE (faculty = ? OR faculty = "") AND group_name = ?', (target_fac, grp))

        if not is_new_academic_year:
            # Если в течение года переименовали/удалили группу в файле, просим затронутых студентов перевыбрать направление
            cursor.execute(
                'SELECT user_id, group_name FROM users WHERE faculty = ? AND group_name != ""',
                (target_fac,),
            )
            orphaned_ids = [uid for uid, grp in cursor.fetchall() if grp not in fac_groups]
            if orphaned_ids:
                for uid_orph in orphaned_ids:
                    cursor.execute(
                        'UPDATE users SET group_name = "", managed_group = "" WHERE user_id = ?',
                        (uid_orph,),
                    )
                    enqueue_message(
                        uid_orph,
                        '🔄 <b>Обновился список направлений твоего факультета!</b>\n\n'
                        'Пожалуйста, выбери свою группу заново, чтобы продолжить получать расписание:',
                        reply_markup=build_faculty_keyboard(),
                        parse_mode='HTML',
                    )
                invalidate_user_cache()

    reset_count = len(reset_users_ids)
    if is_new_academic_year and reset_users_ids:
        # Рассылаем уведомления уже ПОСЛЕ обновления schedule.json и core.FACULTY_STRUCTURE
        notify_users_new_academic_year(reset_users_ids, new_year_label, exclude_user_id=chat_id)

    cur_week, _ = get_current_week_and_day(faculty_code=target_fac)
    fac_courses = core.FACULTY_STRUCTURE.get(target_fac, {})
    groups_summary = '\n'.join(
        f'• <b>{crs} курс:</b> {", ".join(esc(g) for g in grps)}'
        for crs, grps in sorted(fac_courses.items())
    )

    reset_note = (
        f'\n🔄 <b>Новый учебный год:</b> база прошлого года очищена, роли старост сняты ({cleared_starostas}), '
        f'у {reset_count} пользователей сброшены курс и группа!'
        if reset_count > 0
        else ''
    )

    bot.send_message(
        chat_id,
        f'🎉 <b>РАСПИСАНИЕ УСПЕШНО ОБНОВЛЕНО!</b>\n\n'
        f'🏛 <b>Факультет:</b> {esc(get_faculty_short(target_fac))} ({esc(get_faculty_full(target_fac))})\n'
        f'📅 <b>Точка отсчёта 1-й недели года:</b> {year_start_dt.strftime("%d.%m.%Y")} (сейчас идёт <b>{cur_week}-я</b> сквозная неделя)\n'
        f'📚 <b>Загружено пар для факультета:</b> {len(records)} (всего в базе: {len(merged_records)})\n'
        f'🧹 <b>Старые замены и отмены факультета:</b> {"очищены" if clear_old_changes else "сохранены"}'
        f'{reset_note}\n\n'
        f'🎓 <b>Активные направления ({esc(get_faculty_short(target_fac))}):</b>\n{groups_summary}\n\n'
        f'<i>Новое расписание уже работает для всех студентов и сохранено в schedule.json!</i>',
        parse_mode='HTML',
    )


@bot.message_handler(
    chat_types=['private'],
    content_types=['document'],
    func=lambda m: bool(
        m.document
        and m.document.file_name
        and m.document.file_name.lower().endswith(('.docx', '.doc'))
        and get_user_state(m.from_user.id)[0] != 'awaiting_hw_content'
    ),
)
def handle_schedule_docx_upload(message):
    sync_username(message.from_user)
    if not is_admin_user(message.from_user):
        return

    fname = message.document.file_name
    wait_msg = bot.send_message(message.chat.id, '⏳ Скачиваю и анализирую файл расписания...')

    tmp_raw_path = None
    tmp_docx_path = None
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)

        suffix = '.docx' if fname.lower().endswith('.docx') else '.doc'
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(downloaded)
            tmp_raw_path = tmp.name

        converted_path, conv_err = convert_doc_to_docx_if_needed(tmp_raw_path)
        if conv_err:
            bot.edit_message_text(conv_err, message.chat.id, wait_msg.message_id, parse_mode='HTML')
            return
        tmp_docx_path = converted_path

        fac_from_fname = detect_faculty_from_text(fname) or 'ЕМФ'
        records, practice_dict, semester_title, detected_fac = parse_schedule_docx_file(
            tmp_docx_path, default_faculty=fac_from_fname
        )
    except Exception as e:
        bot.edit_message_text(
            f'❌ <b>Ошибка при чтении файла расписания:</b>\n<code>{esc(e)}</code>',
            message.chat.id,
            wait_msg.message_id,
            parse_mode='HTML',
        )
        return
    finally:
        for p_clean in {tmp_raw_path, tmp_docx_path}:
            if p_clean and os.path.exists(p_clean):
                try:
                    os.remove(p_clean)
                    parent_dir = os.path.dirname(p_clean)
                    if parent_dir != tempfile.gettempdir() and os.path.isdir(parent_dir) and not os.listdir(parent_dir):
                        os.rmdir(parent_dir)
                except Exception:
                    pass

    if not records:
        safe_edit_message_text(
            '⚠️ Не удалось найти таблицы с расписанием в этом файле.\n'
            'Убедись, что шапки столбцов содержат номер курса (например, <i>«3 курс Направление ...»</i>), '
            'а время пар указано в формате <code>08:00 - 09:20</code>.',
            message.chat.id,
            wait_msg.message_id,
            parse_mode='HTML',
        )
        return

    by_group = {}
    for r in records:
        by_group[(r['course'], r['group'])] = by_group.get((r['course'], r['group']), 0) + 1

    preview_lines = []
    for (crs, grp), cnt in sorted(by_group.items()):
        prac_info = ''
        if grp in practice_dict and practice_dict[grp]:
            pw = practice_dict[grp]
            prac_info = f' <i>(практика: {min(pw)}–{max(pw)} нед.)</i>'
        preview_lines.append(f'• {esc(grp)}: <b>{cnt}</b> записей{prac_info}')

    now_dt = now_local()
    title_year_start = extract_academic_start_from_title(semester_title)
    if title_year_start is not None:
        suggested_start_dt = title_year_start
    else:
        auto_year_start = get_default_academic_year_start()
        suggested_start_dt = (
            auto_year_start
            if auto_year_start.date() > core.ACADEMIC_YEAR_START_DATE.date()
            else core.ACADEMIC_YEAR_START_DATE
        )
    cur_week = calc_week_number_for_start(now_dt, suggested_start_dt)
    cur_week_lbl = f'<b>{cur_week}-я</b> неделя' if cur_week > 0 else '<i>ещё не началась</i>'
    year_start_str = suggested_start_dt.strftime('%d.%m.%Y')

    import_token = f'{message.from_user.id}_{int(time.time())}'
    save_pending_docx_import(import_token, {
        '_ts': time.time(),
        'records': records,
        'practice_dict': practice_dict,
        'semester_title': semester_title,
        'faculty_code': detected_fac,
        'year_start_dt': suggested_start_dt,
        'clear_old_changes': True,
    })

    title_line = f'📄 <b>Заголовок:</b> <i>{esc(semester_title)}</i>\n' if semester_title else ''
    text = (
        f'✅ <b>Файл расписания успешно распознан!</b>\n\n'
        f'{title_line}'
        f'🏛 <b>Определён факультет:</b> {esc(get_faculty_short(detected_fac))} ({esc(get_faculty_full(detected_fac))})\n'
        f'📊 <b>Всего строк расписания:</b> {len(records)}\n\n'
        f'🎓 <b>Распознанные группы и курсы:</b>\n'
        + '\n'.join(preview_lines)
        + f'\n\n📆 <b>Счётчик учебных недель:</b> от <code>{year_start_str}</code> (сейчас {cur_week_lbl})\n\n'
        + f'Проверь факультет и примени обновление:'
    )

    markup = InlineKeyboardMarkup(row_width=2)
    fac_btns = []
    for f_code in FACULTY_ORDER:
        mark = '✅ ' if f_code == detected_fac else ''
        fac_btns.append(
            InlineKeyboardButton(
                f'{mark}{get_faculty_short(f_code)}',
                callback_data=f'docx_setfac_{f_code}_{import_token}',
            )
        )
    markup.add(*fac_btns)
    markup.add(
        InlineKeyboardButton(
            f'🚀 Применить ({get_faculty_short(detected_fac)}, старт {year_start_str})',
            callback_data=f'docx_apply_auto_{import_token}',
        )
    )
    markup.add(
        InlineKeyboardButton(
            '📅 Изменить дату 1-й недели учебного года',
            callback_data=f'docx_apply_manual_{import_token}',
        )
    )
    markup.add(InlineKeyboardButton('❌ Отмена', callback_data=f'docx_cancel_{import_token}'))

    safe_edit_message_text(
        text,
        message.chat.id,
        wait_msg.message_id,
        reply_markup=markup,
        parse_mode='HTML',
    )


def process_docx_custom_start_date(message, payload):
    import_token = (payload or {}).get('import_token', '')
    clear_user_state(message.chat.id)
    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        get_pending_docx_import(import_token, pop=True)
        bot.send_message(message.chat.id, '❌ Импорт расписания отменён.')
        return

    raw = message.text.strip()
    try:
        dt = datetime.strptime(raw, '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
    except ValueError:
        set_user_state(message.chat.id, 'awaiting_docx_date', {'import_token': import_token})
        bot.send_message(
            message.chat.id,
            '⚠️ Неверный формат даты. Введи дату старта учебного года в формате <code>ДД.ММ.ГГГГ</code> (обычно <code>01.09.2026</code>):',
            parse_mode='HTML',
        )
        return

    apply_parsed_docx_schedule(message.chat.id, message.from_user.id, import_token, dt)


@bot.message_handler(commands=['starosta', 'addstarosta'])
def cmd_add_starosta(message):
    sync_username(message.from_user)
    if not is_admin_user(message.from_user):
        bot.send_message(
            message.chat.id,
            f'⛔ <b>Доступ запрещён!</b> Выдавать роль старосты может только главный администратор (@{ADMIN_USERNAME}).',
            parse_mode='HTML',
        )
        return

    target_user_id, target_display, err = resolve_target_user(message)
    if err == 'not_found':
        bot.send_message(
            message.chat.id,
            f'⚠️ Пользователь <b>{esc(target_display)}</b> ещё не запускал бота или не найден в базе.\n'
            f'Попроси его нажать /start в боте либо укажи его числовой Telegram ID: <code>/starosta ID</code>',
            parse_mode='HTML',
        )
        return
    if not target_user_id:
        bot.send_message(
            message.chat.id,
            'ℹ️ <b>Инструкция по выдаче прав старосты:</b>\n\n'
            'Используй команду одним из способов:\n'
            '• <code>/starosta @username</code> (если человек уже запускал бота)\n'
            '• <code>/starosta 123456789</code> (по Telegram ID)\n'
            '• Или ответь командой <code>/starosta</code> на сообщение нужного пользователя.',
            parse_mode='HTML',
        )
        return

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT course, group_name, subscribed, is_hw_manager, faculty FROM users WHERE user_id = ?',
            (target_user_id,),
        )
        row = cursor.fetchone()
        if row:
            assigned_group = row[1]
            cursor.execute(
                'UPDATE users SET is_starosta = 1, managed_group = ? WHERE user_id = ?',
                (assigned_group, target_user_id),
            )
            sub_state = bool(row[2])
        else:
            default_fac = 'ЕМФ'
            assigned_group = ''
            cursor.execute(
                'INSERT INTO users (user_id, faculty, course, group_name, subscribed, is_starosta, is_hw_manager, notify_updates, managed_group) '
                'VALUES (?, ?, 0, "", 0, 1, 0, 0, "")',
                (target_user_id, default_fac),
            )
            sub_state = False
    invalidate_user_cache(target_user_id)

    bot.send_message(
        message.chat.id,
        f'✅ Пользователю <b>{esc(target_display)}</b> выдана привилегия <b>Старосты</b>!\n'
        f'🛡 <b>Закреплённая группа управления:</b> {esc(assigned_group)}',
        parse_mode='HTML',
    )
    try:
        bot.send_message(
            target_user_id,
            f'🌟 <b>Тебе выдана роль Старосты!</b>\n'
            f'🛡 <b>Закреплённая группа:</b> {esc(assigned_group)}\n\n'
            f'В твоём нижнем меню появилась кнопка <b>«🛠 Спецвозможности»</b> для управления заменами, отменами и домашними заданиями группы.',
            reply_markup=get_main_keyboard(sub_state, is_starosta=True),
            parse_mode='HTML',
        )
    except Exception:
        pass


@bot.message_handler(commands=['unstarosta', 'delstarosta'])
def cmd_remove_starosta(message):
    sync_username(message.from_user)
    if not is_admin_user(message.from_user):
        return

    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            'Укажи <code>@username</code> или <code>ID</code> для снятия роли: <code>/unstarosta @username</code>',
            parse_mode='HTML',
        )
        return

    arg = parts[1].strip()
    affected_users = []
    with db_connection() as conn:
        cursor = conn.cursor()
        if arg.startswith('@'):
            uname_low = arg[1:].lower()
            cursor.execute('SELECT user_id, subscribed FROM users WHERE LOWER(username) = ? AND is_starosta = 1', (uname_low,))
            affected_users = cursor.fetchall()
            cursor.execute(
                'UPDATE users SET is_starosta = 0, managed_group = "" WHERE LOWER(username) = ? AND is_starosta = 1',
                (uname_low,),
            )
        elif arg.isdigit():
            uid_num = int(arg)
            cursor.execute('SELECT user_id, subscribed FROM users WHERE user_id = ? AND is_starosta = 1', (uid_num,))
            affected_users = cursor.fetchall()
            cursor.execute(
                'UPDATE users SET is_starosta = 0, managed_group = "" WHERE user_id = ? AND is_starosta = 1',
                (uid_num,),
            )
        else:
            bot.send_message(
                message.chat.id,
                '⚠️ Укажи корректный <code>@username</code> или числовой <code>ID</code>.',
                parse_mode='HTML',
            )
            return

    if not affected_users:
        bot.send_message(
            message.chat.id,
            f'⚠️ Староста <b>{esc(arg)}</b> не найден в базе (возможно, опечатка в username или роль уже снята).',
            parse_mode='HTML',
        )
        return

    invalidate_user_cache()
    for uid_rem, sub_rem in affected_users:
        try:
            bot.send_message(
                uid_rem,
                'ℹ️ Роль <b>Старосты</b> была снята администратором. Главное меню обновлено.',
                reply_markup=get_main_keyboard(bool(sub_rem), is_starosta=False),
                parse_mode='HTML',
            )
        except Exception:
            pass
    bot.send_message(
        message.chat.id,
        f'✅ Роль старосты снята с пользователя <b>{esc(arg)}</b>.',
        parse_mode='HTML',
    )


@bot.message_handler(commands=['backup'], chat_types=['private'])
def cmd_backup(message):
    sync_username(message.from_user)
    if not is_admin_user(message.from_user):
        return
    send_db_backup(manual_chat_id=message.chat.id)


# --- ПАНЕЛЬ СТАТИСТИКИ ДЛЯ АДМИНА (/stats) ПО ВКЛАДКАМ ---
def _build_stats_tabs_keyboard(active_tab='overview'):
    tabs = [
        ('overview', '📊 Сводка'),
        ('clicks', '📈 Клики'),
        ('groups', '🎓 Группы'),
        ('starostas', '🌟 Старосты'),
        ('changes', '🕵️ Правки'),
    ]
    markup = InlineKeyboardMarkup(row_width=3)
    row1 = []
    row2 = []
    for i, (key, lbl) in enumerate(tabs):
        btn_lbl = f'• {lbl} •' if key == active_tab else lbl
        btn = InlineKeyboardButton(btn_lbl, callback_data=f'spec_stats_tab_{key}')
        if i < 3:
            row1.append(btn)
        else:
            row2.append(btn)
    markup.row(*row1)
    markup.row(*row2)
    markup.row(
        InlineKeyboardButton('🔄 Обновить', callback_data=f'spec_stats_tab_{active_tab}'),
        InlineKeyboardButton('🔙 В меню', callback_data='spec_back'),
    )
    return markup


def send_stats_panel(chat_id, user_obj, tab='overview', message_id=None):
    if not is_admin_user(user_obj):
        bot.send_message(
            chat_id,
            '⛔ Эта команда доступна только администратору.',
            parse_mode='HTML',
        )
        return

    now_ts = time.time()
    ts_24h = now_ts - 86400
    ts_7d = now_ts - 86400 * 7
    ts_30d = now_ts - 86400 * 30
    today_iso = now_local().strftime('%Y-%m-%d')
    week_ago_iso = (now_local() - timedelta(days=6)).strftime('%Y-%m-%d')
    stamp = now_local().strftime('%H:%M')

    with db_connection() as conn:
        cursor = conn.cursor()

        if tab == 'overview':
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE COALESCE(is_blocked, 0) = 1')
            blocked_users = cursor.fetchone()[0]
            alive_users = max(0, total_users - blocked_users)

            cursor.execute('SELECT COUNT(*) FROM users WHERE COALESCE(last_active, 0) >= ?', (ts_24h,))
            dau = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE COALESCE(last_active, 0) >= ?', (ts_7d,))
            wau = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE COALESCE(last_active, 0) >= ?', (ts_30d,))
            mau = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM users WHERE COALESCE(created_at, 0) >= ?', (ts_24h,))
            new_24h = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE COALESCE(created_at, 0) >= ?', (ts_7d,))
            new_7d = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM users WHERE COALESCE(sub_morning, subscribed, 0) = 1')
            sub_m = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE COALESCE(sub_evening, subscribed, 0) = 1')
            sub_e = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE notify_updates = 1')
            notify_u = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM group_chats')
            chats_cnt = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM replacements')
            repl_cnt = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM cancelled_lessons')
            canc_cnt = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM homeworks')
            hw_cnt = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM schedules')
            sched_cnt = cursor.fetchone()[0]

            notify_pct = round((notify_u / alive_users * 100), 1) if alive_users else 0
            cur_week, _ = get_current_week_and_day()
            q_size = MAILING_QUEUE.qsize()

            text = (
                f'📊 <b>СТАТИСТИКА • СВОДКА</b> <i>({stamp})</i>\n\n'
                f'<blockquote>📅 <b>Неделя:</b> {cur_week}-я (старт {core.ACADEMIC_YEAR_START_DATE.strftime("%d.%m.%Y")})\n'
                f'👥 <b>Пользователи:</b> {total_users} (живых: <b>{alive_users}</b>, блок: {blocked_users})\n'
                f'🔥 <b>Онлайн:</b> 24ч: <b>{dau}</b> · 7д: <b>{wau}</b> · 30д: <b>{mau}</b>\n'
                f'🆕 <b>Прирост:</b> 24ч: <b>+{new_24h}</b> · 7д: <b>+{new_7d}</b></blockquote>\n\n'
                f'☀️ <b>Рассылка:</b> утро <b>{sub_m}</b> · вечер <b>{sub_e}</b>\n'
                f'🔔 <b>Уведомления (пары/ДЗ):</b> {notify_u} ({notify_pct}%)\n'
                f'💬 <b>Привязанных бесед:</b> {chats_cnt}\n'
                f'📚 <b>База:</b> пар {sched_cnt} · замен {repl_cnt} · отмен {canc_cnt} · ДЗ {hw_cnt}\n'
                f'📨 <b>В очереди отправки:</b> {q_size}'
            )

        elif tab == 'clicks':
            cursor.execute(
                'SELECT action, SUM(count) FROM daily_stats WHERE date_str = ? GROUP BY action',
                (today_iso,),
            )
            today_actions = dict(cursor.fetchall())
            cursor.execute(
                'SELECT action, SUM(count) FROM daily_stats WHERE date_str >= ? GROUP BY action',
                (week_ago_iso,),
            )
            week_actions = dict(cursor.fetchall())

            action_labels = [
                ('today', '📅 Сегодня'),
                ('tomorrow', '⏭ Завтра'),
                ('week', '🗓 Неделя'),
                ('homework', '📚 Домашка'),
                ('search', '🔍 Поиск'),
                ('inline', '💬 Инлайн'),
                ('group_cmd', '👥 Беседы'),
            ]
            total_today = sum(today_actions.values())
            total_week = sum(week_actions.values())
            lines = []
            for key, lbl in action_labels:
                t_c = today_actions.get(key, 0)
                w_c = week_actions.get(key, 0)
                lines.append(f'• {lbl}: <b>{t_c}</b>  <i>(за 7д: {w_c})</i>')

            text = (
                f'📈 <b>СТАТИСТИКА • КЛИКИ</b> <i>({stamp})</i>\n\n'
                f'<blockquote>⚡ <b>Всего сегодня:</b> {total_today} · <b>За 7 дней:</b> {total_week}</blockquote>\n\n'
                + '\n'.join(lines)
            )

        elif tab == 'groups':
            cursor.execute(
                'SELECT faculty, course, group_name, COUNT(*) FROM users '
                'GROUP BY faculty, course, group_name '
                'ORDER BY faculty ASC, course ASC, COUNT(*) DESC'
            )
            groups_stats = cursor.fetchall()
            cursor.execute(
                'SELECT faculty, group_name FROM group_chats ORDER BY faculty ASC, group_name ASC'
            )
            bound_chats = cursor.fetchall()

            text = f'🎓 <b>СТАТИСТИКА • ГРУППЫ И БЕСЕДЫ</b> <i>({stamp})</i>\n\n'
            if bound_chats:
                text += f'💬 <b>Привязанные беседы ({len(bound_chats)}):</b>\n'
                for b_fac, b_grp in bound_chats:
                    text += f'• [{esc(get_faculty_short(b_fac))}] {esc(b_grp)}\n'
                text += '\n'

            text += '👥 <b>Студенты по группам:</b>\n'
            if groups_stats:
                for fac, crs, grp, cnt in groups_stats:
                    fac_lbl = get_faculty_short(fac or infer_faculty_by_group(grp))
                    text += f'• [{esc(fac_lbl)}] {esc(grp or "не выбрана")}: <b>{cnt}</b>\n'
            else:
                text += '<i>Пока нет зарегистрированных пользователей.</i>'

        elif tab == 'starostas':
            cursor.execute(
                'SELECT user_id, username, group_name, managed_group, faculty FROM users '
                'WHERE is_starosta = 1 ORDER BY faculty ASC, course ASC, group_name ASC'
            )
            starostas = cursor.fetchall()
            text = f'🌟 <b>СТАТИСТИКА • СТАРОСТЫ ({len(starostas)})</b> <i>({stamp})</i>\n\n'
            if starostas:
                for uid, uname, grp, m_grp, fac in starostas:
                    label = f'@{uname}' if uname else f'ID <code>{uid}</code>'
                    target_g = m_grp or grp or 'не выбрана'
                    fac_lbl = get_faculty_short(fac or infer_faculty_by_group(target_g))
                    text += f'• {label} — [{esc(fac_lbl)}] <i>{esc(target_g)}</i>\n'
            else:
                text += '<i>Нет назначенных старост.</i>'

        else:  # changes
            cursor.execute(
                'SELECT r.faculty, r.group_name, r.target_date, r.time_slot, r.subject_info, '
                'r.added_by, COALESCE(r.created_at, ""), COALESCE(u.username, "") '
                'FROM replacements r '
                'LEFT JOIN users u ON u.user_id = r.added_by '
                'ORDER BY r.id DESC LIMIT 15'
            )
            repl_rows = cursor.fetchall()
            cursor.execute(
                'SELECT c.faculty, c.group_name, c.target_date, c.time_slot, '
                'c.cancelled_by, COALESCE(c.created_at, ""), COALESCE(u.username, "") '
                'FROM cancelled_lessons c '
                'LEFT JOIN users u ON u.user_id = c.cancelled_by '
                'ORDER BY c.id DESC LIMIT 15'
            )
            canc_rows = cursor.fetchall()

            text = f'🕵️ <b>ЖУРНАЛ ЗАМЕН И ОТМЕН</b> <i>({stamp})</i>\n\n'
            if not repl_rows and not canc_rows:
                text += '✨ <i>В базе пока нет ни одной замены или отмены.</i>'
            else:
                if repl_rows:
                    text += '🔄 <b>ЗАМЕНЫ:</b>\n'
                    for fac, grp, t_date, t_slot, subj, uid, created_at, uname in repl_rows:
                        fac_lbl = get_faculty_short(fac or infer_faculty_by_group(grp))
                        p_num = get_pair_num(t_slot)
                        p_lbl = f'{p_num}п' if p_num else t_slot
                        who = f'@{uname}' if uname else f'ID <code>{uid}</code>'
                        when_s = f' ({esc(created_at)})' if created_at else ''
                        text += f'• [{esc(fac_lbl)}] <b>{esc(short_text(grp, 24))}</b> | {esc(t_date[:5])} {esc(p_lbl)}\n  → {esc(short_text(subj, 45))} | {who}{when_s}\n'
                    text += '\n'
                if canc_rows:
                    text += '🚫 <b>ОТМЕНЫ ПАР:</b>\n'
                    for fac, grp, t_date, t_slot, uid, created_at, uname in canc_rows:
                        fac_lbl = get_faculty_short(fac or infer_faculty_by_group(grp))
                        p_num = get_pair_num(t_slot)
                        p_lbl = f'{p_num}п' if p_num else t_slot
                        who = f'@{uname}' if uname else f'ID <code>{uid}</code>'
                        when_s = f' ({esc(created_at)})' if created_at else ''
                        text += f'• [{esc(fac_lbl)}] <b>{esc(short_text(grp, 24))}</b> | {esc(t_date[:5])} {esc(p_lbl)} — {who}{when_s}\n'

    kb = _build_stats_tabs_keyboard(active_tab=tab)
    if len(text) > 3900:
        text = text[:3880] + '\n…'
    if message_id:
        safe_edit_message_text(text.strip(), chat_id, message_id, reply_markup=kb, parse_mode='HTML')
    else:
        bot.send_message(chat_id, text.strip(), reply_markup=kb, parse_mode='HTML')


@bot.message_handler(commands=['stats'], chat_types=['private'])
def cmd_stats(message):
    sync_username(message.from_user)
    send_stats_panel(message.chat.id, message.from_user)


# --- МАССОВАЯ РАССЫЛКА ОБЪЯВЛЕНИЙ (/broadcast) ---
def open_broadcast_menu(chat_id, user_obj, message_id=None):
    if not is_admin_user(user_obj):
        bot.send_message(
            chat_id,
            '⛔ Команда <code>/broadcast</code> доступна только администратору.',
            parse_mode='HTML',
        )
        return

    clear_user_state(chat_id)
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton('📢 Всем пользователям бота', callback_data='bc_target_all'))
    markup.add(InlineKeyboardButton('🎯 Конкретной группе', callback_data='bc_group_pick_fac'))
    fac_btns = [
        InlineKeyboardButton(f'🏛 {get_faculty_short(f)}', callback_data=f'bc_target_fac_{f}')
        for f in FACULTY_ORDER
    ]
    markup.add(*fac_btns)
    all_courses = sorted({
        c for courses in core.FACULTY_STRUCTURE.values() for c in courses.keys()
    })
    course_btns = [
        InlineKeyboardButton(f'🎓 {c} курс', callback_data=f'bc_target_{c}')
        for c in all_courses
    ]
    markup.add(*course_btns)
    markup.add(InlineKeyboardButton('🔙 Назад', callback_data='spec_back'))

    text = (
        '📣 <b>МАССОВАЯ РАССЫЛКА ОБЪЯВЛЕНИЙ</b>\n\n'
        'Выбери, кому отправить сообщение от деканата / администрации:'
    )
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


@bot.message_handler(commands=['broadcast'], chat_types=['private'])
def cmd_broadcast(message):
    sync_username(message.from_user)
    open_broadcast_menu(message.chat.id, message.from_user)


def process_broadcast_content(message, payload):
    target = (payload or {}).get('target', 'all')
    clear_user_state(message.chat.id)
    if message.text and message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        bot.send_message(message.chat.id, '❌ Рассылка отменена.')
        return

    photo_id = message.photo[-1].file_id if message.photo else None
    raw_text = message.caption if photo_id else (message.text or '')

    if not photo_id and not raw_text.strip():
        bot.send_message(
            message.chat.id,
            '⚠️ Поддерживаются только текстовые сообщения или фото с подписью. Рассылка отменена.',
        )
        return

    bound_chats = []
    with db_connection() as conn:
        cursor = conn.cursor()
        if target == 'all':
            cursor.execute('SELECT user_id FROM users WHERE COALESCE(is_blocked, 0) = 0')
            target_label = 'всем пользователям'
        elif target == 'group':
            fac_code = (payload or {}).get('faculty', '')
            grp_name = (payload or {}).get('group_name', '')
            cursor.execute(
                'SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") '
                'AND group_name = ? AND COALESCE(is_blocked, 0) = 0',
                (fac_code, grp_name),
            )
            bound_chats = get_bound_group_chats(fac_code, grp_name)
            target_label = f'группе {grp_name} [{get_faculty_short(fac_code)}]'
        elif str(target).startswith('fac_'):
            fac_code = str(target).replace('fac_', '')
            cursor.execute(
                'SELECT user_id FROM users WHERE faculty = ? AND COALESCE(is_blocked, 0) = 0',
                (fac_code,),
            )
            target_label = f'факультету {get_faculty_short(fac_code)}'
        else:
            cursor.execute(
                'SELECT user_id FROM users WHERE course = ? AND COALESCE(is_blocked, 0) = 0',
                (int(target),),
            )
            target_label = f'{target} курсу'
        recipients = [r[0] for r in cursor.fetchall()]
    all_targets = recipients + [cid for cid in bound_chats if cid not in recipients]

    header = '📢 <b>ВАЖНОЕ ОБЪЯВЛЕНИЕ</b>\n\n'
    formatted_text = header + esc(raw_text) if raw_text else header.strip()

    text_chunks = None if photo_id else split_long_html_message(formatted_text, max_len=3900)
    for u_id in all_targets:
        if photo_id:
            enqueue_hw_media(u_id, [photo_id], formatted_text)
        else:
            for ch in text_chunks:
                enqueue_message(u_id, ch, parse_mode='HTML')

    chats_note = f' (+ бесед: {len(bound_chats)})' if bound_chats else ''
    bot.send_message(
        message.chat.id,
        f'🚀 <b>Рассылка поставлена в фоновую очередь!</b>\n\n'
        f'🎯 <b>Аудитория:</b> {esc(target_label)}\n'
        f'👥 <b>Получателей в очереди:</b> {len(recipients)}{chats_note}\n'
        f'⚡ <i>Сообщения плавно доставляются в фоне без блокировки бота.</i>',
        parse_mode='HTML',
    )


# --- INLINE-ВЫБОР ДАТЫ И ПАРЫ ДЛЯ ЗАМЕНЫ / ОТМЕНЫ ---
def build_date_choice_markup(action_prefix):
    today = now_local()
    d0 = today.strftime('%d.%m.%Y')
    d1 = (today + timedelta(days=1)).strftime('%d.%m.%Y')
    d2 = (today + timedelta(days=2)).strftime('%d.%m.%Y')

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(f'📍 Сегодня ({d0})', callback_data=f'{action_prefix}_date_{d0}'),
        InlineKeyboardButton(f'⏭ Завтра ({d1})', callback_data=f'{action_prefix}_date_{d1}'),
        InlineKeyboardButton(f'⏩ Послезавтра ({d2})', callback_data=f'{action_prefix}_date_{d2}'),
        InlineKeyboardButton('⌨️ Ввести дату вручную', callback_data=f'{action_prefix}_datemanual'),
        InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
    )
    return markup


def show_pair_selection_for_action(chat_id, message_id, action, course, group_name, date_str):
    parsed_date = datetime.strptime(date_str, '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
    week_num, day_name = get_current_week_and_day(parsed_date)
    slot_map, _ = get_day_lessons_for_date(parsed_date, course, group_name)

    markup = InlineKeyboardMarkup(row_width=1)

    if action == 'del':
        if not slot_map:
            text = (
                f'📅 <b>{day_name}, {date_str}</b> ({week_num}-я нед.)\n\n'
                f'✨ На этот день у группы <b>{esc(group_name)}</b> нет активных пар для отмены.'
            )
            markup.add(InlineKeyboardButton('🔙 Выбрать другую дату', callback_data='del_back_date'))
            markup.add(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))
        else:
            text = (
                f'🗑 <b>Отмена пары — {day_name}, {date_str}</b>\n'
                f'<blockquote>🎓 {esc(group_name)}</blockquote>\n\n'
                f'<b>Шаг 2 из 2.</b> Нажми на пару, которую нужно отменить:'
            )
            for p_num in sorted(slot_map.keys()):
                subj_preview = short_text(', '.join(slot_map[p_num]), 34)
                emoji = PAIR_EMOJIS.get(p_num, f'{p_num}.')
                markup.add(
                    InlineKeyboardButton(
                        f'{emoji} {p_num} пара — {subj_preview}',
                        callback_data=f'del_pair_{date_str}_{p_num}',
                    )
                )
            markup.add(InlineKeyboardButton('🔙 Выбрать другую дату', callback_data='del_back_date'))
            markup.add(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))
    else:
        text = (
            f'🔄 <b>Добавление замены — {day_name}, {date_str}</b>\n'
            f'<blockquote>🎓 {esc(group_name)}</blockquote>\n\n'
            f'<b>Шаг 2 из 3.</b> Выбери номер пары (1–6):'
        )
        for p_num in range(1, 7):
            emoji = PAIR_EMOJIS.get(p_num, f'{p_num}.')
            if p_num in slot_map:
                subj_preview = short_text(', '.join(slot_map[p_num]), 30)
                btn_text = f'{emoji} {p_num} пара: {subj_preview}'
            else:
                btn_text = f'{emoji} {p_num} пара ({NUM_TO_TIME_SLOT[p_num]}) — свободно'
            markup.add(
                InlineKeyboardButton(
                    btn_text,
                    callback_data=f'repl_pair_{date_str}_{p_num}',
                )
            )
        markup.add(InlineKeyboardButton('🔙 Выбрать другую дату', callback_data='repl_back_date'))
        markup.add(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def open_add_replacement(chat_id, user_obj, message_id=None):
    clear_user_state(chat_id)
    _, group_name, _ = get_managed_context(user_obj)
    if not group_name:
        bot.send_message(chat_id, '⚠️ Сначала выбери свою группу через /start.')
        return
    if not is_user_starosta(user_obj):
        bot.send_message(
            chat_id,
            '⛔ У тебя нет прав старосты для добавления замен.',
            parse_mode='HTML',
        )
        return

    text = (
        f'📝 <b>Добавление замены для группы:</b>\n'
        f'<blockquote>🎓 {esc(group_name)}</blockquote>\n\n'
        f'<b>Шаг 1 из 3.</b> Выбери <b>дату</b> замены:'
    )
    markup = build_date_choice_markup('repl')
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def start_add_replacement(message):
    open_add_replacement(message.chat.id, message.from_user)


def open_delete_pair(chat_id, user_obj, message_id=None):
    clear_user_state(chat_id)
    _, group_name, _ = get_managed_context(user_obj)
    if not group_name:
        bot.send_message(chat_id, '⚠️ Сначала выбери свою группу через /start.')
        return
    if not is_user_starosta(user_obj):
        bot.send_message(
            chat_id,
            '⛔ У тебя нет прав старосты для удаления пар.',
            parse_mode='HTML',
        )
        return

    text = (
        f'🗑 <b>Отмена пары для группы:</b>\n'
        f'<blockquote>🎓 {esc(group_name)}</blockquote>\n\n'
        f'<b>Шаг 1 из 2.</b> Выбери <b>дату</b>, на которую нужно убрать пару:'
    )
    markup = build_date_choice_markup('del')
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def start_delete_pair(message):
    open_delete_pair(message.chat.id, message.from_user)


def process_manual_date_for_action(message, payload):
    action = (payload or {}).get('action', '')
    course = (payload or {}).get('course')
    group_name = (payload or {}).get('group_name', '')
    clear_user_state(message.chat.id)
    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        bot.send_message(message.chat.id, '❌ Действие отменено.')
        return

    date_text = message.text.strip()
    try:
        parsed_date = datetime.strptime(date_text, '%d.%m.%Y')
        date_str = parsed_date.strftime('%d.%m.%Y')
    except ValueError:
        set_user_state(message.from_user.id, 'awaiting_manual_date', dict(payload or {
            'action': action,
            'course': course,
            'group_name': group_name,
        }))
        bot.send_message(
            message.chat.id,
            '⚠️ <b>Неверный формат даты!</b>\n'
            'Введи дату строго в формате <code>ДД.ММ.ГГГГ</code> (например, <code>21.11.2026</code>) или напиши «Отмена»:',
            parse_mode='HTML',
        )
        return

    if action == 'hw_add':
        show_hw_subject_selection(message.chat.id, None, course, group_name, date_str)
    elif action == 'hw_change_date':
        subj_name = (payload or {}).get('hw_subject', '')
        up_dates = (payload or {}).get('hw_upcoming_dates', [])
        ask_hw_content(message.chat.id, group_name, date_str, subj_name, upcoming_dates=up_dates)
    elif action == 'hw_view':
        send_homeworks_for_date_view(message.chat.id, message.from_user, group_name, date_str)
    else:
        show_pair_selection_for_action(message.chat.id, None, action, course, group_name, date_str)


def process_replacement_subject(message, payload):
    group_name = (payload or {}).get('group_name', '')
    date_str = (payload or {}).get('date_str', '')
    time_slot = (payload or {}).get('time_slot', '')
    clear_user_state(message.chat.id)
    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        bot.send_message(message.chat.id, '❌ Добавление замены отменено.')
        return

    subject_info = message.text.strip()
    try:
        parsed_date = datetime.strptime(date_str, '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
    except ValueError:
        bot.send_message(message.chat.id, '⚠️ Ошибка формата даты. Попробуй добавить замену заново.')
        return
    course, fac_code = find_course_and_faculty_by_group(group_name)
    if not fac_code:
        u_cur = get_user_data(message.from_user.id)
        fac_code = (u_cur[7] if u_cur and u_cur[7] else '') or infer_faculty_by_group(group_name)
    week_number, day_name = get_current_week_and_day(parsed_date, faculty_code=fac_code)

    with db_connection() as conn:
        cursor = conn.cursor()

        base_lessons = []
        if course is not None:
            if fac_code:
                cursor.execute(
                    'SELECT s.subject_name, s.lesson_type FROM schedules s '
                    'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                    'WHERE s.faculty = ? AND s.course_number = ? AND s.group_name = ? '
                    'AND s.day_of_week = ? AND sw.week_number = ? AND s.time_slot = ?',
                    (fac_code, course, group_name, day_name, week_number, time_slot),
                )
            else:
                cursor.execute(
                    'SELECT s.subject_name, s.lesson_type FROM schedules s '
                    'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                    'WHERE s.course_number = ? AND s.group_name = ? '
                    'AND s.day_of_week = ? AND sw.week_number = ? AND s.time_slot = ?',
                    (course, group_name, day_name, week_number, time_slot),
                )
            base_lessons = [f'{r[0]} ({r[1]})' for r in cursor.fetchall()]

        cursor.execute(
            'DELETE FROM cancelled_lessons WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? AND time_slot = ?',
            (fac_code, group_name, date_str, time_slot),
        )
        cursor.execute(
            'DELETE FROM replacements WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? AND time_slot = ?',
            (fac_code, group_name, date_str, time_slot),
        )
        cursor.execute(
            'INSERT INTO replacements (faculty, group_name, target_date, time_slot, subject_info, added_by, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (fac_code, group_name, date_str, time_slot, subject_info, message.from_user.id, now_local().strftime('%d.%m.%Y %H:%M')),
        )
        cursor.execute(
            'SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1',
            (fac_code, group_name),
        )
        group_users = cursor.fetchall()

    replaced_line = (
        f'<s>{esc(", ".join(base_lessons))}</s> → '
        if base_lessons
        else ''
    )

    pair_prefix = format_pair_prefix(time_slot)
    bot.send_message(
        message.chat.id,
        f'🎉 <b>Замена успешно добавлена и сохранена в расписании!</b>\n\n'
        f'📅 <b>Дата:</b> {date_str}\n'
        f'🎓 <b>Группа:</b> {esc(group_name)}\n'
        f'{pair_prefix}\n'
        f'📘 <b>Предмет:</b> {replaced_line}<b>{esc(subject_info)}</b>',
        parse_mode='HTML',
    )

    notify_text = (
        f'⚡ <b>НОВАЯ ЗАМЕНА В РАСПИСАНИИ!</b>\n'
        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}\n'
        f'📅 <b>Дата:</b> {date_str}</blockquote>\n\n'
        f'🔄 {pair_prefix}\n'
        f'👉 {replaced_line}<b>{esc(subject_info)}</b>'
    )
    for (u_id,) in group_users:
        if u_id != message.from_user.id:
            enqueue_message(u_id, notify_text, parse_mode='HTML')
    for g_chat_id in get_bound_group_chats(fac_code, group_name):
        enqueue_message(g_chat_id, notify_text, parse_mode='HTML')


def execute_delete_pair(chat_id, user_id, course, group_name, date_str, time_slot):
    try:
        parsed_date = datetime.strptime(date_str, '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
    except ValueError:
        bot.send_message(chat_id, '⚠️ Ошибка формата даты. Попробуй отменить пару заново.')
        return
    _, fac_code = find_course_and_faculty_by_group(group_name)
    if not fac_code:
        u_cur = get_user_data(user_id)
        fac_code = (u_cur[7] if u_cur and u_cur[7] else '') or infer_faculty_by_group(group_name)
    week_number, day_name = get_current_week_and_day(parsed_date, faculty_code=fac_code)

    with db_connection() as conn:
        cursor = conn.cursor()

        if fac_code:
            cursor.execute(
                'SELECT s.subject_name, s.lesson_type FROM schedules s '
                'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                'WHERE s.faculty = ? AND s.course_number = ? AND s.group_name = ? '
                'AND s.day_of_week = ? AND sw.week_number = ? AND s.time_slot = ?',
                (fac_code, course, group_name, day_name, week_number, time_slot),
            )
        else:
            cursor.execute(
                'SELECT s.subject_name, s.lesson_type FROM schedules s '
                'JOIN schedule_weeks sw ON s.id = sw.schedule_id '
                'WHERE s.course_number = ? AND s.group_name = ? '
                'AND s.day_of_week = ? AND sw.week_number = ? AND s.time_slot = ?',
                (course, group_name, day_name, week_number, time_slot),
            )
        sched_rows = cursor.fetchall()

        cursor.execute(
            'SELECT subject_info FROM replacements '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? AND time_slot = ?',
            (fac_code, group_name, date_str, time_slot),
        )
        repl_rows = cursor.fetchall()

        cursor.execute(
            'DELETE FROM replacements WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? AND time_slot = ?',
            (fac_code, group_name, date_str, time_slot),
        )

        if sched_rows:
            cursor.execute(
                'SELECT id FROM cancelled_lessons WHERE (faculty = ? OR faculty = "") AND group_name = ? AND target_date = ? AND time_slot = ?',
                (fac_code, group_name, date_str, time_slot),
            )
            if not cursor.fetchone():
                cursor.execute(
                    'INSERT INTO cancelled_lessons (faculty, group_name, target_date, time_slot, cancelled_by, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (fac_code, group_name, date_str, time_slot, user_id, now_local().strftime('%d.%m.%Y %H:%M')),
                )

        cursor.execute(
            'SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1',
            (fac_code, group_name),
        )
        group_users = cursor.fetchall()

    removed_names = [f'{r[0]} ({r[1]})' for r in sched_rows] + [r[0] for r in repl_rows]
    removed_desc = ', '.join(removed_names) if removed_names else 'Пара по расписанию'

    pair_prefix = format_pair_prefix(time_slot)
    bot.send_message(
        chat_id,
        f'🗑 <b>Пара успешно отменена!</b>\n\n'
        f'📅 <b>Дата:</b> {date_str}\n'
        f'🎓 <b>Группа:</b> {esc(group_name)}\n'
        f'{pair_prefix}\n'
        f'❌ <b>Убрано:</b> {esc(removed_desc)}',
        parse_mode='HTML',
    )

    notify_text = (
        f'🚫 <b>ОТМЕНА ПАРЫ В РАСПИСАНИИ!</b>\n'
        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}\n'
        f'📅 <b>Дата:</b> {date_str}</blockquote>\n\n'
        f'❌ {pair_prefix}\n'
        f'<s>{esc(removed_desc)}</s> — <b>ПАРА ОТМЕНЕНА</b>'
    )
    for (u_id,) in group_users:
        if u_id != user_id:
            enqueue_message(u_id, notify_text, parse_mode='HTML')


# --- ПРОСМОТР И ОТКАТ АКТИВНЫХ ЗАМЕН/ОТМЕН ---
def build_active_changes_view(group_name, faculty_code=None):
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    today = now_local().date()
    valid_dates = {(today + timedelta(days=i)).strftime('%d.%m.%Y') for i in range(8)}

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, target_date, time_slot, subject_info FROM replacements '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (eff_fac, group_name),
        )
        all_repls = cursor.fetchall()

        cursor.execute(
            'SELECT id, target_date, time_slot FROM cancelled_lessons '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (eff_fac, group_name),
        )
        all_cancs = cursor.fetchall()

    repls = [r for r in all_repls if r[1] in valid_dates]
    cancs = [c for c in all_cancs if c[1] in valid_dates]

    repls.sort(key=lambda x: (datetime.strptime(x[1], '%d.%m.%Y'), x[2]))
    cancs.sort(key=lambda x: (datetime.strptime(x[1], '%d.%m.%Y'), x[2]))

    text = (
        f'📋 <b>АКТИВНЫЕ ЗАМЕНЫ И ОТМЕНЫ (на 7 дней вперёд)</b>\n'
        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}</blockquote>\n\n'
    )
    markup = InlineKeyboardMarkup(row_width=1)

    if not repls and not cancs:
        text += '✨ <i>На ближайшую неделю замен и отмен не внесено.</i>'
        return text, markup

    if repls:
        text += '🔄 <b>Замены:</b>\n'
        for r_id, r_date, r_time, r_subj in repls:
            p_num = get_pair_num(r_time)
            p_label = f'{p_num} пара' if p_num else r_time
            text += f'• <b>{r_date}</b> ({p_label}) — {esc(r_subj)}\n'
            markup.add(
                InlineKeyboardButton(
                    f'❌ Удалить замену: {r_date[:5]} ({p_label}) {short_text(r_subj, 18)}',
                    callback_data=f'rev_repl_{r_id}',
                )
            )
        text += '\n'

    if cancs:
        text += '🚫 <b>Отменённые пары:</b>\n'
        for c_id, c_date, c_time in cancs:
            p_num = get_pair_num(c_time)
            p_label = f'{p_num} пара' if p_num else c_time
            text += f'• <b>{c_date}</b> — {p_label} ({c_time})\n'
            markup.add(
                InlineKeyboardButton(
                    f'↩️ Вернуть пару: {c_date[:5]} ({p_label})',
                    callback_data=f'rev_canc_{c_id}',
                )
            )

    text += '\n<i>Нажми на кнопку ниже, чтобы откатить ошибочную правку в один клик:</i>'
    return text, markup


def show_active_changes(message):
    _, group_name, fac_code = get_managed_context(message.from_user)
    if not group_name:
        start_onboarding(message)
        return
    if not is_user_starosta(message.from_user):
        bot.send_message(
            message.chat.id,
            '⛔ Просмотр и откат правок доступен только старосте.',
            parse_mode='HTML',
        )
        return

    text, markup = build_active_changes_view(group_name, faculty_code=fac_code)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')


@bot.callback_query_handler(
    func=lambda call: call.data in ('repl_back_date', 'del_back_date', 'repl_datemanual', 'del_datemanual')
    or call.data.startswith(('spec_', 'docx_', 'bc_target_', 'bc_group_', 'bc_grp_', 'repl_date_', 'del_date_', 'repl_pair_', 'del_pair_', 'rev_repl_', 'rev_canc_'))
)
def handle_admin_and_replacements_callbacks(call):
    sync_username(call.from_user)
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data.startswith('docx_setfac_'):
        if not is_admin_user(call.from_user):
            return
        rest = data.replace('docx_setfac_', '')
        fac_code, token = rest.split('_', 1)
        payload = get_pending_docx_import(token)
        if not payload:
            bot.send_message(chat_id, '⚠️ Сессия импорта устарела. Отправь файл заново.')
            return
        payload['faculty_code'] = fac_code
        save_pending_docx_import(token, payload)
        year_start_str = payload['year_start_dt'].strftime('%d.%m.%Y')
        cur_week = calc_week_number_for_start(now_local(), payload['year_start_dt'])
        by_group = {}
        for r in payload['records']:
            by_group[(r['course'], r['group'])] = by_group.get((r['course'], r['group']), 0) + 1
        preview_lines = [
            f'• {esc(grp)}: <b>{cnt}</b> записей'
            for (crs, grp), cnt in sorted(by_group.items())
        ]
        text = (
            f'✅ <b>Файл расписания готов к загрузке!</b>\n\n'
            f'🏛 <b>Выбран факультет:</b> {esc(get_faculty_short(fac_code))} ({esc(get_faculty_full(fac_code))})\n'
            f'📊 <b>Всего строк:</b> {len(payload["records"])}\n\n'
            f'🎓 <b>Распознанные группы:</b>\n' + '\n'.join(preview_lines) +
            f'\n\n📆 <b>Счётчик учебных недель:</b> от <code>{year_start_str}</code> (сейчас ' + (f'<b>{cur_week}-я</b> неделя)' if cur_week > 0 else '<i>ещё не началась</i>)')
        )
        markup = InlineKeyboardMarkup(row_width=2)
        fac_btns = []
        for f_code in FACULTY_ORDER:
            mark = '✅ ' if f_code == fac_code else ''
            fac_btns.append(
                InlineKeyboardButton(
                    f'{mark}{get_faculty_short(f_code)}',
                    callback_data=f'docx_setfac_{f_code}_{token}',
                )
            )
        markup.add(*fac_btns)
        markup.add(
            InlineKeyboardButton(
                f'🚀 Применить ({get_faculty_short(fac_code)}, старт {year_start_str})',
                callback_data=f'docx_apply_auto_{token}',
            )
        )
        markup.add(
            InlineKeyboardButton(
                '📅 Изменить дату 1-й недели учебного года',
                callback_data=f'docx_apply_manual_{token}',
            )
        )
        markup.add(InlineKeyboardButton('❌ Отмена', callback_data=f'docx_cancel_{token}'))
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        except Exception:
            pass
        return

    if data.startswith('docx_cancel_'):
        if not is_admin_user(call.from_user):
            return
        clear_user_state(user_id)
        token = data.replace('docx_cancel_', '')
        get_pending_docx_import(token, pop=True)
        try:
            bot.edit_message_text('❌ Импорт нового расписания отменён.', chat_id, msg_id)
        except Exception:
            pass
        return

    if data.startswith('docx_apply_auto_'):
        if not is_admin_user(call.from_user):
            return
        token = data.replace('docx_apply_auto_', '')
        payload = get_pending_docx_import(token)
        if not payload:
            bot.send_message(chat_id, '⚠️ Сессия импорта устарела. Отправь файл заново.')
            return
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        apply_parsed_docx_schedule(chat_id, user_id, token, payload['year_start_dt'])
        return

    if data.startswith('docx_apply_manual_'):
        if not is_admin_user(call.from_user):
            return
        token = data.replace('docx_apply_manual_', '')
        if not get_pending_docx_import(token):
            bot.send_message(chat_id, '⚠️ Сессия импорта устарела. Отправь файл заново.')
            return
        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_docx_date', {'import_token': token})
        default_start_hint = get_default_academic_year_start().strftime('%d.%m.%Y')
        safe_edit_message_text(
            f'📅 Введи дату <b>начала учебного года</b> в формате <code>ДД.ММ.ГГГГ</code>\n'
            f'<i>(по умолчанию: {default_start_hint})</i>\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data == 'spec_back':
        clear_user_state(user_id)
        open_special_menu(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_add_repl':
        open_add_replacement(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_del_pair':
        open_delete_pair(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_active_changes':
        if not is_user_starosta(call.from_user):
            return
        _, group_name, fac_code = get_managed_context(call.from_user)
        if not group_name:
            return
        text, markup = build_active_changes_view(group_name, faculty_code=fac_code)
        markup.add(InlineKeyboardButton('🔙 Назад в спецвозможности', callback_data='spec_back'))
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        except Exception:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')
        return

    if data == 'spec_add_hw':
        open_add_homework(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_sec_group':
        open_special_group_menu(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_sec_system':
        open_special_system_menu(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_toggle_extra_lists':
        if not is_user_starosta(call.from_user):
            return
        _, target_grp, target_fac = get_managed_context(call.from_user)
        if not target_grp:
            return
        cur_val = is_extra_hw_lists_enabled(target_fac, target_grp)
        set_extra_hw_lists_enabled(target_fac, target_grp, not cur_val)
        open_special_group_menu(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_stats':
        send_stats_panel(chat_id, call.from_user, tab='overview', message_id=msg_id)
        return

    if data.startswith('spec_stats_tab_'):
        tab_name = data.replace('spec_stats_tab_', '')
        send_stats_panel(chat_id, call.from_user, tab=tab_name, message_id=msg_id)
        return

    if data == 'spec_changes_log':
        send_stats_panel(chat_id, call.from_user, tab='changes', message_id=msg_id)
        return

    if data == 'spec_broadcast':
        open_broadcast_menu(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_upload_docx':
        if not is_admin_user(call.from_user):
            return
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton('🔙 Назад', callback_data='spec_back'))
        safe_edit_message_text(
            '📥 <b>ОБНОВЛЕНИЕ РАСПИСАНИЯ ИЗ WORD (.DOCX / .DOC)</b>\n\n'
            'Просто отправь мне в этот чат документ <code>.docx</code> (или <code>.doc</code>) с расписанием факультета (в табличной форме СИ УУНиТ).\n\n'
            '• Бот автоматически определит факультет (ЕМФ, Педфак, ФЭП, ТФ), курсы, направления, предметы, недели, преподавателей и аудитории.\n'
            '• Записи других факультетов в базе сохранятся нетронутыми.\n'
            '• Счётчик недель и база автоматически обнуляются строго 1 сентября каждого нового учебного года, а во 2-м семестре счётчик продолжает идти сквозным образом без сброса.\n'
            '• Перед перезаписью автоматически сделается резервная копия текущей БД.',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data == 'spec_backup_now':
        if is_admin_user(call.from_user):
            send_db_backup(manual_chat_id=chat_id)
        return

    if data == 'spec_reset_year_1':
        if not is_admin_user(call.from_user):
            return
        clear_user_state(user_id)
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton('⚠️ Да, точно уверен (шаг 1 из 3)', callback_data='spec_reset_year_2'),
            InlineKeyboardButton('❌ Отмена', callback_data='spec_back'),
        )
        safe_edit_message_text(
            '⚠️ <b>ПОЛНЫЙ СБРОС УЧЕБНОГО ГОДА — ПОДТВЕРЖДЕНИЕ 1/3</b>\n\n'
            'Это действие выполнит следующее:\n'
            '• Сделает резервную копию БД и отправит тебе\n'
            '• Полностью очистит <code>schedule.json</code> и таблицы расписания\n'
            '• Удалит все ДЗ, замены, отмены и очереди\n'
            '• Выкинет <b>всех студентов</b> с их курсов и групп\n'
            '• Снимет роль <b>Старосты</b> со всех старост\n\n'
            'Ты точно уверен, что хочешь обнулить всё?',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data == 'spec_reset_year_2':
        if not is_admin_user(call.from_user):
            return
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton('⚠️ Да, точно-точно уверен (шаг 2 из 3)', callback_data='spec_reset_year_3'),
            InlineKeyboardButton('❌ Отмена', callback_data='spec_back'),
        )
        safe_edit_message_text(
            '🚨 <b>ПОДТВЕРЖДЕНИЕ 2/3</b>\n\n'
            'После сброса <b>никто не сможет смотреть старое расписание</b>, а всем студентам придёт уведомление о новом учебном годе.\n'
            'Всех старост придётся назначить заново вручную через <code>/starosta</code>.\n\n'
            'Подтверждаешь намерение?',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data == 'spec_reset_year_3':
        if not is_admin_user(call.from_user):
            return
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton('🔥 Точно уверен — перейти к вводу слова (шаг 3 из 3)', callback_data='spec_reset_year_final'),
            InlineKeyboardButton('❌ Отмена', callback_data='spec_back'),
        )
        safe_edit_message_text(
            '🛑 <b>ПОСЛЕДНЕЕ ПРЕДУПРЕЖДЕНИЕ — ПОДТВЕРЖДЕНИЕ 3/3</b>\n\n'
            'Нажми кнопку ниже только если прямо сейчас нужно полностью обнулить учебный год и базу расписания.',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data == 'spec_reset_year_final':
        if not is_admin_user(call.from_user):
            return
        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_full_reset_confirm', {'initiated_at': time.time()})
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))
        safe_edit_message_text(
            '🔐 <b>ФИНАЛЬНОЕ ПОДТВЕРЖДЕНИЕ</b>\n\n'
            'Чтобы запустить полный сброс учебного года, отправь в этот чат слово:\n'
            '<code>УВЕРЕН</code>\n\n'
            '<i>Любой другой текст или кнопка ниже отменят сброс.</i>',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data == 'bc_group_pick_fac':
        if not is_admin_user(call.from_user):
            return
        clear_user_state(user_id)
        markup = InlineKeyboardMarkup(row_width=2)
        btns = [
            InlineKeyboardButton(f'🏛 {get_faculty_short(f)}', callback_data=f'bc_grp_fac_{f}')
            for f in FACULTY_ORDER
            if any(bool(g) for g in core.FACULTY_STRUCTURE.get(f, {}).values())
        ]
        markup.add(*btns)
        markup.add(InlineKeyboardButton('🔙 Назад', callback_data='spec_broadcast'))
        safe_edit_message_text(
            '🎯 <b>РАССЫЛКА КОНКРЕТНОЙ ГРУППЕ</b>\n\n<b>Шаг 1 из 3.</b> Выбери факультет:',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data.startswith('bc_grp_fac_'):
        if not is_admin_user(call.from_user):
            return
        fac_code = data.replace('bc_grp_fac_', '')
        courses = core.FACULTY_STRUCTURE.get(fac_code, {})
        markup = InlineKeyboardMarkup(row_width=2)
        c_btns = [
            InlineKeyboardButton(f'🎓 {c} курс', callback_data=f'bc_grp_crs_{fac_code}_{c}')
            for c in sorted(courses.keys())
            if courses[c]
        ]
        markup.add(*c_btns)
        markup.add(InlineKeyboardButton('🔙 К выбору факультета', callback_data='bc_group_pick_fac'))
        safe_edit_message_text(
            f'🎯 <b>Факультет:</b> {esc(get_faculty_short(fac_code))}\n\n<b>Шаг 2 из 3.</b> Выбери курс:',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data.startswith('bc_grp_crs_'):
        if not is_admin_user(call.from_user):
            return
        parts = data.replace('bc_grp_crs_', '').split('_')
        fac_code = parts[0]
        course = int(parts[1])
        groups = core.FACULTY_STRUCTURE.get(fac_code, {}).get(course, [])
        if not groups:
            return
        set_user_state(user_id, '', {'bc_groups': groups, 'bc_fac': fac_code, 'bc_course': course})
        markup = InlineKeyboardMarkup(row_width=1)
        for idx, g_nm in enumerate(groups):
            markup.add(InlineKeyboardButton(f'📘 {g_nm}', callback_data=f'bc_grp_sel_{fac_code}_{course}_{idx}'))
        markup.add(InlineKeyboardButton('🔙 К выбору курса', callback_data=f'bc_grp_fac_{fac_code}'))
        safe_edit_message_text(
            f'🎯 <b>{esc(get_faculty_short(fac_code))} • {course} курс</b>\n\n<b>Шаг 3 из 3.</b> Выбери группу:',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data.startswith('bc_grp_sel_'):
        if not is_admin_user(call.from_user):
            return
        parts = data.replace('bc_grp_sel_', '').split('_')
        fac_code = parts[0]
        course = int(parts[1])
        g_idx = int(parts[2])
        _, state = get_user_state(user_id)
        groups = (state or {}).get('bc_groups') or core.FACULTY_STRUCTURE.get(fac_code, {}).get(course, [])
        if not groups or g_idx < 0 or g_idx >= len(groups):
            return
        grp_name = groups[g_idx]
        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_broadcast', {
            'target': 'group',
            'faculty': fac_code,
            'group_name': grp_name,
        })
        safe_edit_message_text(
            f'📣 <b>Аудитория:</b> {esc(grp_name)} [{esc(get_faculty_short(fac_code))}]\n'
            f'<i>(также отправится в привязанную беседу группы, если она есть)</i>\n\n'
            f'Теперь отправь текст объявления (можно прикрепить фото с подписью).\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data.startswith('bc_target_'):
        if not is_admin_user(call.from_user):
            return
        clear_user_state(user_id)
        target = data.replace('bc_target_', '')
        if target == 'all':
            target_label = 'всем пользователям'
        elif target.startswith('fac_'):
            target_label = f'факультету {get_faculty_short(target.replace("fac_", ""))}'
        else:
            target_label = f'{target} курсу'
        set_user_state(user_id, 'awaiting_broadcast', {'target': target})
        safe_edit_message_text(
            f'📣 <b>Аудитория:</b> {target_label}\n\n'
            f'Теперь отправь текст объявления (можно прикрепить фото с подписью).\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data.startswith('repl_date_') or data.startswith('del_date_'):
        if not is_user_starosta(call.from_user):
            return
        course, group_name, _ = get_managed_context(call.from_user)
        if not group_name:
            return
        action = 'repl' if data.startswith('repl_') else 'del'
        date_str = data.split('_date_')[1]
        show_pair_selection_for_action(chat_id, msg_id, action, course, group_name, date_str)
        return

    if data in ('repl_back_date', 'del_back_date'):
        if not is_user_starosta(call.from_user):
            return
        _, group_name, _ = get_managed_context(call.from_user)
        if not group_name:
            return
        action = 'repl' if data.startswith('repl_') else 'del'
        title = '📝 <b>Добавление замены</b>' if action == 'repl' else '🗑 <b>Отмена пары</b>'
        safe_edit_message_text(
            f'{title}\n<blockquote>🎓 {esc(group_name)}</blockquote>\n\nВыбери <b>дату</b>:',
            chat_id,
            msg_id,
            reply_markup=build_date_choice_markup(action),
            parse_mode='HTML',
        )
        return

    if data in ('repl_datemanual', 'del_datemanual'):
        if not is_user_starosta(call.from_user):
            return
        course, group_name, _ = get_managed_context(call.from_user)
        if not group_name:
            return
        action = 'repl' if data.startswith('repl_') else 'del'
        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_manual_date', {
            'action': action,
            'course': course,
            'group_name': group_name,
        })
        try:
            bot.edit_message_text(
                f'⌨️ Введи дату в формате <code>ДД.ММ.ГГГГ</code> (например, <code>{now_local().strftime("%d.%m.%Y")}</code>)\n'
                f'<i>Для отмены напиши «Отмена».</i>',
                chat_id,
                msg_id,
                parse_mode='HTML',
            )
        except Exception:
            pass

        return

    if data.startswith('repl_pair_'):
        if not is_user_starosta(call.from_user):
            return
        _, group_name, _ = get_managed_context(call.from_user)
        if not group_name:
            return
        parts = data.split('_')
        date_str = parts[2]
        pair_num = int(parts[3])
        time_slot = NUM_TO_TIME_SLOT.get(pair_num, str(pair_num))

        clear_user_state(user_id)
        set_user_state(user_id, 'awaiting_repl_subject', {
            'group_name': group_name,
            'date_str': date_str,
            'time_slot': time_slot,
        })
        safe_edit_message_text(
            f'✅ <b>Дата:</b> {date_str}\n'
            f'{format_pair_prefix(time_slot)}\n\n'
            f'<b>Шаг 3 из 3.</b> Введи <b>название предмета, преподавателя и аудиторию</b>\n'
            f'<i>(например: Экологический мониторинг (ПЗ) — ауд. 206, доц. Кужина Г.Ш.)</i>\n'
            f'<i>Для отмены напиши «Отмена».</i>',
            chat_id,
            msg_id,
            parse_mode='HTML',
        )
        return

    if data.startswith('del_pair_'):
        if not is_user_starosta(call.from_user):
            return
        course, group_name, _ = get_managed_context(call.from_user)
        if not group_name:
            return
        parts = data.split('_')
        date_str = parts[2]
        pair_num = int(parts[3])
        time_slot = NUM_TO_TIME_SLOT.get(pair_num, str(pair_num))

        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        execute_delete_pair(chat_id, user_id, course, group_name, date_str, time_slot)
        return

    # --- ОТКАТ АКТИВНЫХ ЗАМЕН И ОТМЕН ---
    if data.startswith('rev_repl_') or data.startswith('rev_canc_'):
        if not is_user_starosta(call.from_user):
            return
        _, group_name, _ = get_managed_context(call.from_user)
        if not group_name:
            return
        rec_id = int(data.split('_')[2])
        notify_text = None

        _, _, rev_fac = get_managed_context(call.from_user)
        eff_rev_fac = rev_fac or infer_faculty_by_group(group_name)
        with db_connection() as conn:
            cursor = conn.cursor()
            if data.startswith('rev_repl_'):
                cursor.execute(
                    'SELECT target_date, time_slot, subject_info FROM replacements '
                    'WHERE id = ? AND (faculty = ? OR faculty = "") AND group_name = ?',
                    (rec_id, eff_rev_fac, group_name),
                )
                row = cursor.fetchone()
                cursor.execute(
                    'DELETE FROM replacements WHERE id = ? AND (faculty = ? OR faculty = "") AND group_name = ?',
                    (rec_id, eff_rev_fac, group_name),
                )
                if row:
                    r_date, r_time, r_subj = row
                    notify_text = (
                        f'↩️ <b>ЗАМЕНА УДАЛЕНА ИЗ РАСПИСАНИЯ</b>\n'
                        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}\n'
                        f'📅 <b>Дата:</b> {r_date}</blockquote>\n\n'
                        f'{format_pair_prefix(r_time)}\n'
                        f'<s>{esc(r_subj)}</s>'
                    )
            else:
                cursor.execute(
                    'SELECT target_date, time_slot FROM cancelled_lessons '
                    'WHERE id = ? AND (faculty = ? OR faculty = "") AND group_name = ?',
                    (rec_id, eff_rev_fac, group_name),
                )
                row = cursor.fetchone()
                cursor.execute(
                    'DELETE FROM cancelled_lessons WHERE id = ? AND (faculty = ? OR faculty = "") AND group_name = ?',
                    (rec_id, eff_rev_fac, group_name),
                )
                if row:
                    c_date, c_time = row
                    notify_text = (
                        f'↩️ <b>ОТМЕНА ПАРЫ СНЯТА (ПАРА ВОЗВРАЩЕНА)</b>\n'
                        f'<blockquote>🎓 <b>Группа:</b> {esc(group_name)}\n'
                        f'📅 <b>Дата:</b> {c_date}</blockquote>\n\n'
                        f'{format_pair_prefix(c_time)} — снова в расписании.'
                    )

            cursor.execute(
                'SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1',
                (eff_rev_fac, group_name),
            )
            group_users = cursor.fetchall()

        if notify_text:
            for (u_id,) in group_users:
                if u_id != user_id:
                    enqueue_message(u_id, notify_text, parse_mode='HTML')
            for g_chat_id in get_bound_group_chats(eff_rev_fac, group_name):
                enqueue_message(g_chat_id, notify_text, parse_mode='HTML')

        text, markup = build_active_changes_view(group_name)
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        except Exception:
            pass
        return
