# -*- coding: utf-8 -*-

import collections
import os

###############
# Defaults
###############


ROOT, FILE = os.path.split(os.path.abspath(__file__))


def LINE(NAME, DOCS, FLAG, ENUM, MISS): return f'''\
# -*- coding: utf-8 -*-


from aenum import IntEnum, extend_enum


class {NAME}(IntEnum):
    """Enumeration class for {NAME}."""
    _ignore_ = '{NAME} _'
    {NAME} = vars()

    # {DOCS}
    {ENUM}

    @staticmethod
    def get(key, default=-1):
        """Backport support for original codes."""
        if isinstance(key, int):
            return {NAME}(key)
        if key not in {NAME}._member_map_:
            extend_enum({NAME}, key, default)
        return {NAME}[key]

    @classmethod
    def _missing_(cls, value):
        """Lookup function used when value is not found."""
        if not ({FLAG}):
            raise ValueError('%r is not a valid %s' % (value, cls.__name__))
        {MISS}
'''


###############
# Macros
###############


NAME = 'Precedence'
DOCS = 'TOS (DS Field) Precedence'
FLAG = 'isinstance(value, int) and 0b000 <= value <= 0b111'
DATA = {
    0b111: 'Network Control',
    0b110: 'Internetwork Control',
    0b101: 'CRITIC/ECP',
    0b100: 'Flash Override',
    0b011: 'Flash',
    0b010: 'Immediate',
    0b001: 'Priority',
    0b000: 'Routine',
}


###############
# Processors
###############


record = collections.Counter(DATA.values())


def rename(name, code):
    if record[name] > 1:
        name = f'{name} [{code}]'
    return name


enum = list()
miss = [
    "extend_enum(cls, 'Unassigned [%d]' % value, value)",
    'return cls(value)'
]
for code, name in DATA.items():
    renm = rename(name, code)
    enum.append(f"{NAME}[{renm!r}] = {code}".ljust(76))


###############
# Defaults
###############


ENUM = '\n    '.join(map(lambda s: s.rstrip(), enum))
MISS = '\n        '.join(map(lambda s: s.rstrip(), miss))
with open(os.path.join(ROOT, f'../_common/{FILE}'), 'w') as file:
    file.write(LINE(NAME, DOCS, FLAG, ENUM, MISS))
