#!/usr/bin/python3

import click
import code
import functools
import git
import logging
import readline
import rlcompleter
import sys
import tabulate

from git_ptt.api import PTT

LOG = logging.getLogger(__name__)
UNDEFINED = object()


def handle_git_error(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except git.exc.GitCommandError as err:
            sys.stdout.write(err.args[3].decode())
            sys.stdout.write('\n\n')
            sys.stderr.write(err.args[2].decode())
            sys.stderr.write('\n\n')
            raise click.ClickException(f'git failed with status {err.status}')

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
    for branch in ptt:
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
        branch = ptt.branches[name]
        print(branch.head)
    except KeyError:
        raise click.ClickException(f'no such branch named {branch}')


@main.command()
@click.argument('selected', nargs=-1)
@click.pass_obj
def push(ptt, selected):
    '''push mapped branches to remote'''
    for branch in ptt:
        if selected and branch.name not in selected:
            continue
        LOG.warning('pushing commit %s -> %s:%s', ptt.format_id(branch.head), ptt.remote, branch.name)
        res = ptt.remote.push(f'+{branch.head}:refs/heads/{branch.name}')
        if res:
            LOG.warning(res)


@main.group()
def remote():
    '''commands for dealing with remote repositories'''
    pass


@remote.command()
@click.pass_obj
def check(ptt):
    '''verify that mapped branches match remote references'''
    LOG.info('updating remote %s', ptt.remote)
    ptt.remote.update()
    results = []
    for branch in ptt:
        local_ref = branch.head
        remote_ref = ptt.remote.refs[branch.name].commit if branch.name in ptt.remote.refs else '-'

        in_sync = local_ref == remote_ref
        results.append(
            (ptt.remote, branch.name, ptt.format_id(local_ref), ptt.format_id(remote_ref), in_sync)
        )

    print(tabulate.tabulate(
        results,
        headers=['remote', 'branch', 'local ref', 'remote ref', 'in sync']))


@remote.command()
@click.pass_obj
@click.argument('selected', nargs=-1)
def prune(ptt, selected):
    '''delete mapped branches from remote repository'''
    for branch in ptt:
        if selected and branch.name not in selected:
            continue
        LOG.warning('deleting branch %s:%s', ptt.remote, branch.name)
        ptt.remote.push(f':refs/heads/{branch.name}', force_with_lease=True)


@main.group()
def branch():
    '''commands for dealing with local git branches'''
    pass


@branch.command()
@click.option('--force', '-f', is_flag=True)
@click.argument('name')
@click.pass_obj
def checkout(ptt, force, name):
    '''checkout a git branch from a mapped branch'''

    try:
        branch = ptt.branches[name]
    except KeyError as err:
        raise click.ClickException(f'no such branch {err}')

    try:
        active_branch = ptt.repo.active_branch
    except TypeError:
        active_branch = None

    if branch.name not in ptt.repo.heads:
        LOG.warning('creating git branch %s@%s', branch.name, branch.head)
        ref = ptt.create_git_branch(branch.name, branch.head, active_branch)
    else:
        ref = ptt.repo.heads[branch.name]

        if ref.commit == branch.head or force:
            LOG.warning('checking out git branch %s@%s',
                        branch.name, branch.head)
        else:
            LOG.warning('checking out git branch %s@%s (out of sync)',
                        branch.name, branch.head)

    ref.checkout()


@branch.command()
@click.option('--all/--no-all', '-a', 'all_')
@click.option('--force/--no-force', '-f')
@click.option('--continue/--no-continue', '-c', 'continue_')
@click.argument('selected', nargs=-1)
@click.pass_obj
def prune(ptt, all_, continue_, force, selected):
    '''remove git branches that correspond to mapped branches'''

    if selected is None and not all_:
        raise click.ClickException('no branches to prune')

    for branch in ptt:
        if selected and branch.name not in selected:
            continue

        if branch.name in ptt.repo.heads:
            LOG.warning('deleting git branch %s', branch.name)
            ref = ptt.repo.heads[branch.name]

            if ref.commit != branch.head and not force:
                LOG.error('not deleting %s (out of sync)', branch.name)
                continue

            try:
                ptt.delete_git_branch(branch.name, force=True)
            except git.exc.GitCommandError as err:
                LOG.error('failed to delete branch %s: %s',
                          branch.name, err)
                if not continue_:
                    raise


@branch.command()
@click.option('-s', '--stack')
@click.option('-p', '--prune', is_flag=True)
@click.argument('name', default=UNDEFINED)
@click.pass_obj
@handle_git_error
def update(ptt, stack, prune, name):
    '''replace mapped branch with current HEAD'''

    if stack is None:
        stack = ptt.config.get('stack')
        if stack is None:
            raise click.ClickException('no target stack defined (try --stack)')

    stack = ptt.repo.refs[stack]

    if name is UNDEFINED:
        try:
            current_branch = ptt.repo.active_branch
        except TypeError:
            raise click.ClickException('unable to determine mapped branch name')

        name = current_branch.name

    branch = ptt.branches[name]
    current_head = ptt.repo.head.commit

    LOG.warning('updating mapped branch %s in %s to %s',
                branch.name,
                stack.name,
                ptt.format_id(current_head.hexsha),
                )
    ptt.repo.git.rebase(current_head, stack.name, onto=branch.head)

    if prune:
        LOG.warning('deleting git branch %s', branch.name)
        ptt.delete_git_branch(branch.name)


@main.command()
@click.pass_obj
def shell(ptt):
    '''interactive shell with access to the PTT object'''
    vars = locals()
    readline.set_completer(rlcompleter.Completer(vars).complete)
    readline.parse_and_bind('tab: complete')
    code.InteractiveConsole(vars).interact(banner='PTT API available as "ptt" object')


@main.command()
@click.pass_obj
def stats(ptt):
    '''show summary diff statistics for each mapped branch'''

    commits = [(branch.name, branch.hexsha) for branch in ptt]
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
