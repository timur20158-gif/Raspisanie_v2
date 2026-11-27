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
    build_progress_bar,
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
    is_session_mode_enabled,
    is_group_valid,
    is_user_starosta,
    now_local,
    reload_schedule_tables,
    resolve_target_user,
    safe_edit_message_text,
    save_pending_docx_import,
    save_semester_meta,
    send_db_backup,
    send_mascot_art,
    set_extra_hw_lists_enabled,
    set_session_mode_enabled,
    set_user_state,
    short_text,
    split_long_html_message,
    start_onboarding,
    sync_username,
)
from docx_parser import convert_doc_to_docx_if_needed, is_session_docx_file, parse_schedule_docx_file, parse_session_docx_file
from homework import (
    ask_hw_content,
    open_add_homework,
    send_homeworks_for_date_view,
    show_hw_subject_selection,
)
from schedule_search import get_all_group_subjects, get_day_lessons_for_date


def toggle_group_session_mode_and_notify(faculty_code, group_name, new_enabled):
    """Переключает режим сессии для группы и обновляет клавиатуру меню у всех её студентов в TG и VK."""
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    set_session_mode_enabled(eff_fac, group_name, new_enabled)

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT user_id, subscribed, is_starosta, is_hw_manager, username FROM users '
            'WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND COALESCE(is_blocked, 0) = 0',
            (eff_fac, group_name),
        )
        tg_users = cursor.fetchall()
        cursor.execute(
            'SELECT vk_user_id, COALESCE(is_starosta, 0) FROM vk_users '
            'WHERE (faculty = ? OR faculty = "") AND group_name = ? AND COALESCE(is_blocked, 0) = 0',
            (eff_fac, group_name),
        )
        vk_users = cursor.fetchall()

    if new_enabled:
        tg_msg = (
            f'🎓 <b>ВКЛЮЧЁН РЕЖИМ СЕССИИ ({esc(group_name)})</b>\n'
            f'<i>Меню обновлено: вместо обычного расписания появилась кнопка «🎓 Сессия».</i>'
        )
        vk_msg = (
            f'🎓 ВКЛЮЧЁН РЕЖИМ СЕССИИ ({group_name})\n'
            f'Меню обновлено: вместо обычного расписания появилась кнопка «🎓 Сессия».'
        )
    else:
        tg_msg = (
            f'📅 <b>РЕЖИМ СЕССИИ ВЫКЛЮЧЕН ({esc(group_name)})</b>\n'
            f'<i>Меню возвращено к обычному расписанию пар.</i>'
        )
        vk_msg = (
            f'📅 РЕЖИМ СЕССИИ ВЫКЛЮЧЕН ({group_name})\n'
            f'Меню возвращено к обычному расписанию пар.'
        )

    for u_id, u_sub, u_st, u_hw, u_uname in tg_users:
        is_st_flag = bool(u_st == 1 or (u_uname or '').lower() == ADMIN_USERNAME.lower())
        kb = get_main_keyboard(bool(u_sub), is_starosta=is_st_flag, is_hw_manager=bool(u_hw), session_mode=bool(new_enabled))
        enqueue_message(u_id, tg_msg, reply_markup=kb, parse_mode='HTML')

    if vk_users and callable(core.VK_SEND_HOOK):
        try:
            import vk_bot
            for vk_uid, v_st in vk_users:
                v_kb = vk_bot.build_main_kb(is_starosta=v_st, session_mode=bool(new_enabled))
                core.enqueue_vk_message(vk_uid, vk_msg, keyboard=v_kb)
        except Exception as e:
            print(f'Ошибка обновления VK-клавиатуры при смене режима сессии: {e}')


def open_special_group_menu(chat_id, user_obj, message_id=None):
    if not is_user_starosta(user_obj):
        return
    _, target_group, target_fac = get_managed_context(user_obj)
    if not target_group:
        bot.send_message(chat_id, '⚠️ Сначала выбери свой факультет и группу через /start.')
        return

    extra_on = is_extra_hw_lists_enabled(target_fac, target_group)
    extra_lbl = '📑 Доп. списки: ВКЛ' if extra_on else '📑 Доп. списки: ВЫКЛ'
    sess_on = is_session_mode_enabled(target_fac, target_group)
    sess_lbl = '🎓 Режим сессии: ВКЛ' if sess_on else '🎓 Режим сессии: ВЫКЛ'

    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton('➕ Замена', callback_data='spec_add_repl'),
        InlineKeyboardButton('🗑 Отмена пары', callback_data='spec_del_pair'),
    )
    markup.row(
        InlineKeyboardButton('📋 Активные правки', callback_data='spec_active_changes'),
        InlineKeyboardButton('📝 Добавить ДЗ', callback_data='spec_add_hw'),
    )
    markup.row(
        InlineKeyboardButton(sess_lbl, callback_data='spec_toggle_session_mode'),
        InlineKeyboardButton(extra_lbl, callback_data='spec_toggle_extra_lists'),
    )
    if is_admin_user(user_obj):
        markup.row(InlineKeyboardButton('🔙 Назад', callback_data='spec_back'))

    text = (
        f'🎓 <b>УПРАВЛЕНИЕ ГРУППОЙ</b>\n'
        f'<blockquote>🛡 <b>Группа:</b> {esc(target_group)}</blockquote>'
    )
    if message_id:
        safe_edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
    else:
        send_mascot_art(chat_id, 'marten_admin', text, reply_markup=markup)


def open_special_system_menu(chat_id, user_obj, message_id=None):
    if not is_admin_user(user_obj):
        return
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton('📥 Обновить расписание (.docx/.doc)', callback_data='spec_upload_docx'),
        InlineKeyboardButton('🎓 Загрузить сессию (.docx)', callback_data='spec_upload_session'),
        InlineKeyboardButton('💾 Бэкап БД сейчас', callback_data='spec_backup_now'),
        InlineKeyboardButton('🔄 Сбросить учебный год (обнулить всех)', callback_data='spec_reset_year_1'),
        InlineKeyboardButton('🔙 Назад', callback_data='spec_back'),
    )
    text = '⚙️ <b>СИСТЕМА И БАЗА ДАННЫХ</b>\n\n<i>Выбери действие:</i>'
    if message_id:
        safe_edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
    else:
        send_mascot_art(chat_id, 'marten_admin', text, reply_markup=markup)


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
        send_mascot_art(chat_id, 'marten_admin', text, reply_markup=markup)


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
        cursor.execute('DELETE FROM session_exams')

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

        cursor.execute(
            'SELECT vk_user_id FROM vk_users '
            'WHERE COALESCE(is_blocked, 0) = 0 AND (course > 0 OR group_name != "" OR is_starosta = 1)'
        )
        vk_reset_ids = [r[0] for r in cursor.fetchall()]
        cursor.execute(
            'UPDATE vk_users SET course = 0, group_name = "", managed_group = "", is_starosta = 0, state = "", payload_json = "{}"'
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
    if notify_users and vk_reset_ids:
        vk_ny_msg = (
            f'🎓 С НОВЫМ УЧЕБНЫМ ГОДОМ ({new_year_label})!\n\n'
            f'Счётчик учебных недель обнулён, старое расписание очищено, а привязка к прошлому курсу и группе сброшена.\n'
            f'Напиши «Начать», чтобы выбрать свой факультет, новый курс и направление.'
        )
        for vk_uid in vk_reset_ids:
            core.enqueue_vk_message(vk_uid, vk_ny_msg)

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
            cursor.execute(
                'SELECT vk_user_id, group_name FROM vk_users WHERE faculty = ? AND group_name != ""',
                (target_fac,),
            )
            vk_orphaned = [uid for uid, grp in cursor.fetchall() if grp not in fac_groups]
            for vk_uid_orph in vk_orphaned:
                cursor.execute(
                    'UPDATE vk_users SET group_name = "", managed_group = "", state = "", payload_json = "{}" WHERE vk_user_id = ?',
                    (vk_uid_orph,),
                )
                core.enqueue_vk_message(
                    vk_uid_orph,
                    '🔄 Обновился список направлений твоего факультета!\nНапиши «Начать», чтобы выбрать свою группу заново.',
                )

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



def apply_parsed_session_docx(chat_id, admin_user_id, import_token, faculty_code=None):
    payload = get_pending_docx_import(import_token, pop=True)
    if not payload or payload.get('kind') != 'session':
        bot.send_message(chat_id, '⚠️ Сессия импорта устарела. Отправь файл сессии заново.')
        return

    target_fac = faculty_code or payload.get('faculty_code') or 'ЕМФ'
    exams = payload['exams']
    fac_groups = sorted({ex['group'] for ex in exams})
    stamp = now_local().strftime('%d.%m.%Y %H:%M')

    with db_connection() as conn:
        cursor = conn.cursor()
        for grp in fac_groups:
            cursor.execute(
                'DELETE FROM session_exams WHERE (faculty = ? OR faculty = "") AND group_name = ?',
                (target_fac, grp),
            )
        for ex in exams:
            cursor.execute(
                'INSERT INTO session_exams (faculty, group_name, exam_date, exam_time, subject_name, exam_type, teacher, room, added_by, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    target_fac,
                    ex['group'],
                    ex['exam_date'],
                    ex['exam_time'],
                    ex['subject'],
                    ex['exam_type'],
                    ex['teacher'],
                    ex['room'],
                    admin_user_id,
                    stamp,
                ),
            )

    bot.send_message(
        chat_id,
        f'🎉 <b>РАСПИСАНИЕ СЕССИИ ЗАГРУЖЕНО!</b>\n\n'
        f'🏛 <b>Факультет:</b> {esc(get_faculty_short(target_fac))} ({esc(get_faculty_full(target_fac))})\n'
        f'🎯 <b>Загружено записей (экзамены/консультации):</b> {len(exams)}\n'
        f'🎓 <b>Обновлено групп:</b> {len(fac_groups)}\n\n'
        f'<i>Данные уже доступны студентам в разделе «🎓 Сессия» в Telegram и ВКонтакте!</i>',
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
        if is_session_docx_file(tmp_docx_path) or 'сесси' in fname.lower():
            exams, session_title, detected_fac = parse_session_docx_file(
                tmp_docx_path, default_faculty=fac_from_fname
            )
            if not exams:
                safe_edit_message_text(
                    '⚠️ Файл распознан как расписание сессии, но экзамены/консультации в таблицах не найдены.',
                    message.chat.id,
                    wait_msg.message_id,
                    parse_mode='HTML',
                )
                return
            by_grp = {}
            for ex in exams:
                by_grp[ex['group']] = by_grp.get(ex['group'], 0) + 1
            prev_lines = [f'• {esc(g)}: <b>{c}</b> записей' for g, c in sorted(by_grp.items())]
            import_token = f'sess_{message.from_user.id}_{int(time.time())}'
            save_pending_docx_import(import_token, {
                '_ts': time.time(),
                'kind': 'session',
                'exams': exams,
                'session_title': session_title,
                'faculty_code': detected_fac,
            })
            t_line = f'📄 <b>Заголовок:</b> <i>{esc(session_title)}</i>\n' if session_title else ''
            text = (
                f'🎓 <b>РАСПИСАНИЕ СЕССИИ РАСПОЗНАНО!</b>\n\n'
                f'{t_line}'
                f'🏛 <b>Факультет:</b> {esc(get_faculty_short(detected_fac))}\n'
                f'📊 <b>Всего записей (экзамены/консультации):</b> {len(exams)}\n\n'
                f'👥 <b>Группы:</b>\n' + '\n'.join(prev_lines)
            )
            markup = InlineKeyboardMarkup(row_width=2)
            fac_btns = [
                InlineKeyboardButton(
                    f'{"✅ " if f == detected_fac else ""}{get_faculty_short(f)}',
                    callback_data=f'docx_sessfac_{f}_{import_token}',
                )
                for f in FACULTY_ORDER
            ]
            markup.add(*fac_btns)
            markup.add(
                InlineKeyboardButton(
                    f'🚀 Загрузить сессию ({get_faculty_short(detected_fac)})',
                    callback_data=f'docx_sessapply_{import_token}',
                )
            )
            markup.add(InlineKeyboardButton('❌ Отмена', callback_data=f'docx_cancel_{import_token}'))
            safe_edit_message_text(text, message.chat.id, wait_msg.message_id, reply_markup=markup, parse_mode='HTML')
            return

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
            reply_markup=get_main_keyboard(sub_state, is_starosta=True, session_mode=is_session_mode_enabled(row[4] if row else None, assigned_group)),
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
            cursor.execute('SELECT user_id, subscribed, faculty, group_name FROM users WHERE LOWER(username) = ? AND is_starosta = 1', (uname_low,))
            affected_users = cursor.fetchall()
            cursor.execute(
                'UPDATE users SET is_starosta = 0, managed_group = "" WHERE LOWER(username) = ? AND is_starosta = 1',
                (uname_low,),
            )
        elif arg.isdigit():
            uid_num = int(arg)
            cursor.execute('SELECT user_id, subscribed, faculty, group_name FROM users WHERE user_id = ? AND is_starosta = 1', (uid_num,))
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
    for uid_rem, sub_rem, fac_rem, grp_rem in affected_users:
        try:
            bot.send_message(
                uid_rem,
                'ℹ️ Роль <b>Старосты</b> была снята администратором. Главное меню обновлено.',
                reply_markup=get_main_keyboard(
                    bool(sub_rem),
                    is_starosta=False,
                    session_mode=is_session_mode_enabled(fac_rem, grp_rem),
                ),
                parse_mode='HTML',
            )
        except Exception:
            pass
    bot.send_message(
        message.chat.id,
        f'✅ Роль старосты снята с пользователя <b>{esc(arg)}</b>.',
        parse_mode='HTML',
    )



@bot.message_handler(commands=['vk_starosta', 'vk_unstarosta'], chat_types=['private'])
def cmd_vk_starosta_toggle(message):
    sync_username(message.from_user)
    if not is_admin_user(message.from_user):
        return
    parts = (message.text or '').split()
    is_grant = parts[0].lower().startswith('/vk_starosta')
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.send_message(
            message.chat.id,
            f'ℹ️ Укажи числовой VK ID пользователя: <code>{parts[0]} 123456789</code>',
            parse_mode='HTML',
        )
        return
    vk_uid = int(parts[1].strip())
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT group_name, full_name FROM vk_users WHERE vk_user_id = ?', (vk_uid,))
        row = cursor.fetchone()
        if not row:
            bot.send_message(
                message.chat.id,
                f'⚠️ Пользователь VK <code>{vk_uid}</code> ещё не запускал VK-бота. Попроси его написать «Начать» в ВК и выбрать группу.',
                parse_mode='HTML',
            )
            return
        grp_name, full_name = row[0] or '', row[1] or f'id{vk_uid}'
        if is_grant:
            cursor.execute(
                'UPDATE vk_users SET is_starosta = 1, managed_group = ? WHERE vk_user_id = ?',
                (grp_name, vk_uid),
            )
        else:
            cursor.execute(
                'UPDATE vk_users SET is_starosta = 0, managed_group = "" WHERE vk_user_id = ?',
                (vk_uid,),
            )

    if is_grant:
        core.enqueue_vk_message(
            vk_uid,
            f'🌟 Тебе выдана роль Старосты в VK-боте!\n🛡 Группа: {grp_name}\n\nНапиши «Меню» или нажми любую кнопку, чтобы появилась кнопка «🛠 Староста».',
        )
        bot.send_message(
            message.chat.id,
            f'✅ Пользователю VK <b>{esc(full_name)}</b> (<code>{vk_uid}</code>) выдана роль <b>Старосты</b> (группа: {esc(grp_name or "не выбрана")}).',
            parse_mode='HTML',
        )
    else:
        core.enqueue_vk_message(vk_uid, 'ℹ️ Роль Старосты в VK-боте была снята администратором.')
        bot.send_message(
            message.chat.id,
            f'✅ Роль старосты снята с пользователя VK <b>{esc(full_name)}</b> (<code>{vk_uid}</code>).',
            parse_mode='HTML',
        )


@bot.message_handler(commands=['backup'], chat_types=['private'])
def cmd_backup(message):
    sync_username(message.from_user)
    if not is_admin_user(message.from_user):
        return
    send_db_backup(manual_chat_id=message.chat.id)


# --- ПАНЕЛЬ СТАТИСТИКИ ДЛЯ АДМИНА (/stats) ПО ВКЛАДКАМ ---

def _build_clicks_chart_png(days=7):
    """Строит компактный PNG-график кликов Telegram vs ВКонтакте за последние N дней (через matplotlib, если установлен)."""
    try:
        import io
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception:
        return None
    today = now_local().date()
    dates = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    iso_dates = [d.strftime('%Y-%m-%d') for d in dates]
    labels = [d.strftime('%d.%m') for d in dates]
    tg_counts = {d: 0 for d in iso_dates}
    vk_counts = {d: 0 for d in iso_dates}
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT date_str, action, count FROM daily_stats WHERE date_str >= ?',
            (iso_dates[0],),
        )
        for d_str, act, cnt in cursor.fetchall():
            if d_str not in tg_counts:
                continue
            if str(act).startswith('vk_'):
                vk_counts[d_str] += int(cnt or 0)
            else:
                tg_counts[d_str] += int(cnt or 0)

    fig, ax = plt.subplots(figsize=(6.4, 3.2), dpi=130)
    fig.patch.set_facecolor('#F8FAFC')
    ax.set_facecolor('#FFFFFF')
    tg_vals = [tg_counts[d] for d in iso_dates]
    vk_vals = [vk_counts[d] for d in iso_dates]
    ax.plot(labels, tg_vals, marker='o', linewidth=2.2, color='#229ED9', label='Telegram')
    ax.plot(labels, vk_vals, marker='s', linewidth=2.2, color='#0077FF', label='ВКонтакте')
    ax.set_title(f'Активность (клики) за {days} дн.: TG vs VK', fontsize=11, fontweight='bold', color='#1B365D')
    ax.grid(True, linestyle='--', alpha=0.4)
    ax.legend(frameon=True, fontsize=9)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    plt.close(fig)
    buf.seek(0)
    buf.name = 'clicks_chart.png'
    return buf


def _build_stats_tabs_keyboard(active_tab='overview'):
    tabs = [
        ('overview', '📊 Сводка'),
        ('clicks', '📈 Клики'),
        ('groups', '🎓 Группы'),
        ('starostas', '🌟 Старосты'),
        ('changes', '🕵️ Правки'),
        ('system', '🖥 Система'),
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
    if active_tab == 'clicks':
        markup.row(InlineKeyboardButton('📊 График TG vs VK (PNG)', callback_data='spec_stats_chart'))
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

            cursor.execute('SELECT COUNT(*) FROM vk_users')
            vk_total = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM vk_users WHERE COALESCE(last_active, 0) >= ?', (ts_24h,))
            vk_dau = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM vk_users WHERE COALESCE(last_active, 0) >= ?', (ts_7d,))
            vk_wau = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM vk_users WHERE COALESCE(sub_morning, 0) = 1')
            vk_sub_m = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM vk_users WHERE COALESCE(sub_evening, 0) = 1')
            vk_sub_e = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM vk_users WHERE COALESCE(notify_updates, 0) = 1')
            vk_notify = cursor.fetchone()[0]

            notify_pct = round((notify_u / alive_users * 100), 1) if alive_users else 0
            cur_week, _ = get_current_week_and_day()
            q_size = MAILING_QUEUE.qsize()

            text = (
                f'📊 <b>СТАТИСТИКА • СВОДКА</b> <i>({stamp})</i>\n\n'
                f'<blockquote>📅 <b>Неделя:</b> {cur_week}-я (старт {core.ACADEMIC_YEAR_START_DATE.strftime("%d.%m.%Y")})\n\n'
                f'📱 <b>TELEGRAM:</b>\n'
                f'👥 Всего: <b>{total_users}</b> (живых: <b>{alive_users}</b>, блок: {blocked_users})\n'
                f'🔥 Онлайн: 24ч: <b>{dau}</b> · 7д: <b>{wau}</b> · 30д: <b>{mau}</b>\n'
                f'🆕 Прирост: 24ч: <b>+{new_24h}</b> · 7д: <b>+{new_7d}</b>\n'
                f'☀️ Рассылка: утро <b>{sub_m}</b> · вечер <b>{sub_e}</b> · 🔔 пуши: {notify_u} ({notify_pct}%)\n\n'
                f'🔵 <b>ВКОНТАКТЕ:</b>\n'
                f'👥 Всего: <b>{vk_total}</b> · 🔥 Онлайн: 24ч: <b>{vk_dau}</b> · 7д: <b>{vk_wau}</b>\n'
                f'☀️ Рассылка: утро <b>{vk_sub_m}</b> · вечер <b>{vk_sub_e}</b> · 🔔 пуши: {vk_notify}</blockquote>\n\n'
                f'💬 <b>Бесед TG:</b> {chats_cnt} · 📨 <b>Очередь TG:</b> {q_size}\n'
                f'📚 <b>База:</b> пар {sched_cnt} · замен {repl_cnt} · отмен {canc_cnt} · ДЗ {hw_cnt}'
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

            tg_labels = [
                ('today', '📅 Сегодня'),
                ('tomorrow', '⏭ Завтра'),
                ('week', '🗓 Неделя'),
                ('homework', '📚 Домашка'),
                ('search', '🔍 Поиск'),
                ('inline', '💬 Инлайн'),
                ('group_cmd', '👥 Беседы'),
            ]
            vk_labels = [
                ('vk_today', '📅 Сегодня'),
                ('vk_tomorrow', '⏭ Завтра'),
                ('vk_week', '🗓 Неделя'),
                ('vk_homework', '📚 Домашка'),
                ('vk_search', '🔍 Поиск'),
            ]
            tg_today = sum(today_actions.get(k, 0) for k, _ in tg_labels)
            tg_week = sum(week_actions.get(k, 0) for k, _ in tg_labels)
            vk_today = sum(today_actions.get(k, 0) for k, _ in vk_labels)
            vk_week = sum(week_actions.get(k, 0) for k, _ in vk_labels)

            tg_lines = [f'• {lbl}: <b>{today_actions.get(k, 0)}</b> <i>(7д: {week_actions.get(k, 0)})</i>' for k, lbl in tg_labels]
            vk_lines = [f'• {lbl}: <b>{today_actions.get(k, 0)}</b> <i>(7д: {week_actions.get(k, 0)})</i>' for k, lbl in vk_labels]

            text = (
                f'📈 <b>СТАТИСТИКА • КЛИКИ</b> <i>({stamp})</i>\n\n'
                f'📱 <b>TELEGRAM</b> <i>(сегодня: {tg_today} · 7д: {tg_week})</i>\n'
                + '\n'.join(tg_lines)
                + f'\n\n🔵 <b>ВКОНТАКТЕ</b> <i>(сегодня: {vk_today} · 7д: {vk_week})</i>\n'
                + '\n'.join(vk_lines)
            )

        elif tab == 'groups':
            cursor.execute(
                'SELECT faculty, course, group_name, COUNT(*) FROM users '
                'GROUP BY faculty, course, group_name'
            )
            tg_grp_rows = cursor.fetchall()
            cursor.execute(
                'SELECT faculty, course, group_name, COUNT(*) FROM vk_users '
                'GROUP BY faculty, course, group_name'
            )
            vk_grp_rows = cursor.fetchall()
            cursor.execute(
                'SELECT faculty, group_name FROM group_chats ORDER BY faculty ASC, group_name ASC'
            )
            bound_chats = cursor.fetchall()

            merged_grps = {}
            for fac, crs, grp, cnt in tg_grp_rows:
                key = (fac or infer_faculty_by_group(grp), int(crs or 0), grp or '')
                merged_grps.setdefault(key, [0, 0])[0] += cnt
            for fac, crs, grp, cnt in vk_grp_rows:
                key = (fac or infer_faculty_by_group(grp), int(crs or 0), grp or '')
                merged_grps.setdefault(key, [0, 0])[1] += cnt

            text = f'🎓 <b>СТАТИСТИКА • ГРУППЫ И БЕСЕДЫ</b> <i>({stamp})</i>\n\n'
            if bound_chats:
                text += f'💬 <b>Привязанные беседы TG ({len(bound_chats)}):</b>\n'
                for b_fac, b_grp in bound_chats:
                    text += f'• [{esc(get_faculty_short(b_fac))}] {esc(b_grp)}\n'
                text += '\n'

            text += '👥 <b>Студенты по группам (TG / VK):</b>\n'
            if merged_grps:
                sorted_grps = sorted(merged_grps.items(), key=lambda kv: (kv[0][0], kv[0][1], -(kv[1][0] + kv[1][1])))
                for (fac, crs, grp), (tg_c, vk_c) in sorted_grps:
                    fac_lbl = get_faculty_short(fac)
                    parts_c = []
                    if tg_c:
                        parts_c.append(f'TG: {tg_c}')
                    if vk_c:
                        parts_c.append(f'VK: {vk_c}')
                    text += f'• [{esc(fac_lbl)}] {esc(grp or "не выбрана")}: <b>{tg_c + vk_c}</b> <i>({", ".join(parts_c)})</i>\n'
            else:
                text += '<i>Пока нет зарегистрированных пользователей.</i>'

        elif tab == 'starostas':
            cursor.execute(
                'SELECT user_id, username, group_name, managed_group, faculty FROM users '
                'WHERE is_starosta = 1 ORDER BY faculty ASC, course ASC, group_name ASC'
            )
            starostas = cursor.fetchall()
            cursor.execute(
                'SELECT vk_user_id, full_name, group_name, managed_group, faculty FROM vk_users '
                'WHERE is_starosta = 1 ORDER BY faculty ASC, course ASC, group_name ASC'
            )
            vk_starostas = cursor.fetchall()
            total_st = len(starostas) + len(vk_starostas)
            text = f'🌟 <b>СТАТИСТИКА • СТАРОСТЫ ({total_st})</b> <i>({stamp})</i>\n\n'
            if starostas:
                text += '📱 <b>Telegram:</b>\n'
                for uid, uname, grp, m_grp, fac in starostas:
                    label = f'@{uname}' if uname else f'ID <code>{uid}</code>'
                    target_g = m_grp or grp or 'не выбрана'
                    fac_lbl = get_faculty_short(fac or infer_faculty_by_group(target_g))
                    text += f'• {label} — [{esc(fac_lbl)}] <i>{esc(target_g)}</i>\n'
            if vk_starostas:
                text += ('\n' if starostas else '') + '🔵 <b>ВКонтакте:</b>\n'
                for vk_uid, fname, grp, m_grp, fac in vk_starostas:
                    label = f'{esc(fname or f"id{vk_uid}")} (<code>{vk_uid}</code>)'
                    target_g = m_grp or grp or 'не выбрана'
                    fac_lbl = get_faculty_short(fac or infer_faculty_by_group(target_g))
                    text += f'• {label} — [{esc(fac_lbl)}] <i>{esc(target_g)}</i>\n'
            if not starostas and not vk_starostas:
                text += '<i>Нет назначенных старост.</i>'

        elif tab == 'system':
            import threading as _th
            uptime_sec = int(max(0, time.time() - getattr(core, 'BOT_START_TS', time.time())))
            up_d, rem = divmod(uptime_sec, 86400)
            up_h, rem = divmod(rem, 3600)
            up_m, up_s = divmod(rem, 60)
            up_str = f'{up_d}д {up_h}ч {up_m}м' if up_d else (f'{up_h}ч {up_m}м {up_s}с' if up_h else f'{up_m}м {up_s}с')

            db_bytes = os.path.getsize(core.DB_PATH) if os.path.exists(core.DB_PATH) else 0
            wal_path = core.DB_PATH + '-wal'
            wal_bytes = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
            db_mb = db_bytes / (1024 * 1024)
            wal_mb = wal_bytes / (1024 * 1024)

            thread_names = {t.name for t in _th.enumerate() if t.is_alive()}
            tg_mail_ok = '🟢 Работает' if 'MailingWorker' in thread_names else '🔴 Остановлен'
            vk_bot_ok = '🟢 Активен' if 'VKBotThread' in thread_names else '⚪ Выключен'
            vk_send_ok = '🟢 Работает' if 'VKSenderWorker' in thread_names else '⚪ Выключен'
            tg_q = MAILING_QUEUE.qsize()
            vk_q = core.VK_QUEUE_SIZE_HOOK() if callable(getattr(core, 'VK_QUEUE_SIZE_HOOK', None)) else 0

            ram_str = ''
            try:
                with open('/proc/self/status', 'r', encoding='utf-8') as f_st:
                    for line in f_st:
                        if line.startswith('VmRSS:'):
                            kb = int(line.split()[1])
                            ram_str = f'\n🧠 <b>RAM процесса:</b> {kb / 1024:.1f} МБ'
                            break
            except Exception:
                pass

            text = (
                f'🖥 <b>СТАТУС СИСТЕМЫ</b> <i>({stamp})</i>\n\n'
                f'<blockquote>⏱ <b>Аптайм:</b> {up_str}'
                f'{ram_str}\n'
                f'💾 <b>БД schedule.db:</b> {db_mb:.2f} МБ <i>(WAL: {wal_mb:.2f} МБ)</i>\n'
                f'🧵 <b>Активных потоков:</b> {len(thread_names)}</blockquote>\n\n'
                f'📱 <b>Telegram рассылка:</b> {tg_mail_ok} (очередь: <b>{tg_q}</b>)\n'
                f'🔵 <b>VK LongPoll:</b> {vk_bot_ok}\n'
                f'🔵 <b>VK рассылка:</b> {vk_send_ok} (очередь: <b>{vk_q}</b>)'
            )

        else:  # changes
            cursor.execute(
                'SELECT r.faculty, r.group_name, r.target_date, r.time_slot, r.subject_info, '
                'r.added_by, COALESCE(r.created_at, ""), COALESCE(u.username, ""), COALESCE(v.full_name, "") '
                'FROM replacements r '
                'LEFT JOIN users u ON u.user_id = r.added_by '
                'LEFT JOIN vk_users v ON v.vk_user_id = -r.added_by AND r.added_by < 0 '
                'ORDER BY r.id DESC LIMIT 15'
            )
            repl_rows = cursor.fetchall()
            cursor.execute(
                'SELECT c.faculty, c.group_name, c.target_date, c.time_slot, '
                'c.cancelled_by, COALESCE(c.created_at, ""), COALESCE(u.username, ""), COALESCE(v.full_name, "") '
                'FROM cancelled_lessons c '
                'LEFT JOIN users u ON u.user_id = c.cancelled_by '
                'LEFT JOIN vk_users v ON v.vk_user_id = -c.cancelled_by AND c.cancelled_by < 0 '
                'ORDER BY c.id DESC LIMIT 15'
            )
            canc_rows = cursor.fetchall()

            text = f'🕵️ <b>ЖУРНАЛ ЗАМЕН И ОТМЕН</b> <i>({stamp})</i>\n\n'
            if not repl_rows and not canc_rows:
                text += '✨ <i>В базе пока нет ни одной замены или отмены.</i>'
            else:
                if repl_rows:
                    text += '🔄 <b>ЗАМЕНЫ:</b>\n'
                    for fac, grp, t_date, t_slot, subj, uid, created_at, uname, vname in repl_rows:
                        fac_lbl = get_faculty_short(fac or infer_faculty_by_group(grp))
                        p_num = get_pair_num(t_slot)
                        p_lbl = f'{p_num}п' if p_num else t_slot
                        if uid is not None and int(uid) < 0:
                            who = f'{esc(vname or f"id{abs(int(uid))}")} (VK)'
                        else:
                            who = f'@{uname}' if uname else f'ID <code>{uid}</code>'
                        when_s = f' ({esc(created_at)})' if created_at else ''
                        text += f'• [{esc(fac_lbl)}] <b>{esc(short_text(grp, 24))}</b> | {esc(t_date[:5])} {esc(p_lbl)}\n  → {esc(short_text(subj, 45))} | {who}{when_s}\n'
                    text += '\n'
                if canc_rows:
                    text += '🚫 <b>ОТМЕНЫ ПАР:</b>\n'
                    for fac, grp, t_date, t_slot, uid, created_at, uname, vname in canc_rows:
                        fac_lbl = get_faculty_short(fac or infer_faculty_by_group(grp))
                        p_num = get_pair_num(t_slot)
                        p_lbl = f'{p_num}п' if p_num else t_slot
                        if uid is not None and int(uid) < 0:
                            who = f'{esc(vname or f"id{abs(int(uid))}")} (VK)'
                        else:
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
        safe_edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
        return
    send_mascot_art(chat_id, 'marten_admin', text, reply_markup=markup)


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

        if target == 'all':
            cursor.execute('SELECT vk_user_id FROM vk_users WHERE COALESCE(is_blocked, 0) = 0')
        elif target == 'group':
            cursor.execute(
                'SELECT vk_user_id FROM vk_users WHERE (faculty = ? OR faculty = "") '
                'AND group_name = ? AND COALESCE(is_blocked, 0) = 0',
                ((payload or {}).get('faculty', ''), (payload or {}).get('group_name', '')),
            )
        elif str(target).startswith('fac_'):
            cursor.execute(
                'SELECT vk_user_id FROM vk_users WHERE faculty = ? AND COALESCE(is_blocked, 0) = 0',
                (str(target).replace('fac_', ''),),
            )
        else:
            cursor.execute(
                'SELECT vk_user_id FROM vk_users WHERE course = ? AND COALESCE(is_blocked, 0) = 0',
                (int(target),),
            )
        vk_recipients = [r[0] for r in cursor.fetchall()]
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

    if vk_recipients:
        vk_bc_text = f'📢 ВАЖНОЕ ОБЪЯВЛЕНИЕ\n\n{raw_text}' if raw_text else '📢 ВАЖНОЕ ОБЪЯВЛЕНИЕ'
        for vk_uid in vk_recipients:
            core.enqueue_vk_message(vk_uid, vk_bc_text)

    chats_note = f' (+ бесед: {len(bound_chats)})' if bound_chats else ''
    vk_note = f' · VK: {len(vk_recipients)}' if vk_recipients else ''
    bot.send_message(
        message.chat.id,
        f'🚀 <b>Рассылка поставлена в фоновую очередь!</b>\n\n'
        f'🎯 <b>Аудитория:</b> {esc(target_label)}\n'
        f'👥 <b>Получателей в очереди:</b> TG: {len(recipients)}{chats_note}{vk_note}\n'
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
    core.notify_vk_group(
        fac_code,
        group_name,
        f'⚡ НОВАЯ ЗАМЕНА В РАСПИСАНИИ!\n🎓 Группа: {group_name} • Дата: {date_str}\n🔄 {time_slot}\n👉 {subject_info}',
    )


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
    for g_chat_id in get_bound_group_chats(fac_code, group_name):
        enqueue_message(g_chat_id, notify_text, parse_mode='HTML')
    core.notify_vk_group(
        fac_code,
        group_name,
        f'🚫 ОТМЕНА ПАРЫ В РАСПИСАНИИ!\n🎓 Группа: {group_name} • Дата: {date_str}\n❌ {time_slot} — {removed_desc} (ОТМЕНЕНА)',
    )


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

    if data.startswith('docx_sessfac_'):
        if not is_admin_user(call.from_user):
            return
        rest = data.replace('docx_sessfac_', '')
        fac_code, token = rest.split('_', 1)
        payload = get_pending_docx_import(token)
        if not payload or payload.get('kind') != 'session':
            bot.send_message(chat_id, '⚠️ Сессия импорта устарела. Отправь файл заново.')
            return
        payload['faculty_code'] = fac_code
        save_pending_docx_import(token, payload)
        markup = InlineKeyboardMarkup(row_width=2)
        fac_btns = [
            InlineKeyboardButton(
                f'{"✅ " if f == fac_code else ""}{get_faculty_short(f)}',
                callback_data=f'docx_sessfac_{f}_{token}',
            )
            for f in FACULTY_ORDER
        ]
        markup.add(*fac_btns)
        markup.add(
            InlineKeyboardButton(
                f'🚀 Загрузить сессию ({get_faculty_short(fac_code)})',
                callback_data=f'docx_sessapply_{token}',
            )
        )
        markup.add(InlineKeyboardButton('❌ Отмена', callback_data=f'docx_cancel_{token}'))
        try:
            bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=markup)
        except Exception:
            pass
        return

    if data.startswith('docx_sessapply_'):
        if not is_admin_user(call.from_user):
            return
        token = data.replace('docx_sessapply_', '')
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        apply_parsed_session_docx(chat_id, user_id, token)
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

    if data == 'spec_toggle_session_mode':
        if not is_user_starosta(call.from_user):
            return
        _, target_grp, target_fac = get_managed_context(call.from_user)
        if not target_grp:
            return
        new_val = not is_session_mode_enabled(target_fac, target_grp)
        toggle_group_session_mode_and_notify(target_fac, target_grp, new_val)
        open_special_group_menu(chat_id, call.from_user, msg_id)
        return

    if data == 'spec_stats':
        send_stats_panel(chat_id, call.from_user, tab='overview', message_id=msg_id)
        return

    if data.startswith('spec_stats_tab_'):
        tab_name = data.replace('spec_stats_tab_', '')
        send_stats_panel(chat_id, call.from_user, tab=tab_name, message_id=msg_id)
        return

    if data == 'spec_stats_chart':
        if not is_admin_user(call.from_user):
            return
        chart_buf = _build_clicks_chart_png(days=7)
        if chart_buf:
            try:
                bot.send_photo(chat_id, chart_buf, caption='📈 <b>График кликов за 7 дней (Telegram vs ВКонтакте)</b>', parse_mode='HTML')
            except Exception as e_ch:
                bot.send_message(chat_id, f'⚠️ Не удалось отправить график: <code>{esc(e_ch)}</code>', parse_mode='HTML')
        else:
            bot.send_message(chat_id, 'ℹ️ Для построения PNG-графика установи библиотеку <code>matplotlib</code> в requirements.txt.', parse_mode='HTML')
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

    if data == 'spec_upload_session':
        if not is_admin_user(call.from_user):
            return
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton('🔙 Назад', callback_data='spec_sec_system'))
        safe_edit_message_text(
            '🎓 <b>ЗАГРУЗКА РАСПИСАНИЯ СЕССИИ (.DOCX / .DOC)</b>\n\n'
            'Просто отправь файл сессии от деканата в этот чат как обычный документ.\n\n'
            '• Бот сам поймёт по заголовку и столбцу «Дата проведения», что это сессия.\n'
            '• Распознает экзамены, консультации, зачёты, аудитории, преподавателей и время из подвала документа (14:00 будни / 10:00 суббота / 08:00 экзамен).\n'
            '• Перед сохранением покажет список групп и даст выбрать факультет.\n'
            '• Обновятся только группы из загруженного файла — остальные группы и факультеты не затронутся.\n\n'
            '<i>Важно: не находись в этот момент на шаге добавления ДЗ.</i>',
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
        vk_notify_text = None

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
                    vk_notify_text = (
                        f'↩️ ЗАМЕНА УДАЛЕНА ИЗ РАСПИСАНИЯ\n'
                        f'🎓 Группа: {group_name} • Дата: {r_date}\n'
                        f'{r_time} — {r_subj}'
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
                    vk_notify_text = (
                        f'↩️ ОТМЕНА ПАРЫ СНЯТА (ПАРА ВОЗВРАЩЕНА)\n'
                        f'🎓 Группа: {group_name} • Дата: {c_date}\n'
                        f'{c_time} — снова в расписании.'
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
        if vk_notify_text:
            core.notify_vk_group(eff_rev_fac, group_name, vk_notify_text)

        text, markup = build_active_changes_view(group_name)
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        except Exception:
            pass
        return


# ==============================================================================
# МОДУЛЬ «🎓 СЕССИЯ» (ЭКЗАМЕНЫ И ЗАЧЁТЫ)
# ==============================================================================
def _format_days_left_label(exam_date_obj, today_date):
    delta = (exam_date_obj - today_date).days
    if delta < 0:
        return 'прошёл'
    if delta == 0:
        return '🔴 СЕГОДНЯ'
    if delta == 1:
        return '🟠 ЗАВТРА'
    return f'через {delta} дн.'


def build_session_view(group_name, faculty_code=None, can_edit=False):
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    today = now_local().date()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, exam_date, exam_time, subject_name, exam_type, teacher, room '
            'FROM session_exams WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (eff_fac, group_name),
        )
        rows = cursor.fetchall()

    upcoming = []
    total_valid = 0
    passed_cnt = 0
    for r in rows:
        try:
            d_obj = datetime.strptime(r[1], '%d.%m.%Y').date()
        except ValueError:
            continue
        total_valid += 1
        if d_obj >= today:
            upcoming.append((d_obj, r))
        else:
            passed_cnt += 1
    upcoming.sort(key=lambda x: (x[0], x[1][2] or '', x[1][0]))

    prog_line = ''
    if total_valid > 0:
        bar = build_progress_bar(passed_cnt, total_valid, width=10)
        prog_line = f'\n🐾 <b>Прогресс:</b> <code>{bar}</code> <i>(позади {passed_cnt} из {total_valid})</i>'

    text = (
        f'🎓 <b>РАСПИСАНИЕ СЕССИИ</b>\n'
        f'<blockquote>📚 <b>Группа:</b> {esc(group_name)}{prog_line}</blockquote>\n\n'
    )
    markup = InlineKeyboardMarkup(row_width=1)

    if not upcoming:
        if passed_cnt > 0:
            text += '🐾 <i>Все экзамены и зачёты позади — Куница поздравляет с закрытой сессией!</i>'
        else:
            text += '🐾 <i>Экзамены и зачёты пока не добавлены — Куница на страже, скоро появятся.</i>'
    else:
        for d_obj, (ex_id, ex_date, ex_time, subj, ex_type, teacher, room) in upcoming:
            left_lbl = _format_days_left_label(d_obj, today)
            time_part = f' • <code>{esc(ex_time)}</code>' if ex_time else ''
            meta_parts = []
            if room:
                meta_parts.append(f'📍 {esc(room)}')
            if teacher:
                meta_parts.append(f'👤 {esc(teacher)}')
            meta_line = f'\n   {" · ".join(meta_parts)}' if meta_parts else ''
            icon = '📝' if 'зач' in str(ex_type or '').lower() else '🎯'
            text += (
                f'{icon} <b>{esc(ex_date)}</b>{time_part} <i>({left_lbl})</i>\n'
                f'   📘 <b>{esc(subj)}</b> <i>({esc(ex_type or "Экзамен")})</i>'
                f'{meta_line}\n\n'
            )
            if can_edit:
                markup.add(
                    InlineKeyboardButton(
                        f'🗑 Удалить: {ex_date[:5]} {short_text(subj, 20)}',
                        callback_data=f'sess_del_{ex_id}',
                    )
                )

    if can_edit:
        markup.add(InlineKeyboardButton('➕ Добавить экзамен / зачёт', callback_data='sess_add'))
    markup.add(InlineKeyboardButton('🔄 Обновить', callback_data='sess_refresh'))
    return text.strip(), markup


def open_session_menu(message):
    user = get_user_data(message.from_user.id)
    if not user or not is_group_valid(user[7], user[0], user[1]):
        start_onboarding(message)
        return
    can_edit = is_admin_user(message.from_user) or (
        is_user_starosta(message.from_user)
        and (get_managed_context(message.from_user)[1] == user[1])
    )
    text, markup = build_session_view(user[1], faculty_code=user[7], can_edit=can_edit)
    art_key = 'marten_grad' if 'поздравляет с закрытой сессией' in text else 'marten_session'
    send_mascot_art(message.chat.id, art_key, text, reply_markup=markup)


def _parse_flexible_session_date(raw_text):
    """Принимает ДД.ММ.ГГГГ или ДД.ММ (год подставляется автоматически)."""
    s = str(raw_text or '').strip().replace('/', '.').replace('-', '.')
    now_d = now_local().date()
    for fmt in ('%d.%m.%Y', '%d.%m.%y'):
        try:
            return datetime.strptime(s, fmt).strftime('%d.%m.%Y')
        except ValueError:
            pass
    try:
        dt = datetime.strptime(s, '%d.%m').replace(year=now_d.year).date()
        if dt < now_d - timedelta(days=30):
            dt = dt.replace(year=now_d.year + 1)
        return dt.strftime('%d.%m.%Y')
    except ValueError:
        return None


def _save_session_exam_and_notify(chat_id, author_id, fac_code, group_name, ex_date, ex_time, subj, ex_type, room='', teacher=''):
    eff_fac = fac_code or infer_faculty_by_group(group_name)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO session_exams (faculty, group_name, exam_date, exam_time, subject_name, exam_type, teacher, room, added_by, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (eff_fac, group_name, ex_date, ex_time, subj, ex_type, teacher, room, author_id, now_local().strftime('%d.%m.%Y %H:%M')),
        )
        cursor.execute(
            'SELECT user_id FROM users WHERE (faculty = ? OR faculty IS NULL OR faculty = "") AND group_name = ? AND notify_updates = 1',
            (eff_fac, group_name),
        )
        tg_users = [r[0] for r in cursor.fetchall()]

    meta_parts = []
    if room:
        meta_parts.append(f'📍 {esc(room)}')
    if teacher:
        meta_parts.append(f'👤 {esc(teacher)}')
    meta_line = f'\n{" · ".join(meta_parts)}' if meta_parts else ''

    bot.send_message(
        chat_id,
        f'✅ <b>Запись сессии добавлена!</b>\n\n'
        f'📅 <b>{ex_date}</b>' + (f' • <code>{esc(ex_time)}</code>' if ex_time else '') + f'\n'
        f'📘 <b>{esc(subj)}</b> <i>({esc(ex_type)})</i>{meta_line}',
        parse_mode='HTML',
    )
    notify_html = (
        f'🎓 <b>ДОБАВЛЕНО В СЕССИЮ: {esc(ex_type.upper())}</b>\n'
        f'<blockquote>📚 <b>Группа:</b> {esc(group_name)}\n'
        f'📅 <b>Дата:</b> {ex_date}' + (f' • {esc(ex_time)}' if ex_time else '') + f'</blockquote>\n\n'
        f'📘 <b>{esc(subj)}</b> <i>({esc(ex_type)})</i>{meta_line}'
    )
    for u_id in tg_users:
        if u_id != author_id:
            enqueue_message(u_id, notify_html, parse_mode='HTML')
    for g_cid in get_bound_group_chats(eff_fac, group_name):
        enqueue_message(g_cid, notify_html, parse_mode='HTML')
    core.notify_vk_group(
        eff_fac,
        group_name,
        f'🎓 СЕССИЯ: ДОБАВЛЕН {ex_type.upper()}\n📚 {group_name} • {ex_date}' + (f' {ex_time}' if ex_time else '') + f'\n📘 {subj}' + (f' ({room})' if room else ''),
    )


def _ask_session_date_step(chat_id, user_id, state_pl, msg_id=None):
    set_user_state(user_id, 'awaiting_session_exam', dict(state_pl, step='date'))
    today = now_local()
    d1 = (today + timedelta(days=1)).strftime('%d.%m.%Y')
    d2 = (today + timedelta(days=2)).strftime('%d.%m.%Y')
    d3 = (today + timedelta(days=3)).strftime('%d.%m.%Y')
    d7 = (today + timedelta(days=7)).strftime('%d.%m.%Y')
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton(f'⏭ Завтра ({d1[:5]})', callback_data=f'sess_date_{d1}'),
        InlineKeyboardButton(f'⏩ Послезавтра ({d2[:5]})', callback_data=f'sess_date_{d2}'),
    )
    markup.row(
        InlineKeyboardButton(f'📅 Через 3 дня ({d3[:5]})', callback_data=f'sess_date_{d3}'),
        InlineKeyboardButton(f'🗓 Через неделю ({d7[:5]})', callback_data=f'sess_date_{d7}'),
    )
    markup.row(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))
    txt = (
        f'🎓 <b>{esc(state_pl.get("exam_type", "Экзамен"))}</b> — <b>{esc(state_pl.get("subject", ""))}</b>\n\n'
        f'<b>Шаг 3 из 5.</b> Выбери дату кнопкой или напиши её в чат (например, <code>25.12</code> или <code>25.12.2026</code>):'
    )
    if msg_id:
        safe_edit_message_text(txt, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, txt, reply_markup=markup, parse_mode='HTML')


def _ask_session_time_step(chat_id, user_id, state_pl, msg_id=None):
    set_user_state(user_id, 'awaiting_session_exam', dict(state_pl, step='time'))
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton('🕗 08:00', callback_data='sess_time_08:00'),
        InlineKeyboardButton('🕘 09:00', callback_data='sess_time_09:00'),
    )
    markup.row(
        InlineKeyboardButton('🕙 10:00', callback_data='sess_time_10:00'),
        InlineKeyboardButton('🕑 14:00', callback_data='sess_time_14:00'),
    )
    markup.row(
        InlineKeyboardButton('⏭ Пропустить', callback_data='sess_time_skip'),
        InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
    )
    txt = (
        f'📅 <b>Дата:</b> {esc(state_pl.get("exam_date", ""))} • <b>{esc(state_pl.get("subject", ""))}</b>\n\n'
        f'<b>Шаг 4 из 5.</b> Выбери время начала или напиши его вручную (например, <code>11:30</code>):'
    )
    if msg_id:
        safe_edit_message_text(txt, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, txt, reply_markup=markup, parse_mode='HTML')


def _ask_session_extra_step(chat_id, user_id, state_pl, msg_id=None):
    set_user_state(user_id, 'awaiting_session_exam', dict(state_pl, step='extra'))
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton('⏭ Пропустить и сохранить', callback_data='sess_extra_skip'),
        InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'),
    )
    t_str = f' • {esc(state_pl.get("exam_time", ""))}' if state_pl.get('exam_time') else ''
    txt = (
        f'📅 <b>{esc(state_pl.get("exam_date", ""))}{t_str}</b> • <b>{esc(state_pl.get("subject", ""))}</b>\n\n'
        f'<b>Шаг 5 из 5.</b> Напиши аудиторию и/или преподавателя (например: <code>ауд. 206, Иванов И.И.</code>) '
        f'или нажми <b>«Пропустить и сохранить»</b>:'
    )
    if msg_id:
        safe_edit_message_text(txt, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, txt, reply_markup=markup, parse_mode='HTML')


def process_session_exam_input(message, payload):
    st = dict(payload or {})
    step = st.get('step', 'subject')
    if not message.text or message.text.strip().lower() in ['отмена', '/cancel', '/start']:
        clear_user_state(message.chat.id)
        bot.send_message(message.chat.id, '❌ Добавление экзамена отменено.')
        return

    raw = message.text.strip()
    if step == 'subject':
        st['subject'] = raw
        _ask_session_date_step(message.chat.id, message.from_user.id, st)
        return

    if step == 'date':
        parsed_d = _parse_flexible_session_date(raw)
        if not parsed_d:
            bot.send_message(
                message.chat.id,
                '⚠️ Не понял дату. Напиши в формате <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code> (например, <code>25.12</code>):',
                parse_mode='HTML',
            )
            return
        st['exam_date'] = parsed_d
        _ask_session_time_step(message.chat.id, message.from_user.id, st)
        return

    if step == 'time':
        if raw.lower() in ('-', '—', 'пропустить', 'нет'):
            st['exam_time'] = ''
        else:
            st['exam_time'] = raw.replace('.', ':')[:10]
        _ask_session_extra_step(message.chat.id, message.from_user.id, st)
        return

    if step == 'extra':
        clear_user_state(message.chat.id)
        room, teacher = '', ''
        if raw.lower() not in ('-', '—', 'пропустить', 'нет'):
            if ',' in raw:
                room, teacher = [x.strip() for x in raw.split(',', 1)]
            else:
                room = raw
        _save_session_exam_and_notify(
            message.chat.id,
            message.from_user.id,
            st.get('faculty', ''),
            st.get('group_name', ''),
            st.get('exam_date', ''),
            st.get('exam_time', ''),
            st.get('subject', ''),
            st.get('exam_type', 'Экзамен'),
            room=room,
            teacher=teacher,
        )
        return


@bot.callback_query_handler(
    func=lambda call: call.data in ('sess_add', 'sess_refresh', 'sess_time_skip', 'sess_extra_skip')
    or call.data.startswith(('sess_del_', 'sess_type_', 'sess_subj_', 'sess_date_', 'sess_time_'))
)
def handle_session_callbacks(call):
    sync_username(call.from_user)
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    user = get_user_data(user_id)
    if not user or not is_group_valid(user[7], user[0], user[1]):
        return
    can_edit = is_admin_user(call.from_user) or (
        is_user_starosta(call.from_user) and (get_managed_context(call.from_user)[1] == user[1])
    )

    if data == 'sess_refresh':
        text, markup = build_session_view(user[1], faculty_code=user[7], can_edit=can_edit)
        safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        return

    if not can_edit:
        return

    if data == 'sess_add':
        m_crs, m_grp, m_fac = get_managed_context(call.from_user)
        set_user_state(user_id, '', {
            'group_name': m_grp or user[1],
            'faculty': m_fac or user[7],
            'course': m_crs or user[0],
        })
        markup = InlineKeyboardMarkup(row_width=2)
        markup.row(
            InlineKeyboardButton('🎯 Экзамен', callback_data='sess_type_Экзамен'),
            InlineKeyboardButton('📝 Зачёт', callback_data='sess_type_Зачёт'),
        )
        markup.row(
            InlineKeyboardButton('📋 Диф. зачёт', callback_data='sess_type_Диф. зачёт'),
            InlineKeyboardButton('💬 Консультация', callback_data='sess_type_Консультация'),
        )
        markup.row(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))
        safe_edit_message_text(
            '➕ <b>ДОБАВЛЕНИЕ В СЕССИЮ</b>\n\n'
            '<b>Шаг 1 из 5.</b> Выбери тип события:',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data.startswith('sess_type_'):
        ex_type = data.replace('sess_type_', '')
        _, st = get_user_state(user_id)
        st = dict(st or {})
        m_grp = st.get('group_name') or user[1]
        m_fac = st.get('faculty') or user[7]
        m_crs = st.get('course') or user[0]
        subjects = get_all_group_subjects(m_crs, m_grp, faculty_code=m_fac)
        st.update({'exam_type': ex_type, 'subjects': subjects, 'step': 'subject', 'group_name': m_grp, 'faculty': m_fac, 'course': m_crs})
        set_user_state(user_id, 'awaiting_session_exam', st)
        markup = InlineKeyboardMarkup(row_width=1)
        for idx, subj in enumerate(subjects[:12]):
            markup.add(InlineKeyboardButton(f'📘 {short_text(subj, 36)}', callback_data=f'sess_subj_{idx}'))
        markup.add(InlineKeyboardButton('❌ Отмена', callback_data='cancel_action'))
        safe_edit_message_text(
            f'🎓 <b>Тип:</b> {esc(ex_type)}\n\n'
            f'<b>Шаг 2 из 5.</b> Выбери предмет из списка ниже <b>или напиши его название вручную</b>:',
            chat_id,
            msg_id,
            reply_markup=markup,
            parse_mode='HTML',
        )
        return

    if data.startswith('sess_subj_'):
        idx = int(data.replace('sess_subj_', ''))
        _, st = get_user_state(user_id)
        subjects = (st or {}).get('subjects') or []
        if not (0 <= idx < len(subjects)):
            return
        st = dict(st)
        st['subject'] = subjects[idx]
        _ask_session_date_step(chat_id, user_id, st, msg_id=msg_id)
        return

    if data.startswith('sess_date_'):
        ex_date = data.replace('sess_date_', '')
        _, st = get_user_state(user_id)
        if not st:
            return
        st = dict(st)
        st['exam_date'] = ex_date
        _ask_session_time_step(chat_id, user_id, st, msg_id=msg_id)
        return

    if data == 'sess_time_skip' or data.startswith('sess_time_'):
        _, st = get_user_state(user_id)
        if not st:
            return
        st = dict(st)
        st['exam_time'] = '' if data == 'sess_time_skip' else data.replace('sess_time_', '')
        _ask_session_extra_step(chat_id, user_id, st, msg_id=msg_id)
        return

    if data == 'sess_extra_skip':
        _, st = get_user_state(user_id)
        clear_user_state(user_id)
        if not st:
            return
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        _save_session_exam_and_notify(
            chat_id,
            user_id,
            st.get('faculty', user[7]),
            st.get('group_name', user[1]),
            st.get('exam_date', ''),
            st.get('exam_time', ''),
            st.get('subject', ''),
            st.get('exam_type', 'Экзамен'),
        )
        return

    if data.startswith('sess_del_'):
        ex_id = int(data.replace('sess_del_', ''))
        with db_connection() as conn:
            conn.execute('DELETE FROM session_exams WHERE id = ? AND group_name = ?', (ex_id, user[1]))
        text, markup = build_session_view(user[1], faculty_code=user[7], can_edit=can_edit)
        safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='HTML')
        return
