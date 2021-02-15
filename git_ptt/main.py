#!/usr/bin/python3

import click
import code
import git
import logging
import readline
import rlcompleter
import tabulate

from git_ptt.api import PTT

LOG = logging.getLogger(__name__)


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
        branch = ptt.get_branch(name)
        print(branch.head)
    except KeyError:
        raise click.ClickException(f'no such branch named {branch}')


@main.command()
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


@main.command()
@click.pass_obj
@click.argument('selected', nargs=-1)
def delete(ptt, selected):
    '''delete mapped branches from remote repository'''
    for branch in ptt:
        if selected and branch.name not in selected:
            continue
        LOG.warning('deleting branch %s:%s', ptt.remote, branch.name)
        ptt.remote.push(f':refs/heads/{branch.name}', force_with_lease=True)


@main.command()
@click.option('-f', '--force', is_flag=True)
@click.argument('name')
@click.pass_obj
def checkout(ptt, force, name):
    try:
        branch = ptt.branches[name]
    except KeyError:
        raise click.ClickException(f'no mapped branch named {name}')

    if branch.name not in ptt.repo.heads:
        LOG.warning('creating branch %s @ %s', branch.name, ptt.format_id(branch.head))
        ref = ptt.repo.create_head(branch.name, commit=branch.head)
    elif branch.name in ptt.repo.heads and force:
        ref = ptt.repo.heads[branch.name]
        LOG.warning('updating branch %s', branch.name)
        ptt.repo.git.update_ref(ref.path, branch.head)
    else:
        ref = ptt.repo.heads[branch.name]

    ref.checkout()


@main.command()
@click.option('-a', '--all', 'all_', is_flag=True)
@click.option('-f', '--force', is_flag=True)
@click.option('--create/--purge', default=True)
@click.argument('selected', nargs=-1)
@click.pass_obj
def branch(ptt, create, all_, force, selected):
    if not selected and not all_:
        LOG.warning('no branches selected.')
        return

    for branch in ptt:
        if selected and branch.name not in selected:
            continue

        if create:
            if branch.name not in ptt.repo.heads:
                LOG.warning('creating branch %s @ %s', branch.name, ptt.format_id(branch.head))
                ptt.repo.create_head(branch.name, commit=branch.head)
            elif branch.name in ptt.repo.heads and force:
                ref = ptt.repo.heads[branch.name]
                LOG.warning('updating branch %s', branch.name)
                ptt.repo.git.update_ref(ref.path, branch.head)
        else:
            if branch.name in ptt.repo.heads:
                ref = ptt.repo.heads[branch.name]
                if ref.commit == branch.head or force:
                    LOG.warning('removing branch %s', branch.name)
                    ptt.repo.git.update_ref('-d', ref.path)
                else:
                    LOG.warning('not removing branch %s (not in sync)', branch.name)


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
