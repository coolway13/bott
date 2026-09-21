"""Outbound boundaries and credential redaction. No telemetry or file credentials."""
import json
import os
import re
import sys
from urllib.parse import urlsplit, quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

HOSTS = frozenset({'statsapi.mlb.com', 'discord.com', 'api.github.com'})
WEBHOOK = re.compile(r'https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/[^\s<>"\x27]+', re.I)
TOKENS = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


def redact(value):
    text=str(value)
    # Values are consulted only in memory; never returned or persisted.
    for name,value in os.environ.items():
        if value and len(value)>=8 and any(word in name.upper() for word in ('TOKEN','SECRET','PASSWORD','WEBHOOK','API_KEY','PRIVATE_KEY')):
            for variant in (value, quote(value,safe=''), json.dumps(value)[1:-1]):
                text=text.replace(variant,'[REDACTED]')
    return TOKENS.sub('[REDACTED]', WEBHOOK.sub('[REDACTED WEBHOOK]',text))


def clean(value):
    if isinstance(value,str): return redact(value)
    if isinstance(value,list): return [clean(v) for v in value]
    if isinstance(value,dict): return {redact(k):clean(v) for k,v in value.items()}
    return value


class RedactedStream:
    """Buffer lines so secrets split over multiple writes are still redacted."""
    def __init__(self, target): self.target,self.pending=target,''
    def write(self, text):
        self.pending+=text
        while '\n' in self.pending:
            line,self.pending=self.pending.split('\n',1)
            self.target.write(redact(line)+'\n')
        return len(text)
    def flush(self): self.target.flush()
    def finish(self):
        if self.pending: self.target.write(redact(self.pending));self.pending=''
        self.target.flush()
    def __getattr__(self,name): return getattr(self.target,name)


def protect_output():
    import atexit
    for name in ('stdout','stderr'):
        stream=RedactedStream(getattr(sys,name))
        setattr(sys,name,stream)
        atexit.register(stream.finish)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('HTTP redirects are disabled')


def urlopen(request, timeout=30):
    url=request.full_url if hasattr(request,'full_url') else request
    parsed=urlsplit(url)
    if (parsed.scheme!='https' or parsed.hostname not in HOSTS or parsed.username
            or parsed.password or parsed.port not in (None,443) or parsed.fragment):
        raise RuntimeError('Outbound destination is not allowed')
    # Ignore proxy environment variables; secrets must go directly to approved hosts.
    return build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=timeout)
