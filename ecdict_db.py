"""
ecdict_db.py — ECDICT 规范化 SQLite 数据库

五张核心表 + 两张关联表的规范化设计。
支持从 ecdict.csv 导入并提供查询接口。
"""

import csv
import json
import os
import sqlite3
import re

__all__ = ['ECDict']


# ---------------------------------------------------------------------------
#  词性前缀识别（用于解析释义行首的 "n. ", "v. " 等标记）
# ---------------------------------------------------------------------------
POS_PATTERN = re.compile(
    r'^([a-zäëïöü]+\.)'       # n.  v.  adj.  prep.  etc.
    r'(?:\s+|(?=\S))'          # 后面跟空格或直接跟文字
)

BRACKET_PATTERN = re.compile(r'^\[([^\]]+)\]\s*(.*)')

# ---------------------------------------------------------------------------
#  标签中文名
# ---------------------------------------------------------------------------
TAG_LABELS = {
    'zk': '中考', 'gk': '高考', 'ky': '考研',
    'cet4': '四级', 'cet6': '六级',
    'toefl': '托福', 'ielts': '雅思', 'gre': 'GRE',
}

# ---------------------------------------------------------------------------
#  词形变换类型说明
# ---------------------------------------------------------------------------
FORM_LABELS = {
    'p': '过去式', 'd': '过去分词', 'i': '现在分词',
    '3': '第三人称单数', 'r': '比较级', 't': '最高级', 's': '复数',
    '0': '原型',
}

# ---------------------------------------------------------------------------
#  建表 SQL
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS words (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    word       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phonetic   TEXT,
    bnc        INTEGER,
    frq        INTEGER,
    collins    INTEGER DEFAULT 0,
    oxford     INTEGER DEFAULT 0,
    audio      TEXT
);

CREATE TABLE IF NOT EXISTS definitions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id      INTEGER NOT NULL REFERENCES words(id),
    pos          TEXT,
    definition   TEXT,
    translation  TEXT,
    sort_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS word_tags (
    word_id INTEGER NOT NULL REFERENCES words(id),
    tag_id  INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (word_id, tag_id)
);

CREATE TABLE IF NOT EXISTS word_forms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id    INTEGER NOT NULL REFERENCES words(id),
    form_type  TEXT NOT NULL,
    form_word  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pos_frequencies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id    INTEGER NOT NULL REFERENCES words(id),
    pos        TEXT NOT NULL,
    frequency  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS details (
    word_id     INTEGER PRIMARY KEY REFERENCES words(id),
    cald        TEXT,
    collins_html TEXT,
    syno        TEXT,
    resemble    TEXT,
    youci       TEXT,
    xdf         TEXT,
    bzsd        TEXT,
    proportion  TEXT
);

CREATE INDEX IF NOT EXISTS idx_definitions_word ON definitions(word_id);
CREATE INDEX IF NOT EXISTS idx_word_tags_tag  ON word_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_word_forms_word ON word_forms(word_id);
CREATE INDEX IF NOT EXISTS idx_word_forms_lookup ON word_forms(form_word);
CREATE INDEX IF NOT EXISTS idx_pos_freq_word  ON pos_frequencies(word_id);
"""


# ---------------------------------------------------------------------------
#  辅助函数
# ---------------------------------------------------------------------------

def _parse_translation_line(line):
    """从一行释义文本中提取词性前缀和正文。

    返回 (pos, text)，pos 可能为 None。
    """
    line = line.strip()
    if not line:
        return (None, '')

    # "n. 释义" / "adj. 释义"
    m = POS_PATTERN.match(line)
    if m:
        pos = m.group(1)
        rest = line[m.end():].strip()
        return (pos, rest)

    # "[网络] 释义"
    m = BRACKET_PATTERN.match(line)
    if m:
        return ('[' + m.group(1) + ']', m.group(2).strip())

    # "> 例句"
    if line.startswith('>'):
        return ('>', line[1:].strip())

    return (None, line)


def _parse_pos_frequencies(raw):
    """解析词性分布字段 'v:80/n:20' → [(pos, freq), ...]"""
    if not raw or raw == '0':
        return []
    parts = raw.split('/')
    result = []
    for part in parts:
        if ':' not in part:
            continue
        p, _, f = part.partition(':')
        p = p.strip()
        try:
            f = int(f)
        except ValueError:
            continue
        if p:
            result.append((p, f))
    return result


def _parse_exchange(raw):
    """解析词形变换字段 'p:xxx/d:xxx/3:xxx/i:xxx' → [(type, word), ...]"""
    if not raw:
        return []
    parts = raw.split('/')
    result = []
    for part in parts:
        if ':' not in part:
            continue
        k, _, v = part.partition(':')
        k = k.strip()
        v = v.strip()
        if k and v and k in FORM_LABELS:
            result.append((k, v))
    return result


def _decode_csv_text(text):
    """解码 CSV 中的 \\n 和 \\\\ 转义。"""
    if text is None:
        return None
    result = []
    i = 0
    size = len(text)
    while i < size:
        c = text[i]
        if c == '\\' and i + 1 < size:
            n = text[i + 1]
            if n == 'n':
                result.append('\n')
            elif n == 'r':
                result.append('\r')
            elif n == '\\':
                result.append('\\')
            else:
                result.append('\\' + n)
            i += 2
        else:
            result.append(c)
            i += 1
    return ''.join(result)


def _readint(text):
    """安全转换整数字段。"""
    if text is None or text == '':
        return None
    try:
        v = int(text)
    except (ValueError, TypeError):
        return None
    return v if v < 0x7fffffff else None


# ---------------------------------------------------------------------------
#  ECDict 类
# ---------------------------------------------------------------------------

class ECDict:
    """ECDICT 规范化数据库封装。

    用法：
        db = ECDict('ecdict.db')
        db.import_csv('ecdict.csv')
        word = db.lookup('hello')
        words = db.search_by_tag('cet4')
    """

    def __init__(self, db_path):
        self.db_path = os.path.abspath(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # ------------------------------------------------------------------
    #  Schema
    # ------------------------------------------------------------------
    def _init_schema(self):
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def clear(self):
        """清空所有数据。"""
        tables = [
            'details', 'pos_frequencies', 'word_forms',
            'word_tags', 'definitions', 'words', 'tags',
        ]
        for t in tables:
            self.conn.execute(f'DELETE FROM {t}')
        self.conn.commit()

    # ------------------------------------------------------------------
    #  从 CSV 导入
    # ------------------------------------------------------------------
    def import_csv(self, csv_path, batch_size=10000):
        """从 ecdict.csv 导入数据到规范化表。

        Args:
            csv_path: CSV 文件路径
            batch_size: 每批提交的行数
        Returns:
            int: 导入的单词数
        """
        self.clear()
        csv_path = os.path.abspath(csv_path)
        print(f'Importing from {csv_path} ...')

        # 预填充 tags 表
        tag_ids = {}
        for name, label in TAG_LABELS.items():
            c = self.conn.execute(
                'INSERT OR IGNORE INTO tags (name, label) VALUES (?, ?)',
                (name, label)
            )
            if c.rowcount == 0:
                row = self.conn.execute(
                    'SELECT id FROM tags WHERE name = ?', (name,)
                ).fetchone()
                tag_ids[name] = row[0]
            else:
                tag_ids[name] = c.lastrowid

        word_count = 0
        batch = []
        total = 0

        with open(csv_path, 'r', encoding='utf-8') as fp:
            reader = csv.reader(fp)
            for row_idx, row in enumerate(reader):
                if row_idx == 0:       # 跳过标题行
                    continue
                if not row or not row[0]:
                    continue

                total += 1
                word, phonetic, definition, translation, pos_raw = \
                    row[0], row[1] if len(row) > 1 else '', \
                    row[2] if len(row) > 2 else '', \
                    row[3] if len(row) > 3 else '', \
                    row[4] if len(row) > 4 else ''
                collins = _readint(row[5]) if len(row) > 5 else None
                oxford = _readint(row[6]) if len(row) > 6 else None
                tag_raw = row[7] if len(row) > 7 else ''
                bnc = _readint(row[8]) if len(row) > 8 else None
                frq = _readint(row[9]) if len(row) > 9 else None
                exchange_raw = row[10] if len(row) > 10 else ''
                detail_raw = row[11] if len(row) > 11 else ''

                # 解码转义
                phonetic = _decode_csv_text(phonetic)
                definition = _decode_csv_text(definition)
                translation = _decode_csv_text(translation)

                batch.append((
                    word, phonetic, bnc, frq,
                    collins if collins else 0,
                    oxford if oxford else 0, '',
                    definition, translation, pos_raw,
                    tag_raw, exchange_raw, detail_raw,
                ))

                if len(batch) >= batch_size:
                    word_count += self._flush_batch(batch, tag_ids)
                    batch = []

        if batch:
            word_count += self._flush_batch(batch, tag_ids)

        self.conn.commit()
        return word_count

    def _flush_batch(self, batch, tag_ids):
        """写入一批数据到所有表。"""
        if not batch:
            return 0

        cursor = self.conn.cursor()

        # words
        cursor.executemany("""
            INSERT INTO words (word, phonetic, bnc, frq, collins, oxford, audio)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in batch])

        # 取回每个 word 的 id
        word_id_map = {}       # word -> id
        for r in batch:
            row = cursor.execute(
                'SELECT id FROM words WHERE word = ?', (r[0],)
            ).fetchone()
            if row:
                word_id_map[r[0]] = row[0]

        # definitions
        def_rows = []
        for r in batch:
            word = r[0]
            word_id = word_id_map.get(word)
            if word_id is None:
                continue
            trans_text = r[8]       # translation
            def_text = r[7]          # definition

            # 解析翻译行
            lines = trans_text.split('\n') if trans_text else []
            for order, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                pos, text = _parse_translation_line(line)
                def_rows.append((word_id, pos, None, text, order))

            # 如果 translation 为空但有 definition，用 definition 生成条目
            if not lines and def_text:
                dlines = def_text.split('\n')
                for order, line in enumerate(dlines):
                    line = line.strip()
                    if not line:
                        continue
                    pos, text = _parse_translation_line(line)
                    def_rows.append((word_id, pos, text, None, order))

        if def_rows:
            cursor.executemany("""
                INSERT INTO definitions (word_id, pos, definition, translation, sort_order)
                VALUES (?, ?, ?, ?, ?)
            """, def_rows)

        # tags
        for r in batch:
            word_id = word_id_map.get(r[0])
            if word_id is None:
                continue
            tag_raw = r[10]
            if not tag_raw:
                continue
            for name in tag_raw.split():
                name = name.strip()
                tid = tag_ids.get(name)
                if tid is not None:
                    cursor.execute(
                        'INSERT OR IGNORE INTO word_tags (word_id, tag_id) VALUES (?, ?)',
                        (word_id, tid)
                    )

        # word_forms
        form_rows = []
        for r in batch:
            word_id = word_id_map.get(r[0])
            if word_id is None:
                continue
            exchange_raw = r[11]
            for fmt, fword in _parse_exchange(exchange_raw):
                form_rows.append((word_id, fmt, fword))
        if form_rows:
            cursor.executemany("""
                INSERT INTO word_forms (word_id, form_type, form_word)
                VALUES (?, ?, ?)
            """, form_rows)

        # pos_frequencies
        pf_rows = []
        for r in batch:
            word_id = word_id_map.get(r[0])
            if word_id is None:
                continue
            pos_raw = r[9]
            for p, f in _parse_pos_frequencies(pos_raw):
                pf_rows.append((word_id, p, f))
        if pf_rows:
            cursor.executemany("""
                INSERT INTO pos_frequencies (word_id, pos, frequency)
                VALUES (?, ?, ?)
            """, pf_rows)

        # details
        for r in batch:
            word_id = word_id_map.get(r[0])
            if word_id is None:
                continue
            detail_raw = r[12]
            if not detail_raw:
                continue
            try:
                detail = json.loads(detail_raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(detail, dict):
                continue

            fields = {}
            field_map = {
                'cald': 'cald',
                'collins': 'collins_html',
                'syno': 'syno',
                'resemble': 'resemble',
                'youci': 'youci',
                'xdf': 'xdf',
                'bzsd': 'bzsd',
                'proportion': 'proportion',
            }
            for src_key, dst_key in field_map.items():
                val = detail.get(src_key)
                if val is not None:
                    if isinstance(val, (list, dict)):
                        val = json.dumps(val, ensure_ascii=False)
                    else:
                        val = str(val)
                    fields[dst_key] = val

            if fields:
                keys = ', '.join(fields.keys())
                placeholders = ', '.join('?' for _ in fields)
                values = list(fields.values())
                cursor.execute(f"""
                    INSERT OR REPLACE INTO details (word_id, {keys})
                    VALUES (?, {placeholders})
                """, [word_id] + values)

        return len(batch)

    # ------------------------------------------------------------------
    #  查询接口
    # ------------------------------------------------------------------
    def lookup(self, word):
        """查询单词，返回包含所有关联数据的字典或 None。"""
        row = self.conn.execute(
            'SELECT * FROM words WHERE word = ?', (word,)
        ).fetchone()
        if row is None:
            return None

        word_id = row[0]
        result = dict(row)

        # definitions
        result['definitions'] = [
            dict(r) for r in self.conn.execute(
                'SELECT * FROM definitions WHERE word_id = ? ORDER BY sort_order',
                (word_id,)
            ).fetchall()
        ]

        # tags
        result['tags'] = [
            r[0] for r in self.conn.execute("""
                SELECT t.name FROM tags t
                JOIN word_tags wt ON t.id = wt.tag_id
                WHERE wt.word_id = ?
                ORDER BY t.name
            """, (word_id,))
        ]

        # word_forms
        result['word_forms'] = {
            r[0]: r[1] for r in self.conn.execute(
                'SELECT form_type, form_word FROM word_forms WHERE word_id = ?',
                (word_id,)
            ).fetchall()
        }

        # pos_frequencies
        result['pos_frequencies'] = {
            r[0]: r[1] for r in self.conn.execute(
                'SELECT pos, frequency FROM pos_frequencies WHERE word_id = ?',
                (word_id,)
            ).fetchall()
        }

        # details
        detail = self.conn.execute(
            'SELECT * FROM details WHERE word_id = ?', (word_id,)
        ).fetchone()
        result['details'] = dict(detail) if detail else {}

        return result

    def search_by_tag(self, tag_name):
        """查询带有指定标签的所有单词。"""
        rows = self.conn.execute("""
            SELECT w.id, w.word, w.phonetic, w.collins
            FROM words w
            JOIN word_tags wt ON w.id = wt.word_id
            JOIN tags t ON t.id = wt.tag_id
            WHERE t.name = ?
            ORDER BY w.word
        """, (tag_name,)).fetchall()
        return [dict(r) for r in rows]

    def search_by_tags(self, tag_names, limit=None):
        """查询同时包含所有指定标签的单词（AND 交集）。

        Args:
            tag_names: 标签名列表，如 ['cet4', 'cet6']
            limit: 可选，限制返回条数
        Returns:
            单词字典列表
        """
        if not tag_names:
            return []

        placeholders = ','.join('?' for _ in tag_names)
        sql = f"""
            SELECT w.id, w.word, w.phonetic, w.collins
            FROM words w
            JOIN word_tags wt ON w.id = wt.word_id
            JOIN tags t ON t.id = wt.tag_id
            WHERE t.name IN ({placeholders})
            GROUP BY w.id
            HAVING COUNT(DISTINCT t.id) = ?
            ORDER BY w.word
        """
        params = list(tag_names) + [len(tag_names)]
        if limit:
            sql += ' LIMIT ?'
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_by_form(self, form_word):
        """通过词形变化查找原词。如 'perceived' → 'perceive'。"""
        rows = self.conn.execute("""
            SELECT w.id, w.word, wf.form_type, wf.form_word
            FROM word_forms wf
            JOIN words w ON w.id = wf.word_id
            WHERE wf.form_word = ?
        """, (form_word,)).fetchall()
        return [dict(r) for r in rows]

    def random_words(self, limit=10, tag=None):
        """随机获取单词，可选按标签筛选。"""
        if tag:
            rows = self.conn.execute("""
                SELECT w.id, w.word, w.phonetic, w.collins
                FROM words w
                JOIN word_tags wt ON w.id = wt.word_id
                JOIN tags t ON t.id = wt.tag_id
                WHERE t.name = ?
                ORDER BY RANDOM()
                LIMIT ?
            """, (tag, limit)).fetchall()
        else:
            rows = self.conn.execute(
                'SELECT id, word, phonetic, collins FROM words ORDER BY RANDOM() LIMIT ?',
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self, tag=None):
        """单词总数，可选按标签筛选。"""
        if tag:
            return self.conn.execute("""
                SELECT COUNT(*) FROM words w
                JOIN word_tags wt ON w.id = wt.word_id
                JOIN tags t ON t.id = wt.tag_id
                WHERE t.name = ?
            """, (tag,)).fetchone()[0]
        return self.conn.execute('SELECT COUNT(*) FROM words').fetchone()[0]

    def close(self):
        self.conn.close()


# end of ECDict class

# ---------------------------------------------------------------------------
#  CLI入口 — 仅用于导入
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import argparse
    import sys
    import time

    parser = argparse.ArgumentParser(description='ECDICT 数据库导入工具')
    parser.add_argument('--csv', default='ecdict.csv', help='CSV 文件路径')
    parser.add_argument('--db', default='ecdict.db', help='SQLite 数据库路径')
    parser.add_argument('--clear', action='store_true', help='重建前清空数据库')
    args = parser.parse_args()

    if os.path.exists(args.db) and not args.clear:
        print(f'Database {args.db} already exists. Use --clear to rebuild.')
        sys.exit(1)

    db = ECDict(args.db)
    if args.clear:
        db.clear()

    t0 = time.time()
    n = db.import_csv(args.csv)
    elapsed = time.time() - t0
    print(f'Done. {n} words imported in {elapsed:.1f}s')
    db.close()
