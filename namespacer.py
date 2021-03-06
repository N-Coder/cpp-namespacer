import os
import pathlib
import re
from collections import defaultdict
from pprint import pprint
from typing import Iterable


def apply(funcs, val):
    for f in reversed(funcs):
        val = f(val)
    return val


class CannotProcess(Exception):
    pass


class Namespacer(object):

    def __init__(self, file, lines: Iterable[str], namespace: str):
        self.file = pathlib.Path(file)
        self.lines = lines
        self.namespace = namespace
        self.out_buf = []
        self.msgs = []

        self.line_nr = -1
        self.full_line = ""
        self.include_guard = False
        self.namespace_active = False
        self.nesting_depth = 0
        self.drop_lines = 0
        self.status = None

    @property
    def open_namespace(self):
        return "namespace %s {\n" % self.namespace

    @property
    def close_namespace(self):
        return "} // end namespace %s\n" % self.namespace

    @property
    def would_open_namespace(self):
        return "// namespace %s {\n" % self.namespace

    @property
    def would_close_namespace(self):
        return "// } // end namespace %s\n" % self.namespace

    def iter_lines(self):
        excluded = [self.would_open_namespace, self.would_close_namespace]
        for self.line_nr, self.full_line in enumerate(self.lines, start=1):
            if self.full_line in excluded:
                continue  # strip the line from the out_buf
            yield self.full_line
            if self.drop_lines > 0:
                self.drop_lines -= 1
            else:
                self.out_buf.append(self.full_line)

    def filter_empty_or_comment(self, iter):
        for l in iter:
            l = l.strip()
            if not l or l.startswith("//"):
                continue
            while l.endswith("\\"):
                l = l[:-1].strip() + next(iter).strip()
            yield l

    def filter_comment_block(self, iter):
        for l in iter:
            while l.startswith("/*"):
                l = l[2:]
                while True:
                    try:
                        yield l[l.index("*/") + 2:].strip()
                        l = next(iter)
                        break
                    except ValueError:  # line contains no '*/'
                        l = next(iter)
                    except StopIteration:  # line contains '*/', but is last line
                        return

            yield l

    def filter_include_guard(self, iter):
        for l in iter:
            if not self.include_guard:
                m = re.match("^#\s*ifndef\s+([A-Za-z0-9_]+)", l)
                if m:  # and self.file.name.rsplit(".")[0].lower() in m.group(1).lower():
                    l_d = next(iter)
                    if not re.match("^#\s*define\s+" + m.group(1), l_d):
                        self.msgs.append("broken include guard for '%s' in line %s:\n%s\n%s" % (m.group(1), self.line_nr - 1, l, l_d))
                        yield l
                        yield l_d
                        yield from iter  # stop processing further include guards
                        return
                    else:
                        self.include_guard = True

                    l = next(iter)

            yield l

    def filter_preprocessor(self, iter):
        for l in iter:
            if not re.match("^#\s*(elif|else|define|undef|error|pragma|warning)", l):
                yield l

    # after filter_preprocessor: only '#if...', '#endif', '#include ...' and code lines

    def consume_if(self, iter, old_line):
        old_line_nr = self.line_nr
        old_out_buf = self.out_buf
        tmp_out_buf = self.out_buf = []
        include_line = code_line = None

        self.nesting_depth = 1
        l = next(iter)
        while self.nesting_depth > 0:
            if re.match("^#\s*endif", l):
                self.nesting_depth -= 1
            elif re.match("^#\s*if", l):
                self.nesting_depth += 1
            elif re.match("^#\s*include", l):
                if not include_line:
                    include_line = (l, self.line_nr, self.nesting_depth)
            else:  # code lines
                if not code_line:
                    code_line = (l, self.line_nr, self.nesting_depth)

            try:
                l = next(iter)
            except StopIteration:
                # file ended with '#endif' (if nesting_depth == 0) or unclosed '#if'
                l = None
                break

        self.out_buf = old_out_buf
        return l, include_line, code_line, tmp_out_buf

    def error(self, msg):
        raise CannotProcess(msg)

    def process(self):
        code_lines = apply(
            [self.filter_preprocessor,
             self.filter_include_guard,
             self.filter_empty_or_comment,
             self.filter_comment_block,
             self.filter_empty_or_comment],
            self.iter_lines()
        )

        includes = set()

        for line in code_lines:
            while line and re.match("^#\s*if", line):
                first_if_line = line
                first_if_line_nr = self.line_nr
                line, include_line, code_line, if_lines = self.consume_if(code_lines, line)

                if include_line and code_line:
                    self.msgs.append(
                        "'%s' starting on line %s contains '#includes' and code:\n%s: %s\n%s: %s"
                        % (first_if_line, first_if_line_nr, include_line[1], include_line[0],
                           code_line[1], code_line[0]))
                    self.error("mixed #if" + (" within namespace" if self.namespace_active else ""))
                elif include_line and self.namespace_active:
                    self.msgs.append(
                        "'%s' in line %s (contained within '%s' starting in line %s) "
                        "after first line of code in line %s: %s"
                        % (include_line[0], include_line[1], first_if_line, first_if_line_nr,
                           self.namespace_active[1], self.namespace_active[0])
                    )
                    self.error("include within #if within namespace")
                    # we can't easily close the namespace, as e.g. a class might be open
                elif code_line and not self.namespace_active:
                    self.msgs.append(
                        "inserting namespace before '%s' on line %s because it contains code on line %s: %s"
                        % (first_if_line, first_if_line_nr, code_line[1], code_line[0]))
                    self.out_buf.append(self.open_namespace)
                    self.namespace_active = (first_if_line, first_if_line_nr)
                # else everything is alright

                self.out_buf.extend(if_lines)
            if not line:
                continue

            if re.match("^#\s*endif", line):
                if self.include_guard:
                    self.include_guard = False
                    if self.namespace_active:
                        self.namespace_active = False
                        self.msgs.append(
                            "closing namespace before closing include guard on line %s: %s" % (self.line_nr, line))
                        self.out_buf.append(self.close_namespace)
                    # else there is nothing to close, i.e. no code within the include guards
                else:
                    self.msgs.append("superfluous '#endif' in line %s: %s" % (self.line_nr, line))
            elif re.match("^#\s*include", line):
                included = re.match("^#\s*include (.*)", line).group(1).strip()
                if self.namespace_active:
                    if included in includes:
                        self.msgs.append(
                            "'%s' in line %s was also included above, ignoring second #include now within namespace"
                            % (line, self.line_nr)
                        )
                        self.drop_lines += 1
                        self.out_buf.append("// this include was already seen before, outside the namespace, so ignoring it here")
                        self.out_buf.append("// " + line)
                    else:
                        self.msgs.append(
                            "'%s' in line %s after first line of code in line %s: %s"
                            % (line, self.line_nr, self.namespace_active[1], self.namespace_active[0])
                        )
                        if includes:
                            self.msgs.append("seen includes: %s" % ", ".join(includes))
                        self.error("#include within namespace")
                else:
                    # an include before we opened or after we closed the namespace is okay
                    includes.add(included)
            else:  # code line
                if "namespace " + self.namespace in line:
                    self.msgs.append("namespace already present in line %s: %s" % (self.line_nr, line))
                    return "namespace already present"

                is_class_predec = re.match("^\s*class\s*([^\s]+)\s*;\s*$", line)
                if not self.namespace_active:
                    if is_class_predec:
                        self.drop_lines += 1
                        self.out_buf.append(self.open_namespace)
                        self.out_buf.append(line + "\n")
                        self.out_buf.append(self.close_namespace)
                        self.msgs.append("namespaced class '%s' predeclaration in line %s: %s" % (is_class_predec.group(1), self.line_nr, line))
                    else:
                        self.msgs.append("inserting namespace before code line %s: %s" % (self.line_nr, line))
                        self.out_buf.append(self.open_namespace)
                        self.namespace_active = (line, self.line_nr)
                # else a code line within the namespace is perfectly fine

        if self.namespace_active:
            self.msgs.append("closing namespace after last line %s: %s" % (self.line_nr, self.full_line.rstrip("\n")))
            self.out_buf.append(self.close_namespace)

        if self.status:
            return self.status
        elif self.out_buf == self.lines:
            return "empty"
        else:
            return "success"


def main():
    import argparse
    from textwrap import indent
    parser = argparse.ArgumentParser(description='Automatically add namespaces around your .h and .cpp files.')
    parser.add_argument('files', type=pathlib.Path, nargs='+', help='the files to process in-place')
    parser.add_argument('--namespace', default='my_namespace', help='the name of the namespace to add')
    parser.add_argument('--dry-run', '-n', action='store_true', help='do not update the files if this flag is given')
    parser.add_argument('--quiet', '-q', action='store_true', help='don\'t print per-file results')
    parser.add_argument('--force', '-f', action='store_true', help='also change files that can\'t correctly be namespaced')
    parser.add_argument('--errors', '-e', action='store_true', help='mark lines with problems')
    parser.add_argument('--comment', '-c', action='store_true', help='only mark namespace begin and end with comment')
    args = parser.parse_args()

    if args.comment:
        Namespacer.open_namespace = Namespacer.would_open_namespace
        Namespacer.close_namespace = Namespacer.would_close_namespace

    if args.errors:
        def error(self, msg):
            if not self.status:
                self.status = msg
            self.msgs.append(msg)
            msg = msg.strip() + "\n"
            self.out_buf.extend(indent(msg, "// ").splitlines(keepends=True))

        Namespacer.error = error

    results = defaultdict(dict)
    for file in sorted(args.files):
        with open(file, "rt") as f:
            ns = Namespacer(file, f.readlines(), args.namespace)
        try:
            result = ns.process()
        except CannotProcess as e:
            result = e.args[0]
        results[result][file] = ns.msgs
        if not args.dry_run and (result == "success" or (result != "namespace already present" and args.force)):
            with open(file, "wt") as f:
                f.writelines(ns.out_buf)

    common = os.path.commonpath(args.files)
    if not args.quiet:
        for key, files in results.items():
            print("\n\n# " + key + "\n")
            for file, msgs in files.items():
                print("- %s" % file.relative_to(common))
                if msgs:
                    print(indent("\n".join(msgs), "  "))

    pprint({k: len(v) for k, v in results.items()})


if __name__ == "__main__":
    main()
