#!/usr/bin/python3

import configparser
import functools
import logging
import re
import tabulate

import click
import git

LOG = logging.getLogger(__name__)


class PTT:
    default_marker = '@'
    default_base = 'master'

    def __init__(self, repo, base=None, remote=None, marker=None):
        self.repo = repo
        self.base = base or self.config.get('base') or self.default_base
        self.remote = remote or self.config.get('remote')
        self.marker = marker or self.config.get('marker') or self.default_marker

    @functools.cache
    def find_branches(self, refspec=None):
        refspec = refspec or self.base

        if '..' in refspec:
            commits = self.repo.iter_commits(refspec)
        else:
            commits = self.repo.iter_commits(f'{refspec}..HEAD')

        bundle = []
        branches = {}

        for rev in commits:
            LOG.debug('inspecting commit %s', rev)

            bundle.append(rev)
            if branch := self.branch_from_commit(rev):
                LOG.info('found branch %s with %d commits', branch, len(bundle))
                branches[branch] = bundle
                bundle = []

            rev = rev.parents[0]

        return branches

    def update_refs(self):
        LOG.info('uppdating ptt refs')
        for branch, commits in self.find_branches().items():
            ref = f'refs/ptt/{branch}'
            commit = commits[0]
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

    @property
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
    for branch, commits in ptt.find_branches().items():
        if selected and branch not in selected:
            continue
        print(f'{branch} {str(commits[0])[:7]}')
        if show_commits:
            for commit in commits:
                print(f'- {str(commit)[:7]}: {commit.message.splitlines()[0]}')


@main.command()
@click.argument('branch')
@click.pass_obj
def head(ptt, branch):
    '''show head of mapped branch'''
    branches = ptt.find_branches()
    if branch in branches:
        print(branches[branch][0])
    else:
        raise click.ClickException(f'no such branch named {branch}')


@main.command()
@click.pass_obj
@needs_remote
def check(ptt, remote):
    '''verify that mapped branches match remote references'''
    LOG.info('updating remote %s', remote)
    remote.update()
    results = []
    for branch, commits in ptt.find_branches().items():
        local_ref = commits[0]
        remote_ref = remote.refs[branch].commit if branch in remote.refs else '-'

        in_sync = local_ref == remote_ref
        results.append(
            (remote, branch, str(local_ref)[:7], str(remote_ref)[:7], in_sync)
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
    for branch, commits in ptt.find_branches().items():
        if selected and branch not in selected:
            continue
        head = str(commits[0])
        LOG.warning('pushing commit %s -> %s:%s', head[:7], remote, branch)
        res = remote.push(f'+{head}:refs/heads/{branch}')
        if res:
            LOG.warning(res)


@main.command()
@click.pass_obj
@click.argument('selected', nargs=-1)
@needs_remote
def delete(ptt, remote, selected):
    '''delete mapped branches from remote repository'''
    for branch, commits in ptt.find_branches().items():
        if selected and branch not in selected:
            continue
        LOG.warning('deleting branch %s:%s', remote, branch)
        remote.push(f':refs/heads/{branch}',
                    force_with_lease=True)


@main.command()
@click.option('-f', '--force', is_flag=True)
@click.argument('selected', nargs=-1)
@click.pass_obj
def prune(ptt, force, selected):
    '''remove local git branches that match mapped branches'''
    for branch, commits in ptt.find_branches().items():
        if selected and branch not in selected:
            continue

        if branch in ptt.repo.heads:
            if ptt.repo.heads[branch].commit != commits[0] and not force:
                LOG.warning('skipping branch %s (not in sync)', branch)
                continue
            elif ptt.repo.heads[branch].commit != commits[0] and force:
                LOG.warning('deleting branch %s (not in sync)', branch)
            else:
                LOG.warning('deleting branch %s',  branch)

            ptt.repo.git.branch('-D', branch)
        else:
            LOG.info('skipping branch %s (does not exist)', branch)


@main.command()
@click.option('-a', '--all', 'all_', is_flag=True)
@click.option('-f', '--force', is_flag=True)
@click.argument('selected', nargs=-1)
@click.pass_obj
def branch(ptt, all_, force, selected):
    if not selected and not all_:
        LOG.warning('no branches selected.')
        return

    for branch, commits in ptt.find_branches().items():
        if selected and branch not in selected:
            continue

        if branch not in ptt.repo.heads:
            LOG.warning('creating branch %s', branch)
            ptt.repo.create_head(branch)
        elif branch in ptt.repo.heads and force:
            ref = ptt.repo.heads[branch]
            LOG.warning('updating branch %s', branch)
            ptt.repo.git.update_ref(ref.path, commits[0])
