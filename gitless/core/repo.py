# Gitless - a version control system built on top of Git.
# Copyright (c) 2013  Santiago Perez De Rosso.
# Licensed under GNU GPL, version 2.

"""Gitless's repo lib."""


import os

import branch

from gitpylib import common as git_common
from gitpylib import config as git_config
from gitpylib import repo as git_repo
from gitpylib import remote as git_remote


# Ret codes of methods.
SUCCESS = 1
NOTHING_TO_INIT = 2
REPO_UNREACHABLE = 3


def cwd():
  """Gets the Gitless's cwd."""
  dr = os.path.dirname(gl_dir())
  cwd = os.getcwd()
  return '/' if dr == cwd else cwd[len(dr):]


def gl_dir():
  """Gets the path to the gl directory.

  Returns:
    the absolute path to the gl directory or None if the current working
    directory is not a Gitless repository.
  """
  # We use the same .git directory.
  return git_common.git_dir()


def init_from(remote_repo):
  """Clones the remote_repo into the cwd."""
  if gl_dir():
    return NOTHING_TO_INIT
  if not git_repo.clone(remote_repo):
    return REPO_UNREACHABLE
  # We get all remote branches as well and create local equivalents.
  for remote_branch in git_remote.branches('origin'):
    if remote_branch == 'master':
      continue
    s = branch.create(remote_branch, 'origin/{}'.format(remote_branch))
    if s != SUCCESS:
      raise Exception(
          'Unexpected status code {} when creating local branch {}'.format(
              s, remote_branch))
  return SUCCESS


def init_dir():
  """Makes the cwd a Gitless's repository."""
  if gl_dir():
    return NOTHING_TO_INIT
  git_repo.init()
  return SUCCESS


def editor():
  """Returns the editor set up by the user (defaults to Vim)."""
  ret = git_config.get('core.editor')
  if ret:
    return ret
  # We check the $EDITOR variable.
  ret = os.environ['EDITOR'] if 'EDITOR' in os.environ else None
  if ret:
    return ret
  # We default to Vim.
  return 'vim'
