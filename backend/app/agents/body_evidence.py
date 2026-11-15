"""Server-owned paragraph references and visible-output audit sanitation."""
import hashlib
import json
import re


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def paragraph_sources(text, source_id, prefix='P'):
    return {f'{prefix}{i:03d}': {'source_id': source_id, 'quote': m.group(),
            'start': m.start(), 'end': m.end()}
            for i, m in enumerate(re.finditer(r'[^\r\n]+', text), 1) if m.group().strip()}


def safe_response(raw):
    # Provider.chat exposes visible content only. Never retain explicit thinking blocks.
    text = re.sub(r'<(?:think|analysis)\b[^>]*>.*?(?:</(?:think|analysis)>|$)',
                  '[REDACTED]', str(raw), flags=re.S | re.I)
    text = re.sub(r'(?i)(bearer\s+|sk-)[A-Za-z0-9_.\-]+', '[REDACTED]', text)
    text = re.sub(r'(?i)(["\']?(?:api[_-]?key|authorization|password|reasoning_content|thinking|analysis)["\']?\s*[:=]\s*)"(?:\\.|[^"\\])*"',
                  r'\1"[REDACTED]"', text)
    return text
