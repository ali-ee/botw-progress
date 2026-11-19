"""Build assets/quests.json from your own Breath of the Wild game files (Savage Wind's extracted folder).

    python build_quests.py --extracted <Savage Wind data folder>\\extracted [--lang USen]

Reads Quest/QuestProduct.sbquestpack (in Pack/TitleBG.pack: each quest's steps, the flag that ends each
step and the places they point to) and the adventure log texts (QuestMsg/QL_<quest>.msbt), quest giver and
region names from Pack/Bootup_<lang>.pack.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import gamefiles
from build_mapdata import crc, game_flags, messages, Text, placed_actors, static_doc, local_json, Heights
from paths import ASSETS

OUT = ASSETS / 'quests.json'
CATEGORY = {'Main': 'main', 'Sub': 'shrine', 'Mini': 'side_quest'}
ORDER = {'main': 0, 'side_quest': 1, 'shrine': 2}


def tidy(s):
    return re.sub(r'\n{3,}', '\n\n', (s or '').replace('\r', '')).strip()


def points(step):
    out = []
    for a in step.get('IndicatorActors') or []:
        loc = a.get('Location')
        if isinstance(loc, dict):
            p = [round(loc['X'], 1), round(loc.get('Y', 0), 1), round(loc['Z'], 1)]
            if p not in out and (p[0], p[2]) != (0, 0):
                out.append(p)
    return out


def _pt(t):
    if isinstance(t, dict):
        return [round(t['X'], 1), round(t['Y'], 1), round(t['Z'], 1)]
    return [round(t[0], 1), round(t[1], 1), round(t[2], 1)]


MAX_GOAL_POINTS = 8


def goal_finder(objs, static):
    """Where a quest's hidden goal is. Many quests point the map at the person to report to, while what they ask
    for is a flag the game sets somewhere else: a trigger placed in the map that sets it (Misko's treasure, a
    riddle's answer) or a shrine that appears (Open_DungeonNNN). Returns flag -> [points] (empty when unknown)."""
    setters = {}
    for o in objs:
        f = (o.get('!Parameters') or {}).get('SaveFlag')
        if f and o.get('Translate') is not None:
            p = _pt(o['Translate'])
            if p[1] != 0:   # logic parked at height 0 (outside the playable world) is no place to go
                setters.setdefault(f, []).append(p)
    shrines = {m.get('MessageID'): _pt(m['Translate']) for m in static.get('LocationMarker', [])
               if m.get('Icon') == 'Dungeon' and m.get('MessageID')}

    def find(flag):
        if not flag:
            return []
        m = re.fullmatch(r'Open_(Dungeon\d+)', flag)
        if m and m.group(1) in shrines:
            return [shrines[m.group(1)]]
        out = []
        for p in setters.get(flag, []):   # triggers of one place (a door and its switch, two sides of a spring) count once
            if all(abs(p[0] - q[0]) + abs(p[2] - q[2]) > GOAL_MERGE for q in out):
                out.append(p)
        return out[:MAX_GOAL_POINTS]
    return find


GOAL_MERGE = 250   # map units: hidden-goal triggers closer than this are one place


def _norm(name):
    return re.sub(r'[^a-z0-9]+', ' ', re.sub(r'\s*\(quest\)\s*$', '', name.lower())).strip()


def giver_spots(ext, objs, where):
    """{normalized English quest name: [x, y, z]} from a local markers-ext.json (its questMain / questSide /
    questShrine rows: where the quest giver stands, by hand, x and z only). The height is the giver's own
    placement's when one is near, else the nearest placed actor's."""
    if not ext:
        return {}
    strings = ext.get('strings') or []
    heights = Heights(objs)
    out = {}
    for r in ext.get('rows') or []:
        if not (isinstance(r, list) and len(r) >= 5 and r[3] in ('questMain', 'questSide', 'questShrine')):
            continue
        if not (isinstance(r[4], int) and r[4] < len(strings)):
            continue
        x, z = r[0], r[1]
        npc = [p for ps in where.values() for p in ps if (p[0] - x) ** 2 + (p[2] - z) ** 2 < 25 ** 2]
        y = min(npc, key=lambda p: (p[0] - x) ** 2 + (p[2] - z) ** 2)[1] if npc else heights.y(x, z)
        if y is not None:
            out.setdefault(_norm(strings[r[4]]), [round(x, 1), round(y, 1), round(z, 1)])
    return out


def build(extracted, out=OUT, lang='USen', log=print, objs=None):
    rf = gamefiles.Romfs(extracted)
    raw, lang = messages(rf, lang)
    text = Text(raw)
    places = text('StaticMsg/LocationMarker')
    actors = text.actors()
    flags = game_flags(rf)
    pack = gamefiles.byml(rf.pack('Pack/TitleBG.pack', dlc=False)['Quest/QuestProduct.sbquestpack'])
    # quest givers: where the NPC who gives the quest is placed (when there's just one of them)
    if objs is None:
        objs = placed_actors(rf, log=lambda m: None)
    find_goal = goal_finder(objs, static_doc(rf))
    where = {}
    for o in objs:
        u = o['UnitConfigName']
        if u.startswith('Npc_') and 'Translate' in o:
            t = o['Translate']
            where.setdefault(u, []).append([round(t[0], 1), round(t[1], 1), round(t[2], 1)])

    # better quest giver spots from a local markers-ext.json (its quest names are English)
    spots_ext = giver_spots(local_json('markers-ext.json'), objs, where)
    en = text if lang == 'USen' or not spots_ext else Text(messages(rf, 'USen')[0])
    quests = []
    for i, q in enumerate(pack):
        qid = q['Name']
        msg = text(f'QuestMsg/QL_{qid}')
        name = msg.get(f'QL_{qid}_Name')
        if not name or q.get('NotPostNote'):
            continue   # not in the adventure log
        # a step's real goal when the game keeps it apart from the step (StepDependencyFlags: the flag that has to
        # be set before the step's own NextFlag can be, e.g. Misko's treasure found before reporting back)
        needs = {}
        for d in q.get('StepDependencyFlags') or []:
            if d.get('NextFlag') and d.get('DependencyFlag'):
                needs.setdefault(d['NextFlag'], []).append(d['DependencyFlag'])
        steps = []
        for s in q.get('Steps', []):
            nf = s.get('NextFlag')
            step = {
                'name': s.get('Name', ''),
                'flag': crc(nf) if nf and nf in flags else None,
                'text': tidy(msg.get(f'QL_{qid}_{s.get("MessageName")}', '')),
                'points': points(s),
            }
            goal = [p for f in needs.get(nf, []) for p in find_goal(f)]
            if not goal and not step['points']:   # no map pin: where the game sets this step's own flag
                goal = find_goal(nf)
            # only a goal that is one place: triggers spread over Hyrule (one per entrance of a region, ...) say
            # nothing about where to go
            if len(goal) == 1 and step['name'] != 'Ready':
                step['goal'] = goal
            steps.append(step)
        giver = q.get('Orderer') or ''
        spots = where.get(giver, [])
        exact = 0 < len(spots) <= 3
        start = spots if exact else next((s['points'] for s in steps if s['points']), [])
        better = spots_ext.get(_norm(en(f'QuestMsg/QL_{qid}').get(f'QL_{qid}_Name') or ''))
        if better:
            start, exact = [better], True
        ready = f'{qid}_Ready'
        quests.append({
            'id': qid,
            'name': name,
            'category': CATEGORY.get(q.get('Type'), 'side_quest'),
            'sort': i,
            'giver': actors.get(f'{giver}_Name', '') if giver.startswith('Npc') else '',
            'region': places.get(q.get('Location') or '', ''),
            'ready': crc(ready) if ready in flags else None,
            'start': {'points': start[:4], 'exact': exact},
            'requires': None,
            'steps': steps,
        })
    quests.sort(key=lambda x: (ORDER.get(x['category'], 9), x['sort']))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.part')
    tmp.write_text(json.dumps({'lang': lang, 'quests': quests}, ensure_ascii=False, separators=(',', ':')),
                   encoding='utf-8')
    tmp.replace(out)
    by = {}
    for x in quests:
        by[x['category']] = by.get(x['category'], 0) + 1
    log(f'{len(quests)} quests {by}')
    return quests


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--extracted', required=True)
    ap.add_argument('--out', default=str(OUT))
    ap.add_argument('--lang', default='USen')
    args = ap.parse_args()
    if not (Path(args.extracted) / 'romfs').is_dir():
        sys.exit('Not a Savage Wind extracted folder (no romfs inside).')
    build(args.extracted, args.out, args.lang)


if __name__ == '__main__':
    main()
