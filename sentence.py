#!/usr/bin/env python
"""
sentence.py — 用 AI 为英文单词造句，高亮目标单词

用法：
    uv run sentence.py <word>
    uv run sentence.py <word> --count 5
    uv run sentence.py <word> --model deepseek-chat --no-color
    uv run sentence.py <word> --read               # 同时生成语音
"""

import argparse
import asyncio
import os
import re
import sys

from dotenv import load_dotenv
import edge_tts
import requests


API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_COUNT = 3


def generate_sentences(word: str, count: int, api_key: str, model: str = DEFAULT_MODEL) -> list[tuple[str, str]]:
    """调用 DeepSeek API 生成例句，返回 (英文, 中文) 元组列表。"""
    prompt = (
        f"Generate {count} example sentences using the word \"{word}\".\n"
        f"Mark the word \"{word}\" with **bold** markers (like **{word}**) in each sentence.\n"
        f"Each sentence should demonstrate a common usage of \"{word}\".\n"
        f"After each sentence, provide a Chinese translation on the same line, "
        f"separated by \" | \".\n"
        f"Return ONLY the lines, one per line, with no numbering or extra text.\n"
        f"Example: This is a sample sentence with **{word}**. | 这是一个包含**{word}**的例句。"
    )

    resp = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 500,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    result = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if " | " in line:
            en, cn = line.split(" | ", 1)
            result.append((en.strip(), cn.strip()))
        else:
            result.append((line, ""))
    return result


def render(text: str, color: bool) -> str:
    """将 **word** 渲染为 ANSI 红色（终端）或纯文本。"""
    if color:
        return re.sub(r"\*\*(.+?)\*\*", lambda m: f"\033[1;31m{m.group(1)}\033[0m", text)
    return text.replace("**", "")


def strip_markup(text: str) -> str:
    """去掉 **marker** 得到纯文本。"""
    return text.replace("**", "")


def slug(text: str, max_len: int = 40) -> str:
    """取文本前 max_len 字符做文件名安全片段。"""
    safe = re.sub(r'[^\w\s-]', '', strip_markup(text)).strip().replace(' ', '_')
    return safe[:max_len].rstrip('_')


AUDIO_CACHE = os.path.join(os.path.dirname(__file__), 'audio_cache')


async def save_audio(text: str, voice: str, path: str):
    """用 edge-tts 生成语音并保存。"""
    communicate = edge_tts.Communicate(strip_markup(text), voice)
    await communicate.save(path)


def main():
    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("Error: DEEPSEEK_API_KEY not set. Add it to .env:\n"
              "  DEEPSEEK_API_KEY=sk-xxx")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="用 AI 为英文单词造句并高亮目标单词")
    parser.add_argument("word", help="英文单词")
    parser.add_argument("-n", "--count", type=int, default=DEFAULT_COUNT,
                        help=f"生成句子数量（默认 {DEFAULT_COUNT}）")
    parser.add_argument("--no-color", action="store_true", help="禁用颜色高亮")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型名称")
    parser.add_argument("-r", "--read", action="store_true", help="生成语音文件")
    parser.add_argument("--voice", default="en-US-AriaNeural", help="edge-tts 语音名")
    args = parser.parse_args()

    try:
        results = generate_sentences(args.word, args.count, api_key, args.model)
    except requests.RequestException as e:
        print(f"API 请求失败: {e}")
        sys.exit(1)

    header = f"\n  \033[1m{args.word}\033[0m 的例句：\n" if not args.no_color else f"\n  {args.word} 的例句：\n"
    print(header)
    color = not args.no_color
    for i, (en, cn) in enumerate(results, 1):
        print(f"  {i}. {render(en, color)}")
        if cn:
            print(f"     {render(cn, color)}")
    print()

    if args.read:
        os.makedirs(AUDIO_CACHE, exist_ok=True)

        async def _gen_all():
            for i, (en, cn) in enumerate(results, 1):
                name = f"{i:02d}_{slug(en)}.wav"
                path = os.path.join(AUDIO_CACHE, name)
                if os.path.exists(path):
                    continue
                communicate = edge_tts.Communicate(strip_markup(en), args.voice)
                await communicate.save(path)

        print(f"  生成语音 (voice: {args.voice})...")
        asyncio.run(_gen_all())

        for i, (en, cn) in enumerate(results, 1):
            name = f"{i:02d}_{slug(en)}.wav"
            print(f"    {i}. {name}")
        print(f"\n  audio_cache/")


if __name__ == "__main__":
    main()
