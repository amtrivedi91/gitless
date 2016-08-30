# -*- coding: utf-8 -*-
# Gitless - a version control system built on top of Git.
# Licensed under GNU GPL v2.

"""Gitless's library."""


from __future__ import unicode_literals

import collections
import io
try:
  from itertools import izip as zip
except ImportError:
  pass

import itertools
import json
from locale import getpreferredencoding
import os
import re
import shutil

import pygit2
from sh import git, ErrorReturnCode


ENCODING = getpreferredencoding() or 'utf-8'


# Errors

class GlError(Exception): pass

class NotInRepoError(GlError): pass

class BranchIsCurrentError(GlError): pass

class ApplyFailedError(GlError): pass


# File status

GL_STATUS_UNTRACKED = 1
GL_STATUS_TRACKED = 2
GL_STATUS_IGNORED = 3


def init_repository(url=None):
  """Creates a new Gitless's repository in the cwd.

  Args:
    url: if given the local repository will be a clone of the remote repository
      given by this url.

  Returns:
    the Gitless's repository created
  """
  cwd = os.getcwd()
  try:
    pygit2.discover_repository(cwd)
    raise GlError('You are already in a Gitless repository')
  except KeyError:  # Expected
    if not url:
      repo = pygit2.init_repository(cwd)
      # We also create an initial root commit
      git.commit(allow_empty=True, m='Initialize repository')
      return repo

    try:
      git.clone(url, cwd)
    except ErrorReturnCode as e:
      raise GlError(stderr(e))

    # We get all remote branches as well and create local equivalents
    repo = Repository()
    remote = repo.remotes['origin']
    for rb in (remote.lookup_branch(bn) for bn in remote.listall_branches()):
      if rb.branch_name == 'master':
        continue
      new_b = repo.create_branch(rb.branch_name, rb.head)
      new_b.upstream = rb
    return repo


class Repository(object):
  """A Gitless's repository.

  Attributes:
    path: absolute path to the Gitless's dir (the .git dir).
    root: absolute path to the root of this repository.
    cwd: the current working directory relative to the root of this
      repository ('' if they are equal).
    config: the repository's configuration.
    current_branch: the current branch (a Branch object).
    remotes: the configured remotes (see RemoteCollection).
  """

  def __init__(self):
    """Create a Repository out of the current working repository.

    Raises:
      NotInRepoError: if there's no current working repository.
    """
    try:
      path = pygit2.discover_repository(os.getcwd())
    except KeyError:
      raise NotInRepoError('You are not in a Gitless\'s repository')

    self.git_repo = pygit2.Repository(path)
    self.remotes = RemoteCollection(self.git_repo.remotes, self)
    self.path = self.git_repo.path
    self.root = self.path[:-6]  # strip trailing /.git/
    self.config = self.git_repo.config

  @property
  def cwd(self):
    ret = os.path.relpath(os.getcwd(), self.root)
    return '' if ret == '.' else ret

  def revparse_single(self, revision):
    if '/' in revision:  # might be a remote branch
      remote, remote_branch = revision.split('/', 1)
      try:
        return self.remotes[remote].lookup_branch(remote_branch).head
      except KeyError:
        pass
    try:
      return self.git_repo.revparse_single(revision)
    except (KeyError, ValueError):
      raise ValueError('No commit found for {0}'.format(revision))

  def merge_base(self, b1, b2):
    try:
      return self.git_repo.merge_base(b1.target, b2.target)
    except KeyError:
      raise GlError('No common commit found between {0} and {1}'.format(b1, b2))

  def _fuse_commits_fp(self, b):
    return os.path.join(
        self.path, 'GL_FUSE_CIS_{0}'.format(b.branch_name.replace('/', '_')))

  def _ref_exists(self, ref):
    try:
      self.git_repo.lookup_reference(ref)
      return True
    except KeyError:
      return False

  def _ref_rm(self, ref):
    ref_path = os.path.join(self.path, ref)
    if os.path.exists(ref_path):
      os.remove(ref_path)

  def _ref_create(self, ref, value):
    ref_path = os.path.join(self.path, ref)
    with io.open(ref_path, 'w', encoding=ENCODING) as f:
      if value.startswith('refs'):
        value = 'ref: ' + value
      f.write(value + '\n')

  def _ref_target(self, ref):
    return self.git_repo.lookup_reference(ref).target


  # Branch related methods

  @property
  def current_branch(self):
    if self.git_repo.head_is_detached:
      b = self.git_repo.lookup_reference('GL_FUSE_ORIG_HEAD').resolve()
    else:
      b = self.git_repo.head
    return self.lookup_branch(b.shorthand)

  def create_branch(self, name, commit):
    """Create a new branch.

    Args:
      name: the name of the new branch.
      commit: the commit that is to become the "head" of the new branch.
    """
    try:
      return Branch(
          self.git_repo.create_branch(name, commit, False),  # force=False
          self)
    except ValueError as e:
      # Edit pygit2's error msg (the message exposes Git details that will
      # confuse the Gitless's user)
      raise ValueError(
          str(e).replace('refs/heads/', '').replace('reference', 'branch'))

  def lookup_branch(self, branch_name):
    """Return the branch object corresponding to the given branch name."""
    git_branch = self.git_repo.lookup_branch(
        branch_name, pygit2.GIT_BRANCH_LOCAL)
    if git_branch:
      return Branch(git_branch, self)

  def listall_branches(self):
    """Return a list with the names of all the branches in this repository.

    Use lookup_branch if you want to get the Branch object corresponding to
    each name.
    """
    return self.git_repo.listall_branches(pygit2.GIT_BRANCH_LOCAL)

  def switch_current_branch(self, dst_b, move_over=False):
    """Switches to the given branch.

    Args:
      dst_b: the destination branch.
      move_over: if True, then uncommited changes made in the current branch are
        moved to the destination branch (defaults to False).
    """
    if dst_b.is_current:
      raise ValueError(
          'You are already on branch {0}. No need to switch.'.format(
             dst_b.branch_name))

    INFO_SEP = '|'
    ANCESTOR = 'ancestor'
    THEIRS = 'theirs'
    OURS = 'ours'
    REF_INFO = 'ref_info'
    CONF_INFO = 'conf_info'
    MSG_INFO = 'msg_info'

    git_repo = self.git_repo
    au_fp = lambda b: os.path.join(
        self.path, 'GL_AU_{0}'.format(b.branch_name.replace('/', '_')))
    update_index = git.bake('update-index', _cwd=self.root)

    def save(b):
      msg = _stash_msg(b.branch_name)

      # Save assumed unchanged info
      au_fps = ' '.join(b._au_files())
      if au_fps:
        with io.open(au_fp(b), mode='w', encoding=ENCODING) as f:
          f.write(au_fps)
        update_index('--no-assume-unchanged', au_fps)

      if b.merge_in_progress or b.fuse_in_progress:
        body = {}
        if move_over:
          raise GlError(
              'Changes can\'t be moved over with a fuse or merge in progress')

        # Save msg info
        merge_msg_fp = os.path.join(self.path, 'MERGE_MSG')
        with io.open(merge_msg_fp, 'r', encoding=ENCODING) as f:
          merge_msg = f.read()
        os.remove(merge_msg_fp)
        body[MSG_INFO] = merge_msg

        # Save conflict info
        conf_info = {}
        index = git_repo.index
        index.read()
        if index.conflicts:
          extract = lambda e: {'mode': e.mode, 'id': str(e.id), 'path': e.path}
          for ancestor, ours, theirs in index.conflicts:
            if ancestor:
              path = ancestor.path
              ancestor = extract(ancestor)
            if theirs:
              path = theirs.path
              theirs = extract(theirs)
            if ours:
              path = ours.path
              ours = extract(ours)

            conf_info[path] = {ANCESTOR: ancestor, THEIRS: theirs, OURS: ours}
            index.add(path)

          index.write()
        body[CONF_INFO] = conf_info

        # Save ref info
        if b.merge_in_progress:
          ref_info = {'MERGE_HEAD': str(self._ref_target('MERGE_HEAD'))}
          self._ref_rm('MERGE_HEAD')
        else:
          ref_info = {
              'HEAD': str(git_repo.head.target),
              'GL_FUSE_ORIG_HEAD': str(self._ref_target('GL_FUSE_ORIG_HEAD')),
              'CHERRY_PICK_HEAD': str(self._ref_target('CHERRY_PICK_HEAD'))
              }
          self._ref_rm('GL_FUSE_ORIG_HEAD')
          self._ref_rm('CHERRY_PICK_HEAD')
        body[REF_INFO] = ref_info

        msg += INFO_SEP + json.dumps(body)

      if not move_over:
        # Stash
        git.stash.save('--all', '--', msg)

    def restore(b):
      s_id, msg = _stash(_stash_msg(b.branch_name))
      if not s_id:
        return

      def restore_au_info():
        au = au_fp(b)
        if os.path.exists(au):
          with io.open(au, mode='r', encoding=ENCODING) as f:
            au_fps = f.read()
          update_index('--assume-unchanged', au_fps)
          os.remove(au)

      split_msg = msg.split(INFO_SEP)

      if len(split_msg) == 1:  # No op to restore
        # Pop
        git.stash.pop(s_id)
        # Restore assumed unchanged info
        restore_au_info()
      else:  # Restore op
        body = json.loads(split_msg[1])
        # Restore ref info
        ref_info = body[REF_INFO]
        if 'GL_FUSE_ORIG_HEAD' in ref_info:  # fuse
          head = git_repo[ref_info['HEAD']]
          git_repo.set_head(head.id)
          git_repo.reset(head.id, pygit2.GIT_RESET_HARD)
          self._ref_create('CHERRY_PICK_HEAD', ref_info['CHERRY_PICK_HEAD'])
          self._ref_create('GL_FUSE_ORIG_HEAD', ref_info['GL_FUSE_ORIG_HEAD'])
        else:  # merge
          self._ref_create('MERGE_HEAD', ref_info['MERGE_HEAD'])

        # Pop
        git.stash.pop(s_id)

        # Restore conflict info
        conf_info = body[CONF_INFO]
        rm_sentinel = lambda path: '0 {0}\t{1}'.format('0' * 40, path)
        build_entry = (
            lambda e, num: '{mode:o} {id} {0}\t{path}'.format(num, **e))
        index_info = []
        for path, index_e in conf_info.items():
          index_info.append(rm_sentinel(path))
          if index_e[ANCESTOR]:
            index_info.append(build_entry(index_e[ANCESTOR], 1))
          if index_e[OURS]:
            index_info.append(build_entry(index_e[OURS], 2))
          if index_e[THEIRS]:
            index_info.append(build_entry(index_e[THEIRS], 3))

        update_index('--unresolve', _in=' '.join(conf_info.keys()))
        update_index('--index-info', _in='\n'.join(index_info))

        # Restore msg info
        merge_msg_fp = os.path.join(self.path, 'MERGE_MSG')
        with io.open(merge_msg_fp, 'w', encoding=ENCODING) as f:
          f.write(body[MSG_INFO])

        # Restore assumed unchanged info
        restore_au_info()

    save(self.current_branch)
    git_repo.checkout(dst_b.git_branch)
    restore(dst_b)


class RemoteCollection(object):

  def __init__(self, git_remote_collection, gl_repo):
    self.git_remote_collection = git_remote_collection
    self.gl_repo = gl_repo

  def __len__(self):
    return len(self.git_remote_collection)

  def __iter__(self):
    return (Remote(r, self.gl_repo) for r in self.git_remote_collection)

  def __getitem__(self, name):
    return Remote(self.git_remote_collection.__getitem__(name), self.gl_repo)

  def __contains__(self, name):
    try:
      self.git_remote_collection.__getitem__(name)
      return True
    except KeyError:
      return False

  def create(self, name, url):
    if '/' in name:
      raise ValueError(
          'Invalid remote name \'{0}\': remotes can\'t have \'/\''.format(name))
    if not url.strip():
      raise ValueError('Invalid url \'{0}\''.format(url))

    # Check that the given url corresponds to a git repo
    try:
      git('ls-remote', '--heads', url)
    except ErrorReturnCode as e:
      raise ValueError(stderr(e))

    self.git_remote_collection.create(name, url)

  def delete(self, name):
    self.git_remote_collection.delete(name)


class Remote(object):
  """Tracked remote repository.

  Attributes:
    name: the name of this remote.
    url: the url of this remote.
  """

  def __init__(self, git_remote, gl_repo):
    self.git_remote = git_remote
    self.gl_repo = gl_repo
    self.name = self.git_remote.name
    self.url = self.git_remote.url


  def create_branch(self, name, commit):
    """Create a new branch in the remote repository.

    Args:
      name: the name of the new branch.
      commit: the commit that is to become the "head" of the new branch.
    """
    if self.lookup_branch(name):
      raise GlError(
          'Branch {0} already exists in remote repository {1}'.format(
              name, self.name))
    # Push won't let us push the creation of a new branch from a SHA. So we
    # create a temporary local ref, make it point to the commit, and do the
    # push
    tmp_b = self.gl_repo.create_branch('gl_tmp_ref', commit)
    try:
      git.push(self.name, '{0}:{1}'.format(tmp_b, name))
      return self.lookup_branch(name)
    except ErrorReturnCode as e:
      raise GlError(stderr(e))
    finally:
      tmp_b.delete()

  def listall_branches(self):
    """Return a list with the names of all the branches in this repository.

    Use lookup_branch if you want to get the RemoteBranch object corresponding
    to each name.
    """
    regex = re.compile(r'.*\trefs/heads/(.*)')
    for head in stdout(git('ls-remote', '--heads', self.name)).splitlines():
      yield regex.match(head).group(1)

  def lookup_branch(self, branch_name):
    """Return the RemoteBranch object corresponding to the given branch name."""
    if not stdout(git('ls-remote', '--heads', self.name, branch_name)):
      return None
    # The branch exists in the remote
    git.fetch(self.git_remote.name, branch_name)
    git_branch = self.gl_repo.git_repo.lookup_branch(
        self.git_remote.name + '/' + branch_name, pygit2.GIT_BRANCH_REMOTE)
    return RemoteBranch(git_branch, self.gl_repo)


class RemoteBranch(object):
  """A branch that lives on some remote repository.

  Attributes:
    branch_name: the name of this branch.
    remote_name: the name of the remote that represents the remote repository
      where this branch lives.
    head: commit that is the head of this branch.
  """

  def __init__(self, git_branch, gl_repo):
    self.git_branch = git_branch
    self.gl_repo = gl_repo
    self.remote_name = self.git_branch.remote_name
    self.branch_name = self.git_branch.branch_name[len(self.remote_name) + 1:]


  def delete(self):
    try:
      git.push(self.remote_name, ':{0}'.format(self.branch_name))
    except ErrorReturnCode as e:
      raise GlError(stderr(e))

  @property
  def target(self):
    """Object Id of the commit this branch points to."""
    self._update()
    return self.git_branch.target

  @property
  def head(self):
    self._update()
    return self.git_branch.peel()

  def history(self, reverse=False):
    return walker(self.gl_repo.git_repo, self.target, reverse=reverse)

  def _update(self):
    git.fetch(self.remote_name, self.branch_name)
    self.git_branch = self.gl_repo.git_repo.lookup_branch(
        self.remote_name + '/' + self.branch_name, pygit2.GIT_BRANCH_REMOTE)

  def __str__(self):
    return self.remote_name + '/' + self.branch_name


class Branch(object):
  """An independent line of development.

  Attributes:
    branch_name: the name of this branch.
    upstream: the upstream of this branch.
    is_current: True if this branch is the current branch in the repository it
      belongs to.
    merge_in_progress: True if a merge op is in progress on this branch.
    fuse_in_progress: True if a fuse op is in progress on this branch.
    head: commit that is the head of this branch.
  """

  def __init__(self, git_branch, gl_repo):
    self.git_branch = git_branch
    self.gl_repo = gl_repo
    self.branch_name = self.git_branch.branch_name


  def delete(self):
    if self.is_current:
      raise BranchIsCurrentError('Can\'t delete the current branch')

    self.git_branch.delete()

    # We also cleanup any stash left
    s_id, _ = _stash(_stash_msg(self.branch_name))
    if s_id:
      git.stash.drop(s_id)

  @property
  def upstream(self):
    git_upstream = self.git_branch.upstream
    if not git_upstream:
      return None

    try:
      git_upstream.remote_name
      return RemoteBranch(git_upstream, self.gl_repo)
    except ValueError:  # Upstream is a local branch
      return Branch(git_upstream, self.gl_repo)

  @upstream.setter
  def upstream(self, new_upstream):
    self.git_branch.upstream = new_upstream.git_branch if new_upstream else None

  @property
  def head(self):
    self._update()
    return self.git_branch.peel()

  @head.setter
  def head(self, new_head):
    self.gl_repo.git_repo.reset(new_head, pygit2.GIT_RESET_SOFT)

  @property
  def target(self):
    """Object Id of the commit this branch points to."""
    self._update()
    return self.git_branch.target

  @property
  def is_current(self):
    return self.gl_repo.current_branch.branch_name == self.branch_name

  def _update(self):
    self.git_branch = self.gl_repo.git_repo.lookup_branch(
        self.branch_name, pygit2.GIT_BRANCH_LOCAL)

  def history(self, reverse=False):
    return walker(self.gl_repo.git_repo, self.target, reverse=reverse)

  def diff_commits(self, c1, c2):
    return c1.tree.diff_to_tree(c2.tree)

  def __str__(self):
    return self.branch_name

  @property
  def _index(self):
    """Convenience wrapper of Git's index."""
    class Index(object):

      def __init__(self, git_index):
        self._git_index = git_index
        self._git_index.read()

      def __enter__(self):
        return self

      def __exit__(self, type, value, traceback):
        if not value:  # no exception
          self._git_index.write()
          return True

      def __getattr__(self, name):
        return getattr(self._git_index, name)

    return Index(self.gl_repo.git_repo.index)

  _st_map = {
    # git status: gl status, exists_at_head, exists_in_wd, modified

    pygit2.GIT_STATUS_CURRENT: (GL_STATUS_TRACKED, True, True, False),
    pygit2.GIT_STATUS_IGNORED: (GL_STATUS_IGNORED, False, True, True),

    ### WT_* ###
    pygit2.GIT_STATUS_WT_NEW: (GL_STATUS_UNTRACKED, False, True, True),
    pygit2.GIT_STATUS_WT_MODIFIED: (GL_STATUS_TRACKED, True, True, True),
    pygit2.GIT_STATUS_WT_DELETED: (GL_STATUS_TRACKED, True, False, True),

    ### INDEX_* ###
    pygit2.GIT_STATUS_INDEX_NEW: (GL_STATUS_TRACKED, False, True, True),
    pygit2.GIT_STATUS_INDEX_MODIFIED: (GL_STATUS_TRACKED, True, True, True),
    pygit2.GIT_STATUS_INDEX_DELETED: (GL_STATUS_TRACKED, True, False, True),

    ### WT_NEW | INDEX_* ###
    # WT_NEW | INDEX_NEW -> can't happen
    # WT_NEW | INDEX_MODIFIED -> can't happen
    # WT_NEW | INDEX_DELETED -> could happen if user broke gl layer (e.g., did
    # `git rm` and then created file with same name). Also, for some reason,
    # files with conflicts have this status code
    pygit2.GIT_STATUS_WT_NEW | pygit2.GIT_STATUS_INDEX_DELETED: (
      GL_STATUS_TRACKED, True, True, True),

    ### WT_MODIFIED | INDEX_* ###
    pygit2.GIT_STATUS_WT_MODIFIED | pygit2.GIT_STATUS_INDEX_NEW: (
      GL_STATUS_TRACKED, False, True, True),
    pygit2.GIT_STATUS_WT_MODIFIED | pygit2.GIT_STATUS_INDEX_MODIFIED: (
      GL_STATUS_TRACKED, True, True, True),
    # WT_MODIFIED | INDEX_DELETED -> can't happen

    ### WT_DELETED | INDEX_* ### -> can't happen
    }

  FileStatus = collections.namedtuple(
    'FileStatus', [
        'fp', 'type', 'exists_at_head', 'exists_in_wd', 'modified',
        'in_conflict'])

  def _au_files(self):
    for f_out in stdout(
        git('ls-files', '-v', _cwd=self.gl_repo.root)).splitlines():
      if f_out[0] == 'h':
        yield f_out[2:].strip()

  def status(self):
    """Return a generator of file statuses (see FileStatus).

    Ignored and tracked unmodified files are not reported.
    File paths are always relative to the repo root.
    """
    index = self._index

    for fp, git_s in self.gl_repo.git_repo.status().items():
      in_conflict = False
      if index.conflicts:
        try:  # `fp in index.conflicts` doesn't work
          index.conflicts[fp]
          in_conflict = True
        except KeyError:
          pass
      yield self.FileStatus(fp, *(self._st_map[git_s] + (in_conflict,)))

    # status doesn't report au files
    au_files = self._au_files()
    if au_files:
      for fp in au_files:
        exists_in_wd = os.path.exists(os.path.join(self.gl_repo.root, fp))
        yield self.FileStatus(
            fp, GL_STATUS_UNTRACKED, True, exists_in_wd, True, False)

  def status_file(self, path):
    """Return the status (see FileStatus) of the given path."""
    return self._status_file(path)[0]

  def _status_file(self, path):
    assert not os.path.isabs(path)

    git_s = self.gl_repo.git_repo.status_file(path)

    cmd_out = stdout(git(
      'ls-files', '-v', '--full-name', path, _cwd=self.gl_repo.root))
    if cmd_out and cmd_out[0] == 'h':
      exists_in_wd = os.path.exists(os.path.join(self.gl_repo.root, path))
      return (
          self.FileStatus(
            path, GL_STATUS_UNTRACKED, True, exists_in_wd, True, False),
          git_s, True)

    index = self._index
    in_conflict = False
    if index.conflicts:
      try:  # `fp in index.conflicts` doesn't work
        index.conflicts[path]
        in_conflict = True
      except KeyError:
        pass
    f_st = self.FileStatus(path, *(self._st_map[git_s] + (in_conflict,)))
    return f_st, git_s, False


  # File related methods

  def track_file(self, path):
    """Start tracking changes to path."""
    assert not os.path.isabs(path)

    gl_st, git_st, is_au = self._status_file(path)

    if gl_st.type == GL_STATUS_TRACKED:
      raise ValueError('File {0} is already tracked'.format(path))
    elif gl_st.type == GL_STATUS_IGNORED:
      raise ValueError(
          'File {0} is ignored. Edit the .gitignore file to stop ignoring '
          'file {0}'.format(path))

    # If we reached this point we know that the file to track is a untracked
    # file. This means that in the Git world, the file could be either:
    #   (i)  a new file for Git => add the file;
    #   (ii) an assumed unchanged file => unmark it.
    if git_st == pygit2.GIT_STATUS_WT_NEW:  # Case (i)
      with self._index as index:
        index.add(path)
    elif is_au:  # Case (ii)
      git('update-index', '--no-assume-unchanged', path,
          _cwd=self.gl_repo.root)
    else:
      raise GlError('File {0} in unkown status {1}'.format(path, git_st))

  def untrack_file(self, path):
    """Stop tracking changes to the given path."""
    assert not os.path.isabs(path)

    gl_st, git_st, is_au = self._status_file(path)

    if gl_st.type == GL_STATUS_UNTRACKED:
      raise ValueError('File {0} is already untracked'.format(path))
    elif gl_st.type == GL_STATUS_IGNORED:
      raise ValueError(
          'File {0} is ignored. Edit the .gitignore file to stop ignoring '
          'file {0}'.format(path))
    elif gl_st.in_conflict:
      raise ValueError('File {0} has conflicts'.format(path))

    # If we reached this point we know that the file to untrack is a tracked
    # file. This means that in the Git world, the file could be either:
    #   (i)  a new file for Git that is staged (the user executed `gl track` on
    #        an uncomitted file) => reset changes;
    #   (ii) the file is a previously committed file => mark it as assumed
    #        unchanged.
    if git_st == pygit2.GIT_STATUS_INDEX_NEW:  # Case (i)
      with self._index as index:
        index.remove(path)
    elif not is_au:  # Case (ii)
      git('update-index', '--assume-unchanged', path,
          _cwd=self.gl_repo.root)
    else:
      raise GlError('File {0} in unkown status {1}'.format(path, git_st))

  def resolve_file(self, path):
    """Mark the given path as resolved."""
    assert not os.path.isabs(path)

    gl_st, _, _ = self._status_file(path)
    if not gl_st.in_conflict:
      raise ValueError('File {0} has no conflicts'.format(path))

    with self._index as index:
      index.add(path)

  def checkout_file(self, path, commit):
    """Checkouts the given path at the given commit."""
    assert not os.path.isabs(path)

    data = self.gl_repo.git_repo[commit.tree[path].id].data
    with io.open(os.path.join(self.gl_repo.root, path), mode='wb') as dst:
      dst.write(data)

    # So as to not get confused with the status of the file we also add it
    with self._index as index:
      index.add(path)

  def diff_file(self, path):
    """Diff the working version of the given path with its committed version."""
    assert not os.path.isabs(path)

    git_repo = self.gl_repo.git_repo
    try:
      blob_at_head = git_repo[git_repo.head.peel().tree[path].id]
    except KeyError:  # no blob at head
      wt_blob = git_repo[git_repo.create_blob_fromworkdir(path)]
      nil_blob = git_repo[git_repo.create_blob('')]
      return nil_blob.diff(wt_blob, 0, path, path)

    try:
      wt_blob = git_repo[git_repo.create_blob_fromworkdir(path)]
    except KeyError:  # no blob at wd (the file was deleted)
      nil_blob = git_repo[git_repo.create_blob('')]
      return blob_at_head.diff(nil_blob, 0, path, path)

    return blob_at_head.diff(wt_blob, 0, path, path)


  # Merge related methods

  def merge(self, src):
    """Merges the divergent changes of the src branch onto this one."""
    self._check_is_current()
    self._check_op_not_in_progress()

    result, unused_ff_conf = self.gl_repo.git_repo.merge_analysis(src.target)
    if result & pygit2.GIT_MERGE_ANALYSIS_UP_TO_DATE:
      raise GlError('No commits to merge')
    try:
      git.merge(src, '--no-ff')
    except ErrorReturnCode as e:
      raise GlError(stdout(e) + stderr(e))

  @property
  def merge_in_progress(self):
    return self.gl_repo._ref_exists('MERGE_HEAD')

  def abort_merge(self):
    if not self.merge_in_progress:
      raise GlError('No merge in progress, nothing to abort')
    git.merge(abort=True)


  # Fuse related methods

  @property
  def _fuse_commits_fp(self):
    return self.gl_repo._fuse_commits_fp(self)

  def _save_fuse_commits(self, commits):
    path = self._fuse_commits_fp
    using_tmp = False
    if os.path.exists(path):
      path = path + '_tmp'
      using_tmp = True
    with io.open(path, mode='w', encoding=ENCODING) as f:
      for ci in commits:
        f.write(ci.id.hex + '\n')
    if using_tmp:
      shutil.move(path, self._fuse_commits_fp)

  def _load_fuse_commits(self):
    git_repo = self.gl_repo.git_repo
    with io.open(self._fuse_commits_fp, mode='r', encoding=ENCODING) as f:
      for ci_id in f:
        ci_id = ci_id.strip()
        yield git_repo[ci_id]
    os.remove(self._fuse_commits_fp)

  def fuse(self, src, ip, only=None, exclude=None, fuse_cb=None):
    """Fuse the given commits onto this branch.

    Args:
      src: the branch (Brach obj) to fuse commits from.
      ip: id of the commit to act as the insertion point. The commits to fuse
        are inserted after this commit. ip has to correspond to one of the
        divergent commits from self or the divergent point.
      only: ids of commits to use only.
      exclude: ids of commtis to exclude.
      fuse_cb: see FuseCb.
    """
    self._check_is_current()
    self._check_op_not_in_progress()

    save_fn = fuse_cb.save if fuse_cb else None
    repo = self.gl_repo
    mb = repo.merge_base(self, src)

    if mb == src.target:  # either self is ahead or both branches are equal
      raise GlError('No commits to fuse')

    mb_to_src = src.history(reverse=True)
    mb_to_src.hide(mb)
    divergent_commits, fuse_commits = itertools.tee(mb_to_src, 2)

    if only:
      fuse_commits = (ci for ci in fuse_commits if ci.id in only)
    elif exclude:
      fuse_commits = (ci for ci in fuse_commits if ci.id not in exclude)

    fuse_commits, _fuse_commits = itertools.tee(fuse_commits, 2)
    if not any(_fuse_commits):
      raise GlError('No commits to fuse')

    # Figure out where to detach head
    # If the ip is not the mb, then we need to detach at the ip, because the
    # first div commit won't have the ip as its parent
    # But, if the ip **is** the mb we can advance the head until the div
    # commits and the commits to fuse diverge
    detach_point = ip
    if ip == mb:
      for ci, fuse_ci in zip(divergent_commits, fuse_commits):
        if ci.id != fuse_ci.id:
          fuse_commits = itertools.chain([fuse_ci], fuse_commits)
          break
        detach_point = ci.id
        if fuse_cb and fuse_cb.apply_ok:
          fuse_cb.apply_ok(ci)

    after_commits = self.history(reverse=True)
    after_commits.hide(ip)
    commits = itertools.chain(fuse_commits, after_commits)
    commits, _commits = itertools.tee(commits, 2)
    if not any(_commits):  # it's a ff
      self._safe_reset(detach_point, _stash_msg_fuse, save_fn=save_fn)
      restore_fn = fuse_cb.restore_ok if fuse_cb else None
      self._safe_restore(_stash_msg_fuse, restore_fn=restore_fn)
      return

    # We are going to have to do some cherry-picking

    # Save the current head so that we remember the current branch
    head_fp = os.path.join(repo.path, 'HEAD')
    orig_head_fp = os.path.join(repo.path, 'GL_FUSE_ORIG_HEAD')
    shutil.copyfile(head_fp, orig_head_fp)

    # Detach head so that reset doesn't reset master and instead
    # resets the head ref
    repo.git_repo.set_head(repo.git_repo.head.peel().id)
    self._safe_reset(detach_point, _stash_msg_fuse, save_fn=save_fn)

    self._fuse(commits, fuse_cb=fuse_cb)

  def fuse_continue(self, fuse_cb=None):
    """Resume a fuse in progress."""
    if not self.fuse_in_progress:
      raise GlError('No fuse in progress, nothing to continue')
    commits = self._load_fuse_commits()
    self._fuse(commits, fuse_cb=fuse_cb)

  def _fuse(self, commits, fuse_cb=None):
    git_repo = self.gl_repo.git_repo
    committer = git_repo.default_signature

    for ci in commits:
      git_repo.cherrypick(ci.id)
      index = self._index
      if index.conflicts:
        if fuse_cb and fuse_cb.apply_err:
          fuse_cb.apply_err(ci)
        self._save_fuse_commits(commits)
        raise GlError('There are conflicts you need to resolve')

      if fuse_cb and fuse_cb.apply_ok:
        fuse_cb.apply_ok(ci)
      tree_oid = index.write_tree(git_repo)
      git_repo.create_commit(
          'HEAD',  # the name of the reference to update
          ci.author, committer, ci.message, tree_oid,
          [git_repo.head.target])

    # We are done fusing => update original branch and re-attach head
    orig_branch_ref = git_repo.lookup_reference('GL_FUSE_ORIG_HEAD').resolve()
    orig_branch_ref.set_target(git_repo.head.target)
    git_repo.set_head(orig_branch_ref.name)
    self._state_cleanup()
    restore_fn = fuse_cb.restore_ok if fuse_cb else None
    self._safe_restore(_stash_msg_fuse, restore_fn=restore_fn)

  @property
  def fuse_in_progress(self):
    return self.gl_repo._ref_exists('GL_FUSE_ORIG_HEAD')

  def abort_fuse(self, fuse_cb=None):
    if not self.fuse_in_progress:
      raise GlError('No fuse in progress, nothing to abort')
    git_repo = self.gl_repo.git_repo
    git_repo.set_head(git_repo.lookup_reference('GL_FUSE_ORIG_HEAD').target)
    git_repo.reset(git_repo.head.peel().hex, pygit2.GIT_RESET_HARD)

    self._state_cleanup()
    restore_fn = fuse_cb.restore_ok if fuse_cb else None
    self._safe_restore(_stash_msg_fuse, restore_fn=restore_fn)

  def _state_cleanup(self):
    self.gl_repo.git_repo.state_cleanup()
    if os.path.exists(self._fuse_commits_fp):
      os.remove(self._fuse_commits_fp)
    self.gl_repo._ref_rm('GL_FUSE_ORIG_HEAD')

  def _safe_reset(self, cid, msg_fn, save_fn=None):
    git_repo = self.gl_repo.git_repo
    tree = git_repo[cid].tree
    try:
      git_repo.checkout_tree(tree)
    except pygit2.GitError:  # conflicts prevent checkout
      # TODO: this hack will cover most cases, but it won't help if the conflict
      # is caused by untracked files (nonetheless `stash pop` won't work in that
      # case either so we need to find an alternative way of doing this)
      if save_fn:
        save_fn()
      git.stash.save('--', msg_fn(self))
      git_repo.checkout_tree(tree)
    git_repo.reset(cid, pygit2.GIT_RESET_SOFT)

  def _safe_restore(self, msg_fn, restore_fn=None):
    s_id, _ = _stash(msg_fn(self))
    if s_id:
      try:
        git.stash.pop(s_id)
        if restore_fn:
          restore_fn()
      except ErrorReturnCode:
        raise ApplyFailedError(
            'Uncommitted changes failed to apply onto the new head of the '
            'branch')


  def create_commit(self, files, msg, author=None):
    """Record a new commit on this branch.

    Args:
      files: the (modified) files to commit.
      msg: the commit message.
      author: the author of the commit (defaults to the default author
        according to the repository's configuration).
    """
    git_repo = self.gl_repo.git_repo
    if not author:
      author = git_repo.default_signature

    index = self._index
    if index.conflicts:
      raise GlError('Unresolved conflicts')

    # We replicate the behaviour of doing `git commit <file>...`
    # If file f is in the list of files to be committed => commit the working
    # version and clear the staged version.
    # If file f is not in the list of files to be committed => leave its staged
    # version (if any) intact.

    def get_tree_and_update_index():

      def update():
        """Add/remove files to the index."""
        for f in files:
          assert not os.path.isabs(f)
          if not os.path.exists(os.path.join(self.gl_repo.root, f)):
            index.remove(f)
          else:
            index.add(f)

      # Update index to how it should look like after the commit
      with index:
        update()

      # To create the commit tree with only the changes to the given files we:
      #   (i)   reset the index to HEAD,
      #   (ii)  update it with the changes to commit,
      #   (iii) create a tree out of this modified index, and
      #   (iv)  discard the changes after being done.
      index.read_tree(git_repo.head.peel().tree)
      update()
      tree_oid = index.write_tree()

      index.read()  #  discard changes

      return tree_oid

    parents = [git_repo.head.target]
    if self.merge_in_progress:
      parents.append(git_repo.lookup_reference('MERGE_HEAD').target)

    ci_oid = git_repo.create_commit(
        'HEAD',  # will point to the new commit
        author, author,  # use author as committer
        msg, get_tree_and_update_index(),  # the commit tree
        parents)

    if self.merge_in_progress:
      self.gl_repo.git_repo.state_cleanup()

    return self.gl_repo.git_repo[ci_oid]

  def publish(self, branch):
    self._check_op_not_in_progress()

    if not isinstance(branch, RemoteBranch):  # TODO: allow this
      raise GlError(
          'Can\'t publish to a local branch (yet---this will be implemented in '
          'the future)')

    try:
      assert self.branch_name.strip()
      assert branch.branch_name in self.gl_repo.remotes[
          branch.remote_name].listall_branches()

      cmd = git.push(
          branch.remote_name,
          '{0}:{1}'.format(self.branch_name, branch.branch_name))
      if 'Everything up-to-date' in stderr(cmd):
        raise GlError('No commits to publish')
    except ErrorReturnCode as e:
      err_msg = stderr(e)
      if 'Updates were rejected' in err_msg:
        raise GlError('There are changes you need to fuse/merge')
      raise GlError(err_msg)


  # Branch helpers

  def _check_op_not_in_progress(self):
    if self.merge_in_progress:
      raise GlError('Merge in progress')
    if self.fuse_in_progress:
      raise GlError('Fuse in progress')

  def _check_is_current(self):
    if not self.is_current:
      raise BranchIsCurrentError(
        'Branch {0} is the current branch'.format(self.branch_name))


# Helpers for stashing

def _stash(pattern):
  """Returns the id and msg of the stash that matches the given pattern."""
  out = stdout(
      git.stash.list(grep=pattern, format='|*|%gd|*|%B|*|', _tty_out=False))
  if not out:
    return None, None

  result = re.match(r'\|\*\|(stash@\{.+\})\|\*\|(.*)\|\*\|', out, re.DOTALL)
  if not result:
    raise GlError('Unexpected output of git stash: {0}'.format(out))

  return result.group(1).strip(), result.group(2).strip()

def _stash_msg(name):
  return '---gl-{0}---'.format(name)

def _stash_msg_fuse(name):
  return _stash_msg('fuse-{0}'.format(name))


# Misc

FuseCb = collections.namedtuple(
    'FuseCb', ['apply_ok', 'apply_err', 'save', 'restore_ok'])

def stdout(p):
  return p.stdout.decode(ENCODING)

def stderr(p):
  return p.stderr.decode(ENCODING)

def walker(git_repo, target, reverse):
  flags = pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_TIME
  if reverse:
    flags = flags | pygit2.GIT_SORT_REVERSE
  return git_repo.walk(target, flags)
