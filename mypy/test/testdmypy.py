"""Type checker test cases"""

import os
import re
import shutil
import sys
import time
import typed_ast

from typing import Dict, List, Optional, Set, Tuple

from mypy import build
from mypy import defaults
from mypy.main import process_options
from mypy.myunit import AssertionFailure
from mypy.test.config import test_temp_dir, test_data_prefix
from mypy.test.data import parse_test_cases, DataDrivenTestCase, DataSuite
from mypy.test.helpers import (
    assert_string_arrays_equal, normalize_error_messages,
    retry_on_error, testcase_pyversion, update_testcase_output,
)
from mypy.errors import CompileError
from mypy.options import Options

from mypy import experiments
from mypy import dmypy
from mypy import dmypy_server

# List of files that contain test case descriptions.
files = [
    'check-enum.test',
    'check-incremental.test',
    'check-newtype.test',
]


class TypeCheckSuite(DataSuite):

    @classmethod
    def cases(cls) -> List[DataDrivenTestCase]:
        if sys.platform == 'win32':
            return []  # Nothing here works on Windows.
        c = []  # type: List[DataDrivenTestCase]
        for f in files:
            tc = parse_test_cases(os.path.join(test_data_prefix, f),
                                  None, test_temp_dir, True)
            c += [case for case in tc
                  if cls.has_stable_flags(case) and cls.is_incremental(case)]
        return c

    def run_case(self, testcase: DataDrivenTestCase) -> None:
        assert self.is_incremental(testcase), "Testcase is not incremental"
        assert self.has_stable_flags(testcase), "Testcase has varying flags"
        # All tests run once with a cold cache, then at least once
        # with a warm cache and maybe changed files.  Expected output
        # is specified separately for each run.
        self.clear_cache()
        num_steps = max([2] + list(testcase.output2.keys()))
        # Check that there are no file changes beyond the last run (they would be ignored).
        for dn, dirs, files in os.walk(os.curdir):
            for file in files:
                m = re.search(r'\.([2-9])$', file)
                if m and int(m.group(1)) > num_steps:
                    raise ValueError(
                        'Output file {} exists though test case only has {} runs'.format(
                            file, num_steps))
        self.server = None  # type: Optional[dmypy_server.Server]
        for step in range(1, num_steps + 1):
            self.run_case_once(testcase, step)

    @classmethod
    def is_incremental(cls, testcase: DataDrivenTestCase) -> bool:
        return 'incremental' in testcase.name.lower() or 'incremental' in testcase.file

    @classmethod
    def has_stable_flags(cls, testcase: DataDrivenTestCase) -> bool:
        if any(re.match(r'# flags[2-9]:', line) for line in testcase.input):
            return False
        for filename, contents in testcase.files:
            if os.path.basename(filename).startswith('mypy.ini.'):
                return False
        return True

    def clear_cache(self) -> None:
        dn = defaults.CACHE_DIR
        if os.path.exists(dn):
            shutil.rmtree(dn)

    def run_case_once(self, testcase: DataDrivenTestCase, incremental_step: int) -> None:
        assert incremental_step >= 1
        build.find_module_clear_caches()
        original_program_text = '\n'.join(testcase.input)
        module_data = self.parse_module(original_program_text, incremental_step)

        if incremental_step == 1:
            # In run 1, copy program text to program file.
            for module_name, program_path, program_text in module_data:
                if module_name == '__main__':
                    with open(program_path, 'w') as f:
                        f.write(program_text)
                    break
        elif incremental_step > 1:
            # In runs 2+, copy *.[num] files to * files.
            for dn, dirs, files in os.walk(os.curdir):
                for file in files:
                    if file.endswith('.' + str(incremental_step)):
                        full = os.path.join(dn, file)
                        target = full[:-2]
                        # Use retries to work around potential flakiness on Windows (AppVeyor).
                        retry_on_error(lambda: shutil.copy(full, target))

                        # In some systems, mtime has a resolution of 1 second which can cause
                        # annoying-to-debug issues when a file has the same size after a
                        # change. We manually set the mtime to circumvent this.
                        new_time = os.stat(target).st_mtime + 1
                        os.utime(target, times=(new_time, new_time))
            # Delete files scheduled to be deleted in [delete <path>.num] sections.
            for path in testcase.deleted_paths.get(incremental_step, set()):
                # Use retries to work around potential flakiness on Windows (AppVeyor).
                retry_on_error(lambda: os.remove(path))

        # Parse options after moving files (in case mypy.ini is being moved).
        options = self.parse_options(original_program_text, testcase, incremental_step)
        if incremental_step == 1:
            self.server = dmypy_server.Server([])  # TODO: Fix ugly API
            self.server.options = options

        assert self.server is not None  # Set in step 1 and survives into next steps
        sources = []
        for module_name, program_path, program_text in module_data:
            # Always set to none so we're forced to reread the module in incremental mode
            sources.append(build.BuildSource(program_path, module_name, None))
        response = self.server.check(sources, alt_lib_path=test_temp_dir)
        a = (response['out'] or response['err']).splitlines()
        a = normalize_error_messages(a)

        # Make sure error messages match
        if incremental_step == 1:
            msg = 'Unexpected type checker output in incremental, run 1 ({}, line {})'
            output = testcase.output
        elif incremental_step > 1:
            msg = ('Unexpected type checker output in incremental, run {}'.format(
                incremental_step) + ' ({}, line {})')
            output = testcase.output2.get(incremental_step, [])
        else:
            raise AssertionError()

        if output != a and self.update_data:
            update_testcase_output(testcase, a)
        assert_string_arrays_equal(output, a, msg.format(testcase.file, testcase.line))

        manager = self.server.last_manager
        if manager is not None:
            if options.follow_imports == 'normal' and testcase.output is None:
                self.verify_cache(module_data, a, manager)
            if incremental_step > 1:
                suffix = '' if incremental_step == 2 else str(incremental_step - 1)
                self.check_module_equivalence(
                    'rechecked' + suffix,
                    testcase.expected_rechecked_modules.get(incremental_step - 1),
                    manager.rechecked_modules)
                self.check_module_equivalence(
                    'stale' + suffix,
                    testcase.expected_stale_modules.get(incremental_step - 1),
                    manager.stale_modules)

    def check_module_equivalence(self, name: str,
                                 expected: Optional[Set[str]], actual: Set[str]) -> None:
        if expected is not None:
            expected_normalized = sorted(expected)
            actual_normalized = sorted(actual.difference({"__main__"}))
            assert_string_arrays_equal(
                expected_normalized,
                actual_normalized,
                ('Actual modules ({}) do not match expected modules ({}) '
                 'for "[{} ...]"').format(
                    ', '.join(actual_normalized),
                    ', '.join(expected_normalized),
                    name))

    def verify_cache(self, module_data: List[Tuple[str, str, str]], a: List[str],
                     manager: build.BuildManager) -> None:
        # There should be valid cache metadata for each module except
        # those in error_paths; for those there should not be.
        #
        # NOTE: When A imports B and there's an error in B, the cache
        # data for B is invalidated, but the cache data for A remains.
        # However build.process_graphs() will ignore A's cache data.
        #
        # Also note that when A imports B, and there's an error in A
        # _due to a valid change in B_, the cache data for B will be
        # invalidated and updated, but the old cache data for A will
        # remain unchanged. As before, build.process_graphs() will
        # ignore A's (old) cache data.
        error_paths = self.find_error_paths(a)
        modules = self.find_module_files()
        modules.update({module_name: path for module_name, path, text in module_data})
        missing_paths = self.find_missing_cache_files(modules, manager)
        if not missing_paths.issubset(error_paths):
            raise AssertionFailure("cache data discrepancy %s != %s" %
                                   (missing_paths, error_paths))

    def find_error_paths(self, a: List[str]) -> Set[str]:
        hits = set()
        for line in a:
            m = re.match(r'([^\s:]+):\d+: error:', line)
            if m:
                # Normalize to Linux paths.
                p = m.group(1).replace(os.path.sep, '/')
                hits.add(p)
        return hits

    def find_module_files(self) -> Dict[str, str]:
        modules = {}
        for dn, dirs, files in os.walk(test_temp_dir):
            dnparts = dn.split(os.sep)
            assert dnparts[0] == test_temp_dir
            del dnparts[0]
            for file in files:
                if file.endswith('.py'):
                    if file == "__init__.py":
                        # If the file path is `a/b/__init__.py`, exclude the file name
                        # and make sure the module id is just `a.b`, not `a.b.__init__`.
                        id = '.'.join(dnparts)
                    else:
                        base, ext = os.path.splitext(file)
                        id = '.'.join(dnparts + [base])
                    modules[id] = os.path.join(dn, file)
        return modules

    def find_missing_cache_files(self, modules: Dict[str, str],
                                 manager: build.BuildManager) -> Set[str]:
        ignore_errors = True
        missing = {}
        for id, path in modules.items():
            meta = build.find_cache_meta(id, path, manager)
            if not build.validate_meta(meta, id, path, ignore_errors, manager):
                missing[id] = path
        return set(missing.values())

    def parse_module(self,
                     program_text: str,
                     incremental_step: int) -> List[Tuple[str, str, str]]:
        """Return the module and program names for a test case.

        Normally, the unit tests will parse the default ('__main__')
        module and follow all the imports listed there. You can override
        this behavior and instruct the tests to check multiple modules
        by using a comment like this in the test case input:

          # cmd: mypy -m foo.bar foo.baz

        You can also use `# cmdN:` to have a different cmd for incremental
        step N (2, 3, ...).

        Return a list of tuples (module name, file name, program text).
        """
        m = re.search('# cmd: mypy -m ([a-zA-Z0-9_. ]+)$', program_text, flags=re.MULTILINE)
        regex = '# cmd{}: mypy -m ([a-zA-Z0-9_. ]+)$'.format(incremental_step)
        alt_m = re.search(regex, program_text, flags=re.MULTILINE)
        if alt_m is not None and incremental_step > 1:
            # Optionally return a different command if in a later step
            # of incremental mode, otherwise default to reusing the
            # original cmd.
            m = alt_m

        if m:
            # The test case wants to use a non-default main
            # module. Look up the module and give it as the thing to
            # analyze.
            module_names = m.group(1)
            out = []
            for module_name in module_names.split(' '):
                path = build.find_module(module_name, [test_temp_dir])
                assert path is not None, "Can't find ad hoc case file"
                with open(path) as f:
                    program_text = f.read()
                out.append((module_name, path, program_text))
            return out
        else:
            return [('__main__', 'main', program_text)]

    def parse_options(self, program_text: str, testcase: DataDrivenTestCase,
                      incremental_step: int) -> Options:
        options = Options()
        flags = re.search('# flags: (.*)$', program_text, flags=re.MULTILINE)
        if incremental_step > 1:
            flags2 = re.search('# flags{}: (.*)$'.format(incremental_step), program_text,
                               flags=re.MULTILINE)
            if flags2:
                flags = flags2

        flag_list = None
        if flags:
            flag_list = flags.group(1).split()
            targets, options = process_options(flag_list, require_targets=False)
            if targets:
                raise RuntimeError('Specifying targets via the flags pragma is not supported.')
        else:
            options = Options()

        # Allow custom python version to override testcase_pyversion
        if (not flag_list or
                all(flag not in flag_list for flag in ['--python-version', '-2', '--py2'])):
            options.python_version = testcase_pyversion(testcase.file, testcase.name)

        options.use_builtins_fixtures = True
        options.show_traceback = True
        options.incremental = True

        return options