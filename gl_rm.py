#!/usr/bin/env python2.7

# Gitless - a version control system built on top of Git.
# Copyright (c) 2013  Santiago Perez De Rosso.
# Licensed under GNU GPL, version 2.

"""gl-rm - Remove Gitless's files."""

# TODO(sperezde): This command is not required in Gitless, is just a stopgap
# until the other commands are fixed to deal with files that have been removed
# via Unix's rm command.

import file_lib

import cmd
import pprint


def parser(subparsers):
  """Adds the rm  parser to the given subparsers object."""
  rm_parser = subparsers.add_parser(
      'rm', help='remove tracked files')
  rm_parser.add_argument(
      'files', nargs='+', help='the file(s) to remove')
  rm_parser.set_defaults(func=main)


def main(args):
  cmd.check_gl_dir()
  errors_found = False

  for fp in args.files:
    ret = file_lib.rm(fp)
    if ret is file_lib.FILE_NOT_FOUND:
      pprint.err('Can\'t remove a non-existent file: %s' % fp)
      errors_found = True
    elif ret is file_lib.FILE_IS_UNTRACKED:
      pprint.err('File %s is an untracked file' % fp)
      errors_found = True
    elif ret is file_lib.SUCCESS:
      pprint.msg('File %s has been removed' % fp)
    else:
      raise Exception('Unexpected return code')

  return cmd.ERRORS_FOUND if errors_found else cmd.SUCCESS
