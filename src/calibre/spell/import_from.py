#!/usr/bin/env python


__license__ = 'GPL v3'
__copyright__ = '2014, Kovid Goyal <kovid at kovidgoyal.net>'

import codecs
import glob
import os
import re
import sys
import tempfile

from lxml import etree

from calibre import browser
from calibre.constants import config_dir
from calibre.utils.resources import get_path as P
from calibre.utils.xml_parse import safe_xml_fromstring
from calibre.utils.zipfile import ZipFile
from polyglot.builtins import iteritems

NS_MAP = {
    'oor': 'http://openoffice.org/2001/registry',
    'xs': 'http://www.w3.org/2001/XMLSchema',
    'manifest': 'http://openoffice.org/2001/manifest',
}


def XPath(x):
    return etree.XPath(x, namespaces=NS_MAP)


BUILTIN_LOCALES = {'en-US', 'en-GB', 'es-ES'}
ONLINE_DICTIONARY_BASE_URL = 'https://raw.githubusercontent.com/LibreOffice/dictionaries/master/'


def parse_xcu(raw, origin='%origin%'):
    ' Get the dictionary and affix file names as well as supported locales for each dictionary '
    ans = {}
    root = safe_xml_fromstring(raw)

    for node in XPath('//prop[@oor:name="Format"]/value[text()="DICT_SPELL"]/../..')(root):
        value = XPath('descendant::prop[@oor:name="Locations"]/value')(node)
        if len(value[0]) == 0:
            # The value node has no children, use its text
            paths = ''.join(XPath('descendant::prop[@oor:name="Locations"]/value/text()')(node)).replace('%origin%', origin).split()
        else:
            # Use the text of the value nodes children
            paths = [c.text.replace('%origin%', origin) for v in value for c in v.iterchildren('*') if c.text]
        aff, dic = paths if paths[0].endswith('.aff') else reversed(paths)
        locales = ''.join(XPath('descendant::prop[@oor:name="Locales"]/value/text()')(node)).split()
        if not locales:
            locales = [str(item) for item in XPath('descendant::prop[@oor:name="Locales"]/value/it/text()')(node)]
        ans[(dic, aff)] = locales
    return ans


def convert_to_utf8(dic_data, aff_data, errors='strict'):
    m = re.search(br'^SET\s+(\S+)$', aff_data[:2048], flags=re.MULTILINE)
    if m is not None:
        enc = m.group(1).decode('ascii', 'replace')
        if enc.upper() not in ('UTF-8', 'UTF8'):
            try:
                codecs.lookup(enc)
            except LookupError:
                pass
            else:
                aff_data = aff_data[:m.start()] + b'SET UTF-8' + aff_data[m.end():]
                aff_data = aff_data.decode(enc, errors).encode('utf-8')
                dic_data = dic_data.decode(enc, errors).encode('utf-8')
    return dic_data, aff_data


def import_from_libreoffice_source_tree(source_path):
    dictionaries = {}
    for x in glob.glob(os.path.join(source_path, '*', 'dictionaries.xcu')):
        origin = os.path.dirname(x)
        with open(x, 'rb') as f:
            dictionaries.update(parse_xcu(f.read(), origin))

    base = P('dictionaries', allow_user_override=False)
    want_locales = set(BUILTIN_LOCALES)

    for (dic, aff), locales in iteritems(dictionaries):
        c = set(locales) & want_locales
        if c:
            locale = tuple(c)[0]
            want_locales.discard(locale)
            dest = os.path.join(base, locale)
            if not os.path.exists(dest):
                os.makedirs(dest)
            with open(dic, 'rb') as df, open(aff, 'rb') as af:
                dd, ad = convert_to_utf8(df.read(), af.read())
            for src, raw in ((dic, dd), (aff, ad)):
                with open(os.path.join(dest, locale + os.path.splitext(src)[1]), 'wb') as df:
                    df.write(raw)
            with open(os.path.join(dest, 'locales'), 'wb') as f:
                locales.sort(key=lambda x: (0, x) if x == locale else (1, x))
                f.write(('\n'.join(locales)).encode('utf-8'))

    if want_locales:
        raise Exception(f'Failed to find dictionaries for some wanted locales: {want_locales}')


def fill_country_code(x):
    return {'lt':'lt_LT'}.get(x, x)


def uniq(vals, kmap=lambda x:x):
    ''' Remove all duplicates from vals, while preserving order. kmap must be a
    callable that returns a hashable value for every item in vals '''
    vals = vals or ()
    lvals = (kmap(x) for x in vals)
    seen = set()
    seen_add = seen.add
    return tuple(x for x, k in zip(vals, lvals) if k not in seen and not seen_add(k))


def _import_from_virtual_directory(read_file_func, name, dest_dir=None, prefix='dic-'):
    from calibre.spell.dictionary import parse_lang_code
    dest_dir = dest_dir or os.path.join(config_dir, 'dictionaries')
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)
    num = 0

    root = safe_xml_fromstring(read_file_func('META-INF/manifest.xml'))
    xcu = XPath('//manifest:file-entry[@manifest:media-type="application/vnd.sun.star.configuration-data"]')(root)[0].get(
        '{{{}}}full-path'.format(NS_MAP['manifest']))
    for (dic, aff), locales in iteritems(parse_xcu(read_file_func(xcu), origin='')):
        dic, aff = dic.lstrip('/'), aff.lstrip('/')
        d = tempfile.mkdtemp(prefix=prefix, dir=dest_dir)
        locales = uniq([x for x in map(fill_country_code, locales) if parse_lang_code(x).countrycode])
        if not locales:
            continue
        metadata = [name] + list(locales)
        with open(os.path.join(d, 'locales'), 'wb') as f:
            f.write(('\n'.join(metadata)).encode('utf-8'))
        dd, ad = convert_to_utf8(read_file_func(dic), read_file_func(aff))
        with open(os.path.join(d, f'{locales[0]}.dic'), 'wb') as f:
            f.write(dd)
        with open(os.path.join(d, f'{locales[0]}.aff'), 'wb') as f:
            f.write(ad)
        num += 1
    return num


def import_from_oxt(source_path, name, dest_dir=None, prefix='dic-'):
    with ZipFile(source_path) as zf:
        def read_file(key):
            try:
                return zf.open(key).read()
            except KeyError:
                # Some dictionaries apparently put the xcu in a sub-directory
                # and incorrectly make paths relative to that directory instead
                # of the root, for example:
                # http://extensions.libreoffice.org/extension-center/italian-dictionary-thesaurus-hyphenation-patterns/releases/4.1/dict-it.oxt
                while key.startswith('../'):
                    key = key[3:]
                return zf.open(key.lstrip('/')).read()

        return _import_from_virtual_directory(read_file, name, dest_dir=dest_dir, prefix=prefix)


def import_from_online(directory, name, dest_dir=None, prefix='dic-'):
    br = browser(timeout=30)
    def read_file(key):
        try:
            rp = br.open('/'.join((ONLINE_DICTIONARY_BASE_URL, directory, key)))
            return rp.read()
        except Exception as err:
            if getattr(err, 'code', -1) != 404:
                raise
            # Some dictionaries apparently put the dic and aff file in a
            # sub-directory dictionaries and incorrectly make paths relative
            # to that directory instead of the root, for example:
            # https://github.com/LibreOffice/dictionaries/tree/master/ca
            rp = br.open('/'.join((ONLINE_DICTIONARY_BASE_URL, directory, 'dictionaries', key)))
            return rp.read()

    return _import_from_virtual_directory(read_file, name, dest_dir=dest_dir, prefix=prefix)


if __name__ == '__main__':
    import_from_libreoffice_source_tree(sys.argv[-1])
