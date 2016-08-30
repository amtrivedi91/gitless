# Gitless - a version control system built on top of Git.
# Copyright (c) 2013  Santiago Perez De Rosso.
# Licensed under GNU GPL, version 2.

"""Common methods used in unit tests."""


from functools import wraps
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import gitless.core.file as file_lib


class TestCore(unittest.TestCase):
  """Base class for core tests."""

  def setUp(self):
    """Creates temporary dir and cds to it."""
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    self.path = tempfile.mkdtemp(prefix='gl-core-test')
    logging.debug('Created temporary directory {0}'.format(self.path))
    os.chdir(self.path)
    self._git_call('init')
    self._git_call('config user.name \"test\"')
    self._git_call('config user.email \"test@test.com\"')

  def tearDown(self):
    """Removes the temporary dir."""
    shutil.rmtree(self.path)
    logging.debug('Removed dir {0}'.format(self.path))

  # Python 2/3 compatibility.
  def assertItemsEqual(self, actual, expected, msg=None):
    try:
      return super(TestCore, self).assertItemsEqual(actual, expected, msg=msg)
    except AttributeError:
      try:
        return super(TestCore, self).assertCountEqual(actual, expected, msg=msg)
      except AttributeError:
        return self.assertEqual(sorted(actual), sorted(expected), msg=msg)

  def _write_file(self, fp, contents='hello'):
    dirs, _ = os.path.split(fp)
    if dirs and not os.path.exists(dirs):
      os.makedirs(dirs)
    f = open(fp, 'w')
    f.write(contents)
    f.close()

  def _append_to_file(self, fp, contents='hello'):
    f = open(fp, 'a')
    f.write(contents)
    f.close()

  def _read_file(self, fp):
    f = open(fp, 'r')
    ret = f.read()
    f.close()
    return ret

  def _git_call(self, subcmd, expected_ret_code=0):
    """Issues a git call with the given subcmd.

    Args:
      subcmd: e.g., 'add f1'.

    Returns:
      a tuple (out, err).
    """
    logging.debug('Calling git {0}'.format(subcmd))
    p = subprocess.Popen(
        'git {0}'.format(subcmd), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=True)
    out, err = p.communicate()
    if p.returncode != expected_ret_code:
      raise Exception(
          'Git call {0} failed (got ret code {1})\nout:{2} \nerr:{3}'.format(
              subcmd, p.returncode, out, err))
    return out, err


def stub(module, fake):
  """Stub the given module with the given fake.

  Each symbol in the module is overrwritten with its matching symbol
  in the fake.

  Args:
    module: the module to stub.
    fake: an instance of a class used for stubbing.
  """
  clz = dir(fake)
  for key_mod in dir(module):
    if key_mod in clz:
      fake_obj = getattr(fake, key_mod)
      if hasattr(fake_obj, '__call__'):
        fake_obj = fake_obj()
      setattr(module, key_mod, fake_obj)


def assert_contents_unchanged(*fps):
  """Decorator that fails the test if the contents of the file fp changed.

  The method decorated should be a unit test.

  Usage:
    @common.assert_contents_unchanged('f1')
    def test_method_that_shouldnt_modify_f1(self):
      # do something here.
      # assert something here.

  Args:
    fps: the filepath(s) to assert.
  """
  return __assert_decorator(
      'Contents', lambda self, fp: self._read_file(fp), *fps)


def assert_status_unchanged(*fps):
  """Decorator that fails the test if the status of fp changed.

  The method decorated should be a unit test.

  Usage:
    @common.assert_status_unchanged('f1')
    def test_method_that_shouldnt_modify_f1_status(self):
      # do something here.
      # assert something here.

  Args:
    fps: the filepath(s) to assert.
  """
  return __assert_decorator(
      'Status', lambda unused_self, fp: file_lib.status(fp), *fps)


def assert_no_side_effects(*fps):
  """Decorator that fails the test if the contents or status of fp changed.

  The method decorated should be a unit test.

  Usage:
    @common.assert_no_side_effects('f1')
    def test_method_that_shouldnt_affect_f1(self):
      # do something here.
      # assert something here.

  It is a shorthand of:
    @common.assert_status_unchanged('f1')
    @common.assert_contents_unchanged('f1')
    def test_method_that_shouldnt_affect_f1(self):
      # do something here.
      # assert something here.

  Args:
    fps: the filepath(s) to assert.
  """
  def decorator(f):
    @assert_contents_unchanged(*fps)
    @assert_status_unchanged(*fps)
    @wraps(f)
    def wrapper(*args, **kwargs):
      f(*args, **kwargs)
    return wrapper
  return decorator


# Private functions.


def __assert_decorator(msg, prop, *fps):
  def decorator(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
      self = args[0]
      # We save up the cwd to chdir to it after the test has run so that the
      # the given fps still "work" even if the test changed the cwd.
      cwd_before = os.getcwd()
      before_list = [prop(self, fp) for fp in fps]
      f(*args, **kwargs)
      os.chdir(cwd_before)
      after_list = [prop(self, fp) for fp in fps]
      for fp, before, after in zip(fps, before_list, after_list):
        self.assertEqual(
            before, after,
            '{0} of file "{1}" changed: from "{2}" to "{3}"'.format(
                msg, fp, before, after))
    return wrapper
  return decorator
