"""AI meme judge: look at the meme, predict how viral it would be on X, write a caption.

Providers: Google Gemini (GEMINI_API_KEY) first, Groq vision models (GROQ_API_KEY) as
backup. Free-tier models change often, so busy (503), over-quota (429), retired (404) or
hanging models are skipped automatically and the working one is remembered.
Nothing here posts anything; the model never sees credentials.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request

GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash')
GEMINI_FALLBACKS = ('gemini-3.5-flash', 'gemini-3.6-flash', 'gemini-3.5-flash-lite', 'gemini-2.5-flash')
GEMINI_TIMEOUT = int(os.environ.get('GEMINI_TIMEOUT', '30'))
GEMINI_THINKING = ({'thinkingLevel': 'low'}, {'thinkingBudget': 0}, None)
GROQ_VISION = ('meta-llama/llama-4-scout-17b-16e-instruct', 'meta-llama/llama-4-maverick-17b-128e-instruct')
PROVIDER_ORDER = [p.strip() for p in os.environ.get('AI_PROVIDER_ORDER', 'gemini,groq').split(',') if p.strip()]
MAX_CAPTION = 110

SYSTEM = """You run a viral meme account on X. ONLY VIRALITY MATTERS: predict how much this exact post would spread (likes, reposts, quote-posts, replies, views).

You get one meme (an image, or a still frame from a video) and its original caption, if any.

Decide:
- post=false if it is not actually funny or relatable, low quality/blurry, mostly non-English text, an ad/promo/channel watermark plug, NSFW, gore, or built on slurs/hate. Also false if it would get almost no reaction (virality 3 or lower).
- virality: 1 (nobody reacts) to 10 (blows up). Be strict: most memes are 4-6.
- caption: what the account writes above the meme. Short, punchy, relatable (max 80 characters), like top meme accounts ("me after one gym session", "nobody: / my cat at 3am:"). Empty string if the meme works best with no caption. Never explain the joke. No hashtags, no links, no @handles, at most one emoji. English only.
- For a video still: judge the likely clip, and caption the moment ("wait for it" style teasers are fine).

Return JSON only: {"post": bool, "virality": 1-10, "caption": "...", "reason": "short reason"}"""

SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'post': {'type': 'BOOLEAN'},
        'virality': {'type': 'INTEGER'},
        'caption': {'type': 'STRING'},
        'reason': {'type': 'STRING'},
    },
    'required': ['post', 'virality', 'caption'],
}


class JudgeUnavailable(RuntimeError):
    pass


class HTTPFailure(JudgeUnavailable):
    def __init__(self, status, detail):
        self.status = status
        super().__init__(detail)


_gemini = {'model': None, 'thinking': None, 'down': False, 'discovered': False, 'bad': set()}
_groq = {'model': None, 'listed': None}


def available():
    return bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GROQ_API_KEY'))


def _detail(error):
    try:
        body = json.loads(error.read(20_000))
        message = body.get('error', {}).get('message', '') if isinstance(body.get('error'), dict) else str(body.get('error', ''))
    except Exception:
        message = ''
    message = re.sub(r'(?i)(key|token)[=:]\s*\S+', r'\1=***', message)
    return ('HTTP %s %s' % (error.code, message[:160])).strip()


def _request(url, headers, body=None, timeout=60):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     method='POST' if body is not None else 'GET',
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'fnuny-posts/2.0', **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(4_000_000))
    except urllib.error.HTTPError as error:
        raise HTTPFailure(error.code, _detail(error)) from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        raise JudgeUnavailable(type(error).__name__) from None


def mime(data):
    if data.startswith(b'\x89PNG'):
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    return 'image/jpeg'


def gemini_models(key):
    data = _request('https://generativelanguage.googleapis.com/v1beta/models?pageSize=200', {'x-goog-api-key': key}, timeout=20)
    names = [m['name'].split('/', 1)[-1] for m in data.get('models', [])
             if 'generateContent' in m.get('supportedGenerationMethods', [])]
    flash = [n for n in names if 'flash' in n and not re.search(r'image|tts|audio|live|exp|preview|thinking|8b', n)]
    return sorted(flash, key=lambda n: ('lite' in n, [-int(v) for v in re.findall(r'\d+', n)] or [0]))


def gemini(prompt, image):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise JudgeUnavailable('no Gemini key')
    if _gemini['down']:
        raise JudgeUnavailable('Gemini skipped after earlier failures in this run')
    models = [m for m in dict.fromkeys([_gemini['model'] or GEMINI_MODEL, GEMINI_MODEL, *GEMINI_FALLBACKS]) if m not in _gemini['bad']]
    last = 'no usable Gemini model'
    parts = [{'inline_data': {'mime_type': mime(image), 'data': base64.b64encode(image).decode()}}, {'text': prompt}]
    while models:
        model = models.pop(0)
        options = [_gemini['thinking']] if model == _gemini['model'] else list(GEMINI_THINKING)
        failed = False
        for option in options:
            config = {'temperature': 0.7, 'maxOutputTokens': 2048, 'responseMimeType': 'application/json', 'responseSchema': SCHEMA}
            if option:
                config['thinkingConfig'] = option
            try:
                data = _request('https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent' % model,
                                {'x-goog-api-key': key},
                                {'systemInstruction': {'parts': [{'text': SYSTEM}]},
                                 'contents': [{'role': 'user', 'parts': parts}], 'generationConfig': config},
                                timeout=GEMINI_TIMEOUT)
            except HTTPFailure as error:
                if error.status == 400 and option is not None:
                    continue
                if error.status not in (404, 429, 500, 503):
                    raise
                last, failed = str(error), True
                break
            except JudgeUnavailable as error:
                last, failed = str(error), True
                break
            try:
                text = ''.join(p.get('text', '') for p in data['candidates'][0]['content']['parts'] if not p.get('thought'))
            except (KeyError, IndexError, TypeError):
                raise JudgeUnavailable('empty Gemini response') from None
            _gemini.update(model=model, thinking=option)
            return text
        if failed:
            _gemini['bad'].add(model)
            if model == _gemini['model']:
                _gemini.update(model=None, thinking=None)
        if not models and not _gemini['discovered']:
            _gemini['discovered'] = True
            try:
                models.extend([m for m in gemini_models(key) if m not in _gemini['bad']][:2])
            except JudgeUnavailable:
                pass
    _gemini['down'] = True
    raise JudgeUnavailable(last)


def groq_models(key):
    data = _request('https://api.groq.com/openai/v1/models', {'Authorization': 'Bearer '+key}, timeout=20)
    names = [m['id'] for m in data.get('data', []) if m.get('active', True)]
    vision = [n for n in names if re.search(r'scout|maverick|vision|vl\b|-vl-', n)]
    return [n for n in GROQ_VISION if n in vision]+[n for n in vision if n not in GROQ_VISION]


def groq(prompt, image):
    key = os.environ.get('GROQ_API_KEY')
    if not key:
        raise JudgeUnavailable('no Groq key')
    models = [_groq['model'] or GROQ_VISION[0]]
    tried = set()
    url = 'data:%s;base64,%s' % (mime(image), base64.b64encode(image).decode())
    while models:
        model = models.pop(0)
        if model in tried:
            continue
        tried.add(model)
        try:
            data = _request('https://api.groq.com/openai/v1/chat/completions', {'Authorization': 'Bearer '+key}, {
                'model': model, 'temperature': 0.7, 'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': SYSTEM},
                             {'role': 'user', 'content': [{'type': 'text', 'text': prompt},
                                                          {'type': 'image_url', 'image_url': {'url': url}}]}]})
        except HTTPFailure as error:
            if error.status in (400, 404, 429) and len(tried) <= 3:
                if _groq['listed'] is None:
                    _groq['listed'] = groq_models(key)
                models.extend(m for m in _groq['listed'] if m not in tried and m not in models)
                del models[3:]
                if models:
                    continue
            raise
        try:
            text = data['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            raise JudgeUnavailable('empty Groq response') from None
        _groq['model'] = model
        return text
    raise JudgeUnavailable('no usable Groq vision model')


def _json(text):
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', (text or '').strip())
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise JudgeUnavailable('no JSON object')
    try:
        return json.loads(text[start:end+1])
    except ValueError:
        raise JudgeUnavailable('invalid JSON') from None


def weight(text):
    return sum(1 if ord(ch) < 0x1100 else 2 for ch in text)


def clean_caption(text):
    """Enforce caption rules locally: no links/handles/hashtags, one emoji, short."""
    text = re.sub(r'https?://\S+|www\.\S+', '', text or '')
    text = re.sub(r'(?<!\w)[@#]\w+', '', text)
    text = re.sub(r'[ \t]+', ' ', text).strip().strip('"“”')
    emoji = re.compile('[\U0001F300-\U0001FAFF☀-➿]')
    seen = 0
    def keep(match):
        nonlocal seen
        seen += 1
        return match[0] if seen == 1 else ''
    text = emoji.sub(keep, text).strip()
    if weight(text) > MAX_CAPTION:
        text = text[:MAX_CAPTION].rsplit(' ', 1)[0].rstrip(',;:')+'…'
    return text


def judge(image, original_caption='', kind='image'):
    """Return {'post', 'virality', 'caption', 'reason', 'provider'} or raise JudgeUnavailable."""
    if not available():
        raise JudgeUnavailable('no AI key configured')
    prompt = 'Media type: %s%s\nOriginal caption: %s' % (
        kind, ' (this is a still frame from the video)' if kind == 'video' else '', original_caption or '(none)')
    providers = {'gemini': gemini, 'groq': groq}
    errors = []
    for name in [n for n in PROVIDER_ORDER if n in providers]:
        try:
            raw = _json(providers[name](prompt, image))
            return dict(post=bool(raw.get('post')),
                        virality=max(1, min(10, int(raw.get('virality') or 1))),
                        caption=clean_caption(str(raw.get('caption') or '')),
                        reason=str(raw.get('reason') or '')[:200], provider=name)
        except (JudgeUnavailable, TypeError, ValueError) as error:
            errors.append('%s: %s' % (name, error))
    raise JudgeUnavailable('; '.join(errors) or 'no provider')


def remembered():
    return {'gemini_model': _gemini['model'], 'gemini_thinking': _gemini['thinking'], 'groq_model': _groq['model']}


def remember(value):
    if value.get('gemini_model'):
        _gemini.update(model=value['gemini_model'], thinking=value.get('gemini_thinking'))
    if value.get('groq_model'):
        _groq['model'] = value['groq_model']
