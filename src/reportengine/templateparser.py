# -*- coding: utf-8 -*-
"""
Created on Fri Nov 27 14:58:12 2015

@author: zah
"""
import re
from io import StringIO
from collections import namedtuple
import logging

from reportengine.resourcebuilder import FuzzyTarget

log = logging.getLogger(__name__)


#TODO: Do a real tokenizer/lexer/parser? Would avoid having r'\s*?'
#verywhere and scale better. The parser+lexer is some 100 lines of
#code with SLY, excluding the logic of finding special strings. So until this
#grows to some ~400 lines, we are better off without the extra dependency.
custom_delimiter_re = r'{@\s*(.*?)\s*@}'
#fun with regexp
custom_delimeter_for_exact_match = r'\s*?\{@\s*(.*?)\s*@\}\s*?'

with_re = r'with\s+(\S+)'

endwith_re = r'endwith'

assignment_re = r'(\w)\s*=\s*(\w)'

target_re = r'((?P<fuzzy>\S+)\s+)?(?P<func>\w+)\s*(\((?P<args>.*)\))?'

def tokenize_fuzzy(s):
    return s.split('::')

def parse_assignments(args):
    splits = re.split('\s*,\s*', args)
    res = []
    for i, s in enumerate(splits,1):
        m =  re.fullmatch(assignment_re, s)
        if m:
            res.append((m.group(1), m.group(2)))
        else:
            raise ValueError(("Couldn't process arguments '%s'. Expected a "
            "coma separated sequence arg1 = val1, arg2 = val2, ..., but "
            "for the assignment %d got %s") % (args, i ,s))
    return tuple(res)

Match = namedtuple('Match', ('type', 'value'))

class BadTemplate(Exception): pass

class CustomParsingError(BadTemplate):
    def __init__(self, message ,lineno, pos):
        super().__init__("Error in line %d at pos %d: %s" % (lineno, pos, message))

class BadToken(BadTemplate): pass

def parse_with(with_match, line, lineno, out):

    newfuzzy = tokenize_fuzzy(with_match.group(1))
    line = "{{% for spec in expand_fuzzyspec(ns, {newfuzzy!r}, spec) %}}\n".format(newfuzzy=newfuzzy)
    out.write(line)
    return Match('with', tuple(newfuzzy))

def parse_endwith(deli_match, lineno, out):

    out.write("{% endfor %}\n")
    return Match('endwith', None)

def parse_target(deli_match, target_match, line, lineno, out):
    fuzzy_match = target_match.group('fuzzy')
    if fuzzy_match is not None:
        fuzzy = tuple(tokenize_fuzzy(fuzzy_match))
    else:
        fuzzy = ()

    args_match = target_match.group('args')
    if args_match is not None:
        try:
            extraargs = parse_assignments(args_match)
        except ValueError as e:
            raise CustomParsingError(("Bad arguments: %s"%
                  (e,)), lineno, target_match.start('args')) from e
    else:
        extraargs = ()
    target = FuzzyTarget(target_match.group('func'), fuzzy, (), extraargs)
    log.debug("Found target %s in line %s.", target, lineno)

    out.write(line[:deli_match.start()])
    out.write("{{{{ collect_fuzzyspec(ns, {name!r}, {fuzzy!r}, spec) }}}}".format(
              name=target.name, fuzzy=target.fuzzyspec)
             )
    out.write(line[deli_match.end():])
    return Match('target', target)


def parse_match(deli_match, line, lineno, out):
    magic_text = deli_match.group(1)

    with_match = re.fullmatch(with_re, magic_text)
    if with_match:
        if not re.fullmatch(custom_delimeter_for_exact_match, line):
            raise CustomParsingError("with blocks have to be on "
                                 "a separate line.", lineno, deli_match.start())
        return parse_with(with_match, line, lineno, out)


    if re.match(endwith_re, magic_text):
        if not re.fullmatch(custom_delimeter_for_exact_match, line):
            raise CustomParsingError("endwith blocks have to be on "
                                     "a separate line.", lineno, deli_match.start())
        return parse_endwith(deli_match, lineno, out)


    target_match = re.match(target_re, magic_text)
    if target_match:
        return parse_target(deli_match, target_match,
                                line, lineno, out)

    raise CustomParsingError("Could not interpret: '%s'."
                                 " Format not understood." %
                                 deli_match.group(0), line, deli_match.start())


def get_targets_and_replace(source):

    out = StringIO()

    for lineno, line in enumerate(source, 1):
        deli_matches = list(re.finditer(custom_delimiter_re, line))
        if not deli_matches:
            out.write(line)
            continue
        for deli_match in deli_matches:
            try:
                yield parse_match(deli_match, line, lineno, out)
            except BadToken as e:
                raise CustomParsingError(e, lineno, deli_match.pos)

    return out.getvalue()
