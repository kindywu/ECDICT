#!/usr/bin/env python
"""
query.py — ECDICT 命令行查询工具

用法：
    uv run query.py lookup <word>         查单词
    uv run query.py form   <word>         通过词形反查原型
    uv run query.py tag    <tag>           按标签列出单词
    uv run query.py random [--tag <tag>]   随机单词
    uv run query.py stats                  统计信息
"""

import argparse
import asyncio
import json
import os
import re
import sys

from dotenv import load_dotenv
import edge_tts
from ecdict_db import ECDict, TAG_LABELS, FORM_LABELS
from sentence import generate_sentences, strip_markup, AUDIO_CACHE, DEFAULT_MODEL

DEFAULT_DB = 'ecdict.db'


def cmd_lookup(db, args):
    result = db.lookup(args.word.lower())
    if result is None:
        print(f'"{args.word}" not found.')
        return 1

    # 自动生成例句（如缺失且启用了 --sentence）
    if args.sentence and not result.get('sentences'):
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if api_key:
            try:
                _ensure_sentences(db, args.word.lower(), result['id'],
                                  api_key, args.model, args.voice)
            except Exception:
                pass  # 生成失败不阻塞主流程
            result = db.lookup(args.word.lower())

    if args.json:
        print(json.dumps(_clean(result), ensure_ascii=False, indent=2))
        return 0

    print(f'Word:       {result["word"]}')
    print(f'Phonetic:   {result["phonetic"] or "-"}')
    print(f'Collins:    {"★" * result["collins"] if result["collins"] else "-"}')
    print(f'Oxford 3000: {"Yes" if result["oxford"] else "No"}')
    print(f'BNC rank:   {result["bnc"] or "-"}')
    print(f'COCA rank:  {result["frq"] or "-"}')
    print(f'Tags:       {", ".join(result["tags"]) or "-"}')
    print()

    for d in result['definitions']:
        parts = []
        if d.get('pos'):
            parts.append(f'[{d["pos"]}]')
        if d.get('definition'):
            parts.append(d['definition'])
        if d.get('translation'):
            parts.append(d['translation'])
        print('  ' + (' ' if parts else '') + ' / '.join(parts))
    print()

    if result.get('word_forms'):
        print('Forms:')
        for ft, fw in sorted(result['word_forms'].items(),
                             key=lambda x: list(FORM_LABELS.keys()).index(x[0]) if x[0] in FORM_LABELS else 99):
            label = FORM_LABELS.get(ft, ft)
            print(f'  {label}: {fw}')
        print()

    details = result.get('details', {})
    if details.get('proportion'):
        print(f'POS distribution: {details["proportion"]}')

    # 显示例句
    sentences = result.get('sentences', [])
    if sentences:
        print('Sentences:')
        for s in sentences:
            en = _render_bold(s['sentence'])
            print(f'  • {en}')
            print(f'    {s["translation"]}')
            if s.get('audio'):
                print(f'    └ {s["audio"]}')
        print()


def cmd_form(db, args):
    rows = db.search_by_form(args.word.lower())
    if not rows:
        print(f'No word forms found for "{args.word}".')
        return 1

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f'"{args.word}" appears as a form of:')
    for r in rows:
        label = FORM_LABELS.get(r['form_type'], r['form_type'])
        print(f'  {label} → {r["word"]}')


def cmd_tag(db, args):
    if args.tag not in TAG_LABELS:
        print(f'Unknown tag: {args.tag}')
        print(f'Available tags: {", ".join(TAG_LABELS.keys())}')
        return 1

    rows = db.search_by_tag(args.tag)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f'{TAG_LABELS[args.tag]} ({args.tag}): {len(rows)} words')
    for r in rows:
        collins = f' ★{r["collins"]}' if r['collins'] else ''
        print(f'  {r["word"]}{collins}')


def cmd_tagged(db, args):
    rows = db.search_by_tags(args.tags, limit=args.num)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f'Words tagged with {" + ".join(args.tags)}: {len(rows)}')
    for r in rows:
        collins = f' ★{r["collins"]}' if r['collins'] else ''
        print(f'  {r["word"]}{collins}')


def cmd_random(db, args):
    rows = db.random_words(args.num, tag=args.tag)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.tag:
        print(f'Random {args.num} {TAG_LABELS.get(args.tag, args.tag)} words:')
    else:
        print(f'Random {args.num} words:')
    for r in rows:
        print(f'  {r["word"]}')


def cmd_stats(db, args):
    total = db.count()
    print(f'Total words: {total}')
    print()
    for name in TAG_LABELS:
        c = db.count(tag=name)
        print(f'  {name:8s} ({TAG_LABELS[name]:4s}): {c:>6d}')


def _clean(obj):
    """递归清理 Row 对象为可 JSON 序列化的 dict。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _render_bold(text):
    """将 **word** 渲染为 ANSI 红色。"""
    return re.sub(r"\*\*(.+?)\*\*", lambda m: f"\033[1;31m{m.group(1)}\033[0m", text)


async def _gen_sentence_audio(sentences, word, voice):
    """为每条例句生成音频，返回 (sentence, translation, audio_name) 列表。"""
    os.makedirs(AUDIO_CACHE, exist_ok=True)
    items = []
    for i, (en, cn) in enumerate(sentences):
        safe = re.sub(r'[^\w\s-]', '', strip_markup(en)).strip().replace(' ', '_')[:40].rstrip('_')
        audio_name = f"{word}_{i+1:02d}_{safe}.wav"
        audio_path = os.path.join(AUDIO_CACHE, audio_name)
        if not os.path.exists(audio_path):
            communicate = edge_tts.Communicate(strip_markup(en), voice)
            await communicate.save(audio_path)
        items.append((en, cn, audio_name))
    return items


def _ensure_sentences(db, word, word_id, api_key, model, voice, count=3):
    """调用 DeepSeek 生成例句，生成音频，保存到数据库。"""
    results = generate_sentences(word, count, api_key, model)
    items = asyncio.run(_gen_sentence_audio(results, word, voice))
    db.save_sentences(word_id, items)


def main():
    load_dotenv()
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument('--db', default=DEFAULT_DB, help='SQLite 数据库路径')
    parent.add_argument('--json', action='store_true', help='JSON 格式输出')

    parser = argparse.ArgumentParser(description='ECDICT 命令行查询工具')
    sub = parser.add_subparsers(dest='command', required=True)

    p_lookup = sub.add_parser('lookup', parents=[parent], help='查单词')
    p_lookup.add_argument('word', help='要查询的单词')
    p_lookup.add_argument('--sentence', action='store_true', default=True,
                          help='自动生成/显示例句（默认启用）')
    p_lookup.add_argument('--no-sentence', dest='sentence', action='store_false',
                          help='跳过例句')
    p_lookup.add_argument('--model', default=DEFAULT_MODEL,
                          help=f'AI 模型（默认 {DEFAULT_MODEL}）')
    p_lookup.add_argument('--voice', default='en-US-AriaNeural',
                          help='TTS 语音（默认 en-US-AriaNeural）')

    p_form = sub.add_parser('form', parents=[parent], help='通过词形反查原型')
    p_form.add_argument('word', help='词形单词')

    p_tag = sub.add_parser('tag', parents=[parent], help='按标签列出单词')
    p_tag.add_argument('tag', help=f'标签 ({", ".join(TAG_LABELS.keys())})')

    p_tagged = sub.add_parser('tagged', parents=[parent], help='多标签交集查询')
    p_tagged.add_argument('tags', nargs='+', help=f'标签名，如 cet4 cet6')
    p_tagged.add_argument('--num', '-n', type=int, default=10, help='返回条数')

    p_random = sub.add_parser('random', parents=[parent], help='随机单词')
    p_random.add_argument('--tag', help='按标签筛选')
    p_random.add_argument('--num', '-n', type=int, default=10, help='数量')

    p_stats = sub.add_parser('stats', parents=[parent], help='统计信息')

    args = parser.parse_args()

    db = ECDict(args.db)

    cmds = {
        'lookup': cmd_lookup,
        'form': cmd_form,
        'tag': cmd_tag,
        'tagged': cmd_tagged,
        'random': cmd_random,
        'stats': cmd_stats,
    }

    try:
        rc = cmds[args.command](db, args)
    finally:
        db.close()

    sys.exit(rc if rc else 0)


if __name__ == '__main__':
    main()
