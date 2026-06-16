"""
Fix spaces erroneously inserted between every Japanese character in router.py.

Removes ASCII spaces that appear between two adjacent Japanese characters
(katakana, hiragana, kanji, or Japanese punctuation), while leaving
intentional spaces alone (e.g. between Latin and Japanese: 'gg スプリーム').
"""

import re
import sys

# Unicode ranges for Japanese characters
JP_CHAR = (
    r'[　-〿'   # CJK symbols & punctuation
    r'぀-ゟ'    # Hiragana
    r'゠-ヿ'    # Katakana
    r'一-鿿]'   # CJK Unified Ideographs
)

# Matches a space (or full-width space) sandwiched between two Japanese chars
_SPACED_JP = re.compile(rf'({JP_CHAR}) ({JP_CHAR})')


def fix_spacing(text: str) -> str:
    """Remove spaces between adjacent Japanese characters (applied repeatedly)."""
    while True:
        fixed = _SPACED_JP.sub(r'\1\2', text)
        if fixed == text:
            return fixed
        text = fixed


def process_file(path: str) -> None:
    with open(path, 'r', encoding='utf-8') as f:
        original = f.read()

    fixed = fix_spacing(original)

    if fixed == original:
        print(f'{path}: no changes needed')
        return

    with open(path, 'w', encoding='utf-8') as f:
        f.write(fixed)

    # Report changed lines
    orig_lines = original.splitlines()
    fixed_lines = fixed.splitlines()
    for i, (a, b) in enumerate(zip(orig_lines, fixed_lines), 1):
        if a != b:
            print(f'  line {i}: {repr(a.strip())}')
            print(f'       -> {repr(b.strip())}')
    print(f'{path}: fixed.')


if __name__ == '__main__':
    targets = sys.argv[1:] or ['router.py']
    for target in targets:
        process_file(target)
