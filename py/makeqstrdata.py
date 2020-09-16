"""
Process raw qstr file and output qstr data with length, hash and data bytes.

This script works with Python 2.7, 3.3 and 3.4.

For documentation about the format of compressed translated strings, see
supervisor/shared/translate.h
"""

from __future__ import print_function

import re
import sys

from math import log
import collections
import gettext
import os.path

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(errors='backslashreplace')

py = os.path.dirname(sys.argv[0])
top = os.path.dirname(py)

sys.path.append(os.path.join(top, "tools/huffman"))

import huffman

# Python 2/3 compatibility:
#   - iterating through bytes is different
#   - codepoint2name lives in a different module
import platform
if platform.python_version_tuple()[0] == '2':
    bytes_cons = lambda val, enc=None: bytearray(val)
    from htmlentitydefs import codepoint2name
elif platform.python_version_tuple()[0] == '3':
    bytes_cons = bytes
    from html.entities import codepoint2name
# end compatibility code

codepoint2name[ord('-')] = 'hyphen';

# add some custom names to map characters that aren't in HTML
codepoint2name[ord(' ')] = 'space'
codepoint2name[ord('\'')] = 'squot'
codepoint2name[ord(',')] = 'comma'
codepoint2name[ord('.')] = 'dot'
codepoint2name[ord(':')] = 'colon'
codepoint2name[ord(';')] = 'semicolon'
codepoint2name[ord('/')] = 'slash'
codepoint2name[ord('%')] = 'percent'
codepoint2name[ord('#')] = 'hash'
codepoint2name[ord('(')] = 'paren_open'
codepoint2name[ord(')')] = 'paren_close'
codepoint2name[ord('[')] = 'bracket_open'
codepoint2name[ord(']')] = 'bracket_close'
codepoint2name[ord('{')] = 'brace_open'
codepoint2name[ord('}')] = 'brace_close'
codepoint2name[ord('*')] = 'star'
codepoint2name[ord('!')] = 'bang'
codepoint2name[ord('\\')] = 'backslash'
codepoint2name[ord('+')] = 'plus'
codepoint2name[ord('$')] = 'dollar'
codepoint2name[ord('=')] = 'equals'
codepoint2name[ord('?')] = 'question'
codepoint2name[ord('@')] = 'at_sign'
codepoint2name[ord('^')] = 'caret'
codepoint2name[ord('|')] = 'pipe'
codepoint2name[ord('~')] = 'tilde'

C_ESCAPES = {
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
    "\'": "\\'",
    "\"": "\\\""
}

# this must match the equivalent function in qstr.c
def compute_hash(qstr, bytes_hash):
    hash = 5381
    for b in qstr:
        hash = (hash * 33) ^ b
    # Make sure that valid hash is never zero, zero means "hash not computed"
    return (hash & ((1 << (8 * bytes_hash)) - 1)) or 1

def translate(translation_file, i18ns):
    with open(translation_file, "rb") as f:
        table = gettext.GNUTranslations(f)

        translations = []
        for original in i18ns:
            unescaped = original
            for s in C_ESCAPES:
                unescaped = unescaped.replace(C_ESCAPES[s], s)
            translation = table.gettext(unescaped)
            # Add in carriage returns to work in terminals
            translation = translation.replace("\n", "\r\n")
            translations.append((original, translation))
        return translations

class TextSplitter:
    def __init__(self, words):
        words.sort(key=lambda x: len(x), reverse=True)
        self.words = set(words)
        self.pat = re.compile("|".join(re.escape(w) for w in words) + "|.", flags=re.DOTALL)

    def iter_words(self, text):
        s = []
        words = self.words
        for m in self.pat.finditer(text):
            t = m.group(0)
            if t in words:
                if s:
                    yield (False, "".join(s))
                    s = []
                yield (True, t)
            else:
                s.append(t)
        if s:
            yield (False, "".join(s))

    def iter(self, text):
        for m in self.pat.finditer(text):
            yield m.group(0)

def iter_substrings(s, minlen, maxlen):
    len_s = len(s)
    maxlen = min(len_s, maxlen)
    for n in range(minlen, maxlen + 1):
        for begin in range(0, len_s - n + 1):
            yield s[begin : begin + n]

def compute_huffman_coding(translations, compression_filename):
    texts = [t[1] for t in translations]
    words = []

    start_unused = 0x80
    end_unused = 0xff
    max_ord = 0
    for text in texts:
        for c in text:
            ord_c = ord(c)
            max_ord = max(ord_c, max_ord)
            if 0x80 <= ord_c < 0xff:
                end_unused = min(ord_c, end_unused)
    max_words = end_unused - 0x80

    values_type = "uint16_t" if max_ord > 255 else "uint8_t"
    max_words_len = 160 if max_ord > 255 else 255

    sum_len = 0
    while True:
        # Until the dictionary is filled to capacity, use a heuristic to find
        # the best "word" (2- to 9-gram) to add to it.
        #
        # The TextSplitter allows us to avoid considering parts of the text
        # that are already covered by a previously chosen word, for example
        # if "the" is in words then not only will "the" not be considered
        # again, neither will "there" or "wither", since they have "the"
        # as substrings.
        extractor = TextSplitter(words)
        counter = collections.Counter()
        for t in texts:
            for (found, word) in extractor.iter_words(t):
                if not found:
                    for substr in iter_substrings(word, minlen=2, maxlen=9):
                        counter[substr] += 1

        # Score the candidates we found.  This is an empirical formula only,
        # chosen for its effectiveness.
        scores = sorted(
            (
                (s, (len(s) - 1) ** log(max(occ - 2, 1)), occ)
                for (s, occ) in counter.items()
            ),
            key=lambda x: x[1],
            reverse=True,
        )

        # Do we have a "word" that occurred 5 times and got a score of at least
        # 5?  Horray.  Pick the one with the highest score.
        word = None
        for (s, score, occ) in scores:
            if occ < 5:
                continue
            if score < 5:
                break
            word = s
            break

        # If we can successfully add it to the dictionary, do so.  Otherwise,
        # we've filled the dictionary to capacity and are done.
        if not word:
            break
        if sum_len + len(word) - 2 > max_words_len:
            break
        if len(words) == max_words:
            break
        words.append(word)
        sum_len += len(word) - 2

    extractor = TextSplitter(words)
    counter = collections.Counter()
    for t in texts:
        for atom in extractor.iter(t):
            counter[atom] += 1
    cb = huffman.codebook(counter.items())

    word_start = start_unused
    word_end = word_start + len(words) - 1
    print("// # words", len(words))
    print("// words", words)

    values = []
    length_count = {}
    renumbered = 0
    last_length = None
    canonical = {}
    for atom, code in sorted(cb.items(), key=lambda x: (len(x[1]), x[0])):
        values.append(atom)
        length = len(code)
        if length not in length_count:
            length_count[length] = 0
        length_count[length] += 1
        if last_length:
            renumbered <<= (length - last_length)
        canonical[atom] = '{0:0{width}b}'.format(renumbered, width=length)
        # print(f"atom={repr(atom)} code={code}", file=sys.stderr)
        if len(atom) > 1:
            o = words.index(atom) + 0x80
            s = "".join(C_ESCAPES.get(ch1, ch1) for ch1 in atom)
        else:
            s = C_ESCAPES.get(atom, atom)
            o = ord(atom)
        print("//", o, s, counter[atom], canonical[atom], renumbered)
        renumbered += 1
        last_length = length
    lengths = bytearray()
    print("// length count", length_count)

    for i in range(1, max(length_count) + 2):
        lengths.append(length_count.get(i, 0))
    print("// values", values, "lengths", len(lengths), lengths)

    print("//", values, lengths)
    values = [(atom if len(atom) == 1 else chr(0x80 + words.index(atom))) for atom in values]
    print("//", values, lengths)
    max_translation_encoded_length = max(
        len(translation.encode("utf-8")) for (original, translation) in translations)

    wends = list(len(w) - 2 for w in words)
    for i in range(1, len(wends)):
        wends[i] += wends[i - 1]

    with open(compression_filename, "w") as f:
        f.write("const uint8_t lengths[] = {{ {} }};\n".format(", ".join(map(str, lengths))))
        f.write("const {} values[] = {{ {} }};\n".format(values_type, ", ".join(str(ord(u)) for u in values)))
        f.write("#define compress_max_length_bits ({})\n".format(max_translation_encoded_length.bit_length()))
        f.write("const {} words[] = {{ {} }};\n".format(values_type, ", ".join(str(ord(c)) for w in words for c in w)))
        f.write("const uint8_t wends[] = {{ {} }};\n".format(", ".join(str(p) for p in wends)))
        f.write("#define word_start {}\n".format(word_start))
        f.write("#define word_end {}\n".format(word_end))

    return (values, lengths, words, canonical, extractor)

def decompress(encoding_table, encoded, encoded_length_bits):
    (values, lengths, words, _, _) = encoding_table
    dec = []
    this_byte = 0
    this_bit = 7
    b = encoded[this_byte]
    bits = 0
    for i in range(encoded_length_bits):
        bits <<= 1
        if 0x80 & b:
            bits |= 1

        b <<= 1
        if this_bit == 0:
            this_bit = 7
            this_byte += 1
            if this_byte < len(encoded):
                b = encoded[this_byte]
        else:
            this_bit -= 1
    length = bits

    i = 0
    while i < length:
        bits = 0
        bit_length = 0
        max_code = lengths[0]
        searched_length = lengths[0]
        while True:
            bits <<= 1
            if 0x80 & b:
                bits |= 1

            b <<= 1
            bit_length += 1
            if this_bit == 0:
                this_bit = 7
                this_byte += 1
                if this_byte < len(encoded):
                    b = encoded[this_byte]
            else:
                this_bit -= 1
            if max_code > 0 and bits < max_code:
                #print('{0:0{width}b}'.format(bits, width=bit_length))
                break
            max_code = (max_code << 1) + lengths[bit_length]
            searched_length += lengths[bit_length]

        v = values[searched_length + bits - max_code]
        if v >= chr(0x80) and v < chr(0x80 + len(words)):
            v = words[ord(v) - 0x80]
        i += len(v.encode('utf-8'))
        dec.append(v)
    return ''.join(dec)

def compress(encoding_table, decompressed, encoded_length_bits, len_translation_encoded):
    if not isinstance(decompressed, str):
        raise TypeError()
    (_, _, _, canonical, extractor) = encoding_table

    enc = bytearray(len(decompressed) * 3)
    current_bit = 7
    current_byte = 0

    bits = encoded_length_bits + 1
    for i in range(bits - 1, 0, -1):
        if len_translation_encoded & (1 << (i - 1)):
            enc[current_byte] |= 1 << current_bit
        if current_bit == 0:
            current_bit = 7
            current_byte += 1
        else:
            current_bit -= 1

    for atom in extractor.iter(decompressed):
        for b in canonical[atom]:
            if b == "1":
                enc[current_byte] |= 1 << current_bit
            if current_bit == 0:
                current_bit = 7
                current_byte += 1
            else:
                current_bit -= 1

    if current_bit != 7:
        current_byte += 1
    return enc[:current_byte]

def qstr_escape(qst):
    def esc_char(m):
        c = ord(m.group(0))
        try:
            name = codepoint2name[c]
        except KeyError:
            name = '0x%02x' % c
        return "_" + name + '_'
    return re.sub(r'[^A-Za-z0-9_]', esc_char, qst)

def parse_input_headers(infiles):
    # read the qstrs in from the input files
    qcfgs = {}
    qstrs = {}
    i18ns = set()
    for infile in infiles:
        with open(infile, 'rt') as f:
            for line in f:
                line = line.strip()

                # is this a config line?
                match = re.match(r'^QCFG\((.+), (.+)\)', line)
                if match:
                    value = match.group(2)
                    if value[0] == '(' and value[-1] == ')':
                        # strip parenthesis from config value
                        value = value[1:-1]
                    qcfgs[match.group(1)] = value
                    continue


                match = re.match(r'^TRANSLATE\("(.*)"\)$', line)
                if match:
                    i18ns.add(match.group(1))
                    continue

                # is this a QSTR line?
                match = re.match(r'^Q\((.*)\)$', line)
                if not match:
                    continue

                # get the qstr value
                qstr = match.group(1)

                # special case to specify control characters
                if qstr == '\\n':
                    qstr = '\n'

                # work out the corresponding qstr name
                ident = qstr_escape(qstr)

                # don't add duplicates
                if ident in qstrs:
                    continue

                # add the qstr to the list, with order number to retain original order in file
                order = len(qstrs)
                # but put special method names like __add__ at the top of list, so
                # that their id's fit into a byte
                if ident == "":
                    # Sort empty qstr above all still
                    order = -200000
                elif ident == "__dir__":
                    # Put __dir__ after empty qstr for builtin dir() to work
                    order = -190000
                elif ident.startswith("__"):
                    order -= 100000
                qstrs[ident] = (order, ident, qstr)

    if not qcfgs and qstrs:
        sys.stderr.write("ERROR: Empty preprocessor output - check for errors above\n")
        sys.exit(1)

    return qcfgs, qstrs, i18ns

def make_bytes(cfg_bytes_len, cfg_bytes_hash, qstr):
    qbytes = bytes_cons(qstr, 'utf8')
    qlen = len(qbytes)
    qhash = compute_hash(qbytes, cfg_bytes_hash)
    if all(32 <= ord(c) <= 126 and c != '\\' and c != '"' for c in qstr):
        # qstr is all printable ASCII so render it as-is (for easier debugging)
        qdata = qstr
    else:
        # qstr contains non-printable codes so render entire thing as hex pairs
        qdata = ''.join(('\\x%02x' % b) for b in qbytes)
    if qlen >= (1 << (8 * cfg_bytes_len)):
        print('qstr is too long:', qstr)
        assert False
    qlen_str = ('\\x%02x' * cfg_bytes_len) % tuple(((qlen >> (8 * i)) & 0xff) for i in range(cfg_bytes_len))
    qhash_str = ('\\x%02x' * cfg_bytes_hash) % tuple(((qhash >> (8 * i)) & 0xff) for i in range(cfg_bytes_hash))
    return '(const byte*)"%s%s" "%s"' % (qhash_str, qlen_str, qdata)

def print_qstr_data(encoding_table, qcfgs, qstrs, i18ns):
    # get config variables
    cfg_bytes_len = int(qcfgs['BYTES_IN_LEN'])
    cfg_bytes_hash = int(qcfgs['BYTES_IN_HASH'])

    # print out the starter of the generated C header file
    print('// This file was automatically generated by makeqstrdata.py')
    print('')

    # add NULL qstr with no hash or data
    print('QDEF(MP_QSTR_NULL, (const byte*)"%s%s" "")' % ('\\x00' * cfg_bytes_hash, '\\x00' * cfg_bytes_len))

    total_qstr_size = 0
    total_qstr_compressed_size = 0
    # go through each qstr and print it out
    for order, ident, qstr in sorted(qstrs.values(), key=lambda x: x[0]):
        qbytes = make_bytes(cfg_bytes_len, cfg_bytes_hash, qstr)
        print('QDEF(MP_QSTR_%s, %s)' % (ident, qbytes))
        total_qstr_size += len(qstr)

    total_text_size = 0
    total_text_compressed_size = 0
    max_translation_encoded_length = max(len(translation.encode("utf-8")) for original, translation in i18ns)
    encoded_length_bits = max_translation_encoded_length.bit_length()
    for original, translation in i18ns:
        translation_encoded = translation.encode("utf-8")
        compressed = compress(encoding_table, translation, encoded_length_bits, len(translation_encoded))
        total_text_compressed_size += len(compressed)
        decompressed = decompress(encoding_table, compressed, encoded_length_bits)
        assert decompressed == translation
        for c in C_ESCAPES:
            decompressed = decompressed.replace(c, C_ESCAPES[c])
        print("TRANSLATION(\"{}\", {}) // {}".format(original, ", ".join(["{:d}".format(x) for x in compressed]), decompressed))
        total_text_size += len(translation.encode("utf-8"))

    print()
    print("// {} bytes worth of qstr".format(total_qstr_size))
    print("// {} bytes worth of translations".format(total_text_size))
    print("// {} bytes worth of translations compressed".format(total_text_compressed_size))
    print("// {} bytes saved".format(total_text_size - total_text_compressed_size))

def print_qstr_enums(qstrs):
    # print out the starter of the generated C header file
    print('// This file was automatically generated by makeqstrdata.py')
    print('')

    # add NULL qstr with no hash or data
    print('QENUM(MP_QSTR_NULL)')

    # go through each qstr and print it out
    for order, ident, qstr in sorted(qstrs.values(), key=lambda x: x[0]):
        print('QENUM(MP_QSTR_%s)' % (ident,))

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Process QSTR definitions into headers for compilation')
    parser.add_argument('infiles', metavar='N', type=str, nargs='+',
                        help='an integer for the accumulator')
    parser.add_argument('--translation', default=None, type=str,
                        help='translations for i18n() items')
    parser.add_argument('--compression_filename', default=None, type=str,
                        help='header for compression info')

    args = parser.parse_args()

    qcfgs, qstrs, i18ns = parse_input_headers(args.infiles)
    if args.translation:
        i18ns = sorted(i18ns)
        translations = translate(args.translation, i18ns)
        encoding_table = compute_huffman_coding(translations, args.compression_filename)
        print_qstr_data(encoding_table, qcfgs, qstrs, translations)
    else:
        print_qstr_enums(qstrs)
