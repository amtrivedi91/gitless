#!/usr/bin/env python

import sys

from setuptools import setup

import gitless.cli.gl as gl


reqs = ['gitpylib>=0.5', 'clint==0.3.6']
if sys.version_info < (2, 7) or (
    sys.version_info < (3, 3) and sys.version_info > (3, 0)):
  reqs.append('argparse')


ld = """
Gitless is an experimental version control system built on top of Git.
Many people complain that Git is hard to use. We think the problem lies
deeper than the user interface, in the concepts underlying Git. Gitless
is an experiment to see what happens if you put a simple veneer on an
app that changes the underlying concepts. Because Gitless is implemented
on top of Git (could be considered what Git pros call a 'porcelain' of
Git), you can always fall back on Git. And of course your coworkers you
share a repo with need never know that you're not a Git aficionado.

More info, downloads and documentation @ `Gitless's
website <http://gitless.com>`__.

Questions or comments about Gitless can be sent to the `Gitless users
mailing list <https://groups.google.com/forum/#!forum/gl-users>`__.
"""

setup(
    name='gitless',
    version=gl.VERSION,
    description='A version control system built on top of Git',
    long_description=ld,
    author='Santiago Perez De Rosso',
    author_email='sperezde@csail.mit.edu',
    url=gl.URL,
    packages=['gitless', 'gitless.cli', 'gitless.core'],
    install_requires=reqs,
    license='GPLv2',
    classifiers=(
        'Development Status :: 2 - Pre-Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU General Public License v2 (GPLv2)',
        'Natural Language :: English',
        'Programming Language :: Python',
        'Topic :: Software Development :: Version Control'),
    entry_points={
        'console_scripts': [
            'gl = gitless.cli.gl:main'
        ]},
    test_suite='gitless.tests')
