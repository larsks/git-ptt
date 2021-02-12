#!/usr/bin/python3

import configparser
import functools
import logging
import re

import click
import git

LOG = logging.getLogger(__name__)


class PTT:
    default_header = 'x-branch-name'

    def __init__(self, repo, remote=None, header=None):
        self.repo = repo
        self.remote = self.get_remote(remote)
        self.header = header or self.default_header

    def find_branches(self, refspec):
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

    def branch_from_commit(self, rev):
        rev = self.repo.commit(rev)
        pattern = re.compile(r'^\s*{}: (?P<branch>\S+)$'.format(self.header),
                             re.IGNORECASE | re.MULTILINE)
        rev_message = rev.message
        try:
            rev_note = self.repo.git.notes('show', rev)
        except git.exc.GitCommandError:
            rev_note = ''

        for content in [rev_message, rev_note]:
            if match := pattern.search(content):
                return match.group('branch')

    def get_remote(self, remote):
        if remote:
            LOG.info('found remote %s from global config', remote)
        elif remote := self.config.get('remote'):
            LOG.info('found remote %s from git config', remote)
        else:
            LOG.info('no remote has been configured')

        return remote

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
@click.option('-R', '--remote')
@click.pass_context
def main(ctx, verbose, repo, remote):
    '''git-ptt is a tool for maintaining stacked pull requests'''

    try:
        loglevel = ['WARNING', 'INFO', 'DEBUG'][verbose]
    except IndexError:
        loglevel = 'DEBUG'

    logging.basicConfig(
        level=loglevel,
    )

    repo = git.Repo(repo)
    ctx.obj = PTT(repo, remote=remote)


@main.command()
@click.option('-b', '--base', default='master')
@click.pass_obj
def ls(ptt, base):
    '''list branch mappings in the local repository'''
    for branch, commits in ptt.find_branches(base).items():
        print(branch)
        for commit in commits:
            print(f'- {str(commit)[:7]}: {commit.message.splitlines()[0]}')


@main.command()
@click.option('-b', '--base', default='master')
@click.argument('branch')
@click.pass_obj
def head(ptt, base, branch):
    '''show head of mapped branch'''
    branches = ptt.find_branches(base)
    if branch in branches:
        print(branches[branch][0])
    else:
        raise click.ClickException(f'no such branch named {branch}')


@main.command()
@click.option('-b', '--base', default='master')
@click.pass_obj
@needs_remote
def check(ptt, remote, base):
    '''verify that mapped branches match remote references'''
    LOG.info('updating remote %s', remote)
    remote.update()
    for branch, commits in ptt.find_branches(base).items():
        in_sync = branch in remote.refs and remote.refs[branch].commit == commits[0]
        print(f'{remote}:{branch} {str(commits[0])[:7]} {"in sync" if in_sync else "needs update"}')


@main.command()
@click.option('-b', '--base', default='master')
@click.pass_obj
@needs_remote
def push(ptt, remote, base):
    '''push commits to mapped remote branches'''
    for branch, commits in ptt.find_branches(base).items():
        head = str(commits[0])
        LOG.warning('pushing commit %s -> %s:%s', head[:7], remote, branch)
        res = remote.push(f'+{head}:refs/heads/{branch}')
        if res:
            LOG.warning(res)


@main.command()
@click.option('-b', '--base', default='master')
@click.pass_obj
@needs_remote
def delete(ptt, remote, base):
    '''delete mapped branches from remote repository'''
    for branch, commits in ptt.find_branches(base).items():
        LOG.warning('deleting branch %s:%s', remote, branch)
        remote.push(f':refs/heads/{branch}',
                    force_with_lease=True)


@main.command()
@click.option('-b', '--base', default='master')
@click.argument('branch')
@click.pass_obj
def checkout(ptt, base, branch):
    '''create new branch from mapped branch'''
    branches = ptt.find_branches(base)
    if branch not in branches:
        raise click.ClickException(f'no ptt branch named "{branch}"')

    new_head = branches[branch][0]
    new_branch = ptt.repo.create_head(branch, new_head)
    new_branch.checkout()
