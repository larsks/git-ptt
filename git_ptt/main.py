#!/usr/bin/python3

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
        self.remote = remote
        self.header = header or self.default_header

    def find_branches(self, since):
        since = self.repo.commit(since)
        rev = self.repo.head.commit
        bundle = []
        branches = {}

        while True:
            LOG.debug('inspecting commit %s', rev)
            if rev == since:
                break

            bundle.append(rev)
            if branch := self.branch_from_commit(rev):
                LOG.info('found branch %s with %d commits', branch, len(bundle))
                branches[branch] = bundle
                bundle = []

            rev = rev.parents[0]

        return branches

    def branch_from_commit(self, rev):
        rev = self.repo.commit(rev)
        pattern = re.compile(r'\s*{}: (?P<branch>\S+)'.format(self.header), re.IGNORECASE)
        rev_message = rev.message
        try:
            rev_note = self.repo.git.notes('show', rev)
        except git.exc.GitCommandError:
            rev_note = ''

        for content in [rev_message, rev_note]:
            if match := pattern.match(content):
                return match.group('branch')


def needs_remote(func):
    @functools.wraps(func)
    def wrapper(ptt, *args, **kwargs):
        if ptt.remote is None:
            raise click.ClickException(f'{func.__name__} requires a valid remote')

        return func(ptt, *args, **kwargs)

    return wrapper


@click.group(context_settings={'auto_envvar_prefix': 'GIT_PTT'})
@click.option('-v', '--verbose', count=True)
@click.option('-r', '--repo')
@click.option('-R', '--remote', default='origin')
@click.pass_context
def main(ctx, verbose, repo, remote):

    try:
        loglevel = ['WARNING', 'INFO', 'DEBUG'][verbose]
    except IndexError:
        loglevel = 'DEBUG'

    logging.basicConfig(
        level=loglevel,
    )

    repo = git.Repo(repo)
    try:
        remote = repo.remote(remote)
        LOG.info('using remote %s', remote)
    except ValueError:
        remote = None

    ctx.obj = PTT(repo, remote=remote)


@main.command()
@click.argument('since', default='master')
@click.pass_obj
def ls(ptt, since):
    for branch, commits in ptt.find_branches(since).items():
        print(branch)
        for commit in commits:
            print(f'- {str(commit)[:7]}: {commit.message.splitlines()[0]}')


@main.command()
@click.argument('since', default='master')
@click.pass_obj
@needs_remote
def push(ptt, since):
    for branch, commits in ptt.find_branches(since).items():
        head = str(commits[0])
        LOG.warning('pushing commit %s -> %s:%s', head[:7], ptt.remote, branch)
        res = ptt.remote.push(f'+{head}:refs/heads/{branch}')
        if res:
            LOG.warning(res)


@main.command()
@click.argument('since', default='master')
@click.pass_obj
@needs_remote
def delete(ptt, since):
    for branch, commits in ptt.find_branches(since).items():
        LOG.warning('deleting branch %s:%s', ptt.remote, branch)
        ptt.remote.push(f':refs/heads/{branch}',
                        force_with_lease=True)
