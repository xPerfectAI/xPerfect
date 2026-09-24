"""Quiet native authentication probe, used by the existing account setup flow."""
from __future__ import annotations
import argparse
import os
import subprocess

try:
    from .grok_acp import AcpClient, AcpError
except ImportError:
    from grok_acp import AcpClient, AcpError


def verify(binary, *, environment=None, cwd=None):
    try:
        process=subprocess.Popen([binary,'agent','--no-leader','stdio'],env=environment,cwd=cwd,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        with AcpClient(process) as client:
            initialized=client.request('initialize',{'protocolVersion':1,'clientInfo':{'name':'xperfect-auth','version':'1'},'clientCapabilities':{}},timeout=8)
            if initialized.get('protocolVersion')!=1 or initialized.get('_meta',{}).get('grokShell') is not True:
                return False
            method=initialized.get('_meta',{}).get('defaultAuthMethodId')
            if method not in ('xai.api_key','cached_token'):
                return False
            client.request('authenticate',{'methodId':method,'_meta':{'headless':True}},timeout=8)
            return True
    except (OSError,AcpError):
        return False


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--binary',required=True)
    args=parser.parse_args()
    return 0 if verify(args.binary) else 1

if __name__=='__main__':
    raise SystemExit(main())
