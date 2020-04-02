import os
import os.path
import shutil
import sys
import unittest
from contextlib import contextmanager

from mypy import defaults
from mypy.stubgen import Options, parse_options, generate_stubs
from mypy.stubutil import (
    default_py2_interpreter
)
from mypy.test.helpers import assert_equal


class SystemTests(unittest.TestCase):
    def test_python2_compatibility(self):
        with generate_stubs_from_sample('python2_compatibility.py'):
            os.system(default_py2_interpreter() + ' out/python2_compatibility.py')

    def test_basic_consts_keep_value(self):
        with generate_stubs_from_sample('basic_consts.py') as module:
            assert_equal(module.int_const, 5)
            assert_equal(module.bool_const, True)
            assert_equal(module.string_const, 'abc')
            assert_equal(module.unicode_const, None)
            assert_equal(module.float_const, 1.23)

    def test_function_body_is_pass(self):
        with generate_stubs_from_sample('function_translation.py') as module:
            module.foo()
            module.A.bar()

    def test_properties_keep_original_code(self):
        with generate_stubs_from_sample('property_with_original_code.py') as module:
            obj = module.A()
            assert_equal(obj.five_prop, 5)
            with self.assertRaises(Exception):
                obj.raising_prop



@contextmanager
def generate_stubs_from_sample(sample, python2=True, *args):
    full_sample_path = os.path.join('.', 'samples', sample)
    options = parse_options((['--py2'] if python2 else []) + list(args) + [full_sample_path])
    generate_stubs(options)
    try:
        yield __import__(f'out.{sample.split(".py")[0]}', fromlist=['a'])
    finally:
        shutil.rmtree('out')


if __name__ == '__main__':
    unittest.main()
