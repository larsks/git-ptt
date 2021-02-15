import configparser
import git
import logging
import re

from dataclasses import dataclass, field
from functools import cached_property

LOG = logging.getLogger(__name__)


@dataclass
class Branch:
    name: str
    head: str = field(init=False)
    commits: list = field(repr=False)

    shortid_len = 10

    def __post_init__(self):
        self.head = self.commits[0]

    @property
    def hexsha(self):
        return self.head.hexsha


class ApplicationError(Exception):
    pass


class NoRemoteError(ApplicationError):
    pass


class InvalidRemoteError(ApplicationError):
    pass


class PTT:
    default_marker = '@'
    default_base = 'master'
    default_short_id_len = 10

    def __init__(self, repo, base=None, remote=None, marker=None,
                 short_id_len=None):
        self.repo = repo
        self.base = repo.commit(
            base or
            self.config.get('base', self.default_base)
        )
        self.marker = (
            marker
            or self.config.get('marker', self.default_marker)
        )
        self.short_id_len = (
            short_id_len or
            self.config.get('short_id_len', self.default_short_id_len)
        )

        self._remote = remote or self.config.get('remote')
        self.update_branches()

    @cached_property
    def remote(self):
        if self._remote is None:
            raise NoRemoteError()

        try:
            _remote = self.repo.remote(self._remote)
        except ValueError:
            raise InvalidRemoteError()

        return _remote

    @cached_property
    def config(self):
        reader = self.repo.config_reader()
        _config = {}

        # the confgi reader apparently needs to be "primed" before it
        # will return accurate information.
        reader.sections()

        try:
            _config.update(dict(reader.items('ptt')))
        except configparser.NoSectionError:
            pass

        try:
            _config.update(dict(reader.items(f'ptt "{self.repo.head.ref.name}"')))
        except configparser.NoSectionError:
            pass

        return _config

    def update_branches(self):
        commits = self.repo.iter_commits(f'{self.base.hexsha}..HEAD')

        bundle = []
        branches = {}

        for rev in commits:
            LOG.debug('inspecting commit %s', rev)

            bundle.append(rev)
            if branch := self.branch_from_commit(rev):
                LOG.info('found branch %s with %d commits', branch, len(bundle))
                branch = Branch(name=branch, commits=bundle)
                branches[branch.name] = branch
                bundle = []

            rev = rev.parents[0]

        self.branches = branches

    def __contains__(self, k):
        return k in self.branches

    def __iter__(self):
        return iter(self.branches.values())

    def format_id(self, val):
        commit = self.repo.commit(val)
        return commit.hexsha[:self.short_id_len]

    def update_refs(self):
        LOG.info('updating ptt refs')

        # create/update refs
        for branch in self:
            ref = git.Reference.from_path(self.repo, f'refs/ptt/{branch.name}')

            if ref.is_valid() and ref.commit == branch.head:
                LOG.debug('not updating %s (%s == %s)',
                          ref.path, ref.commit, branch.head)
            else:
                if ref.is_valid():
                    LOG.debug('update ref %s (%s -> %s)',
                              ref.path, ref.commit, branch.head)
                else:
                    LOG.debug('create ref %s (%s)',
                              ref.path, branch.head)

                self.repo.git.update_ref(ref.path, branch.head)

        # purge obsolete refs
        for ref in self.repo.refs:
            if ref.path.startswith('refs/ptt/'):
                if ref.name not in self:
                    LOG.debug('delete ref %s', ref.path)
                    self.repo.git.update_ref('-d', ref.path)

    def branch_from_commit(self, rev):
        rev = self.repo.commit(rev)
        pattern = re.compile(r'^\s*{}(?P<branch>\S+)$'.format(self.marker),
                             re.IGNORECASE | re.MULTILINE)
        rev_message = rev.message
        try:
            rev_note = self.repo.git.notes('show', rev)
        except git.exc.GitCommandError:
            rev_note = ''

        for content in [rev_message, rev_note]:
            if match := pattern.search(content):
                return match.group('branch')