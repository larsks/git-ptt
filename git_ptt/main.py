#!/usr/bin/python3

import configparser
import logging
import re

import click
import git

LOG = logging.getLogger(__name__)


@click.command()
@click.option('-h', '--header', default='X-branch-name')
@click.option('-q', '--query', is_flag=True)
@click.option('-d', '--delete', is_flag=True)
@click.option('-v', '--verbose', count=True)
@click.option('-c', '--continue', '_continue', is_flag=True)
@click.option('-R', '--remote')
@click.option('-s', '--since')
@click.option('-p', '--prefix')
@click.option('-P', '--prefix-branch', is_flag=True)
@click.argument('revisions', nargs=-1)
def main(header, delete, query, verbose, _continue, remote, since,
         prefix, prefix_branch,
         revisions):
    try:
        loglevel = ['WARNING', 'INFO', 'DEBUG'][verbose]
    except IndexError:
        loglevel = 'DEBUG'

    logging.basicConfig(
        level=loglevel,
    )

    repo = git.Repo()
    conf = repo.config_reader()

    if prefix_branch:
        LOG.debug('setting prefix from --prefix-branch')
        if prefix:
            raise click.ClickException(
                'Must select one of --prefix/--prefix-branch')
        prefix = '{}/'.format(repo.active_branch.name)

    if prefix is None:
        LOG.debug('setting prefix from branch config')
        try:
            prefix = conf.get('ptt "{}"'.format(repo.active_branch.name),
                              'prefix')
        except configparser.Error:
            pass

    LOG.debug('prefix: %s', prefix)

    if remote is None:
        try:
            remote = conf.get('ptt', 'remote')
        except configparser.Error:
            remote = 'origin'

    rem = repo.remote(remote)

    if since and revisions:
        raise click.ClickException('you cannot use --since and provide '
                                   'a list of revisions')

    if since:
        revisions = repo.iter_commits('{}..'.format(since))

    targets = []
    for rev in revisions if revisions else ['HEAD']:
        com = repo.commit(rev)
        LOG.info('checking commit %s', com.hexsha[:7])

        match = re.search(r'{}: (?P<name>\S+)'.format(
            header.lower()), com.message.lower())
        if not match:
            if _continue:
                LOG.warning('No branch name in %s', com.hexsha[:7])
                continue
            else:
                raise click.ClickException(
                    'No branch name in {}'.format(com.hexsha[:7]))

        target = match.group(1)
        if prefix:
            target = '{}{}'.format(prefix, target)
        LOG.info('found branch name %s for commit %s', target, com.hexsha[:7])
        targets.append((com, target))

    for com, target in targets:
        if query:
            print('{}: {}'.format(com.hexsha[:7], target))
        elif delete:
            LOG.warning('deleting branch %s from remote %s', target, rem)
            rem.push(':refs/heads/{}'.format(target),
                     force_with_lease=True)
        else:
            LOG.warning('pushing %s to branch %s on remote %s', com.hexsha[:7],
                        target, rem)
            res = rem.push('+{}:refs/heads/{}'.format(com.hexsha, target))
            if res:
                LOG.warning('res: %s', res)
