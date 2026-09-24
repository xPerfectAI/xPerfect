"""Build-time NSS entries for the immutable shared workspace UID range.

No passwords, homes, processes, or runtime privileges are created here. The
controller separately admits members and grants their private volume ACLs.
"""
from pathlib import Path

FIRST_UID = 20001
LAST_UID = 60000
FIRST_ACCOUNT_UID = 100001
LAST_ACCOUNT_UID = 200000


def project_accounts(passwd: str, group: str) -> tuple[str, str]:
    users = [line.split(':') for line in passwd.splitlines() if line]
    groups = [line.split(':') for line in group.splitlines() if line]
    for records, width in ((users, 7), (groups, 4)):
        if any(len(record) != width or not record[2].isdecimal() for record in records):
            raise ValueError('Base image has an unsupported account database')
        if any(FIRST_UID <= int(record[2]) <= LAST_UID
               or FIRST_ACCOUNT_UID <= int(record[2]) <= LAST_ACCOUNT_UID
               or record[0].startswith(('member-', 'account-'))
               for record in records):
            raise ValueError('Base image already uses the shared member identity range')
    for uid in range(FIRST_UID, LAST_UID + 1):
        name = f'member-{uid}'
        users.append([name, '!', str(uid), str(uid), '',
                      f'/workspace/data/members/{uid}/home', '/bin/bash'])
        groups.append([name, '!', str(uid), ''])
    for uid in range(FIRST_ACCOUNT_UID, LAST_ACCOUNT_UID + 1):
        name = f'account-{uid}'
        users.append([name, '!', str(uid), str(uid), '', '/workspace/account', '/bin/bash'])
        groups.append([name, '!', str(uid), ''])
    # Docker's NSS reader caps each database at 10 MiB. Keep informational
    # GECOS fields empty so the complete immutable identity range fits.
    return ('\n'.join(':'.join(record) for record in users) + '\n',
            '\n'.join(':'.join(record) for record in groups) + '\n')


def main():
    passwd_path, group_path = Path('/etc/passwd'), Path('/etc/group')
    passwd, group = project_accounts(passwd_path.read_text(), group_path.read_text())
    passwd_path.write_text(passwd)
    group_path.write_text(group)


if __name__ == '__main__':
    main()
