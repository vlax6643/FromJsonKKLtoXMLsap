# -*- coding: utf-8 -*-
"""
converter.py — Конвертер ККЛ JSON Домино → IDoc WPUBON01 XML SAP
Читает KKL JSON из inbox/, создаёт WPUBON01 XML в outbox/.

Запуск: python converter.py
"""

import io
import os
import sys
import json
import sqlite3
import logging
import shutil
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import configparser


# ---------------------------------------------------------------------------
# Таблицы кодов
# ---------------------------------------------------------------------------

# vat из ккл (строка) → MWSKZ SAP
VAT_MWSKZ = {
    '0':  'B0',
    '10': 'B1',
    '20': 'B2',
    '22': 'E5',
}

# pay_type из ккл → ZAHLART SAP
PAY_ZAHLART = {
    '1': 'PTCS',   # наличные
    '2': 'PTVI',   # безналичные
}

# oper из ккл → знак операции VORZEICHEN в E1WPB02
# Все карты в cards[] являются картами лояльности (социальные, покупателей, сотрудников)
# Банковских карт в cards[] нет — KARTENNR в E1WPB06 всегда пустой
OPER_VORZEICHEN = {
    '01': '-',   # продажа
    '02': '+',   # возврат
}

REQUIRED_KKL_FIELDS  = ('code', 'dpt', 'date', 'register', 'cashier', 'full')
REQUIRED_LINE_FIELDS = ('receipt', 'product_code', 'qnt', 'price', 'sale', 'vat', 'date')


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def parse_kkl_datetime(date_str):
    """
    Разбирает дату ККЛ в (YYYYMMDD, HHMMSS).
    Поддерживает форматы: "ДД/ММ/ГГГГ ЧЧ:ММ" и "ГГГГ-ММ-ДД ЧЧ:ММ:СС".
    Возвращает ('', '') при ошибке.
    """
    if not date_str:
        return '', ''
    s = date_str.strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%d/%m/%Y %H:%M'):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime('%Y%m%d'), dt.strftime('%H%M%S')
        except ValueError:
            continue
    return '', ''


def pad_shop(dpt):
    """'7708' → '0000007708' (10 символов с ведущими нулями)."""
    return str(dpt).strip().zfill(10)


def to_dec(value):
    """Преобразует значение в Decimal. Возвращает Decimal('0') при ошибке."""
    try:
        return Decimal(str(value).strip().replace(',', '.'))
    except InvalidOperation:
        return Decimal('0')


def fmt2(value):
    """Decimal → строка с 2 знаками после точки."""
    return str(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def fmt3(value):
    """Decimal → строка с 3 знаками после точки."""
    return str(value.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP))


def fmt5(value):
    """Decimal → строка с 5 знаками после точки."""
    return str(value.quantize(Decimal('0.00001'), rounding=ROUND_HALF_UP))


def detect_qualartnr(product_code):
    """
    Определяет квалификатор артикула по числовому диапазону.
    Диапазоны внутренних кодов: 100000–399999, 500000–599999, 700000–799999 → ARTN.
    Всё остальное → EANN (штрихкод).
    """
    try:
        num = int(str(product_code).strip())
    except (ValueError, TypeError):
        return 'EANN'
    if (100000 <= num <= 399999 or
            500000 <= num <= 599999 or
            700000 <= num <= 799999):
        return 'ARTN'
    return 'EANN'


def normalize_vat(raw):
    """Нормализует поле vat к строке целого числа: "20.0" → "20", 10 → "10"."""
    try:
        return str(int(float(str(raw).strip())))
    except (ValueError, TypeError):
        return str(raw).strip()


# ---------------------------------------------------------------------------
# Парсинг ККЛ JSON
# ---------------------------------------------------------------------------

def load_kkl(filepath):
    """
    Читает JSON файл с ККЛ.
    Возвращает (list_of_kkl_entries, None) или (None, error_str).
    """
    try:
        with open(filepath, 'rb') as f:
            raw = f.read()
    except IOError as e:
        return None, 'Ошибка чтения файла: {}'.format(e)

    text = None
    for enc in ('utf-8-sig', 'utf-8', 'cp1251'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode('utf-8', errors='replace')

    try:
        data = json.loads(text)
    except ValueError as e:
        return None, 'JSON ParseError: {}'.format(e)

    kkl_list = data.get('kkl')
    if not isinstance(kkl_list, list):
        return None, 'Отсутствует массив "kkl" в корне JSON'

    return kkl_list, None


def validate_kkl_entry(entry, index):
    """Проверяет обязательные поля одной записи ккл. Возвращает список ошибок."""
    errors = []
    for field in REQUIRED_KKL_FIELDS:
        if not str(entry.get(field, '')).strip():
            errors.append('kkl[{}]: отсутствует обязательное поле "{}"'.format(index, field))
    return errors


def group_receipts(kkl_entry):
    """
    Группирует lines[], payments[] и cards[] по номеру чека (поле receipt).
    Возвращает список: [{'receipt': str, 'lines': [...], 'payments': [...], 'cards': [...]}, ...].
    Порядок чеков — по первому появлению в lines[].
    """
    lines_by_receipt = {}
    for line in kkl_entry.get('lines', []):
        r = str(line.get('receipt', '')).strip()
        lines_by_receipt.setdefault(r, []).append(line)

    payments_by_receipt = {}
    for payment in kkl_entry.get('payments', []):
        r = str(payment.get('receipt', '')).strip()
        payments_by_receipt.setdefault(r, []).append(payment)

    cards_by_receipt = {}
    for card in kkl_entry.get('cards', []):
        r = str(card.get('receipt', '')).strip()
        cards_by_receipt.setdefault(r, []).append(card)

    seen = {}
    receipts = []
    for line in kkl_entry.get('lines', []):
        r = str(line.get('receipt', '')).strip()
        if r and r not in seen:
            seen[r] = True
            receipts.append({
                'receipt':  r,
                'lines':    lines_by_receipt.get(r, []),
                'payments': payments_by_receipt.get(r, []),
                'cards':    cards_by_receipt.get(r, []),
            })

    return receipts


# ---------------------------------------------------------------------------
# Построение XML
# ---------------------------------------------------------------------------

def sub(parent, tag, text=None, **attrib):
    """
    Добавляет дочерний элемент к parent.
    text=None → пустой самозакрывающийся тег (<TAG/>).
    """
    el = ET.SubElement(parent, tag, **attrib)
    if text is not None:
        el.text = str(text)
    return el


def build_edi_dc40(idoc_el, kkl_entry, sap_cfg, now):
    edi = sub(idoc_el, 'EDI_DC40', SEGMENT='1')
    sub(edi, 'TABNAM', 'EDI_DC40')
    sub(edi, 'DIRECT', '2')
    sub(edi, 'IDOCTYP', 'WPUBON01')
    sub(edi, 'MESTYP',  'WPUBON')
    sub(edi, 'STDMES',  'WPUBON')
    sub(edi, 'SNDPOR',  sap_cfg['sndpor'])
    sub(edi, 'SNDPRT',  'KU')
    sub(edi, 'SNDPRN',  pad_shop(kkl_entry.get('dpt', '')))
    sub(edi, 'RCVPOR',  sap_cfg['rcvpor'])
    sub(edi, 'RCVPRT',  'KU')
    sub(edi, 'RCVPRN',  sap_cfg['rcvprn'])
    sub(edi, 'CREDAT',  now.strftime('%Y%m%d'))
    sub(edi, 'CRETIM',  now.strftime('%H%M%S'))


def build_e1wpb02(parent, line, warnings, logger):
    product_code = str(line.get('product_code', ''))
    qnt          = to_dec(line.get('qnt',  0))
    sale         = to_dec(line.get('sale', 0))
    vat_str      = normalize_vat(line.get('vat', '0'))
    vat_num      = to_dec(vat_str)
    oper         = str(line.get('oper', '')).strip()
    vorzeichen   = OPER_VORZEICHEN.get(oper)
    if vorzeichen is None:
        msg = 'E1WPB02: product_code={} неизвестный oper={!r}, используется "-"'.format(product_code, oper)
        logger.warning(msg)
        warnings.append(msg)
        vorzeichen = '-'

    # KONDVALUE = sale × qnt, округлить до 2 знаков
    kondvalue_exact = sale * qnt
    kondvalue       = kondvalue_exact.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    zround          = (kondvalue_exact - kondvalue).quantize(
                          Decimal('0.00001'), rounding=ROUND_HALF_UP)

    # MWSBT = sale × qnt × vat / (100 + vat)
    if vat_num > 0:
        mwsbt = (sale * qnt * vat_num / (Decimal('100') + vat_num)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
    else:
        mwsbt = Decimal('0.00')

    mwskz = VAT_MWSKZ.get(vat_str)
    if mwskz is None:
        msg = 'E1WPB02: product_code={} неизвестный vat={!r}'.format(product_code, vat_str)
        logger.warning(msg)
        warnings.append(msg)
        mwskz = '??'

    e2 = sub(parent, 'E1WPB02', SEGMENT='1')
    sub(e2, 'QUALARTNR', detect_qualartnr(product_code))
    sub(e2, 'ARTNR',     product_code)
    sub(e2, 'SERIENNR')               # пустой тег
    sub(e2, 'VORZEICHEN', vorzeichen)
    sub(e2, 'MENGE', fmt3(qnt))

    e3 = sub(e2, 'E1WPB03', SEGMENT='1')
    sub(e3, 'VORZEICHEN', '-')
    sub(e3, 'KONDITION',  'VKP0')
    sub(e3, 'KONDVALUE',  fmt2(kondvalue))
    sub(e3, 'CONDID20')               # пустой тег
    sub(e3, 'ZROUND',     fmt5(zround))

    e4 = sub(e2, 'E1WPB04', SEGMENT='1')
    sub(e4, 'MWSKZ', mwskz)
    sub(e4, 'MWSBT', fmt2(mwsbt))


def build_e1wpb06(parent, payment, warnings, logger):
    pay_type = str(payment.get('pay_type', '')).strip()
    zahlart  = PAY_ZAHLART.get(pay_type)
    if zahlart is None:
        msg = 'E1WPB06: неизвестный pay_type={!r}'.format(pay_type)
        logger.warning(msg)
        warnings.append(msg)
        zahlart = '????'

    e6 = sub(parent, 'E1WPB06', SEGMENT='1')
    sub(e6, 'VORZEICHEN')
    sub(e6, 'ZAHLART', zahlart)
    sub(e6, 'SUMME',   fmt2(to_dec(payment.get('sum', '0'))))
    sub(e6, 'CCINS')
    sub(e6, 'KARTENNR')
    sub(e6, 'GUELTBIS')
    sub(e6, 'AUTORINR')


def build_e1wpb01(idoc_el, receipt_data, kkl_entry, warnings, logger):
    """Добавляет один сегмент <E1WPB01> (чек) внутрь <IDOC>."""
    e1wpb01 = sub(idoc_el, 'E1WPB01', SEGMENT='1')

    # Дата/время берём из первой строки чека, иначе из шапки ккл
    first_line  = receipt_data['lines'][0] if receipt_data['lines'] else {}
    line_date   = first_line.get('date') or kkl_entry.get('date', '')
    vorgdatum, vorgzeit = parse_kkl_datetime(line_date)

    receipt_cards = receipt_data.get('cards', [])
    loyalty_card  = receipt_cards[0].get('card', '') if receipt_cards else ''

    sub(e1wpb01, 'KASSID',    kkl_entry.get('register', ''))
    sub(e1wpb01, 'VORGDATUM', vorgdatum)
    sub(e1wpb01, 'VORGZEIT',  vorgzeit)
    sub(e1wpb01, 'BONNUMMER', receipt_data['receipt'])
    sub(e1wpb01, 'KASSIERER', kkl_entry.get('cashier', ''))
    sub(e1wpb01, 'CSHNAME',   kkl_entry.get('cashier', ''))
    sub(e1wpb01, 'ZNOCARD',   loyalty_card or None)

    for line in receipt_data['lines']:
        build_e1wpb02(e1wpb01, line, warnings, logger)

    for payment in receipt_data['payments']:
        build_e1wpb06(e1wpb01, payment, warnings, logger)


def build_xml(kkl_entry, receipts, sap_cfg, now, warnings, logger):
    """Строит XML дерево WPUBON01: один <IDOC> на ккл, все чеки как <E1WPB01> внутри."""
    root    = ET.Element('WPUBON01')
    idoc_el = ET.SubElement(root, 'IDOC')
    idoc_el.set('BEGIN', '1')
    build_edi_dc40(idoc_el, kkl_entry, sap_cfg, now)
    for receipt_data in receipts:
        build_e1wpb01(idoc_el, receipt_data, kkl_entry, warnings, logger)
    return root


def indent_xml(elem, level=0):
    """Добавляет отступы для читаемого XML (Python 3.6 совместимо)."""
    pad = '\n' + '  ' * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + '  '
        for child in elem:
            indent_xml(child, level + 1)
        last = elem[-1]
        if not last.tail or not last.tail.strip():
            last.tail = pad
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = pad


def write_xml(root, filepath):
    """Записывает XML с декларацией кодировки."""
    indent_xml(root)
    ET.ElementTree(root).write(filepath, encoding='utf-8', xml_declaration=True)


# ---------------------------------------------------------------------------
# Журнал SQLite
# ---------------------------------------------------------------------------

def init_journal(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS journal (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kkl_filename    TEXT    NOT NULL,
            kkl_code        TEXT,
            kkl_date        TEXT,
            register        TEXT,
            full            TEXT,
            receipts_count  INTEGER,
            out_filename    TEXT,
            processed_at    TEXT,
            status          TEXT,
            warnings        TEXT,
            error_message   TEXT
        )
    ''')
    conn.commit()
    return conn


def is_already_processed(conn, filename):
    """Возвращает True если файл уже обработан (status=OK или WARNING)."""
    cur = conn.execute(
        "SELECT id FROM journal WHERE kkl_filename = ? AND status IN ('OK', 'WARNING')",
        (filename,)
    )
    return cur.fetchone() is not None


def write_journal(conn, kkl_filename, status,
                  kkl_code=None, kkl_date=None, register=None, full=None,
                  receipts_count=None, out_filename=None,
                  warnings=None, error_message=None):
    """Добавляет запись в журнал."""
    conn.execute('''
        INSERT INTO journal
            (kkl_filename, kkl_code, kkl_date, register, full,
             receipts_count, out_filename, processed_at, status, warnings, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        kkl_filename,
        kkl_code,
        kkl_date,
        register,
        full,
        receipts_count,
        out_filename,
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        status,
        '\n'.join(warnings) if warnings else None,
        error_message,
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

def load_config(config_path):
    """
    Читает config.ini, возвращает (paths_dict, sap_dict).
    Пути могут быть абсолютными или относительными от папки скрипта.
    """
    base = os.path.dirname(os.path.abspath(config_path))

    path_defaults = {
        'inbox':     os.path.join(base, 'inbox'),
        'outbox':    os.path.join(base, 'outbox'),
        'processed': os.path.join(base, 'processed'),
        'journal':   os.path.join(base, 'journal.db'),
        'log':       os.path.join(base, 'logs', 'converter.log'),
    }
    sap_defaults = {
        'sndpor': 'SAPVOG',
        'rcvpor': 'SAPVOG',
        'rcvprn': 'SAP',
    }

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding='utf-8')

    paths = {}
    for key, default in path_defaults.items():
        raw = cfg.get('paths', key, fallback=default).strip()
        if not os.path.isabs(raw):
            raw = os.path.join(base, raw)
        paths[key] = raw

    try:
        paths['retention_days'] = int(cfg.get('paths', 'retention_days', fallback='0'))
    except ValueError:
        paths['retention_days'] = 0

    sap_cfg = {}
    for key, default in sap_defaults.items():
        sap_cfg[key] = cfg.get('sap', key, fallback=default).strip()

    return paths, sap_cfg


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

def setup_logging(log_path):
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger('converter')
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    stdout_utf8 = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    ch = logging.StreamHandler(stdout_utf8)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Обработка одного файла
# ---------------------------------------------------------------------------

def process_file(filepath, paths, sap_cfg, conn, logger):
    """
    Полный цикл обработки одного ККЛ JSON файла:
      1. Проверка дубля в журнале
      2. Парсинг JSON
      3. Группировка чеков по receipt
      4. Построение XML IDoc WPUBON01 (один <IDOC> на чек)
      5. Запись XML в outbox (3 попытки с паузой 30 сек)
      6. Перемещение JSON в processed
      7. Запись результата в журнал
    """
    filename = os.path.basename(filepath)

    if is_already_processed(conn, filename):
        logger.info('SKIPPED (дубль): %s', filename)
        write_journal(conn, filename, 'SKIPPED',
                      warnings=['Файл уже был успешно обработан ранее'])
        try:
            shutil.move(filepath, os.path.join(paths['processed'], filename))
        except Exception as e:
            logger.warning('Не удалось переместить дубль в processed: %s', e)
        return

    logger.info('Обработка: %s', filename)
    warnings = []

    kkl_list, err = load_kkl(filepath)
    if err:
        logger.error('Ошибка парсинга JSON %s: %s', filename, err)
        write_journal(conn, filename, 'ERROR', error_message=err)
        return

    if not kkl_list:
        msg = 'Пустой массив kkl в файле: {}'.format(filename)
        logger.warning(msg)
        write_journal(conn, filename, 'WARNING', warnings=[msg])
        return

    now            = datetime.now()
    total_receipts = 0
    out_filenames  = []

    for idx, kkl_entry in enumerate(kkl_list):
        entry_errors = validate_kkl_entry(kkl_entry, idx)
        if entry_errors:
            for msg in entry_errors:
                logger.warning(msg)
            warnings.extend(entry_errors)
            continue

        receipts = group_receipts(kkl_entry)
        if not receipts:
            msg = 'kkl[{}]: нет чеков (пустые lines[])'.format(idx)
            logger.warning(msg)
            warnings.append(msg)
            continue

        total_receipts += len(receipts)

        # Имя файла: WPUBON01_{dpt}_{YYYYMMDD}_{register}_{kkl_code}.xml
        kkl_date_part, _ = parse_kkl_datetime(kkl_entry.get('date', ''))
        out_filename = 'WPUBON01_{dpt}_{date}_{register}_{code}.xml'.format(
            dpt      = pad_shop(kkl_entry.get('dpt', '')),
            date     = kkl_date_part or 'NODATE',
            register = kkl_entry.get('register', ''),
            code     = kkl_entry.get('code', ''),
        )
        out_filepath = os.path.join(paths['outbox'], out_filename)

        root = build_xml(kkl_entry, receipts, sap_cfg, now, warnings, logger)

        written  = False
        last_err = ''
        for attempt in range(3):
            try:
                write_xml(root, out_filepath)
                written = True
                break
            except Exception as e:
                last_err = str(e)
                logger.warning('Ошибка записи XML (попытка %d/3): %s', attempt + 1, e)
                if attempt < 2:
                    time.sleep(30)

        if not written:
            msg = 'Ошибка записи XML {}: {}'.format(out_filename, last_err)
            logger.error(msg)
            warnings.append(msg)
            continue

        out_filenames.append(out_filename)
        logger.info('Создан: %s (%d чеков)', out_filename, len(receipts))

    # Перемещение в processed
    try:
        shutil.move(filepath, os.path.join(paths['processed'], filename))
    except Exception as e:
        logger.warning('Не удалось переместить файл в processed: %s', e)
        warnings.append('Не удалось переместить в processed: {}'.format(e))

    status    = 'WARNING' if warnings else 'OK'
    first_kkl = kkl_list[0] if kkl_list else {}

    logger.info('%s: %s → %s (%d чеков)',
                status, filename, ', '.join(out_filenames) or '—', total_receipts)

    write_journal(conn, filename, status,
                  kkl_code      = first_kkl.get('code'),
                  kkl_date      = first_kkl.get('date'),
                  register      = first_kkl.get('register'),
                  full          = first_kkl.get('full'),
                  receipts_count = total_receipts,
                  out_filename  = ', '.join(out_filenames) or None,
                  warnings      = warnings)


# ---------------------------------------------------------------------------
# Очистка устаревших файлов из processed/
# ---------------------------------------------------------------------------

def cleanup_processed(processed_path, retention_days, logger):
    """Удаляет файлы из processed/ старше retention_days дней. 0 — не удалять."""
    if retention_days <= 0:
        return

    cutoff  = datetime.now().timestamp() - retention_days * 86400
    removed = 0

    for fname in os.listdir(processed_path):
        fpath = os.path.join(processed_path, fname)
        if os.path.getmtime(fpath) < cutoff:
            try:
                os.remove(fpath)
                logger.info('Удалён устаревший файл из processed: %s', fname)
                removed += 1
            except Exception as e:
                logger.warning('Не удалось удалить %s: %s', fname, e)

    if removed:
        logger.info('Очистка processed: удалено файлов: %d', removed)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, 'config.ini')

    paths, sap_cfg = load_config(config_path)
    logger = setup_logging(paths['log'])
    logger.info('=== Конвертер ККЛ JSON → IDoc WPUBON01 SAP запущен ===')

    for key in ('inbox', 'outbox', 'processed'):
        os.makedirs(paths[key], exist_ok=True)

    conn = init_journal(paths['journal'])

    try:
        all_files = os.listdir(paths['inbox'])
    except Exception as e:
        logger.error('Не удалось прочитать inbox: %s', e)
        conn.close()
        return

    json_files = sorted(f for f in all_files if f.lower().endswith('.json'))

    if not json_files:
        logger.info('Нет новых файлов в inbox.')
        conn.close()
        cleanup_processed(paths['processed'], paths['retention_days'], logger)
        logger.info('=== Готово ===')
        return

    logger.info('Найдено файлов для обработки: %d', len(json_files))

    for fname in json_files:
        fpath = os.path.join(paths['inbox'], fname)
        try:
            process_file(fpath, paths, sap_cfg, conn, logger)
        except Exception as e:
            logger.error('Необработанное исключение для %s: %s', fname, e)
            write_journal(conn, fname, 'ERROR', error_message=str(e))

    conn.close()
    cleanup_processed(paths['processed'], paths['retention_days'], logger)
    logger.info('=== Готово ===')


if __name__ == '__main__':
    main()
