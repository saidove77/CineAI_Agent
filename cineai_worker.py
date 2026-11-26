#!/usr/bin/env python3
"""CineAI worker: يعمل في الخلفية (GitHub Actions) بلا تدخل.
يقرأ project.json (مُصدَّر من الصفحة) أو prompt.txt، يبحث عن Spaces لتوليد الفيديو في Hugging Face،
يولّد المقاطع المتبقية، يحفظها في videos/، ويدمجها في film.mp4 عند الاكتمال."""
import json, os, pathlib, re, shutil, subprocess, sys
import requests

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT, PROMPT, OUT = ROOT / 'project.json', ROOT / 'prompt.txt', ROOT / 'videos'
TOKEN = os.environ.get('HF_TOKEN', '').strip()
PIN = os.environ.get('HF_SPACE', '').strip()
MAX_CLIPS = int(os.environ.get('MAX_CLIPS_PER_RUN', '6'))
CLIP_SECONDS = int(os.environ.get('CLIP_SECONDS', '5'))
QUOTA = re.compile(r'quota|exceed|daily|too many', re.I)
VID = re.compile(r'\.(mp4|webm|mov|mkv)$', re.I)


def log(*a):
    print(*a, flush=True)


def save(p):
    PROJECT.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding='utf-8')


def load_project():
    if PROJECT.exists():
        p = json.loads(PROJECT.read_text(encoding='utf-8'))
    elif PROMPT.exists():
        text = PROMPT.read_text(encoding='utf-8').strip()
        units = [u.strip() for u in re.split(r'\n\s*\n', text) if u.strip()]
        if len(units) == 1:
            units = [u for u in re.split(r'(?<=[.!?؟])\s+', text) if u.strip()]
        p = {'style': 'Cinematic, coherent visual style', 'characters': '', 'scenes': [
            {'id': i + 1, 'seconds': CLIP_SECONDS, 'description': u, 'status': 'pending'}
            for i, u in enumerate(units)]}
    else:
        sys.exit('لا يوجد project.json ولا prompt.txt')
    for s in p.get('scenes', []):
        if s.get('status') == 'working':
            s['status'] = 'pending'
    return p


def prompt_of(p, s):
    if s.get('prompt'):
        return s['prompt']
    parts = [p.get('style'), s.get('description'), p.get('characters')]
    return '. '.join(x for x in parts if x) + '. No logos, no watermarks.'


def find_spaces(p):
    ids = [PIN] if PIN else []
    last = (p.get('worker') or {}).get('space')
    if last:
        ids.append(last)
    try:
        r = requests.get('https://huggingface.co/api/spaces',
                         params={'search': 'text-to-video', 'sort': 'likes', 'direction': -1, 'limit': 30},
                         headers={'Authorization': f'Bearer {TOKEN}'} if TOKEN else {}, timeout=30)
        r.raise_for_status()
        ids += [x['id'] for x in r.json() if x.get('id')]
    except Exception as e:
        log('تعذر البحث في HF:', e)
    seen = set()
    return [i for i in ids if not (i in seen or seen.add(i))][:12]


def pick_endpoint(info):
    for name, ep in (info.get('named_endpoints') or {}).items():
        if not any(re.search(r'video|gallery|file', str(r.get('component') or ''), re.I)
                   for r in ep.get('returns', [])):
            continue
        ps, pi, ok = ep.get('parameters', []), -1, True
        for i, q in enumerate(ps):
            c = str(q.get('component') or '').lower()
            n = str(q.get('parameter_name') or q.get('label') or '').lower()
            if re.search(r'image|video|audio|file|model3d', c) and not q.get('parameter_has_default'):
                ok = False
            if pi < 0 and 'text' in c and re.search(r'prompt|text|desc', n) and 'negative' not in n:
                pi = i
        if ok and pi >= 0:
            return name, ps, pi
    return None


def files_in(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from files_in(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from files_in(v)


def generate(client, ep, prompt):
    name, ps, pi = ep
    args = [prompt if i == pi else
            (q.get('parameter_default') if q.get('parameter_has_default')
             else ('' if 'text' in str(q.get('component', '')).lower() else None))
            for i, q in enumerate(ps)]
    res = client.submit(*args, api_name=name).result(timeout=1200)
    for f in files_in(res):
        if VID.search(f) and os.path.exists(f):
            return f
    raise RuntimeError('لا ملف فيديو في النتيجة')


def finish(p):
    save(p)
    scenes = p.get('scenes', [])
    if scenes and all(s.get('status') == 'done' for s in scenes) and shutil.which('ffmpeg'):
        lst = OUT / 'clips-list.txt'
        lst.write_text(''.join(f"file '{pathlib.Path(s['outputUrl']).name}'\n" for s in scenes), encoding='utf-8')
        r = subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', str(lst), '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(OUT / 'film.mp4')],
                           capture_output=True, text=True)
        if r.returncode == 0:
            p['film'] = 'videos/film.mp4'
            save(p)
            log('🎉 الفيلم النهائي: videos/film.mp4')
        else:
            log('فشل الدمج (قد تختلف أبعاد المقاطع):', r.stderr[-300:])
    log(f"الحالة: {sum(s.get('status') == 'done' for s in scenes)}/{len(scenes)} مقطعاً.")


def main():
    if not TOKEN:
        log('تحذير: لا HF_TOKEN؛ ستُحسب الحصة كزائر.')
    p = load_project()
    OUT.mkdir(exist_ok=True)
    todo = [s for s in p['scenes'] if s.get('status') != 'done']
    if not todo:
        return finish(p)
    from gradio_client import Client
    done_now = 0
    for space in find_spaces(p):
        if not todo or done_now >= MAX_CLIPS:
            break
        try:
            log('أجرّب', space)
            client = Client(space, hf_token=TOKEN or None, verbose=False)
            ep = pick_endpoint(client.view_api(return_format='dict', print_info=False))
        except Exception as e:
            log('تخطٍّ:', str(e)[:200])
            continue
        if not ep:
            log('تخطٍّ: لا endpoint نصي مناسب')
            continue
        log('اعتُمد', space, ep[0])
        bad = 0
        while todo and done_now < MAX_CLIPS:
            s = todo[0]
            try:
                f = generate(client, ep, prompt_of(p, s))
                dest = OUT / f"scene-{int(s['id']):03d}{pathlib.Path(f).suffix.lower()}"
                shutil.copy(f, dest)
                s.update(status='done', outputUrl=f'videos/{dest.name}', space=space)
                s.pop('error', None)
                p['worker'] = {'space': space}
                todo.pop(0)
                done_now += 1
                bad = 0
                save(p)
                log('✔ المقطع', s['id'])
            except Exception as e:
                m = str(e)
                s.update(status='failed', error=m[:300])
                save(p)
                log('✘ المقطع', s['id'], m[:200])
                if QUOTA.search(m):
                    log('انتهت الحصة اليومية؛ يكمل التشغيل التالي.')
                    return finish(p)
                bad += 1
                if bad >= 2:
                    break
    finish(p)


if __name__ == '__main__':
    main()
