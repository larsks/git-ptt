#!/usr/bin/python3

import click
import code
import configparser
import functools
import git
import logging
import re
import readline
import rlcompleter
import tabulate

from dataclasses import dataclass, field

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


class PTT:
    default_marker = '@'
    default_base = 'master'
    default_short_id_len = 10

    def __init__(self, repo, base=None, remote=None, marker=None,
                 short_id_len=None):
        self.repo = repo
        self.base = base or self.config.get('base') or self.default_base
        self.remote = remote or self.config.get('remote')
        self.marker = marker or self.config.get('marker') or self.default_marker
        self.short_id_len = short_id_len or self.default_short_id_len

    @functools.cached_property
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

    @functools.cached_property
    def branches(self, refspec=None):
        refspec = refspec or self.base

        if '..' in refspec:
            commits = self.repo.iter_commits(refspec)
        else:
            commits = self.repo.iter_commits(f'{refspec}..HEAD')

        bundle = []
        branches = []

        for rev in commits:
            LOG.debug('inspecting commit %s', rev)

            bundle.append(rev)
            if branch := self.branch_from_commit(rev):
                LOG.info('found branch %s with %d commits', branch, len(bundle))
                branch = Branch(name=branch, commits=bundle)
                branches.append(branch)
                bundle = []

            rev = rev.parents[0]

        return branches

    def get_branch(self, name):
        for branch in self.branches:
            if branch.name == name:
                return branch

        raise KeyError(name)

    def format_id(self, val):
        commit = self.repo.commit(val)
        return commit.hexsha[:self.short_id_len]

    def update_refs(self):
        LOG.info('uppdating ptt refs')
        for branch in self.branches:
            ref = f'refs/ptt/{branch.name}'
            commit = branch.head
            LOG.debug('update ref %s to commit %s', ref, commit)
            self.repo.git.update_ref(f'{ref}', commit)

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


def needs_remote(func):
    @functools.wraps(func)
    def wrapper(ptt, *args, **kwargs):
        remote = ptt.remote
        if remote is None:
            raise click.ClickException('this action requires a valid remote')

        try:
            remote = ptt.repo.remote(remote)
        except ValueError:
            raise click.ClickException(f'no remote named {remote}')

        return func(ptt, remote, *args, **kwargs)

    return wrapper


@click.group(context_settings={'auto_envvar_prefix': 'GIT_PTT'})
@click.option('-v', '--verbose', count=True)
@click.option('-r', '--repo')
@click.option('-b', '--base')
@click.option('-R', '--remote')
@click.pass_context
def main(ctx, verbose, repo, base, remote):
    '''git-ptt is a tool for maintaining stacked pull requests'''

    try:
        loglevel = ['WARNING', 'INFO', 'DEBUG'][verbose]
    except IndexError:
        loglevel = 'DEBUG'

    logging.basicConfig(
        level=loglevel,
    )

    repo = git.Repo(repo)
    ptt = PTT(repo, base=base, remote=remote)
    ptt.update_refs()
    ctx.obj = ptt


@main.command()
@click.option('-c', '--show-commits', is_flag=True)
@click.argument('selected', nargs=-1)
@click.pass_obj
def ls(ptt, show_commits, selected):
    '''list branch mappings in the local repository'''
    for branch in ptt.branches:
        if selected and branch.name not in selected:
            continue
        print(f'{branch.name} {ptt.format_id(branch.hexsha)}')
        if show_commits:
            for commit in branch.commits:
                print(f'- {ptt.format_id(commit.hexsha)}: {commit.message.splitlines()[0]}')


@main.command()
@click.argument('name')
@click.pass_obj
def head(ptt, name):
    '''show head of mapped branch'''
    try:
        branch = ptt.get_branch(name)
        print(branch.head)
    except KeyError:
        raise click.ClickException(f'no such branch named {branch}')


@main.command()
@click.pass_obj
@needs_remote
def check(ptt, remote):
    '''verify that mapped branches match remote references'''
    LOG.info('updating remote %s', remote)
    remote.update()
    results = []
    for branch in ptt.branches:
        local_ref = branch.head
        remote_ref = remote.refs[branch.name].commit if branch.name in remote.refs else '-'

        in_sync = local_ref == remote_ref
        results.append(
            (remote, branch.name, ptt.format_id(local_ref), ptt.format_id(remote_ref), in_sync)
        )

    print(tabulate.tabulate(
        results,
        headers=['remote', 'branch', 'local ref', 'remote ref', 'in sync']))


@main.command()
@click.argument('selected', nargs=-1)
@click.pass_obj
@needs_remote
def push(ptt, remote, selected):
    '''push mapped branches to remote'''
    for branch in ptt.branches:
        if selected and branch.name not in selected:
            continue
        LOG.warning('pushing commit %s -> %s:%s', ptt.format_id(branch.head), remote, branch.name)
        res = remote.push(f'+{branch.head}:refs/heads/{branch.name}')
        if res:
            LOG.warning(res)


@main.command()
@click.pass_obj
@click.argument('selected', nargs=-1)
@needs_remote
def delete(ptt, remote, selected):
    '''delete mapped branches from remote repository'''
    for branch in ptt.branches:
        if selected and branch.name not in selected:
            continue
        LOG.warning('deleting branch %s:%s', remote, branch.name)
        remote.push(f':refs/heads/{branch.name}', force_with_lease=True)


@main.command()
@click.option('-f', '--force', is_flag=True)
@click.argument('selected', nargs=-1)
@click.pass_obj
def prune(ptt, force, selected):
    '''remove local git branches that match mapped branches'''
    for branch in ptt.branches:
        if selected and branch.name not in selected:
            continue

        if branch.name in ptt.repo.heads:
            if ptt.repo.heads[branch.name].commit != branch.head and not force:
                LOG.warning('skipping branch %s (not in sync)', branch.name)
                continue
            elif ptt.repo.heads[branch.name].commit != branch.head and force:
                LOG.warning('deleting branch %s (not in sync)', branch.name)
            else:
                LOG.warning('deleting branch %s',  branch.name)

            ptt.repo.git.branch('-D', branch.name)
        else:
            LOG.info('skipping branch %s (does not exist)', branch.name)


@main.command()
@click.option('-a', '--all', 'all_', is_flag=True)
@click.option('-f', '--force', is_flag=True)
@click.argument('selected', nargs=-1)
@click.pass_obj
def branch(ptt, all_, force, selected):
    if not selected and not all_:
        LOG.warning('no branches selected.')
        return

    for branch in ptt.branches:
        if selected and branch.name not in selected:
            continue

        if branch.name not in ptt.repo.heads:
            LOG.warning('creating branch %s @ %s', branch.name, ptt.format_id(branch.head))
            ptt.repo.create_head(branch.name, commit=branch.head)
        elif branch.name in ptt.repo.heads and force:
            ref = ptt.repo.heads[branch.name]
            LOG.warning('updating branch %s', branch.name)
            ptt.repo.git.update_ref(ref.path, branch.head)


@main.command()
@click.pass_obj
def shell(ptt):
    '''interactive shell with access to the PTT object'''
    vars = locals()
    readline.set_completer(rlcompleter.Completer(vars).complete)
    readline.parse_and_bind('tab: complete')
    code.InteractiveConsole(vars).interact()


@main.command()
@click.pass_obj
def stats(ptt):
    '''show summary diff statistics for each mapped branch'''

    commits = [(branch.name, branch.hexsha) for branch in ptt.branches]
    master = ('master', ptt.repo.heads['master'].commit.hexsha)
    table = []

    for b1, b2 in zip([None] + commits, commits + [master]):
        if b1 is None:
            continue

        res = ptt.repo.git.diff(b2[1], b1[1], numstat=True)
        t_added = t_deleted = t_files = 0
        for line in res.splitlines():
            added, deleted, fn = line.split(None, 2)
            t_added += int(added)
            t_deleted += int(deleted)
            t_files += 1

        table.append((b1[0], t_added, t_deleted, t_added-t_deleted, t_files))

    print(tabulate.tabulate(table,
                            headers=['branch', 'added', 'deleted', 'delta', 'files']))


@main.command()
@click.argument('target')
@click.pass_obj
def merge(ptt, target):
    '''merge current branch back into stack'''
    current_branch = ptt.repo.active_branch

    try:
        target = ptt.repo.heads[target]
    except IndexError:
        raise click.ClickException(f'no branch named {target}')

    try:
        source = ptt.get_branch(current_branch.name)
    except KeyError:
        raise click.ClickException(f'no mapped branch named {current_branch.name}')

    LOG.warning('merging current branch@%s into %s@%s',
                ptt.format_id(current_branch.commit.hexsha),
                target.name,
                ptt.format_id(target.commit.hexsha))
    ptt.repo.git.rebase(
        'HEAD',
        target,
        onto=source.head,
    )
