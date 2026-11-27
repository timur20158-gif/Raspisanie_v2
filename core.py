from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import html
import io
import json
import urllib.request
import os
import queue
import re
import sqlite3
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


# --- НАСТРОЙКИ ---
# Укажи актуальный токен в переменной окружения BOT_TOKEN или вставь вместо 'YOUR_BOT_TOKEN_HERE'
TOKEN = os.getenv('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
ADMIN_USERNAME = 'tyamuuuuuuuuur'
LOCAL_TZ = ZoneInfo('Asia/Yekaterinburg')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv('DATA_DIR') or ('/data' if os.path.isdir('/data') else BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, 'schedule.db')
SCHEDULE_JSON_PATH = os.path.join(DATA_DIR, 'schedule.json')
SEMESTER_META_PATH = os.path.join(DATA_DIR, 'semester_meta.json')

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)

DAYS_RUS = [
    'Понедельник',
    'Вторник',
    'Среда',
    'Четверг',
    'Пятница',
    'Суббота',
    'Воскресенье',
]

# Жёсткая привязка времени к реальному номеру пары
PAIR_NUMBERS = {
    '08:00 - 09:20': 1,
    '09:35 - 10:55': 2,
    '11:10 - 12:30': 3,
    '13:30 - 14:50': 4,
    '15:05 - 16:25': 5,
    '16:40 - 18:00': 6,
}

NUM_TO_TIME_SLOT = {v: k for k, v in PAIR_NUMBERS.items()}

PAIR_EMOJIS = {
    1: '1️⃣',
    2: '2️⃣',
    3: '3️⃣',
    4: '4️⃣',
    5: '5️⃣',
    6: '6️⃣',
}

# --- СПРАВОЧНИК ФАКУЛЬТЕТОВ СИ УУНиТ ---
FACULTIES_META = {
    'ЕМФ': {
        'short': 'ЕМФ',
        'full': 'Естественно-математический факультет',
        'genitive': 'Естественно-математического факультета',
        'emoji': '🧬',
    },
    'ПФ': {
        'short': 'Педфак',
        'full': 'Педагогический факультет',
        'genitive': 'Педагогического факультета',
        'emoji': '📚',
    },
    'ФЭП': {
        'short': 'ФЭП',
        'full': 'Факультет экономики и права',
        'genitive': 'Факультета экономики и права',
        'emoji': '⚖️',
    },
    'ТФ': {
        'short': 'ТФ',
        'full': 'Технологический факультет',
        'genitive': 'Технологического факультета',
        'emoji': '⚙️',
    },
}

FACULTY_ORDER = ['ЕМФ', 'ПФ', 'ФЭП', 'ТФ']

# Базовые практики (дополняются автоматически из загружаемых .docx файлов)
DEFAULT_PRACTICE_WEEKS = {}

# Динамические структуры (полностью формируются из schedule.json и semester_meta.json)
FACULTY_STRUCTURE = {fac: {} for fac in FACULTY_ORDER}
SCHEDULE_CACHE_VERSION = 0
PRACTICE_WEEKS = {k: set(v) for k, v in DEFAULT_PRACTICE_WEEKS.items()}

# Старт учебного года (строго 1 сентября, сквозной счёт недель на весь учебный год без весеннего сброса)
ACADEMIC_YEAR_START_DATE = datetime(2026, 9, 1, tzinfo=LOCAL_TZ)

PENDING_DOCX_DIR = os.path.join(tempfile.gettempdir(), 'si_uunit_docx_imports')
os.makedirs(PENDING_DOCX_DIR, exist_ok=True)
pending_docx_imports = {}



SHORT_DAYS_RUS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']


def atomic_write_json(filepath, data):
    dir_name = os.path.dirname(os.path.abspath(filepath)) or '.'
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


def save_pending_docx_import(token, data_dict):
    pending_docx_imports[token] = data_dict
    serializable = dict(data_dict)
    if isinstance(serializable.get('year_start_dt'), datetime):
        serializable['year_start_dt'] = serializable['year_start_dt'].strftime('%d.%m.%Y')
    try:
        atomic_write_json(os.path.join(PENDING_DOCX_DIR, f'{token}.json'), serializable)
    except Exception as e:
        print(f'Ошибка сохранения сессии импорта: {e}')


def get_pending_docx_import(token, pop=False):
    item = pending_docx_imports.pop(token, None) if pop else pending_docx_imports.get(token)
    fpath = os.path.join(PENDING_DOCX_DIR, f'{token}.json')
    if item is None and os.path.exists(fpath):
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                item = json.load(f)
            if isinstance(item.get('year_start_dt'), str):
                item['year_start_dt'] = datetime.strptime(item['year_start_dt'], '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
            if not pop:
                pending_docx_imports[token] = item
        except Exception as e:
            print(f'Ошибка чтения сессии импорта: {e}')
            item = None
    if pop and os.path.exists(fpath):
        try:
            os.remove(fpath)
        except Exception:
            pass
    return item

# Буфер для сборки альбомов (media_group) при добавлении ДЗ
media_group_buffer = {}
media_group_lock = threading.Lock()

# --- ФОНОВАЯ ОЧЕРЕДЬ РАССЫЛКИ УВЕДОМЛЕНИЙ ---

# --- ХУКИ ДЛЯ СИНХРОНИЗАЦИИ УВЕДОМЛЕНИЙ С VK-БОТОМ ---
BOT_START_TS = time.time()
VK_SEND_HOOK = None
VK_NOTIFY_GROUP_HOOK = None
VK_QUEUE_SIZE_HOOK = None


def enqueue_vk_message(vk_user_id, text, attachment=None, keyboard=None):
    if callable(VK_SEND_HOOK):
        try:
            VK_SEND_HOOK(int(vk_user_id), text, attachment=attachment, keyboard=keyboard)
        except Exception as e:
            print(f'Ошибка отправки в VK ({vk_user_id}): {e}')


def notify_vk_group(faculty_code, group_name, plain_text, exclude_vk_id=None):
    if callable(VK_NOTIFY_GROUP_HOOK):
        try:
            VK_NOTIFY_GROUP_HOOK(faculty_code, group_name, plain_text, exclude_vk_id=exclude_vk_id)
        except Exception as e:
            print(f'Ошибка уведомления группы в VK ({group_name}): {e}')

MAILING_QUEUE = queue.Queue()
MAILING_INTERVAL_SEC = 0.06


def _requeue_after_delay(task, delay_sec):
    def _put_back():
        MAILING_QUEUE.put(task)
    t = threading.Timer(max(1.0, float(delay_sec)), _put_back)
    t.daemon = True
    t.start()


def _mailing_worker():
    while True:
        task = MAILING_QUEUE.get()
        if task is None:
            MAILING_QUEUE.task_done()
            break
        try:
            kind = task.get('kind', 'text')
            user_id = task['user_id']
            retries = int(task.get('_retries', 0))
            if kind == 'media':
                status, retry_after = _direct_send_hw_media(
                    user_id,
                    task.get('photo_ids') or [],
                    task.get('caption_html', ''),
                    reply_markup=task.get('reply_markup'),
                    doc_file_id=task.get('doc_file_id', ''),
                    doc_file_name=task.get('doc_file_name', ''),
                    hw_id=task.get('hw_id'),
                )
            else:
                status, retry_after = _direct_send_message(
                    user_id,
                    task.get('text', ''),
                    reply_markup=task.get('reply_markup'),
                    parse_mode=task.get('parse_mode', 'HTML'),
                )
            if status == 'retry' and retries < 3:
                requeued_task = dict(task)
                requeued_task['_retries'] = retries + 1
                _requeue_after_delay(requeued_task, retry_after)
        except Exception as e:
            print(f'Ошибка в фоновом потоке рассылки: {e}')
        finally:
            MAILING_QUEUE.task_done()
            time.sleep(MAILING_INTERVAL_SEC)


_mailing_thread = threading.Thread(target=_mailing_worker, name='MailingWorker', daemon=True)
_mailing_thread.start()


def enqueue_message(user_id, text, reply_markup=None, parse_mode='HTML'):
    chunks = split_long_html_message(text, max_len=3900) if parse_mode == 'HTML' else [
        text[i:i + 3900] for i in range(0, len(text), 3900)
    ] or ['']
    for idx, ch in enumerate(chunks):
        kb = reply_markup if idx == len(chunks) - 1 else None
        MAILING_QUEUE.put({
            'kind': 'text',
            'user_id': user_id,
            'text': ch,
            'reply_markup': kb,
            'parse_mode': parse_mode,
            '_retries': 0,
        })


def enqueue_hw_media(user_id, photo_ids, caption_html, reply_markup=None, doc_file_id='', doc_file_name='', hw_id=None):
    MAILING_QUEUE.put({
        'kind': 'media',
        'user_id': user_id,
        'photo_ids': photo_ids,
        'caption_html': caption_html,
        'reply_markup': reply_markup,
        'doc_file_id': doc_file_id,
        'doc_file_name': doc_file_name,
        'hw_id': hw_id,
        '_retries': 0,
    })



def now_local():
    return datetime.now(LOCAL_TZ)


def get_academic_year_start_by_year(start_year):
    # Обязательный старт и сброс учебного года — строго 1 сентября, независимо от дня недели.
    return datetime(int(start_year), 9, 1, tzinfo=LOCAL_TZ)


def get_academic_year_end_date(start_dt):
    """Последний день учебного года перед обнулением (31 августа следующего года)."""
    start_year = start_dt.year - 1 if start_dt.month <= 6 else start_dt.year
    next_start = get_academic_year_start_by_year(start_year + 1)
    return next_start - timedelta(days=1)


def calc_week_number_for_start(target_date, start_dt):
    """Вычисляет номер учебной недели: год стартует строго с start_dt (1 сентября),
    а переключение на следующую неделю всегда происходит по понедельникам."""
    year_end_dt = get_academic_year_end_date(start_dt)
    t_date = target_date.date()
    s_date = start_dt.date()
    if t_date < s_date:
        return 0
    if t_date > year_end_dt.date():
        return 53
    # Если 1 сентября выпало на воскресенье (weekday == 6), то 1-я учебная неделя начинается с понедельника 2 сентября
    if s_date.weekday() == 6:
        if t_date == s_date:
            return 1
        week1_monday = s_date + timedelta(days=1)
    else:
        week1_monday = s_date - timedelta(days=s_date.weekday())
    delta_days = (t_date - week1_monday).days
    return (delta_days // 7) + 1


def extract_academic_start_from_title(title_text):
    txt = str(title_text or '')
    m = re.search(r'(20\d{2})\s*[/\\–—-]\s*(20\d{2})', txt)
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if y2 == y1 + 1:
            return get_academic_year_start_by_year(y1)
    return None


def get_default_academic_year_start(ref_dt=None):
    if ref_dt is None:
        ref_dt = now_local()
    # В июле уже публикуют расписание на новый учебный год (сентябрь), поэтому границей считаем июнь (<= 6).
    start_year = ref_dt.year - 1 if ref_dt.month <= 6 else ref_dt.year
    return get_academic_year_start_by_year(start_year)



def get_faculty_short(fac_code):
    meta = FACULTIES_META.get(fac_code)
    return meta['short'] if meta else str(fac_code or 'ЕМФ')


def get_faculty_full(fac_code):
    meta = FACULTIES_META.get(fac_code)
    return meta['full'] if meta else str(fac_code or 'Естественно-математический факультет')


def detect_faculty_from_text(text):
    low = str(text or '').lower()
    if any(k in low for k in ('педагогического факультета', 'педагогический факультет', 'педфак', 'офо_пф', 'офо пф', '_пф_', ' пф ')):
        return 'ПФ'
    if any(k in low for k in ('экономики и права', 'факультет экономики', 'фэп', 'офо_фэп', '_фэп_')):
        return 'ФЭП'
    if any(k in low for k in ('технологического факультета', 'технологический факультет', 'техфак', 'офо_тф', '_тф_')):
        return 'ТФ'
    if any(k in low for k in ('естественно-математического', 'естественно-математический', 'емф', 'офо_емф', '_емф_')):
        return 'ЕМФ'
    return None


def infer_faculty_by_group(group_name):
    for fac_code, courses in FACULTY_STRUCTURE.items():
        for crs, groups in courses.items():
            if group_name in groups:
                return fac_code
    low = str(group_name or '').lower()
    if any(k in low for k in ('экономик', 'менеджмент', 'юриспруденц', 'право', 'финанс', 'бухгалтер', 'государственное и муниципальное')):
        return 'ФЭП'
    if any(k in low for k in ('технолог', 'агроинженер', 'пожарн', 'безопасность техносфер', 'строительств', 'машиностроен', 'электроэнерг')):
        return 'ТФ'
    if any(k in low for k in ('иностранный язык', 'психолог', 'дошкольн', 'начальн', 'логопед', 'дефектолог')):
        return 'ПФ'
    return 'ЕМФ'


def is_group_valid(faculty_code, course, group_name):
    courses = FACULTY_STRUCTURE.get(faculty_code, {})
    return group_name in courses.get(course, [])


def get_academic_year_label(start_dt):
    # Январь–июнь относятся к текущему учебному году (год-1 / год), а не к следующему
    start_year = start_dt.year - 1 if start_dt.month <= 6 else start_dt.year
    return f'{start_year}/{start_year + 1}'


def load_semester_meta():
    global ACADEMIC_YEAR_START_DATE, PRACTICE_WEEKS
    ACADEMIC_YEAR_START_DATE = get_default_academic_year_start()
    if os.path.exists(SEMESTER_META_PATH):
        try:
            with open(SEMESTER_META_PATH, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            start_raw = meta.get('academic_year_start') or meta.get('start_date')
            if start_raw:
                dt = datetime.strptime(start_raw, '%d.%m.%Y').replace(tzinfo=LOCAL_TZ)
                ACADEMIC_YEAR_START_DATE = dt
            if isinstance(meta.get('practice_weeks'), dict):
                PRACTICE_WEEKS = {k: set(v) for k, v in meta['practice_weeks'].items()}
        except Exception as e:
            print(f'Ошибка чтения semester_meta.json: {e}')


def save_semester_meta(
    academic_start_dt,
    practice_dict,
    semester_title='',
    fac_groups=None,
    reset_done_faculties=None,
    academic_year_label=None,
):
    global ACADEMIC_YEAR_START_DATE, PRACTICE_WEEKS
    ACADEMIC_YEAR_START_DATE = academic_start_dt.replace(tzinfo=LOCAL_TZ)
    if fac_groups:
        for grp in fac_groups:
            PRACTICE_WEEKS.pop(grp, None)
    for k, v in practice_dict.items():
        PRACTICE_WEEKS[k] = set(v)
    existing_meta = {}
    if os.path.exists(SEMESTER_META_PATH):
        try:
            with open(SEMESTER_META_PATH, 'r', encoding='utf-8') as f:
                existing_meta = json.load(f)
        except Exception:
            existing_meta = {}
    yr_label = academic_year_label or get_academic_year_label(ACADEMIC_YEAR_START_DATE)
    meta = {
        'academic_year': yr_label,
        'academic_year_start': ACADEMIC_YEAR_START_DATE.strftime('%d.%m.%Y'),
        'semester_title': semester_title,
        'practice_weeks': {k: sorted(list(v)) for k, v in PRACTICE_WEEKS.items()},
        'reset_done_faculties': reset_done_faculties if reset_done_faculties is not None else existing_meta.get('reset_done_faculties', []),
        'updated_at': now_local().strftime('%d.%m.%Y %H:%M'),
    }
    atomic_write_json(SEMESTER_META_PATH, meta)


# --- ПОСТОЯННОЕ ХРАНИЛИЩЕ СОСТОЯНИЙ ПОЛЬЗОВАТЕЛЕЙ (RAM-КЭШ + SQLITE user_states) ---
STATE_TTL_SECONDS = 3600 * 12
_USER_STATES_CACHE = {}
_USER_STATES_LOCK = threading.RLock()


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    try:
        conn.execute('PRAGMA busy_timeout = 30000')
        conn.execute('PRAGMA foreign_keys = ON')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_user_states_cache():
    now_ts = time.time()
    loaded = {}
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, state, payload_json, updated_at FROM user_states')
        for uid, state_name, payload_json, updated_at in cursor.fetchall():
            ts = float(updated_at or 0)
            if now_ts - ts > STATE_TTL_SECONDS:
                continue
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except Exception:
                payload = {}
            loaded[int(uid)] = (state_name or '', payload, ts)
    with _USER_STATES_LOCK:
        _USER_STATES_CACHE.clear()
        _USER_STATES_CACHE.update(loaded)


def set_user_state(user_id, state_name='', payload=None):
    uid = int(user_id)
    st_str = str(state_name or '')
    pl_dict = dict(payload) if payload else {}
    payload_str = json.dumps(pl_dict, ensure_ascii=False)
    now_ts = time.time()
    with _USER_STATES_LOCK:
        _USER_STATES_CACHE[uid] = (st_str, pl_dict, now_ts)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO user_states (user_id, state, payload_json, updated_at) '
            'VALUES (?, ?, ?, ?)',
            (uid, st_str, payload_str, now_ts),
        )


def get_user_state(user_id):
    uid = int(user_id)
    now_ts = time.time()
    with _USER_STATES_LOCK:
        entry = _USER_STATES_CACHE.get(uid)
        if not entry:
            return '', {}
        state_name, payload, updated_at = entry
        if now_ts - updated_at > STATE_TTL_SECONDS:
            _USER_STATES_CACHE.pop(uid, None)
        else:
            return state_name, dict(payload)

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_states WHERE user_id = ?', (uid,))
    return '', {}


def clear_user_state(user_id, keep_payload=False):
    uid = int(user_id)
    if keep_payload:
        _, old_payload = get_user_state(uid)
        set_user_state(uid, '', old_payload)
    else:
        with _USER_STATES_LOCK:
            _USER_STATES_CACHE.pop(uid, None)
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM user_states WHERE user_id = ?', (uid,))


def cleanup_stale_states():
    now_ts = time.time()
    with _USER_STATES_LOCK:
        expired_uids = [
            uid for uid, (_, _, ts) in _USER_STATES_CACHE.items()
            if now_ts - ts > STATE_TTL_SECONDS
        ]
        for uid in expired_uids:
            _USER_STATES_CACHE.pop(uid, None)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_states WHERE ? - updated_at > ?', (now_ts, STATE_TTL_SECONDS))
    with media_group_lock:
        stale_mgs = [
            mg_id for mg_id, ent in media_group_buffer.items()
            if now_ts - float(ent.get('_created_ts', 0)) > 120
        ]
        for mg_id in stale_mgs:
            ent = media_group_buffer.pop(mg_id, None)
            if ent and ent.get('timer'):
                try:
                    ent['timer'].cancel()
                except Exception:
                    pass
    for k in list(pending_docx_imports.keys()):
        if now_ts - pending_docx_imports[k].get('_ts', 0) > 3600:
            get_pending_docx_import(k, pop=True)
    if os.path.isdir(PENDING_DOCX_DIR):
        for fn in os.listdir(PENDING_DOCX_DIR):
            fp = os.path.join(PENDING_DOCX_DIR, fn)
            try:
                if now_ts - os.path.getmtime(fp) > 3600:
                    os.remove(fp)
            except Exception:
                pass


def cleanup_old_db_records(days_keep=30):
    cutoff = (now_local() - timedelta(days=days_keep)).date()
    with db_connection() as conn:
        cursor = conn.cursor()
        for table in ('replacements', 'cancelled_lessons'):
            cursor.execute(f'SELECT id, target_date FROM {table}')
            stale_ids = []
            for rec_id, d_str in cursor.fetchall():
                try:
                    if datetime.strptime(d_str, '%d.%m.%Y').date() < cutoff:
                        stale_ids.append((rec_id,))
                except ValueError:
                    pass
            if stale_ids:
                cursor.executemany(f'DELETE FROM {table} WHERE id = ?', stale_ids)

        cursor.execute('SELECT id, target_date, queue_id, annot_queue_id FROM homeworks')
        stale_hw_ids = []
        stale_q_ids = []
        for hw_id, d_str, q_id, aq_id in cursor.fetchall():
            try:
                if datetime.strptime(d_str, '%d.%m.%Y').date() < cutoff:
                    stale_hw_ids.append((hw_id,))
                    if q_id:
                        stale_q_ids.append((q_id,))
                    if aq_id:
                        stale_q_ids.append((aq_id,))
            except ValueError:
                pass
        if stale_q_ids:
            cursor.executemany('DELETE FROM queue_slots WHERE queue_id = ?', stale_q_ids)
            cursor.executemany('DELETE FROM queues WHERE id = ?', stale_q_ids)
        if stale_hw_ids:
            cursor.executemany('DELETE FROM hw_done WHERE hw_id = ?', stale_hw_ids)
            cursor.executemany('DELETE FROM homeworks WHERE id = ?', stale_hw_ids)
        cursor.execute('PRAGMA optimize')
    try:
        wal_conn = sqlite3.connect(DB_PATH, timeout=30.0)
        wal_conn.execute('PRAGMA busy_timeout = 30000')
        wal_conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        wal_conn.close()
    except Exception as e:
        print(f'Ошибка при выполнении WAL checkpoint: {e}')


# --- БАЗА ДАННЫХ И ИНИЦИАЛИЗАЦИЯ ---
def init_db():
    load_semester_meta()
    with db_connection() as conn:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
        cursor = conn.cursor()

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS users ('
            'user_id INTEGER PRIMARY KEY, '
            'faculty TEXT DEFAULT "ЕМФ", '
            'course INTEGER, '
            'group_name TEXT, '
            'subscribed INTEGER DEFAULT 0, '
            'username TEXT DEFAULT "", '
            'is_starosta INTEGER DEFAULT 0, '
            'is_hw_manager INTEGER DEFAULT 0, '
            'notify_updates INTEGER DEFAULT 0, '
            'managed_group TEXT DEFAULT "")'
        )

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS user_states ('
            'user_id INTEGER PRIMARY KEY, '
            'state TEXT DEFAULT "", '
            'payload_json TEXT DEFAULT "{}", '
            'updated_at REAL)'
        )

        cursor.execute("PRAGMA table_info(users)")
        existing_cols = [col[1] for col in cursor.fetchall()]
        if 'faculty' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN faculty TEXT DEFAULT "ЕМФ"')
            cursor.execute('UPDATE users SET faculty = "ЕМФ" WHERE faculty IS NULL OR faculty = ""')
        if 'username' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN username TEXT DEFAULT ""')
        if 'is_starosta' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN is_starosta INTEGER DEFAULT 0')
        if 'is_hw_manager' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN is_hw_manager INTEGER DEFAULT 0')
        if 'notify_updates' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN notify_updates INTEGER DEFAULT 0')
        if 'managed_group' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN managed_group TEXT DEFAULT ""')
            cursor.execute(
                'UPDATE users SET managed_group = group_name '
                'WHERE (is_starosta = 1 OR is_hw_manager = 1) AND (managed_group IS NULL OR managed_group = "")'
            )
        if 'sub_morning' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN sub_morning INTEGER DEFAULT 0')
            cursor.execute('UPDATE users SET sub_morning = subscribed WHERE subscribed = 1')
        if 'sub_evening' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN sub_evening INTEGER DEFAULT 0')
            cursor.execute('UPDATE users SET sub_evening = subscribed WHERE subscribed = 1')
        if 'last_active' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN last_active REAL DEFAULT 0')
        if 'is_blocked' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0')
        if 'created_at' not in existing_cols:
            cursor.execute('ALTER TABLE users ADD COLUMN created_at REAL DEFAULT 0')

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS replacements ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'faculty TEXT DEFAULT "", '
            'group_name TEXT, '
            'target_date TEXT, '
            'time_slot TEXT, '
            'subject_info TEXT, '
            'added_by INTEGER)'
        )

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS cancelled_lessons ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'faculty TEXT DEFAULT "", '
            'group_name TEXT, '
            'target_date TEXT, '
            'time_slot TEXT, '
            'cancelled_by INTEGER)'
        )

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS homeworks ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'faculty TEXT DEFAULT "", '
            'group_name TEXT, '
            'target_date TEXT, '
            'subject_name TEXT, '
            'hw_text TEXT, '
            'photo_file_id TEXT DEFAULT "", '
            'doc_file_id TEXT DEFAULT "", '
            'doc_file_name TEXT DEFAULT "", '
            'queue_id INTEGER DEFAULT NULL, '
            'added_by INTEGER, '
            'created_at TEXT)'
        )

        cursor.execute("PRAGMA table_info(replacements)")
        repl_cols = [col[1] for col in cursor.fetchall()]
        if 'faculty' not in repl_cols:
            cursor.execute('ALTER TABLE replacements ADD COLUMN faculty TEXT DEFAULT ""')
        if 'created_at' not in repl_cols:
            cursor.execute('ALTER TABLE replacements ADD COLUMN created_at TEXT DEFAULT ""')

        cursor.execute("PRAGMA table_info(cancelled_lessons)")
        canc_cols = [col[1] for col in cursor.fetchall()]
        if 'faculty' not in canc_cols:
            cursor.execute('ALTER TABLE cancelled_lessons ADD COLUMN faculty TEXT DEFAULT ""')
        if 'created_at' not in canc_cols:
            cursor.execute('ALTER TABLE cancelled_lessons ADD COLUMN created_at TEXT DEFAULT ""')

        cursor.execute("PRAGMA table_info(homeworks)")
        hw_cols = [col[1] for col in cursor.fetchall()]
        if 'faculty' not in hw_cols:
            cursor.execute('ALTER TABLE homeworks ADD COLUMN faculty TEXT DEFAULT ""')
        if 'doc_file_id' not in hw_cols:
            cursor.execute('ALTER TABLE homeworks ADD COLUMN doc_file_id TEXT DEFAULT ""')
        if 'doc_file_name' not in hw_cols:
            cursor.execute('ALTER TABLE homeworks ADD COLUMN doc_file_name TEXT DEFAULT ""')
        if 'queue_id' not in hw_cols:
            cursor.execute('ALTER TABLE homeworks ADD COLUMN queue_id INTEGER DEFAULT NULL')
        if 'annot_queue_id' not in hw_cols:
            cursor.execute('ALTER TABLE homeworks ADD COLUMN annot_queue_id INTEGER DEFAULT NULL')
        if 'vk_attachments' not in hw_cols:
            cursor.execute('ALTER TABLE homeworks ADD COLUMN vk_attachments TEXT DEFAULT ""')

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS vk_users ('
            'vk_user_id INTEGER PRIMARY KEY, '
            'faculty TEXT DEFAULT "", '
            'course INTEGER DEFAULT 0, '
            'group_name TEXT DEFAULT "", '
            'state TEXT DEFAULT "", '
            'payload_json TEXT DEFAULT "{}", '
            'last_active REAL DEFAULT 0, '
            'created_at REAL DEFAULT 0, '
            'is_starosta INTEGER DEFAULT 0, '
            'managed_group TEXT DEFAULT "", '
            'sub_morning INTEGER DEFAULT 0, '
            'sub_evening INTEGER DEFAULT 0, '
            'notify_updates INTEGER DEFAULT 0, '
            'full_name TEXT DEFAULT "", is_blocked INTEGER DEFAULT 0)'
        )
        cursor.execute("PRAGMA table_info(vk_users)")
        vk_cols = [col[1] for col in cursor.fetchall()]
        for col_name, col_def in [
            ('created_at', 'REAL DEFAULT 0'),
            ('is_starosta', 'INTEGER DEFAULT 0'),
            ('managed_group', 'TEXT DEFAULT ""'),
            ('sub_morning', 'INTEGER DEFAULT 0'),
            ('sub_evening', 'INTEGER DEFAULT 0'),
            ('notify_updates', 'INTEGER DEFAULT 0'),
            ('full_name', 'TEXT DEFAULT ""'),
            ('is_blocked', 'INTEGER DEFAULT 0'),
        ]:
            if col_name not in vk_cols:
                cursor.execute(f'ALTER TABLE vk_users ADD COLUMN {col_name} {col_def}')

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS queues ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'faculty TEXT DEFAULT "", '
            'group_name TEXT, '
            'title TEXT, '
            'max_slots INTEGER DEFAULT 25, '
            'is_open INTEGER DEFAULT 1, '
            'homework_id INTEGER DEFAULT NULL, '
            'created_by INTEGER, '
            'created_at TEXT)'
        )

        cursor.execute("PRAGMA table_info(queues)")
        q_cols = [col[1] for col in cursor.fetchall()]
        if 'faculty' not in q_cols:
            cursor.execute('ALTER TABLE queues ADD COLUMN faculty TEXT DEFAULT ""')
        if 'homework_id' not in q_cols:
            cursor.execute('ALTER TABLE queues ADD COLUMN homework_id INTEGER DEFAULT NULL')

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS hw_done ('
            'user_id INTEGER, '
            'hw_id INTEGER, '
            'done_at TEXT, '
            'PRIMARY KEY (user_id, hw_id))'
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hwdone_hw ON hw_done(hw_id)')

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS group_chats ('
            'chat_id INTEGER PRIMARY KEY, '
            'faculty TEXT DEFAULT "", '
            'course INTEGER DEFAULT 0, '
            'group_name TEXT DEFAULT "", '
            'bound_by INTEGER, '
            'bound_at TEXT)'
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_group_chats_grp ON group_chats(faculty, group_name)')

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS daily_stats ('
            'date_str TEXT, '
            'action TEXT, '
            'count INTEGER DEFAULT 0, '
            'PRIMARY KEY (date_str, action))'
        )

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS group_settings ('
            'faculty TEXT DEFAULT "", '
            'group_name TEXT, '
            'extra_hw_lists INTEGER DEFAULT 0, '
            'session_mode INTEGER DEFAULT 0, '
            'PRIMARY KEY (faculty, group_name))'
        )
        cursor.execute('PRAGMA table_info(group_settings)')
        gs_cols = [c[1] for c in cursor.fetchall()]
        if 'session_mode' not in gs_cols:
            cursor.execute('ALTER TABLE group_settings ADD COLUMN session_mode INTEGER DEFAULT 0')

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS session_exams ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'faculty TEXT DEFAULT "", '
            'group_name TEXT, '
            'exam_date TEXT, '
            'exam_time TEXT DEFAULT "", '
            'subject_name TEXT, '
            'exam_type TEXT DEFAULT "Экзамен", '
            'teacher TEXT DEFAULT "", '
            'room TEXT DEFAULT "", '
            'added_by INTEGER, '
            'created_at TEXT)'
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_session_grp ON session_exams(faculty, group_name, exam_date)')

        cursor.execute(
            'CREATE TABLE IF NOT EXISTS queue_slots ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'queue_id INTEGER, '
            'slot_num INTEGER, '
            'user_id INTEGER, '
            'user_label TEXT, '
            'joined_at TEXT)'
        )
        cursor.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_qslots_num '
            'ON queue_slots(queue_id, slot_num)'
        )
        cursor.execute('DROP INDEX IF EXISTS idx_qslots_user')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_qslots_user_nonuniq ON queue_slots(queue_id, user_id)')

        cursor.execute(
            'DELETE FROM queue_slots WHERE queue_id IN '
            '(SELECT id FROM queues WHERE homework_id IS NULL)'
        )
        cursor.execute('DELETE FROM queues WHERE homework_id IS NULL')

        reload_schedule_tables(cursor, conn, force=True)

        for tbl in ('replacements', 'cancelled_lessons', 'homeworks', 'queues'):
            cursor.execute(f'SELECT DISTINCT group_name FROM {tbl} WHERE faculty IS NULL OR faculty = ""')
            for (g_nm,) in cursor.fetchall():
                inferred_f = infer_faculty_by_group(g_nm)
                cursor.execute(
                    f'UPDATE {tbl} SET faculty = ? WHERE group_name = ? AND (faculty IS NULL OR faculty = "")',
                    (inferred_f, g_nm),
                )

        cursor.execute(
            'DELETE FROM replacements WHERE id NOT IN ('
            'SELECT MAX(id) FROM replacements GROUP BY faculty, group_name, target_date, time_slot)'
        )
        cursor.execute(
            'DELETE FROM cancelled_lessons WHERE id NOT IN ('
            'SELECT MAX(id) FROM cancelled_lessons GROUP BY faculty, group_name, target_date, time_slot)'
        )

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_group ON users(group_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_faculty ON users(faculty, course)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')
        cursor.execute('DROP INDEX IF EXISTS idx_repl_unique_slot')
        cursor.execute('DROP INDEX IF EXISTS idx_canc_unique_slot')
        cursor.execute('DROP INDEX IF EXISTS idx_hw_group_date')
        cursor.execute('DROP INDEX IF EXISTS idx_queues_group')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_queues_group ON queues(faculty, group_name, is_open)')
        cursor.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_repl_unique_slot '
            'ON replacements(faculty, group_name, target_date, time_slot)'
        )
        cursor.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_canc_unique_slot '
            'ON cancelled_lessons(faculty, group_name, target_date, time_slot)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_hw_group_date '
            'ON homeworks(faculty, group_name, target_date)'
        )
    _load_user_states_cache()


def parse_weeks(weeks_str):
    weeks = set()
    normalized = str(weeks_str or '').replace('–', '-').replace('—', '-').replace(' ', '')
    for part in normalized.split(','):
        if not part:
            continue
        if '-' in part:
            bounds = part.split('-')
            if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                s, e = int(bounds[0]), int(bounds[1])
                if s > e:
                    s, e = e, s
                if 1 <= s <= 60 and 1 <= e <= 60:
                    weeks.update(range(s, e + 1))
        elif part.isdigit():
            w = int(part)
            if 1 <= w <= 60:
                weeks.add(w)
    return sorted(list(weeks))


def reload_schedule_tables(cursor, conn, force=False):
    global FACULTY_STRUCTURE, SCHEDULE_CACHE_VERSION
    SCHEDULE_CACHE_VERSION += 1
    if not os.path.exists(SCHEDULE_JSON_PATH):
        atomic_write_json(SCHEDULE_JSON_PATH, [])

    with open(SCHEDULE_JSON_PATH, 'rb') as fb:
        raw_bytes = fb.read()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    schedule_data = json.loads(raw_bytes.decode('utf-8'))

    cursor.execute(
        'CREATE TABLE IF NOT EXISTS app_meta ('
        'key TEXT PRIMARY KEY, '
        'value TEXT)'
    )
    cursor.execute("SELECT value FROM app_meta WHERE key = 'schedule_json_hash'")
    hash_row = cursor.fetchone()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schedules'")
    has_sched_tbl = bool(cursor.fetchone())
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schedule_weeks'")
    has_weeks_tbl = bool(cursor.fetchone())

    dynamic_struct = {fac: {} for fac in FACULTY_ORDER}
    parsed_items = []
    for item in schedule_data:
        if isinstance(item, dict):
            fac = item.get('faculty') or infer_faculty_by_group(item['group'])
            crs = int(item['course'])
            grp = item['group']
            day = item['day']
            time_slot = item['time']
            subj = item['subject']
            l_type = item['type']
            teacher = item['teacher']
            room = item['room']
            weeks_str = str(item['weeks'])
        else:
            crs, grp, day, time_slot, subj, l_type, teacher, room, weeks_str = item
            fac = infer_faculty_by_group(grp)
        dynamic_struct.setdefault(fac, {}).setdefault(crs, set()).add(grp)
        parsed_items.append((fac, crs, grp, day, time_slot, subj, l_type, teacher, room, weeks_str))

    new_struct = {fac: {} for fac in FACULTY_ORDER}
    for fac, courses in dynamic_struct.items():
        new_struct[fac] = {
            crs: sorted(list(grps)) for crs, grps in sorted(courses.items())
        }
    FACULTY_STRUCTURE = new_struct

    if not force and has_sched_tbl and has_weeks_tbl and hash_row and hash_row[0] == file_hash:
        return

    cursor.execute('DROP TABLE IF EXISTS schedules')
    cursor.execute('DROP TABLE IF EXISTS schedule_weeks')

    cursor.execute(
        'CREATE TABLE schedules ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'faculty TEXT DEFAULT "ЕМФ", '
        'course_number INTEGER, '
        'group_name TEXT, '
        'day_of_week TEXT, '
        'time_slot TEXT, '
        'subject_name TEXT, '
        'lesson_type TEXT, '
        'teacher TEXT, '
        'room TEXT, '
        'weeks_raw TEXT)'
    )
    cursor.execute(
        'CREATE TABLE schedule_weeks ('
        'schedule_id INTEGER, '
        'week_number INTEGER)'
    )

    weeks_batch = []
    for row_vals in parsed_items:
        cursor.execute(
            'INSERT INTO schedules ('
            'faculty, course_number, group_name, day_of_week, time_slot, '
            'subject_name, lesson_type, teacher, room, weeks_raw'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            row_vals,
        )
        sched_id = cursor.lastrowid
        for w in parse_weeks(row_vals[9]):
            weeks_batch.append((sched_id, w))

    if weeks_batch:
        cursor.executemany(
            'INSERT INTO schedule_weeks (schedule_id, week_number) VALUES (?, ?)',
            weeks_batch,
        )

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_sched_lookup '
        'ON schedules(course_number, group_name, day_of_week, time_slot)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_sched_fac_lookup '
        'ON schedules(faculty, course_number, group_name, day_of_week)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_sched_day '
        'ON schedules(day_of_week, time_slot)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_sched_weeks_week '
        'ON schedule_weeks(week_number, schedule_id)'
    )
    cursor.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('schedule_json_hash', ?)",
        (file_hash,),
    )


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def esc(text):
    return html.escape(str(text or ''))


def short_text(text, max_len=28):
    text = str(text or '')
    return text if len(text) <= max_len else text[: max_len - 1] + '…'


def parse_photo_ids(raw_photo_field):
    if not raw_photo_field:
        return []
    val = str(raw_photo_field).strip()
    if not val:
        return []
    if val.startswith('['):
        try:
            parsed = json.loads(val)
            return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    return [p.strip() for p in val.split(',') if p.strip()]


def serialize_photo_ids(photo_ids):
    clean = [p.strip() for p in photo_ids if p and p.strip()]
    if not clean:
        return ''
    if len(clean) == 1:
        return clean[0]
    return json.dumps(clean, ensure_ascii=False)


_HTML_TAG_RE = re.compile(r'<(/?)([a-zA-Z0-9]+)(?:\s[^>]*)?>')


def _balance_html_fragment(fragment):
    """Убирает оборванный тег в конце и закрывает все незакрытые HTML-теги Telegram."""
    last_lt = fragment.rfind('<')
    last_gt = fragment.rfind('>')
    if last_lt > last_gt:
        fragment = fragment[:last_lt]
    last_amp = fragment.rfind('&')
    if last_amp != -1 and ';' not in fragment[last_amp:] and len(fragment) - last_amp <= 10:
        fragment = fragment[:last_amp]

    stack = []
    for m in _HTML_TAG_RE.finditer(fragment):
        is_close, tag = m.group(1) == '/', m.group(2).lower()
        if not is_close:
            stack.append(tag)
        elif stack and stack[-1] == tag:
            stack.pop()
    closing = ''.join(f'</{t}>' for t in reversed(stack))
    return fragment + closing, list(stack)


def truncate_html_safe(html_text, max_len=1024):
    text = str(html_text or '')
    if len(text) <= max_len:
        return text
    reserve = 40
    cut_limit = max(1, max_len - reserve)
    balanced, _ = _balance_html_fragment(text[:cut_limit])
    if len(balanced) + 1 <= max_len:
        return balanced + '…'
    return balanced[:max_len]


def safe_edit_message_text(text, chat_id, message_id, reply_markup=None, parse_mode='HTML', fallback_send=True):
    try:
        return bot.edit_message_text(
            text,
            chat_id,
            message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except ApiTelegramException as e:
        low_err = str(e).lower()
        if 'message is not modified' in low_err:
            return None
        if 'there is no text in the message to edit' in low_err and len(str(text or '')) <= 1024:
            try:
                return bot.edit_message_caption(
                    caption=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except ApiTelegramException as e_cap:
                if 'message is not modified' in str(e_cap).lower():
                    return None
            except Exception:
                pass
        if fallback_send:
            try:
                return send_long_html_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception:
                return None
        return None
    except Exception:
        if fallback_send:
            try:
                return send_long_html_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception:
                return None
        return None


def send_long_html_message(chat_id, text, reply_markup=None, parse_mode='HTML'):
    chunks = split_long_html_message(text, max_len=3900)
    last_msg = None
    for idx, ch in enumerate(chunks):
        kb = reply_markup if idx == len(chunks) - 1 else None
        last_msg = bot.send_message(
            chat_id,
            ch,
            reply_markup=kb,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    return last_msg



_TG_DOC_URL_CACHE = {}
_TG_URL_TO_FILE_ID = {}
_TG_DOC_URL_CACHE_LOCK = threading.Lock()


def _save_hw_permanent_tg_ids(hw_id, new_photo_ids=None, new_doc_id=None):
    if not hw_id:
        return
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            if new_photo_ids is not None:
                cursor.execute(
                    'UPDATE homeworks SET photo_file_id = ? WHERE id = ?',
                    (serialize_photo_ids(new_photo_ids), int(hw_id)),
                )
            if new_doc_id:
                cursor.execute(
                    'UPDATE homeworks SET doc_file_id = ? WHERE id = ?',
                    (str(new_doc_id), int(hw_id)),
                )
    except Exception as e:
        print(f'Ошибка сохранения постоянных TG file_id для ДЗ {hw_id}: {e}')


def _prepare_tg_doc_payload(doc_file_id, doc_file_name=''):
    """Если doc_file_id — это URL (например, документ из VK), сначала проверяет кэш
    постоянных Telegram file_id, а если его нет — скачивает байты в BytesIO."""
    if not doc_file_id:
        return doc_file_id
    val = str(doc_file_id).strip()
    if not val.startswith(('http://', 'https://')):
        return val
    with _TG_DOC_URL_CACHE_LOCK:
        cached_fid = _TG_URL_TO_FILE_ID.get(val)
    if cached_fid:
        return cached_fid
    now_ts = time.time()
    with _TG_DOC_URL_CACHE_LOCK:
        # Очистка устаревших записей (> 15 мин)
        stale = [k for k, (ts, _) in _TG_DOC_URL_CACHE.items() if now_ts - ts > 900]
        for k in stale:
            _TG_DOC_URL_CACHE.pop(k, None)
        cached = _TG_DOC_URL_CACHE.get(val)
    if cached is not None:
        bio = io.BytesIO(cached[1])
        bio.name = doc_file_name or 'document.bin'
        return bio
    try:
        req = urllib.request.Request(val, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(25 * 1024 * 1024)  # лимит 25 МБ
        with _TG_DOC_URL_CACHE_LOCK:
            if len(_TG_DOC_URL_CACHE) > 20:
                _TG_DOC_URL_CACHE.clear()
            _TG_DOC_URL_CACHE[val] = (now_ts, raw)
        bio = io.BytesIO(raw)
        bio.name = doc_file_name or 'document.bin'
        return bio
    except Exception as e:
        print(f'Ошибка скачивания документа по URL для TG: {e}')
        return val


def _prepare_tg_photo_payload(pid, idx=0):
    """Если photo_id — это URL (например, фото из VK), скачивает его байтами в BytesIO
    с кэшированием в памяти, чтобы Telegram не падал на CDN/редиректах VK."""
    if not pid:
        return pid
    val = str(pid).strip()
    if not val.startswith(('http://', 'https://')):
        return val
    return _prepare_tg_doc_payload(val, f'photo_{idx + 1}.jpg')


def _remember_sent_media(hw_id, raw_photo_ids, raw_doc_id, sent_photo_msgs=None, sent_doc_msg=None):
    updated_photos = None
    if raw_photo_ids and sent_photo_msgs and any(str(p).startswith(('http://', 'https://')) for p in raw_photo_ids):
        new_pids = []
        changed = False
        for idx, orig in enumerate(raw_photo_ids):
            fid = orig
            if idx < len(sent_photo_msgs) and getattr(sent_photo_msgs[idx], 'photo', None):
                fid = sent_photo_msgs[idx].photo[-1].file_id
                if str(orig).startswith(('http://', 'https://')):
                    with _TG_DOC_URL_CACHE_LOCK:
                        _TG_URL_TO_FILE_ID[str(orig).strip()] = fid
                    changed = True
            new_pids.append(fid)
        if changed:
            updated_photos = new_pids

    updated_doc = None
    if raw_doc_id and str(raw_doc_id).startswith(('http://', 'https://')) and sent_doc_msg and getattr(sent_doc_msg, 'document', None):
        updated_doc = sent_doc_msg.document.file_id
        with _TG_DOC_URL_CACHE_LOCK:
            _TG_URL_TO_FILE_ID[str(raw_doc_id).strip()] = updated_doc

    if hw_id and (updated_photos is not None or updated_doc):
        _save_hw_permanent_tg_ids(hw_id, new_photo_ids=updated_photos, new_doc_id=updated_doc)


def cache_vk_media_to_tg_async(hw_id, photo_urls, doc_url='', doc_name=''):
    """В фоне тихо загружает фото/документ из VK в Telegram, чтобы сразу сохранить
    постоянный file_id в БД homeworks (защита от протухания ссылок VK CDN)."""
    if not hw_id or (not photo_urls and not doc_url):
        return

    def _worker():
        cache_chat_env = (os.getenv('TG_MEDIA_CACHE_CHAT_ID') or '').strip()
        target_chat_id = None
        if cache_chat_env and cache_chat_env.lstrip('-').isdigit():
            target_chat_id = int(cache_chat_env)
        else:
            try:
                with db_connection() as conn:
                    row = conn.execute(
                        'SELECT user_id FROM users WHERE LOWER(username) = ? LIMIT 1',
                        (ADMIN_USERNAME.lower(),),
                    ).fetchone()
                if row:
                    target_chat_id = int(row[0])
            except Exception:
                pass
        if not target_chat_id:
            return

        new_photo_fids = []
        for idx, p_url in enumerate(photo_urls or []):
            if not str(p_url).startswith(('http://', 'https://')):
                new_photo_fids.append(p_url)
                continue
            try:
                payload = _prepare_tg_photo_payload(p_url, idx)
                m = bot.send_photo(target_chat_id, payload, disable_notification=True)
                if getattr(m, 'photo', None):
                    fid = m.photo[-1].file_id
                    new_photo_fids.append(fid)
                    with _TG_DOC_URL_CACHE_LOCK:
                        _TG_URL_TO_FILE_ID[str(p_url).strip()] = fid
                else:
                    new_photo_fids.append(p_url)
                try:
                    bot.delete_message(target_chat_id, m.message_id)
                except Exception:
                    pass
            except Exception as e:
                print(f'Ошибка кэширования VK-фото в TG (hw {hw_id}): {e}')
                new_photo_fids.append(p_url)

        new_doc_fid = None
        if doc_url and str(doc_url).startswith(('http://', 'https://')):
            try:
                payload = _prepare_tg_doc_payload(doc_url, doc_name)
                m = bot.send_document(target_chat_id, payload, disable_notification=True)
                if getattr(m, 'document', None):
                    new_doc_fid = m.document.file_id
                    with _TG_DOC_URL_CACHE_LOCK:
                        _TG_URL_TO_FILE_ID[str(doc_url).strip()] = new_doc_fid
                try:
                    bot.delete_message(target_chat_id, m.message_id)
                except Exception:
                    pass
            except Exception as e:
                print(f'Ошибка кэширования VK-документа в TG (hw {hw_id}): {e}')

        if (photo_urls and new_photo_fids != list(photo_urls)) or new_doc_fid:
            _save_hw_permanent_tg_ids(
                hw_id,
                new_photo_ids=(new_photo_fids if photo_urls else None),
                new_doc_id=new_doc_fid,
            )

    threading.Thread(target=_worker, name=f'VKMediaCache_{hw_id}', daemon=True).start()


def send_hw_media(chat_id, photo_ids, caption_html, reply_markup=None, doc_file_id='', doc_file_name='', hw_id=None):
    raw_photo_ids = list(photo_ids or [])
    raw_doc_id = doc_file_id
    has_doc = bool(doc_file_id)
    if has_doc:
        doc_file_id = _prepare_tg_doc_payload(doc_file_id, doc_file_name)
    if photo_ids:
        photo_ids = [_prepare_tg_photo_payload(p, i) for i, p in enumerate(photo_ids)]
    if not photo_ids and not has_doc:
        send_long_html_message(chat_id, caption_html, reply_markup=reply_markup, parse_mode='HTML')
        return

    caption_fits = len(caption_html) <= 1024

    if has_doc and not photo_ids:
        if caption_fits:
            d_msg = bot.send_document(
                chat_id,
                doc_file_id,
                caption=caption_html,
                reply_markup=reply_markup,
                parse_mode='HTML',
            )
        else:
            d_msg = bot.send_document(
                chat_id,
                doc_file_id,
                caption=f'📎 <b>Прикреплённый файл:</b> {esc(doc_file_name or "Документ")}',
                parse_mode='HTML',
            )
            send_long_html_message(chat_id, caption_html, reply_markup=reply_markup, parse_mode='HTML')
        _remember_sent_media(hw_id, raw_photo_ids, raw_doc_id, sent_doc_msg=d_msg)
        return

    if len(photo_ids) == 1 and not has_doc:
        if caption_fits:
            p_msg = bot.send_photo(
                chat_id,
                photo_ids[0],
                caption=caption_html,
                reply_markup=reply_markup,
                parse_mode='HTML',
            )
        else:
            p_msg = bot.send_photo(chat_id, photo_ids[0])
            send_long_html_message(chat_id, caption_html, reply_markup=reply_markup, parse_mode='HTML')
        _remember_sent_media(hw_id, raw_photo_ids, raw_doc_id, sent_photo_msgs=[p_msg])
        return

    if len(photo_ids) == 1 and has_doc:
        if caption_fits:
            p_msg = bot.send_photo(
                chat_id,
                photo_ids[0],
                caption=caption_html,
                parse_mode='HTML',
            )
            d_msg = bot.send_document(
                chat_id,
                doc_file_id,
                caption=f'📎 <b>Прикреплённый файл:</b> {esc(doc_file_name or "Документ")}',
                reply_markup=reply_markup,
                parse_mode='HTML',
            )
        else:
            p_msg = bot.send_photo(chat_id, photo_ids[0])
            d_msg = bot.send_document(
                chat_id,
                doc_file_id,
                caption=f'📎 <b>Прикреплённый файл:</b> {esc(doc_file_name or "Документ")}',
                parse_mode='HTML',
            )
            send_long_html_message(chat_id, caption_html, reply_markup=reply_markup, parse_mode='HTML')
        _remember_sent_media(hw_id, raw_photo_ids, raw_doc_id, sent_photo_msgs=[p_msg], sent_doc_msg=d_msg)
        return

    media = []
    for idx, pid in enumerate(photo_ids):
        if idx == 0 and caption_fits:
            media.append(InputMediaPhoto(pid, caption=caption_html, parse_mode='HTML'))
        else:
            media.append(InputMediaPhoto(pid))
    p_msgs = bot.send_media_group(chat_id, media)
    d_msg = None
    if has_doc:
        d_msg = bot.send_document(
            chat_id,
            doc_file_id,
            caption=f'📎 <b>Прикреплённый файл:</b> {esc(doc_file_name or "Документ")}',
            reply_markup=(reply_markup if caption_fits else None),
            parse_mode='HTML',
        )
    _remember_sent_media(hw_id, raw_photo_ids, raw_doc_id, sent_photo_msgs=p_msgs, sent_doc_msg=d_msg)
    if not caption_fits:
        send_long_html_message(chat_id, caption_html, reply_markup=reply_markup, parse_mode='HTML')
    elif not has_doc and reply_markup:
        bot.send_message(
            chat_id,
            '⚙️ <i>Управление записью выше:</i>',
            reply_markup=reply_markup,
            parse_mode='HTML',
        )


def _direct_send_message(user_id, text, reply_markup=None, parse_mode='HTML'):
    try:
        bot.send_message(
            user_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return 'ok', 0
    except ApiTelegramException as e:
        if e.error_code == 403:
            with db_connection() as conn:
                cursor = conn.cursor()
                if int(user_id) < 0:
                    cursor.execute('DELETE FROM group_chats WHERE chat_id = ?', (user_id,))
                else:
                    cursor.execute(
                        'UPDATE users SET subscribed = 0, sub_morning = 0, sub_evening = 0, notify_updates = 0, is_blocked = 1 WHERE user_id = ?',
                        (user_id,),
                    )
            invalidate_user_cache(user_id)
            return 'blocked', 0
        elif e.error_code == 429:
            retry_after = int(getattr(e, 'result_json', {}).get('parameters', {}).get('retry_after', 2))
            return 'retry', retry_after
        return 'error', 0
    except Exception:
        return 'error', 0


def _direct_send_hw_media(user_id, photo_ids, caption_html, reply_markup=None, doc_file_id='', doc_file_name='', hw_id=None):
    try:
        send_hw_media(
            user_id,
            photo_ids,
            caption_html,
            reply_markup=reply_markup,
            doc_file_id=doc_file_id,
            doc_file_name=doc_file_name,
            hw_id=hw_id,
        )
        return 'ok', 0
    except ApiTelegramException as e:
        if e.error_code == 403:
            with db_connection() as conn:
                cursor = conn.cursor()
                if int(user_id) < 0:
                    cursor.execute('DELETE FROM group_chats WHERE chat_id = ?', (user_id,))
                else:
                    cursor.execute(
                        'UPDATE users SET subscribed = 0, sub_morning = 0, sub_evening = 0, notify_updates = 0, is_blocked = 1 WHERE user_id = ?',
                        (user_id,),
                    )
            invalidate_user_cache(user_id)
            return 'blocked', 0
        elif e.error_code == 429:
            retry_after = int(getattr(e, 'result_json', {}).get('parameters', {}).get('retry_after', 2))
            return 'retry', retry_after
        return 'error', 0
    except Exception:
        return 'error', 0


def split_long_html_message(text, max_len=3900):
    if len(text) <= max_len:
        return [text]

    raw_parts = []
    blocks = text.split('\n\n')
    current = ''
    safe_slice_len = max(100, max_len - 80)

    def _push_long_line(line_str):
        nonlocal current
        rem = line_str
        while len(rem) > safe_slice_len:
            cut = rem.rfind(' ', 0, safe_slice_len)
            if cut < safe_slice_len // 2:
                cut = safe_slice_len
            raw_parts.append(rem[:cut])
            rem = rem[cut:].lstrip()
        current = rem

    for block in blocks:
        candidate = f'{current}\n\n{block}' if current else block
        if len(candidate) <= safe_slice_len:
            current = candidate
        else:
            if current:
                raw_parts.append(current)
                current = ''
            if len(block) <= safe_slice_len:
                current = block
            else:
                for line in block.split('\n'):
                    cand_line = f'{current}\n{line}' if current else line
                    if len(cand_line) <= safe_slice_len:
                        current = cand_line
                    else:
                        if current:
                            raw_parts.append(current)
                            current = ''
                        if len(line) <= safe_slice_len:
                            current = line
                        else:
                            _push_long_line(line)
    if current:
        raw_parts.append(current)

    balanced_parts = []
    carry_open_tags = []
    for part in raw_parts:
        prefix = ''.join(f'<{t}>' for t in carry_open_tags)
        balanced, open_after = _balance_html_fragment(prefix + part)
        balanced_parts.append(balanced)
        carry_open_tags = open_after
    return balanced_parts


def get_pair_num(time_slot):
    clean_slot = time_slot.strip()
    if clean_slot in PAIR_NUMBERS:
        return PAIR_NUMBERS[clean_slot]
    for slot, num in PAIR_NUMBERS.items():
        if clean_slot.startswith(slot[:5]):
            return num
    return None


def format_pair_prefix(time_slot):
    num = get_pair_num(time_slot)
    if num:
        emoji = PAIR_EMOJIS.get(num, f'{num}.')
        return f'{emoji} <b>{num} пара</b> <code>({time_slot})</code>'
    return f'⏰ <code>{esc(time_slot)}</code>'


def is_admin_user(user_obj):
    return bool(user_obj and user_obj.username and user_obj.username.lower() == ADMIN_USERNAME.lower())


_USER_DATA_CACHE = {}
_USER_USERNAME_CACHE = {}
_USER_LAST_ACTIVE_CACHE = {}
_USER_CACHE_LOCK = threading.RLock()


def invalidate_user_cache(user_id=None):
    with _USER_CACHE_LOCK:
        if user_id is None:
            _USER_DATA_CACHE.clear()
            _USER_USERNAME_CACHE.clear()
        else:
            uid = int(user_id)
            _USER_DATA_CACHE.pop(uid, None)
            _USER_USERNAME_CACHE.pop(uid, None)


_ADMIN_COMMANDS_SYNCED_IDS = set()


def sync_admin_personal_commands(admin_user_id):
    uid = int(admin_user_id)
    if uid in _ADMIN_COMMANDS_SYNCED_IDS:
        return
    try:
        bot.set_my_commands(
            [
                BotCommand('start', 'Перезапустить бота / главное меню'),
                BotCommand('starosta', 'Выдать роль старосты (админ)'),
                BotCommand('unstarosta', 'Снять роль старосты (админ)'),
                BotCommand('stats', 'Статистика бота (админ)'),
                BotCommand('broadcast', 'Массовая рассылка объявлений (админ)'),
                BotCommand('backup', 'Скачать резервную копию БД (админ)'),
            ],
            scope=BotCommandScopeChat(uid),
        )
        _ADMIN_COMMANDS_SYNCED_IDS.add(uid)
    except Exception:
        pass


def sync_username(user_obj):
    if not user_obj:
        return
    uid = int(user_obj.id)
    uname = (user_obj.username or '').lower()
    is_admin = 1 if uname == ADMIN_USERNAME.lower() else 0
    if is_admin:
        sync_admin_personal_commands(uid)
    now_ts = time.time()
    need_db_update = False
    with _USER_CACHE_LOCK:
        cached_uname = _USER_USERNAME_CACHE.get(uid)
        last_act = _USER_LAST_ACTIVE_CACHE.get(uid, 0.0)
        if cached_uname != uname:
            need_db_update = True
        elif is_admin:
            u_data = _USER_DATA_CACHE.get(uid)
            if not (u_data and u_data[3] == 1 and u_data[4] == 1):
                need_db_update = True
        if now_ts - last_act >= 300:
            need_db_update = True
    if not need_db_update:
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        if is_admin:
            cursor.execute(
                'UPDATE users SET username = ?, is_starosta = 1, is_hw_manager = 1, last_active = ?, is_blocked = 0, '
                'created_at = CASE WHEN COALESCE(created_at, 0) = 0 THEN ? ELSE created_at END '
                'WHERE user_id = ?',
                (uname, now_ts, now_ts, uid),
            )
        else:
            cursor.execute(
                'UPDATE users SET username = ?, last_active = ?, is_blocked = 0, '
                'created_at = CASE WHEN COALESCE(created_at, 0) = 0 THEN ? ELSE created_at END '
                'WHERE user_id = ?',
                (uname, now_ts, now_ts, uid),
            )
    with _USER_CACHE_LOCK:
        _USER_USERNAME_CACHE[uid] = uname
        _USER_LAST_ACTIVE_CACHE[uid] = now_ts
        if cached_uname != uname or is_admin:
            _USER_DATA_CACHE.pop(uid, None)


def get_current_week_and_day(target_date=None, faculty_code=None):
    """Подсчёт учебных недель от старта учебного года (строго 1 сентября).
    Смена номера недели происходит по понедельникам; 1 сентября следующего года счётчик обнуляется."""
    if target_date is None:
        target_date = now_local()
    day_name = DAYS_RUS[target_date.weekday()]
    week_number = calc_week_number_for_start(target_date, ACADEMIC_YEAR_START_DATE)
    return week_number, day_name


def get_user_data(user_id):
    uid = int(user_id)
    with _USER_CACHE_LOCK:
        if uid in _USER_DATA_CACHE:
            return _USER_DATA_CACHE[uid]
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT course, group_name, subscribed, is_starosta, is_hw_manager, notify_updates, managed_group, faculty '
            'FROM users WHERE user_id = ?',
            (uid,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    fac = row[7] if row[7] else infer_faculty_by_group(row[1])
    res = (row[0], row[1], row[2], row[3], row[4], row[5], row[6], fac)
    with _USER_CACHE_LOCK:
        _USER_DATA_CACHE[uid] = res
    return res


def find_course_and_faculty_by_group(group_name):
    for fac_code, courses in FACULTY_STRUCTURE.items():
        for crs, groups in courses.items():
            if group_name in groups:
                return crs, fac_code
    return None, None


def find_course_by_group(group_name):
    crs, _ = find_course_and_faculty_by_group(group_name)
    return crs


def get_managed_context(user_obj):
    data = get_user_data(user_obj.id)
    if not data:
        return None, None, None
    course, group_name, _, is_st, _, _, managed_group, fac_code = data
    if is_admin_user(user_obj):
        return course, group_name, fac_code
    if is_st == 1 and managed_group:
        target_grp = managed_group
        target_crs, target_fac = find_course_and_faculty_by_group(target_grp)
        return target_crs or course, target_grp, target_fac or fac_code
    return course, group_name, fac_code


def is_user_starosta(user_obj):
    if is_admin_user(user_obj):
        return True
    data = get_user_data(user_obj.id)
    return bool(data and data[3] == 1)


def can_manage_hw(user_obj):
    if is_admin_user(user_obj):
        return True
    data = get_user_data(user_obj.id)
    return bool(data and is_group_valid(data[7], data[0], data[1]))


def get_main_keyboard(subscribed=False, is_starosta=False, is_hw_manager=False, session_mode=False):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    if session_mode:
        markup.row(
            KeyboardButton('🎓 Сессия'),
            KeyboardButton('📚 Домашка'),
            KeyboardButton('🔍 Поиск'),
        )
    else:
        markup.row(
            KeyboardButton('📅 Сегодня'),
            KeyboardButton('⏭ Завтра'),
            KeyboardButton('🗓 Неделя'),
        )
        markup.row(
            KeyboardButton('📚 Домашка'),
            KeyboardButton('🔍 Поиск'),
        )
    if is_starosta:
        markup.row(
            KeyboardButton('⚙️ Настройки'),
            KeyboardButton('🛠 Спецвозможности'),
        )
    else:
        markup.row(KeyboardButton('⚙️ Настройки'))
    return markup


def get_role_label(user_obj):
    if is_admin_user(user_obj):
        return '👑 Администратор'
    if is_user_starosta(user_obj):
        return '🌟 Староста'
    return '🎓 Студент'


def build_faculty_keyboard():
    markup = InlineKeyboardMarkup(row_width=1)
    for fac_code in FACULTY_ORDER:
        meta = FACULTIES_META[fac_code]
        courses = FACULTY_STRUCTURE.get(fac_code, {})
        has_data = any(bool(grps) for grps in courses.values())
        badge = '' if has_data else ' (скоро)'
        markup.add(
            InlineKeyboardButton(
                f'{meta["emoji"]} {meta["short"]} — {meta["full"]}{badge}',
                callback_data=f'sel_fac_{fac_code}',
            )
        )
    return markup


def build_course_keyboard(faculty_code):
    markup = InlineKeyboardMarkup(row_width=1)
    courses = FACULTY_STRUCTURE.get(faculty_code, {})
    for c in sorted(courses.keys()):
        if courses[c]:
            markup.add(InlineKeyboardButton(f'🎓 {c} курс', callback_data=f'sel_course_{faculty_code}_{c}'))
    markup.add(InlineKeyboardButton('🔙 Сменить факультет', callback_data='back_to_faculties'))
    return markup


def get_lesson_type_badge(lesson_type):
    """Возвращает единую иконку занятия."""
    return '📘'


def build_progress_bar(current, total, width=10):
    if total <= 0:
        return '░' * width + ' 0%'
    ratio = max(0.0, min(1.0, float(current) / float(total)))
    filled = int(round(ratio * width))
    pct = int(round(ratio * 100))
    return f'[{"▓" * filled}{"░" * (width - filled)}] {pct}%'


_MASCOT_TG_FILE_IDS = {}
_MASCOT_LOCK = threading.Lock()


def _find_mascot_file(art_name):
    for base in (DATA_DIR, BASE_DIR):
        assets_dir = os.path.join(base, 'assets')
        for ext in ('.png', '.jpg', '.jpeg', '.webp'):
            candidate = os.path.join(assets_dir, art_name + ext)
            if os.path.isfile(candidate):
                return candidate
    return None


def send_mascot_art(chat_id, art_name, caption_html, reply_markup=None):
    """Отправляет иллюстрацию маскота (куницы) из папки assets/ с подписью и кэшированием file_id.
    Если файла нет или подпись длиннее 1024 символов — аккуратно отправляет обычным текстом."""
    if len(str(caption_html or '')) <= 1024:
        with _MASCOT_LOCK:
            cached_fid = _MASCOT_TG_FILE_IDS.get(art_name)
        if cached_fid:
            try:
                return bot.send_photo(
                    chat_id,
                    cached_fid,
                    caption=caption_html,
                    reply_markup=reply_markup,
                    parse_mode='HTML',
                )
            except Exception:
                with _MASCOT_LOCK:
                    _MASCOT_TG_FILE_IDS.pop(art_name, None)

        fpath = _find_mascot_file(art_name)
        if fpath:
            try:
                with open(fpath, 'rb') as img_f:
                    m = bot.send_photo(
                        chat_id,
                        img_f,
                        caption=caption_html,
                        reply_markup=reply_markup,
                        parse_mode='HTML',
                    )
                if getattr(m, 'photo', None):
                    with _MASCOT_LOCK:
                        _MASCOT_TG_FILE_IDS[art_name] = m.photo[-1].file_id
                return m
            except Exception as e_art:
                print(f'Ошибка отправки арта маскота {art_name}: {e_art}')

    return send_long_html_message(chat_id, caption_html, reply_markup=reply_markup, parse_mode='HTML')


def start_onboarding(message):
    sync_username(message.from_user)
    clear_user_state(message.chat.id)
    send_mascot_art(
        message.chat.id,
        'marten_welcome',
        '🐾 <b>Привет! Я — Яша!</b>\n'
        'Слежу за расписанием, заменами, домашкой и сессией <i>Сибайского института (филиала) УУНиТ</i>.\n'
        f'<i>by @{ADMIN_USERNAME}</i>\n\n'
        '👇 Для начала выбери свой <b>факультет</b>:',
        reply_markup=build_faculty_keyboard(),
    )


def resolve_target_user(message):
    parts = message.text.split()
    if message.reply_to_message:
        t_id = message.reply_to_message.from_user.id
        t_disp = (
            f'@{message.reply_to_message.from_user.username}'
            if message.reply_to_message.from_user.username
            else str(t_id)
        )
        return t_id, t_disp, None
    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.startswith('@'):
            uname_search = arg[1:].lower()
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT user_id FROM users WHERE LOWER(username) = ?',
                    (uname_search,),
                )
                row = cursor.fetchone()
            if row:
                return row[0], arg, None
            return None, arg, 'not_found'
        if arg.isdigit():
            return int(arg), f'ID {arg}', None
    return None, '', 'no_args'


def send_db_backup(manual_chat_id=None):
    if not os.path.exists(DB_PATH):
        if manual_chat_id:
            bot.send_message(manual_chat_id, '⚠️ Файл базы данных schedule.db не найден.')
        return

    if manual_chat_id:
        target_ids = [manual_chat_id]
    else:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT user_id FROM users WHERE LOWER(username) = ?',
                (ADMIN_USERNAME.lower(),),
            )
            target_ids = [r[0] for r in cursor.fetchall()]

    if not target_ids:
        return

    stamp = now_local().strftime('%d.%m.%Y %H:%M')
    caption = f'💾 <b>Резервная копия БД schedule.db</b>\n🕒 <i>{stamp} (UTC+5)</i>'

    tmp_backup_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_f:
            tmp_backup_path = tmp_f.name

        src_conn = sqlite3.connect(DB_PATH, timeout=30.0)
        dst_conn = sqlite3.connect(tmp_backup_path)
        try:
            src_conn.execute('PRAGMA busy_timeout = 30000')
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
            src_conn.close()

        for uid in target_ids:
            try:
                with open(tmp_backup_path, 'rb') as db_file:
                    bot.send_document(
                        uid,
                        db_file,
                        visible_file_name=f'schedule_backup_{now_local().strftime("%Y%m%d_%H%M")}.db',
                        caption=caption,
                        parse_mode='HTML',
                    )
            except Exception as e:
                print(f'Ошибка отправки бэкапа БД пользователю {uid}: {e}')
    except Exception as e:
        print(f'Ошибка создания консистентного бэкапа SQLite: {e}')
        if manual_chat_id:
            bot.send_message(manual_chat_id, f'❌ Ошибка при создании бэкапа БД: <code>{esc(e)}</code>', parse_mode='HTML')
    finally:
        if tmp_backup_path and os.path.exists(tmp_backup_path):
            try:
                os.remove(tmp_backup_path)
            except Exception:
                pass


_BOT_USERNAME_CACHE = None


def get_bot_username():
    global _BOT_USERNAME_CACHE
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE
    try:
        me = bot.get_me()
        if me and me.username:
            _BOT_USERNAME_CACHE = me.username
            return _BOT_USERNAME_CACHE
    except Exception:
        pass
    return ''

def get_user_mailing_prefs(user_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT COALESCE(sub_morning, subscribed, 0), COALESCE(sub_evening, subscribed, 0) '
            'FROM users WHERE user_id = ?',
            (int(user_id),),
        )
        row = cursor.fetchone()
    if not row:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def get_group_chat_binding(chat_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT faculty, course, group_name FROM group_chats WHERE chat_id = ?',
            (int(chat_id),),
        )
        return cursor.fetchone()


def get_bound_group_chats(faculty_code, group_name):
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT chat_id FROM group_chats WHERE (faculty = ? OR faculty = "") AND group_name = ?',
            (eff_fac, group_name),
        )
        return [r[0] for r in cursor.fetchall()]

def log_stat_event(action):
    try:
        d_str = now_local().strftime('%Y-%m-%d')
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO daily_stats (date_str, action, count) VALUES (?, ?, 1) '
                'ON CONFLICT(date_str, action) DO UPDATE SET count = count + 1',
                (d_str, str(action)),
            )
    except Exception:
        pass

def is_extra_hw_lists_enabled(faculty_code, group_name):
    if not group_name:
        return False
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT extra_hw_lists FROM group_settings '
                'WHERE (faculty = ? OR faculty = "") AND group_name = ? LIMIT 1',
                (eff_fac, group_name),
            )
            row = cursor.fetchone()
        return bool(row and row[0] == 1)
    except Exception:
        return False


def set_extra_hw_lists_enabled(faculty_code, group_name, enabled):
    if not group_name:
        return
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    val = 1 if enabled else 0
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO group_settings (faculty, group_name, extra_hw_lists, session_mode) '
            'VALUES (?, ?, ?, 0) '
            'ON CONFLICT(faculty, group_name) DO UPDATE SET extra_hw_lists = excluded.extra_hw_lists',
            (eff_fac, group_name, val),
        )


def is_session_mode_enabled(faculty_code, group_name):
    if not group_name:
        return False
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT COALESCE(session_mode, 0) FROM group_settings '
                'WHERE (faculty = ? OR faculty = "") AND group_name = ? LIMIT 1',
                (eff_fac, group_name),
            )
            row = cursor.fetchone()
        return bool(row and row[0] == 1)
    except Exception:
        return False


def set_session_mode_enabled(faculty_code, group_name, enabled):
    if not group_name:
        return
    eff_fac = faculty_code or infer_faculty_by_group(group_name)
    val = 1 if enabled else 0
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO group_settings (faculty, group_name, extra_hw_lists, session_mode) '
            'VALUES (?, ?, 0, ?) '
            'ON CONFLICT(faculty, group_name) DO UPDATE SET session_mode = excluded.session_mode',
            (eff_fac, group_name, val),
        )
