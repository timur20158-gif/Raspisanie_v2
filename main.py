from datetime import timedelta
import json
import time
from apscheduler.schedulers.background import BackgroundScheduler
from telebot.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import core
from core import (
    ADMIN_USERNAME,
    FACULTIES_META,
    STATE_TTL_SECONDS,
    TOKEN,
    bot,
    build_course_keyboard,
    build_faculty_keyboard,
    can_manage_hw,
    cleanup_old_db_records,
    cleanup_stale_states,
    clear_user_state,
    db_connection,
    enqueue_hw_media,
    enqueue_message,
    esc,
    get_faculty_full,
    get_faculty_short,
    get_main_keyboard,
    get_pending_docx_import,
    get_role_label,
    get_group_chat_binding,
    get_user_data,
    get_user_mailing_prefs,
    get_user_state,
    infer_faculty_by_group,
    init_db,
    invalidate_user_cache,
    is_admin_user,
    is_group_valid,
    is_user_starosta,
    now_local,
    parse_photo_ids,
    send_db_backup,
    send_long_html_message,
    set_user_state,
    split_long_html_message,
    start_onboarding,
    sync_admin_personal_commands,
    sync_username,
)

# Импортируем модули с хендлерами ДО регистрации fsm_message_router,
# чтобы команды (/start и др.) и кнопки меню имели приоритет над FSM, как в оригинале.
import admin_replacements  # noqa: F401
import homework  # noqa: F401
import schedule_search  # noqa: F401

from admin_replacements import (
    check_academic_year_rollover,
    open_special_menu,
    process_broadcast_content,
    process_docx_custom_start_date,
    process_full_reset_confirm,
    process_manual_date_for_action,
    process_replacement_subject,
    show_active_changes,
    start_add_replacement,
    start_delete_pair,
)
from homework import (
    open_homework_menu,
    process_hw_content,
    process_hw_edit_text,
    process_hw_manual_subject,
    process_hw_queue_count_input,
    send_homeworks_for_period,
    send_hw_items,
    start_add_homework,
)
from schedule_search import (
    get_homeworks_for_date,
    get_pagination_keyboard,
    get_schedule_text,
    open_search_menu,
    process_teacher_search,
    send_week_schedule_message,
)


def open_settings_menu(chat_id, user_obj, message_id=None):
    user = get_user_data(user_obj.id)
    if not user or not is_group_valid(user[7], user[0], user[1]):
        bot.send_message(
            chat_id,
            '👇 Сначала выбери свой <b>факультет</b>:',
            reply_markup=build_faculty_keyboard(),
            parse_mode='HTML',
        )
        return

    course, group_name, subscribed, is_st, is_hw, notify_updates, managed_group, fac_code = user
    fac_short = get_faculty_short(fac_code)
    role_text = get_role_label(user_obj)
    sub_m, sub_e = get_user_mailing_prefs(user_obj.id)
    if sub_m and sub_e:
        sub_status = 'утро (07:00) и вечер (18:00)'
    elif sub_m:
        sub_status = 'только утро (07:00)'
    elif sub_e:
        sub_status = 'только вечер (18:00)'
    else:
        sub_status = 'выключена'
    sub_m_btn = '☀️ Утренняя рассылка (07:00): ВКЛ' if sub_m else '☀️ Утренняя рассылка (07:00): ВЫКЛ'
    sub_e_btn = '🌙 Вечерняя рассылка (18:00): ВКЛ' if sub_e else '🌙 Вечерняя рассылка (18:00): ВЫКЛ'
    notify_status = 'включены' if notify_updates else 'выключены'
    notify_btn_text = (
        '🔔 Уведомления (пары и ДЗ): ВКЛ'
        if notify_updates
        else '🔕 Уведомления (пары и ДЗ): ВЫКЛ'
    )

    managed_line = ''
    if is_st == 1 and not is_admin_user(user_obj) and managed_group:
        managed_line = f'\n🛡 <b>Управляемая группа:</b> {esc(managed_group)}'

    text = (
        '⚙️ <b>НАСТРОЙКИ И ПРОФИЛЬ</b>\n\n'
        f'<blockquote>🏛 <b>Факультет:</b> {esc(fac_short)} ({esc(get_faculty_full(fac_code))})\n'
        f'🎓 <b>Курс:</b> {course}\n'
        f'📚 <b>Группа просмотра:</b> {esc(group_name)}'
        f'{managed_line}\n'
        f'👤 <b>Статус:</b> {role_text}\n'
        f'🗓 <b>Авторассылка (с ДЗ):</b> {sub_status}\n'
        f'⚡ <b>Уведомления (пары и ДЗ):</b> {notify_status}</blockquote>\n\n'
        '<i>Выбери нужный пункт ниже:</i>'
    )

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(sub_m_btn, callback_data='settings_toggle_sub_morning'),
        InlineKeyboardButton(sub_e_btn, callback_data='settings_toggle_sub_evening'),
        InlineKeyboardButton(notify_btn_text, callback_data='settings_toggle_notify'),
        InlineKeyboardButton('🏛 Сменить факультет / группу', callback_data='settings_change_group'),
        InlineKeyboardButton('🔄 Обновить главное меню', callback_data='settings_restart'),
    )

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


@bot.message_handler(commands=['start'], chat_types=['private'])
def cmd_start(message):
    sync_username(message.from_user)
    clear_user_state(message.chat.id)
    start_parts = (message.text or '').split(maxsplit=1)
    deep_arg = start_parts[1].strip() if len(start_parts) > 1 else ''
    admin_flag = is_admin_user(message.from_user)
    user = get_user_data(message.from_user.id)
    if user is None:
        if admin_flag:
            uname = (message.from_user.username or '').lower()
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR IGNORE INTO users (user_id, faculty, course, group_name, subscribed, username, is_starosta, is_hw_manager, notify_updates, managed_group) '
                    'VALUES (?, "ЕМФ", 1, "", 0, ?, 1, 1, 0, "")',
                    (message.from_user.id, uname),
                )
            invalidate_user_cache(message.from_user.id)
            user = get_user_data(message.from_user.id)
        else:
            start_onboarding(message)
            return

    course, group_name, subscribed, _, _, notify_updates, managed_group, fac_code = user
    has_valid_group = is_group_valid(fac_code, course, group_name)
    if not has_valid_group and not admin_flag:
        start_onboarding(message)
        return
    starosta_flag = is_user_starosta(message.from_user)
    hw_flag = can_manage_hw(message.from_user)
    sub_m, sub_e = get_user_mailing_prefs(message.from_user.id)
    if sub_m and sub_e:
        sub_status = 'утро (07:00) и вечер (18:00)'
    elif sub_m:
        sub_status = 'только утро (07:00)'
    elif sub_e:
        sub_status = 'только вечер (18:00)'
    else:
        sub_status = 'выключена'
    notify_status = 'включены' if notify_updates else 'выключены'
    if admin_flag:
        role_text = '👑 Администратор'
    elif starosta_flag:
        role_text = '🌟 Староста'
    else:
        role_text = 'Студент'

    group_info_block = (
        f'🏛 <b>Факультет:</b> {esc(get_faculty_short(fac_code))} ({esc(get_faculty_full(fac_code))})\n'
        f'🎓 <b>Курс:</b> {course}\n'
        f'📚 <b>Направление:</b> {esc(group_name)}\n'
        if has_valid_group
        else '📚 <b>Направление:</b> <i>не выбрано (загрузи расписание в Спецвозможностях)</i>\n'
    )

    if deep_arg == 'hw_all' and has_valid_group:
        send_homeworks_for_period(message.chat.id, message.from_user, group_name, days_limit=None, faculty_code=fac_code)
        return

    if deep_arg.startswith('hw_item_') and has_valid_group:
        try:
            target_hw_id = int(deep_arg.replace('hw_item_', ''))
            eff_fac_s = fac_code or infer_faculty_by_group(group_name)
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT id, target_date, subject_name, hw_text, photo_file_id, doc_file_id, doc_file_name, queue_id, added_by '
                    'FROM homeworks WHERE id = ? AND (faculty = ? OR faculty = "") AND group_name = ?',
                    (target_hw_id, eff_fac_s, group_name),
                )
                hw_single = cursor.fetchone()
            if hw_single:
                send_hw_items(
                    message.chat.id,
                    message.from_user,
                    [hw_single],
                    f'Вложения к ДЗ ({hw_single[1]})',
                    target_group_name=group_name,
                )
                return
        except Exception:
            pass

    bot.send_message(
        message.chat.id,
        f'👋 <b>С возвращением!</b> Бот готов к работе.\n\n'
        f'<blockquote>{group_info_block}'
        f'👤 <b>Статус:</b> {role_text}\n'
        f'🗓 <b>Авторассылка:</b> {sub_status}\n'
        f'🔔 <b>Уведомления (пары и ДЗ):</b> {notify_status}</blockquote>\n\n'
        f'👇 <i>Выбери нужное действие в меню ниже:</i>',
        reply_markup=get_main_keyboard(bool(subscribed), starosta_flag, hw_flag),
        parse_mode='HTML',
    )


@bot.message_handler(
    chat_types=['private'],
    func=lambda msg: msg.text in [
        '📅 Сегодня',
        '⏭ Завтра',
        '🗓 Неделя',
        '📚 Домашка',
        '🔍 Поиск',
        '⚙️ Настройки',
        '🛠 Спецвозможности',
        'Расписание на сегодня',
        '📅 Расписание на сегодня',
        'Расписание на неделю',
        '🗓 Расписание на неделю',
        '📚 Домашние задания',
        'Домашние задания',
        'Сменить группу',
        '⚙️ Сменить группу',
        '⚙️ Изменить группу',
        'Включить рассылку (07:00)',
        '🔔 Включить рассылку (07:00 и 18:00)',
        '🔔 Включить рассылку в 7:00',
        'Отключить утреннюю рассылку',
        '🔕 Отключить рассылку',
        'Перезапустить бота (/start)',
        '🔄 Перезапустить бота (/start)',
        '➕ Добавить замену',
        'Добавить замену',
        '🗑 Удалить пару',
        'Удалить пару',
        '📋 Активные замены/отмены',
        '📚 Добавить ДЗ',
    ]
)
def handle_menu(message):
    sync_username(message.from_user)
    clear_user_state(message.chat.id, keep_payload=True)

    user_id = message.from_user.id
    text_lower = message.text.lower()

    if message.text == '🔍 Поиск':
        open_search_menu(message.chat.id)
        return

    if message.text == '⚙️ Настройки':
        open_settings_menu(message.chat.id, message.from_user)
        return

    if message.text == '🛠 Спецвозможности':
        open_special_menu(message.chat.id, message.from_user)
        return

    if 'Перезапустить бота' in message.text:
        cmd_start(message)
        return

    if 'Сменить группу' in message.text or 'Изменить группу' in message.text:
        start_onboarding(message)
        return

    if 'Добавить замену' in message.text:
        start_add_replacement(message)
        return

    if 'Удалить пару' in message.text:
        start_delete_pair(message)
        return

    if 'Активные замены/отмены' in message.text:
        show_active_changes(message)
        return

    if 'Добавить ДЗ' in message.text:
        start_add_homework(message)
        return

    if 'Домашние задания' in message.text or 'Домашка' in message.text:
        open_homework_menu(message)
        return

    user = get_user_data(user_id)
    if not user or not is_group_valid(user[7], user[0], user[1]):
        start_onboarding(message)
        return

    course, group_name, fac_code = user[0], user[1], user[7]

    if 'рассылку' in message.text:
        open_settings_menu(message.chat.id, message.from_user)
        return

    if 'сегодня' in text_lower:
        text = get_schedule_text(now_local(), course, group_name, faculty_code=fac_code)
        send_long_html_message(
            user_id,
            text,
            reply_markup=get_pagination_keyboard(0, group_name),
            parse_mode='HTML',
        )
        return

    if 'завтра' in text_lower:
        tomorrow_dt = now_local() + timedelta(days=1)
        text = get_schedule_text(tomorrow_dt, course, group_name, faculty_code=fac_code)
        send_long_html_message(
            user_id,
            text,
            reply_markup=get_pagination_keyboard(1, group_name),
            parse_mode='HTML',
        )
        return

    if 'недел' in text_lower:
        send_week_schedule_message(
            user_id, now_local(), course, group_name, week_offset=0, faculty_code=fac_code
        )
        return


@bot.callback_query_handler(
    func=lambda call: call.data in ('cancel_action', 'back_to_faculties')
    or call.data.startswith(('sel_fac_', 'sel_course_', 'sel_group_', 'settings_'))
)
def handle_onboarding_and_settings_callbacks(call):
    sync_username(call.from_user)
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    if data not in ('settings_toggle_notify', 'settings_toggle_sub', 'settings_toggle_sub_morning', 'settings_toggle_sub_evening'):
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

    if data == 'cancel_action':
        clear_user_state(user_id)
        try:
            bot.edit_message_text('❌ Действие отменено.', chat_id, msg_id)
        except Exception:
            pass
        return

    # --- ВЫБОР ФАКУЛЬТЕТА -> КУРСА -> НАПРАВЛЕНИЯ ---
    if data == 'back_to_faculties':
        clear_user_state(user_id)
        try:
            bot.edit_message_text(
                '🏛 <b>Выбор факультета</b>\n\n👇 Выбери свой <b>факультет</b>:',
                chat_id,
                msg_id,
                reply_markup=build_faculty_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            pass
        return

    if data.startswith('sel_fac_'):
        fac_code = data.replace('sel_fac_', '')
        meta = FACULTIES_META.get(fac_code)
        if not meta:
            return
        courses = core.FACULTY_STRUCTURE.get(fac_code, {})
        has_any_groups = any(bool(grps) for grps in courses.values())

        if not has_any_groups:
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(InlineKeyboardButton('🔙 Выбрать другой факультет', callback_data='back_to_faculties'))
            try:
                bot.edit_message_text(
                    f'{meta["emoji"]} <b>{esc(meta["full"])} ({esc(meta["short"])})</b>\n\n'
                    f'⏳ <i>Расписание для этого факультета ещё не загружено и появится в ближайшее время!</i>\n'
                    f'Пока ты можешь выбрать другой факультет.',
                    chat_id,
                    msg_id,
                    reply_markup=markup,
                    parse_mode='HTML',
                )
            except Exception:
                pass
            return

        set_user_state(user_id, '', {'faculty': fac_code})
        try:
            bot.edit_message_text(
                f'{meta["emoji"]} Выбран факультет: <b>{esc(meta["full"])} ({esc(meta["short"])})</b>.\n\n'
                f'👇 Теперь выбери свой <b>курс</b>:',
                chat_id,
                msg_id,
                reply_markup=build_course_keyboard(fac_code),
                parse_mode='HTML',
            )
        except Exception:
            pass
        return

    if data.startswith('sel_course_'):
        parts = data.replace('sel_course_', '').split('_')
        fac_code = parts[0]
        course = int(parts[1])
        groups = core.FACULTY_STRUCTURE.get(fac_code, {}).get(course, [])
        if not groups:
            return

        markup = InlineKeyboardMarkup(row_width=1)
        for idx, g_name in enumerate(groups):
            markup.add(
                InlineKeyboardButton(f'📘 {g_name}', callback_data=f'sel_group_{fac_code}_{course}_{idx}')
            )
        markup.add(InlineKeyboardButton('🔙 Сменить курс', callback_data=f'sel_fac_{fac_code}'))

        set_user_state(user_id, '', {
            'faculty': fac_code,
            'course': course,
            'current_groups': groups,
        })
        try:
            bot.edit_message_text(
                f'🏛 <b>{esc(get_faculty_short(fac_code))}</b>  •  ✅ Выбран <b>{course} курс</b>.\n\n'
                f'👇 Теперь укажи своё <b>направление подготовки</b>:',
                chat_id,
                msg_id,
                reply_markup=markup,
                parse_mode='HTML',
            )
        except Exception:
            pass
        return

    if data.startswith('sel_group_'):
        parts = data.replace('sel_group_', '').split('_')
        fac_code = parts[0]
        course = int(parts[1])
        g_idx = int(parts[2])

        _, state = get_user_state(user_id)
        groups_list = state.get('current_groups') or core.FACULTY_STRUCTURE.get(fac_code, {}).get(course, [])
        if not groups_list or g_idx < 0 or g_idx >= len(groups_list):
            bot.send_message(
                chat_id,
                '⚠️ Список групп обновился. Пожалуйста, выбери факультет и группу заново:',
                reply_markup=build_faculty_keyboard(),
                parse_mode='HTML',
            )
            return
        group_name = groups_list[g_idx]
        clear_user_state(user_id)
        uname = (call.from_user.username or '').lower()
        is_admin = 1 if uname == ADMIN_USERNAME.lower() else 0

        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT subscribed, is_starosta, is_hw_manager, notify_updates, managed_group, '
                'COALESCE(sub_morning, subscribed, 0), COALESCE(sub_evening, subscribed, 0) '
                'FROM users WHERE user_id = ?',
                (user_id,),
            )
            existing = cursor.fetchone()
            sub_val = existing[0] if existing else 0
            sub_m_val = existing[5] if existing else 0
            sub_e_val = existing[6] if existing else 0
            starosta_val = 1 if (is_admin or (existing and existing[1] == 1)) else 0
            hw_val = 1 if (is_admin or (existing and existing[2] == 1)) else 0
            notify_val = existing[3] if existing else 0
            # Восстанавливаем managed_group старосте после ежегодного сброса курсов, но не даём перезаписать уже закреплённую группу
            if existing and existing[4]:
                managed_val = existing[4]
            elif starosta_val == 1 and not is_admin:
                managed_val = group_name
            else:
                managed_val = ''

            cursor.execute(
                'INSERT OR REPLACE INTO users '
                '(user_id, faculty, course, group_name, subscribed, username, is_starosta, is_hw_manager, notify_updates, managed_group, sub_morning, sub_evening) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, fac_code, course, group_name, sub_val, uname, starosta_val, hw_val, notify_val, managed_val, sub_m_val, sub_e_val),
            )
        invalidate_user_cache(user_id)

        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        bot.send_message(
            user_id,
            f'🎉 <b>Профиль успешно сохранён!</b>\n\n'
            f'<blockquote>🏛 <b>Факультет:</b> {esc(get_faculty_short(fac_code))} ({esc(get_faculty_full(fac_code))})\n'
            f'🎓 <b>Курс:</b> {course}\n'
            f'📚 <b>Направление:</b> {esc(group_name)}</blockquote>\n\n'
            'Расписание автоматически подстраивается под текущую учебную неделю.\n'
            '🔕 <i>Уведомления выключены по умолчанию. Включить оповещения об изменении пар и ДЗ можно в разделе <b>⚙️ Настройки</b>.</i>',
            reply_markup=get_main_keyboard(
                subscribed=bool(sub_val),
                is_starosta=bool(starosta_val),
                is_hw_manager=bool(hw_val),
            ),
            parse_mode='HTML',
        )
        return

    if data in ('settings_toggle_sub', 'settings_toggle_sub_morning', 'settings_toggle_sub_evening'):
        user = get_user_data(user_id)
        if not user:
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            return
        sub_m, sub_e = get_user_mailing_prefs(user_id)
        if data == 'settings_toggle_sub_morning':
            sub_m = 0 if sub_m == 1 else 1
            alert = '☀️ Утренняя рассылка (07:00) включена' if sub_m else '☀️ Утренняя рассылка (07:00) выключена'
        elif data == 'settings_toggle_sub_evening':
            sub_e = 0 if sub_e == 1 else 1
            alert = '🌙 Вечерняя рассылка (18:00) включена' if sub_e else '🌙 Вечерняя рассылка (18:00) выключена'
        else:
            new_both = 0 if (sub_m or sub_e) else 1
            sub_m, sub_e = new_both, new_both
            alert = '🔔 Авторассылка включена' if new_both else '🔕 Авторассылка отключена'
        new_sub = 1 if (sub_m or sub_e) else 0
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE users SET subscribed = ?, sub_morning = ?, sub_evening = ? WHERE user_id = ?',
                (new_sub, sub_m, sub_e, user_id),
            )
        invalidate_user_cache(user_id)
        try:
            bot.answer_callback_query(call.id, alert)
        except Exception:
            pass
        open_settings_menu(chat_id, call.from_user, msg_id)
        return

    if data == 'settings_toggle_notify':
        user = get_user_data(user_id)
        if not user:
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            return
        new_val = 0 if user[5] == 1 else 1
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE users SET notify_updates = ? WHERE user_id = ?',
                (new_val, user_id),
            )
        invalidate_user_cache(user_id)
        try:
            alert = (
                '🔔 Уведомления о парах и ДЗ включены'
                if new_val
                else '🔕 Уведомления о парах и ДЗ выключены'
            )
            bot.answer_callback_query(call.id, alert)
        except Exception:
            pass
        open_settings_menu(chat_id, call.from_user, msg_id)
        return

    if data == 'settings_change_group':
        clear_user_state(user_id)
        try:
            bot.edit_message_text(
                '🏛 <b>Смена факультета и группы</b>\n\n👇 Выбери свой <b>факультет</b>:',
                chat_id,
                msg_id,
                reply_markup=build_faculty_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            pass
        return

    if data == 'settings_restart':
        clear_user_state(user_id)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        user = get_user_data(user_id)
        if not user or not is_group_valid(user[7], user[0], user[1]):
            bot.send_message(
                chat_id,
                '👇 Выбери свой <b>факультет</b>:',
                reply_markup=build_faculty_keyboard(),
                parse_mode='HTML',
            )
            return
        starosta_flag = is_user_starosta(call.from_user)
        hw_flag = can_manage_hw(call.from_user)
        bot.send_message(
            chat_id,
            '🔄 <b>Главное меню обновлено!</b>',
            reply_markup=get_main_keyboard(bool(user[2]), starosta_flag, hw_flag),
            parse_mode='HTML',
        )
        return


# --- РАБОТА В ОБЩИХ БЕСЕДАХ ГРУПП (ГРУППОВЫЕ ЧАТЫ) ---
def _can_manage_group_chat(chat_id, user_obj):
    if is_admin_user(user_obj) or is_user_starosta(user_obj):
        return True
    try:
        member = bot.get_chat_member(chat_id, user_obj.id)
        if member and getattr(member, 'status', '') in ('administrator', 'creator'):
            return True
    except Exception:
        pass
    return False


@bot.message_handler(commands=['bind'], chat_types=['group', 'supergroup'])
def cmd_group_bind(message):
    sync_username(message.from_user)
    if not _can_manage_group_chat(message.chat.id, message.from_user):
        bot.reply_to(
            message,
            '⛔ Привязывать беседу к группе может только администратор этого чата, староста или админ бота.',
        )
        return
    user = get_user_data(message.from_user.id)
    if not user or not is_group_valid(user[7], user[0], user[1]):
        bot.reply_to(
            message,
            '⚠️ Сначала выбери свой факультет и группу в личке с ботом (/start), а потом снова напиши <code>/bind</code> в этой беседе.',
            parse_mode='HTML',
        )
        return

    course, group_name, fac_code = user[0], user[1], user[7]
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO group_chats (chat_id, faculty, course, group_name, bound_by, bound_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (
                message.chat.id,
                fac_code,
                course,
                group_name,
                message.from_user.id,
                now_local().strftime('%d.%m.%Y %H:%M'),
            ),
        )
    bot.reply_to(
        message,
        f'✅ <b>Беседа привязана к учебной группе!</b>\n\n'
        f'<blockquote>🏛 <b>Факультет:</b> {esc(get_faculty_short(fac_code))}\n'
        f'🎓 <b>Курс:</b> {course}\n'
        f'📚 <b>Группа:</b> {esc(group_name)}</blockquote>\n\n'
        f'Теперь здесь работают команды:\n'
        f'• <code>/today</code> (или <code>/сегодня</code>) — пары на сегодня\n'
        f'• <code>/tomorrow</code> (или <code>/завтра</code>) — пары на завтра\n'
        f'• <code>/week</code> (или <code>/неделя</code>) — расписание на неделю\n'
        f'• <code>/hw</code> (или <code>/дз</code>) — актуальные ДЗ\n'
        f'• <code>/unbind</code> — отвязать беседу\n\n'
        f'⚡ <i>Замены, отмены пар и новые ДЗ от старосты будут автоматически прилетать в этот чат!</i>',
        parse_mode='HTML',
    )


@bot.message_handler(commands=['unbind'], chat_types=['group', 'supergroup'])
def cmd_group_unbind(message):
    sync_username(message.from_user)
    if not _can_manage_group_chat(message.chat.id, message.from_user):
        bot.reply_to(
            message,
            '⛔ Отвязать беседу может только администратор этого чата, староста или админ бота.',
        )
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM group_chats WHERE chat_id = ?', (message.chat.id,))
        removed = cursor.rowcount
    if removed:
        bot.reply_to(message, '🔓 Беседа отвязана от учебной группы. Авто-уведомления отключены.')
    else:
        bot.reply_to(message, 'ℹ️ Эта беседа и так не привязана ни к одной группе.')


def _extract_group_cmd(text):
    raw = (text or '').strip()
    if not raw.startswith('/'):
        return ''
    cmd = raw.split()[0].split('@')[0].lower()
    return cmd


@bot.message_handler(
    chat_types=['group', 'supergroup'],
    func=lambda m: _extract_group_cmd(m.text) in (
        '/today', '/сегодня',
        '/tomorrow', '/завтра',
        '/week', '/неделя',
        '/hw', '/дз',
    ),
)
def handle_group_chat_commands(message):
    cmd = _extract_group_cmd(message.text)
    binding = get_group_chat_binding(message.chat.id)
    if not binding or not is_group_valid(binding[0], binding[1], binding[2]):
        bot.reply_to(
            message,
            '⚠️ Эта беседа ещё не привязана к учебной группе.\n'
            'Попроси старосту или любого участника (у кого выбрана группа в боте) написать здесь <code>/bind</code>.',
            parse_mode='HTML',
        )
        return

    fac_code, course, group_name = binding
    if cmd in ('/today', '/сегодня'):
        text = get_schedule_text(now_local(), course, group_name, faculty_code=fac_code)
        send_long_html_message(message.chat.id, text, parse_mode='HTML')
    elif cmd in ('/tomorrow', '/завтра'):
        text = get_schedule_text(now_local() + timedelta(days=1), course, group_name, faculty_code=fac_code)
        send_long_html_message(message.chat.id, text, parse_mode='HTML')
    elif cmd in ('/week', '/неделя'):
        send_week_schedule_message(message.chat.id, now_local(), course, group_name, week_offset=0, faculty_code=fac_code)
    elif cmd in ('/hw', '/дз'):
        send_homeworks_for_period(message.chat.id, message.from_user, group_name, days_limit=None, faculty_code=fac_code)


# --- ЕДИНЫЙ РОУТЕР МАШИНЫ СОСТОЯНИЙ (FSM НА БАЗЕ SQLITE user_states) ---
STATE_HANDLERS = {
    'awaiting_hw_content': process_hw_content,
    'awaiting_hw_slots_count': process_hw_queue_count_input,
    'awaiting_hw_manual_subj': process_hw_manual_subject,
    'awaiting_hw_edit': process_hw_edit_text,
    'awaiting_repl_subject': process_replacement_subject,
    'awaiting_manual_date': process_manual_date_for_action,
    'awaiting_broadcast': process_broadcast_content,
    'awaiting_teacher_query': process_teacher_search,
    'awaiting_docx_date': process_docx_custom_start_date,
    'awaiting_full_reset_confirm': process_full_reset_confirm,
}


@bot.message_handler(
    chat_types=['private'],
    content_types=['text', 'photo', 'document'],
    func=lambda m: bool(m.from_user and get_user_state(m.from_user.id)[0] in STATE_HANDLERS),
)
def fsm_message_router(message):
    sync_username(message.from_user)
    state_name, payload = get_user_state(message.from_user.id)
    handler = STATE_HANDLERS.get(state_name)
    if handler:
        handler(message, payload)


# --- ПРОВЕРКА ЗАВИСШИХ СОСТОЯНИЙ ПРИ ЗАПУСКЕ БОТА ---
def restore_interrupted_dialogs():
    now_ts = time.time()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, state, payload_json, updated_at FROM user_states WHERE state != ""')
        rows = cursor.fetchall()

    for u_id, state_name, payload_json, updated_at in rows:
        if now_ts - float(updated_at or 0) > STATE_TTL_SECONDS or state_name not in STATE_HANDLERS:
            clear_user_state(u_id)
            continue
        if state_name == 'awaiting_docx_date':
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except Exception:
                payload = {}
            if not get_pending_docx_import(payload.get('import_token', '')):
                clear_user_state(u_id)


# --- АВТОМАТИЧЕСКАЯ РАССЫЛКА (07:00 И 18:00) ЧЕРЕЗ ФОНОВУЮ ОЧЕРЕДЬ ---
def _run_scheduled_mailing(day_offset, greeting_line, day_label_genitive, sub_column='sub_morning'):
    target_dt = now_local() + timedelta(days=day_offset)
    target_str = target_dt.strftime('%d.%m.%Y')
    col = 'sub_morning' if sub_column == 'sub_morning' else 'sub_evening'
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT user_id, course, group_name, faculty FROM users "
            f"WHERE {col} = 1 AND group_name IS NOT NULL AND group_name != '' AND course > 0"
        )
        users = cursor.fetchall()

    group_cache = {}
    for u_id, course, group_name, fac_code in users:
        cache_key = (fac_code, course, group_name)
        if cache_key not in group_cache:
            text = (
                f'{greeting_line}\n\n'
                + get_schedule_text(target_dt, course, group_name, faculty_code=fac_code)
            )
            kb = get_pagination_keyboard(day_offset, group_name)
            attachments = []
            for _, hw_subj, hw_text, hw_photo, hw_doc_id, hw_doc_name in get_homeworks_for_date(group_name, target_str, faculty_code=fac_code):
                photo_ids = parse_photo_ids(hw_photo)
                if photo_ids or hw_doc_id:
                    cap = (
                        f'📎 <b>Вложение к ДЗ на {day_label_genitive} ({target_str})</b>\n'
                        f'📘 <b>{esc(hw_subj)}</b>\n'
                        f'📝 {esc(hw_text) if hw_text else ""}'
                    ).strip()
                    attachments.append((photo_ids, cap, hw_doc_id, hw_doc_name))
            group_cache[cache_key] = (text, kb, attachments)

        text, kb, attachments = group_cache[cache_key]
        chunks = split_long_html_message(text, max_len=3900)
        for idx_c, ch in enumerate(chunks):
            enqueue_message(u_id, ch, reply_markup=(kb if idx_c == len(chunks) - 1 else None), parse_mode='HTML')
        for photo_ids, cap, hw_doc_id, hw_doc_name in attachments:
            enqueue_hw_media(u_id, photo_ids, cap, doc_file_id=hw_doc_id, doc_file_name=hw_doc_name)


def morning_mailing():
    _run_scheduled_mailing(
        0,
        '☀️ <b>Доброе утро!</b> Актуальное расписание пар и ДЗ на сегодня:',
        'сегодня',
        sub_column='sub_morning',
    )


def evening_mailing():
    _run_scheduled_mailing(
        1,
        '🌙 <b>Добрый вечер!</b> Напоминание о расписании пар и ДЗ на завтра:',
        'завтра',
        sub_column='sub_evening',
    )


scheduler = BackgroundScheduler(timezone='Asia/Yekaterinburg')
scheduler.add_job(morning_mailing, 'cron', day_of_week='mon-fri', hour=7, minute=0)
scheduler.add_job(evening_mailing, 'cron', day_of_week='sun,mon,tue,wed,thu', hour=18, minute=0)
scheduler.add_job(send_db_backup, 'cron', hour=3, minute=0)
scheduler.add_job(cleanup_stale_states, 'interval', minutes=30)
scheduler.add_job(cleanup_old_db_records, 'cron', day_of_week='sun', hour=4, minute=0)
scheduler.add_job(check_academic_year_rollover, 'cron', hour=0, minute=5)


if __name__ == '__main__':
    if not TOKEN or TOKEN == 'YOUR_BOT_TOKEN_HERE':
        raise RuntimeError('Не задан токен бота! Укажи переменную окружения BOT_TOKEN или впиши токен в TOKEN.')
    init_db()
    check_academic_year_rollover()
    restore_interrupted_dialogs()
    scheduler.start()
    try:
        # Обычным пользователям в личке показываем только /start
        bot.set_my_commands([
            BotCommand('start', 'Перезапустить бота / главное меню'),
        ])
        # В групповых чатах показываем команды для бесед групп
        bot.set_my_commands(
            [
                BotCommand('today', 'Расписание на сегодня'),
                BotCommand('tomorrow', 'Расписание на завтра'),
                BotCommand('week', 'Расписание на неделю'),
                BotCommand('hw', 'Актуальные домашние задания'),
                BotCommand('bind', 'Привязать беседу к группе'),
                BotCommand('unbind', 'Отвязать беседу от группы'),
            ],
            scope=BotCommandScopeAllGroupChats(),
        )
        # Админу устанавливаем персональное меню команд только в его личном чате
        with db_connection() as conn:
            c_adm = conn.cursor()
            c_adm.execute(
                'SELECT user_id FROM users WHERE LOWER(username) = ?',
                (ADMIN_USERNAME.lower(),),
            )
            for (adm_id,) in c_adm.fetchall():
                sync_admin_personal_commands(adm_id)
    except Exception as e:
        print(f'Не удалось установить меню команд: {e}')
    print('Бот СИ УУНиТ успешно запущен и работает!')
    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=25,
                allowed_updates=['message', 'callback_query', 'inline_query'],
            )
        except Exception as e:
            print(f'Сбой соединения polling, перезапуск через 5 сек: {e}')
            time.sleep(5)
