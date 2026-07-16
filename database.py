#!/usr/bin/env python3
"""
ATEQ 测试数据数据库模块
使用 SQLite 存储测试记录
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

# 数据库文件路径
DB_PATH = os.path.join(os.path.dirname(__file__), 'ateq_test_data.db')


def init_database():
    """初始化数据库，创建表结构"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 创建测试记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            daily_serial INTEGER NOT NULL,
            qr_code TEXT,
            pressure1 REAL,
            leak1 REAL,
            pressure2 REAL,
            leak2 REAL,
            pressure1_unit TEXT,
            leak1_unit TEXT,
            pressure2_unit TEXT,
            leak2_unit TEXT,
            test_result TEXT NOT NULL,
            product_model TEXT,
            operator TEXT,
            serial_number TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Upgrade existing databases without rewriting historical measurements.
    existing_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(test_records)").fetchall()
    }
    unit_columns = {
        'pressure1_unit': 'TEXT',
        'leak1_unit': 'TEXT',
        'pressure2_unit': 'TEXT',
        'leak2_unit': 'TEXT',
    }
    for column_name, column_type in unit_columns.items():
        if column_name not in existing_columns:
            cursor.execute(
                f"ALTER TABLE test_records ADD COLUMN {column_name} {column_type}"
            )
    
    # 创建索引
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_test_time ON test_records(test_time)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_daily_serial ON test_records(daily_serial)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_qr_code ON test_records(qr_code)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_serial_number ON test_records(serial_number)
    ''')
    
    conn.commit()
    conn.close()
    print(f"数据库初始化完成: {DB_PATH}")


def get_next_daily_serial() -> int:
    """获取当日下一个序列号"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    today = datetime.now().strftime('%Y-%m-%d')
    cursor.execute('''
        SELECT COUNT(*) FROM test_records 
        WHERE date(test_time) = date(?)
    ''', (today,))
    
    count = cursor.fetchone()[0]
    conn.close()
    
    return count + 1


def save_test_record(
    qr_code: Optional[str],
    pressure1: float,
    leak1: float,
    pressure2: Optional[float],
    leak2: Optional[float],
    test_result: str,
    product_model: Optional[str],
    operator: Optional[str],
    serial_number: Optional[str] = None,
    pressure1_unit: Optional[str] = None,
    leak1_unit: Optional[str] = None,
    pressure2_unit: Optional[str] = None,
    leak2_unit: Optional[str] = None
) -> int:
    """
    保存测试记录
    
    Args:
        qr_code: 二维码
        pressure1: 压力1
        leak1: 泄漏量1
        pressure2: 压力2 (可选)
        leak2: 泄漏量2 (可选)
        test_result: 测试结果 (PASS/FAIL)
        product_model: 产品型号
        operator: 操作人员
        serial_number: 序列号
    
    Returns:
        记录ID
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 获取当日序列号
    daily_serial = get_next_daily_serial()
    
    cursor.execute('''
        INSERT INTO test_records 
        (test_time, daily_serial, qr_code, pressure1, leak1, pressure2, leak2,
         pressure1_unit, leak1_unit, pressure2_unit, leak2_unit,
         test_result, product_model, operator, serial_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        daily_serial, qr_code, pressure1, leak1, pressure2, leak2,
        pressure1_unit, leak1_unit, pressure2_unit, leak2_unit,
        test_result, product_model, operator, serial_number
    ))
    
    record_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return record_id


def get_records_by_date(date_str: str) -> List[Dict[str, Any]]:
    """获取指定日期的所有记录"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM test_records 
        WHERE date(test_time) = date(?)
        ORDER BY daily_serial
    ''', (date_str,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_records_by_qr_code(qr_code: str) -> List[Dict[str, Any]]:
    """根据二维码查询记录"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM test_records 
        WHERE qr_code = ?
        ORDER BY test_time DESC
    ''', (qr_code,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_latest_records(limit: int = 100) -> List[Dict[str, Any]]:
    """获取最新的测试记录"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM test_records 
        ORDER BY test_time DESC
        LIMIT ?
    ''', (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_statistics_by_date(date_str: str) -> Dict[str, Any]:
    """获取指定日期的统计信息"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN test_result = 'PASS' THEN 1 ELSE 0 END) as pass_count,
            SUM(CASE WHEN test_result = 'FAIL' THEN 1 ELSE 0 END) as fail_count
        FROM test_records 
        WHERE date(test_time) = date(?)
    ''', (date_str,))
    
    row = cursor.fetchone()
    conn.close()
    
    total = row[0]
    pass_count = row[1] or 0
    fail_count = row[2] or 0
    
    return {
        'total': total,
        'pass_count': pass_count,
        'fail_count': fail_count,
        'pass_rate': round(pass_count / total * 100, 2) if total > 0 else 0
    }


def query_records(start_date: str = None, end_date: str = None, 
                  product_model: str = None, result: str = None,
                  qr_code: str = None, serial: str = None,
                  limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    """
    多条件查询测试记录
    返回: {records: 记录列表, total: 总记录数, pass_count: PASS数量, fail_count: FAIL数量}
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 构建查询条件
    conditions = []
    params = []
    
    if start_date:
        conditions.append("date(test_time) >= date(?)")
        params.append(start_date)
    
    if end_date:
        conditions.append("date(test_time) <= date(?)")
        params.append(end_date)
    
    if product_model:
        conditions.append("product_model = ?")
        params.append(product_model)
    
    if result:
        conditions.append("test_result = ?")
        params.append(result)
    
    if qr_code:
        conditions.append("qr_code LIKE ?")
        params.append(f"%{qr_code}%")
    
    if serial:
        conditions.append("serial_number LIKE ?")
        params.append(f"%{serial}%")
    
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    
    # 查询记录（带分页）
    query_sql = f'''
        SELECT * FROM test_records 
        {where_clause}
        ORDER BY test_time DESC
        LIMIT ? OFFSET ?
    '''
    cursor.execute(query_sql, params + [limit, offset])
    rows = cursor.fetchall()
    records = [dict(row) for row in rows]
    
    # 查询总记录数和统计
    count_sql = f'''
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN test_result = 'PASS' THEN 1 ELSE 0 END) as pass_count,
            SUM(CASE WHEN test_result = 'FAIL' THEN 1 ELSE 0 END) as fail_count
        FROM test_records 
        {where_clause}
    '''
    cursor.execute(count_sql, params)
    row = cursor.fetchone()
    
    conn.close()
    
    total = row[0] or 0
    pass_count = row[1] or 0
    fail_count = row[2] or 0
    
    return {
        'records': records,
        'total': total,
        'pass_count': pass_count,
        'fail_count': fail_count,
        'pass_rate': round(pass_count / total * 100, 2) if total > 0 else 0
    }


def get_all_product_models() -> List[str]:
    """获取所有产品型号"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT DISTINCT product_model 
        FROM test_records 
        WHERE product_model IS NOT NULL AND product_model != ''
        ORDER BY product_model
    ''')
    
    rows = cursor.fetchall()
    conn.close()
    
    return [row[0] for row in rows if row[0]]


# 初始化数据库
if __name__ == '__main__':
    init_database()
    print("数据库表结构已创建")
