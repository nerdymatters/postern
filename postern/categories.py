"""Category labels use the same grouping rules as the reading-room contents."""
import re

# ECMAScript trim()/\s whitespace, matching contents.tsx (not Python's C1 separators).
CATEGORY_SPACE = '\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
MODULE = re.compile(r'^module[' + re.escape(CATEGORY_SPACE) + r']*([0-9]+)$', re.IGNORECASE)


def category_name(value):
    value = value.strip(CATEGORY_SPACE)
    return MODULE.sub(r'Module \1', value) or 'General'


def category_key(value):
    return category_name(value).lower()
