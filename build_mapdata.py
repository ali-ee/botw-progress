"""Build assets/map_data.json from your own Breath of the Wild game files (Savage Wind's extracted folder).

    python build_mapdata.py --extracted <Savage Wind data folder>\\extracted [--lang USen]

Everything the map shows comes from the game files: the location markers and Korok list of
Map/MainField/Static.smubin, the actors placed in the 80 map sections (Map/MainField/<A-J>-<1-8>_Static and
_Dynamic), the save-data flag list (GameData/gamedata.ssarc) and the names in Pack/Bootup_<lang>.pack.
Each object carries the CRC32 of the save flag that says it's done (`completion_hash`).
"""
import argparse
import json
import re
import sys
import zlib
from pathlib import Path

import gamefiles
from paths import ASSETS, DATA

OUT = ASSETS / 'map_data.json'

# Location marker icons that are a place you discover (shops sit inside towns and get no marker here)
PLACE_ICONS = {'Village', 'Hatago', 'CheckPoint', 'Labo', 'Castle', 'StartPoint', None}
# Placed enemies tracked once each (the DLC's monster medals keep a flag per actor: MainField_IsDefeat_<actor>_<id>)
BOSSES = (('Enemy_Giant', 'hinoxes'), ('Enemy_Golem', 'taluses'), ('Enemy_Sandworm', 'moldugas'))


def crc(name):
    return zlib.crc32(name.encode('utf-8'))


def pos(t):
    if isinstance(t, dict):
        x, y, z = t['X'], t['Y'], t['Z']
    else:
        x, y, z = t[0], t[1], t[2]
    return {'x': round(x, 2), 'y': round(y, 2), 'z': round(z, 2)}


def messages(rf, lang):
    """{msbt path without .msbt: {label: text}} of the game text in `lang` (USen when missing)."""
    for code in (lang, 'USen'):
        pack = f'Pack/Bootup_{code}.pack'
        if rf.exists(pack):
            files = rf.pack(pack, dlc=False)
            ssarc = next(n for n in files if n.endswith('.ssarc'))
            return {n[:-5]: d for n, d in gamefiles.sarc(files[ssarc]).items() if n.endswith('.msbt')}, code
    raise FileNotFoundError('no Pack/Bootup_<language>.pack in the game files')


class Text:
    def __init__(self, raw):
        self.raw = raw
        self.cache = {}

    def __call__(self, name):
        if name not in self.cache:
            self.cache[name] = gamefiles.msbt(self.raw[name]) if name in self.raw else {}
        return self.cache[name]

    def actors(self):
        out = {}
        for n in self.raw:
            if n.startswith('ActorType/'):
                out.update(self(n))
        return out


def game_flags(rf):
    """{flag name: data type} of every save flag the game knows (GameData/gamedata.ssarc in Bootup.pack)."""
    boot = rf.pack('Pack/Bootup.pack', dlc=False)
    out = {}
    for fn, data in gamefiles.sarc(boot['GameData/gamedata.ssarc']).items():
        kind = fn.rsplit('/', 1)[-1].rsplit('_', 1)[0]
        doc = gamefiles.byml(data)
        for lst in doc.values():
            for e in lst:
                out[e['DataName']] = kind
    return out


def static_doc(rf):
    """Map/MainField/Static.smubin: the DLC's copy when installed (it adds the DLC shrines), else the base."""
    if rf.exists('Map/MainField/Static.smubin'):
        return gamefiles.byml(rf.read('Map/MainField/Static.smubin'))
    return gamefiles.byml(rf.pack('Pack/Bootup.pack', dlc=False)['Map/MainField/Static.smubin'])


def map_sections(rf, log=lambda m: None):
    """The 80 MainField sections' Static and Dynamic documents. The DLC's copies of a section win over the base game's."""
    titlebg = rf.pack('Pack/TitleBG.pack', dlc=False)
    aoc = rf.pack('Pack/AocMainField.pack') if rf.exists('Pack/AocMainField.pack') else {}
    for c in 'ABCDEFGHIJ':
        for r in range(1, 9):
            sec = f'{c}-{r}'
            for kind in ('Static', 'Dynamic'):
                rel = f'Map/MainField/{sec}/{sec}_{kind}.smubin'
                if rf.aoc and rf.path(rel) and rf.path(rel) != rf.base / rel:
                    data = rf.read(rel)
                elif rel in aoc:
                    data = aoc[rel]
                elif rel in titlebg:
                    data = titlebg[rel]
                elif rf.exists(rel):
                    data = rf.read(rel, dlc=False)
                else:
                    continue
                yield gamefiles.byml(data)
        log(f'map data: section {c} read')


def placed_actors(rf, log=print, rails=None):
    """Every actor placed in the 80 MainField sections. Pass a dict as `rails` to also get the sections' rails
    ({rail HashId: [points]}, the paths moving things follow, e.g. a Korok's sparkle) in the same read."""
    objs = []
    for doc in map_sections(rf, log):
        objs.extend(doc.get('Objs', []))
        if rails is not None:
            for rl in doc.get('Rails', []):
                rails[rl.get('HashId')] = [pt['Translate'] for pt in rl.get('RailPoints', []) if 'Translate' in pt]
    return objs


# ---- Korok puzzles. A hidden Korok appears when the logic linked to it fires; following those links back
# (through LinkTag/ActorObserver/Area nodes) reaches the puzzle: the targets to shoot, the flowers to follow,
# the rock that completes a circle... The English labels are translated through the `data` table of the page.
LOGIC = re.compile(r'^(LinkTag|SwitchTimeLag|EventTag|AreaTag|SignalFlowchart|TimerTag|ActorObserver|Area$|AutoPlacement)')
KOROK_RULES = (   # (label, test on (puzzle actors, actors watched by observers, Korok parameters)); first match wins
    ('Under a rock', lambda u, w, p: p.get('IsHiddenKorokLiftAppear')),
    ('Catch the sparkle', lambda u, w, p: p.get('IsInvisibleKorok')),
    ('Race to the goal', lambda u, w, p: 'FldObj_KorokGoal_A' in u),
    ('Shoot the target', lambda u, w, p: 'FldObj_KorokTarget_A' in u),
    ('Shoot the acorns', lambda u, w, p: 'Obj_KorokPot_A' in u),
    ('Follow the flowers', lambda u, w, p: 'Obj_Plant_Korok_A' in u),
    ('Melt the ice', lambda u, w, p: 'IceWall' in u),
    ('Fruit offering', lambda u, w, p: any(x.startswith('Item_Fruit') for x in w)),
    ('Match the metal blocks', lambda u, w, p: 'Obj_KorokIronRock_A_01' in w),
    ('Boulder into the hole', lambda u, w, p: any(x.startswith('FldObj_PushRock') for x in w)),
    ('Complete the rock circle', lambda u, w, p: any(x.startswith(('Obj_LiftRock', 'FldObj_KorokStoneLift')) for x in w)),
    ('Reach the spot', lambda u, w, p: 'GameROMPlayer' in w),
    ('Bomb the rocks', lambda u, w, p: 'Obj_RockCover' in w),
)


class Links:
    """Who links to whom among the placed actors (to walk a Korok's puzzle backwards)."""

    def __init__(self, objs):
        self.by_hash = {}
        self.incoming = {}
        for o in objs:
            self.by_hash.setdefault(o.get('HashId'), o)
            for link in o.get('LinksToObj') or []:
                self.incoming.setdefault(link.get('DestUnitHashId'), []).append(o)

    def puzzle(self, h, depth=6):
        """(puzzle actors [(unit, position)], logic units, actor names watched by observers) behind actor h."""
        seen, actors, logic, watched, frontier = {h}, [], set(), set(), [h]
        for _ in range(depth):
            nxt = []
            for x in frontier:
                for s in self.incoming.get(x, []):
                    sh = s.get('HashId')
                    if sh in seen:
                        continue
                    seen.add(sh)
                    u = s.get('UnitConfigName', '?')
                    p = s.get('!Parameters') or {}
                    if u.startswith('ActorObserver'):
                        watched.add(p.get('ActorName') or f"group{p.get('ActorGroupForObserveTag')}")
                    if LOGIC.match(u):
                        nxt.append(sh)
                        logic.add(u)
                        if u == 'Area' and s.get('Translate') is not None:
                            actors.append(('Area', s['Translate']))
                    else:
                        if s.get('Translate') is not None:
                            actors.append((re.sub(r'_\d+$', '', u), s['Translate']))
                        if 'Korok' in u:   # a Korok puzzle piece has its own triggers (a race's start links to its goal)
                            nxt.append(sh)
            frontier = nxt
        return actors, logic, watched


def _xz(t):
    return (t['X'], t['Z']) if isinstance(t, dict) else (t[0], t[2])


def _dist(a, b):
    (ax, az), (bx, bz) = _xz(a), _xz(b)
    return ((ax - bx) ** 2 + (az - bz) ** 2) ** 0.5


def korok_puzzle(actor, korok_pos, links, rails):
    """(label or None, path or None) of a hidden Korok. Paths: the start of a race (start -> Korok), the flowers to
    follow in order, or the loop a sparkle flies."""
    p = actor.get('!Parameters') or {}
    found, logic, watched = links.puzzle(actor.get('HashId'))
    units = {u for u, _ in found} | logic
    label = next((lbl for lbl, test in KOROK_RULES if test(units, watched, p)), None)
    path = None
    if label == 'Follow the flowers':
        left = [t for u, t in found if u == 'Obj_Plant_Korok_A']
        cur = max(left, key=lambda t: _dist(t, korok_pos))   # the first flower is the one farthest from the Korok
        order = []
        while left:   # then always the nearest one left
            cur = min(left, key=lambda t: _dist(t, cur))
            left.remove(cur)
            order.append(cur)
        path = {'flowers': [{'position': pos(t)} for t in order]}
    elif label == 'Catch the sparkle':
        for link in actor.get('LinksToRail') or []:
            pts = rails.get(link.get('DestUnitHashId'))
            if pts and len(pts) > 1:
                path = {'rail': [pos(t) for t in pts]}
                break
    elif label in ('Race to the goal', 'Reach the spot'):
        areas = [t for u, t in found if u == 'Area' and 20 < _dist(t, korok_pos) < 1000]
        if areas:
            path = {'start': pos(max(areas, key=lambda t: _dist(t, korok_pos))), 'end': pos(korok_pos)}
    return label, path


def korok_hints():
    """Korok hints from a local file the user keeps next to the app's data (korok_hints.json, never in the
    repository: it holds a guide's text). {Korok flag: {"hint": text}}; an empty dict when there is none."""
    f = DATA / 'korok_hints.json'
    try:
        raw = json.loads(f.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    out = {}
    for flag, v in raw.items():
        text = v.get('hint') if isinstance(v, dict) else v
        if isinstance(text, str) and text.strip():
            out[flag] = text.strip()
    return out


def local_json(name):
    """A data file the user keeps next to the app's data (the repository folder, or the app's data folder for
    the exe), never in the repository: markers.json and markers-ext.json are an online map's marker exports.
    None when there is none or it can't be read."""
    try:
        return json.loads((DATA / name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


class Heights:
    """The height of the ground near an x, z (a file that only has x and z): the nearest placed actor's."""

    def __init__(self, objs, cell=64):
        self.cell, self.grid = cell, {}
        for o in objs:
            t = o.get('Translate')
            if t is not None:
                self.grid.setdefault((int(t[0] // cell), int(t[2] // cell)), []).append(t)

    def y(self, x, z, default=None):
        cx, cz = int(x // self.cell), int(z // self.cell)
        near = [t for dx in (-1, 0, 1) for dz in (-1, 0, 1) for t in self.grid.get((cx + dx, cz + dz), ())]
        if not near:
            return default
        return min(near, key=lambda t: (t[0] - x) ** 2 + (t[2] - z) ** 2)[1]


def korok_types(koroks, mk, max_dist=15):
    """Puzzle types from a local markers.json (its Korok rows [x, z, id, type index, region], 33 kinds, every Korok
    typed): replace the game-file guess for each Korok it has within max_dist. Returns how many it typed."""
    if not mk:
        return 0
    types = mk.get('korokTypes') or []
    rows = [r for r in mk.get('korok') or []
            if isinstance(r, list) and len(r) >= 4 and isinstance(r[3], int) and 0 <= r[3] < len(types)]
    n = 0
    for o in koroks:
        p = o['position']
        best = min(rows, key=lambda r: (r[0] - p['x']) ** 2 + (r[1] - p['z']) ** 2, default=None)
        if best and (best[0] - p['x']) ** 2 + (best[1] - p['z']) ** 2 <= max_dist ** 2:
            o['type'] = types[best[3]]
            n += 1
    return n


# A short hint per dragon (our own words); the dragon's name, spring, parts and route come from markers-ext.json.
DRAGON_HINTS = {
    'farosh': 'Flies over Faron and the Gerudo Highlands; shoot a part from a cliff or while gliding.',
    'dinraal': 'Flies over Eldin, Akkala and Tabantha; its heat burns, so get close only with fire protection.',
    'naydra': 'Covered in Malice until you free it at the Spring of Wisdom; then it circles Mount Lanayru.',
}


def dragons(ext, objs):
    """Farosh, Dinraal and Naydra from a local markers-ext.json: the spring each rises from (the marker), the parts
    it drops and its flight routes (segments [x1, z1, x2, z2]). Not tracked: dragons never go away."""
    if not ext:
        return []
    strings, routes = ext.get('strings') or [], ext.get('routes') or {}
    heights = None
    out = []
    for r in ext.get('rows') or []:
        if not (isinstance(r, list) and len(r) >= 6 and r[3] == 'dragon'):
            continue
        key, spring, parts = (str(r[5]).split('|') + ['', '', ''])[:3]
        name = strings[r[4]] if isinstance(r[4], int) and r[4] < len(strings) else key.title()
        flat = [v for v in routes.get(key) or [] if isinstance(v, (int, float))]
        segs = {tuple(flat[i:i + 4]) for i in range(0, len(flat) - 3, 4) if flat[i:i + 2] != flat[i + 2:i + 4]}
        heights = heights or Heights(objs)
        o = {'display_name': name, 'key': key,
             'position': pos([r[0], heights.y(r[0], r[1], 100.0), r[1]]),
             'route': [list(s) for s in sorted(segs)]}
        if spring:
            o['place'] = spring
        if parts:
            o['drops'] = [p for p in parts.split(';') if p]
        if key in DRAGON_HINTS:
            o['hint'] = DRAGON_HINTS[key]
        out.append(o)
    return out


# ---- Captured Memories: the 13 spots (RememberTag actors named after their cutscene, Demo126_0..Demo138_0).
# The album photos UI/Album/pict_000..012.jpg are in the same order; the memory titles are labels of
# LayoutMsg/PauseMenu_00 (the memory list, which also holds the main-story memories in between).
MEMORY_DEMOS = [f'Demo{n}_0' for n in range(126, 139)]
MEMORY_TITLES = ['0043', '0044', '0045', '0047', '0048', '0049', '0051', '0052', '0053', '0054', '0055', '0056', '0057']


def memories(rf, objs, text, flags, out_dir):
    spots = {o.get('UniqueName'): o for o in objs if o.get('UnitConfigName') == 'RememberTag'}
    titles = text('LayoutMsg/PauseMenu_00')
    places = text('LayoutMsg/AppAlbum_00')
    out = []
    for i, demo in enumerate(MEMORY_DEMOS):
        o = spots.get(demo)
        if not o or f'IsPlayed_{demo}' not in flags:
            continue
        t = (o.get('!Parameters') or {}).get('TraversePos') or o.get('Translate')
        entry = {'internal_name': demo, 'display_name': titles.get(MEMORY_TITLES[i]) or demo, 'number': i + 1,
                 'position': pos(t), 'completion_hash': crc(f'IsPlayed_{demo}')}
        place = places.get(f'{i:04d}')
        if place and i < 12:
            entry['place'] = place
        photo = f'UI/Album/pict_{i:03d}.jpg'
        if out_dir is not None and rf.exists(photo):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f'{demo}.jpg').write_bytes(rf.read(photo, dlc=False))
            entry['photo'] = f'{demo}.jpg'
        out.append(entry)
    return out


# ---- place names, as the game's map shows them: Location.smubin (regions, lakes, mountains, ...), with the zoom
# they show from (ShowLevel 1 = whole map ... 8 = close up) and their kind (Type: 0 water, 3 lava, 4 forest,
# 5 land, 6 region, 7 mountain); then the named spots of Static.smubin's LocationPointer (ruins, bridges, ...).
def place_labels(rf, static, places):
    out, seen = [], {}
    boot = rf.pack('Pack/Bootup.pack', dlc=False)
    try:
        doc = gamefiles.byml(boot['Map/MainField/Location.smubin'])
    except KeyError:
        doc = []
    entries = [(m, m.get('Type')) for m in doc] + [(m, 'spot') for m in static.get('LocationPointer', [])]
    for m, kind in entries:
        name = places.get(m.get('MessageID') or '')
        if not name or 'Translate' not in m:
            continue
        p = pos(m['Translate'])
        # the game repeats a name along a long river, moat or meadow: once within LABEL_REPEAT is enough
        if any(abs(p['x'] - q['x']) + abs(p['z'] - q['z']) < LABEL_REPEAT for q in seen.get(name, [])):
            continue
        seen.setdefault(name, []).append(p)
        out.append({'name': name, 'level': m.get('ShowLevel', 4), 'kind': kind, 'position': p})
    return out


LABEL_REPEAT = 700   # map units (metres)


def build(extracted, out=OUT, lang='USen', log=print):
    rf = gamefiles.Romfs(extracted)
    raw, lang = messages(rf, lang)
    text = Text(raw)
    places = text('StaticMsg/LocationMarker')
    dungeons = text('StaticMsg/Dungeon')   # shrine names and their trial subtitles (DungeonNNN_sub)
    actors = text.actors()
    flags = game_flags(rf)
    log(f'map data: {len(flags)} save flags')
    static = static_doc(rf)
    rails = {}
    objs = placed_actors(rf, log, rails=rails)
    log(f'map data: {len(objs)} placed actors')

    def flag(name):
        return crc(name) if name in flags else None

    def actor_name(a):
        return actors.get(f'{a}_Name') or actors.get(f'{a.replace("Put", "", 1)}_Name')

    data = {k: [] for k in ('koroks', 'shrines', 'towers', 'divine_beasts', 'locations', 'great_fairies',
                            'hinoxes', 'taluses', 'moldugas', 'lynels', 'armor_chests', 'treasure_chests',
                            'horse_gods', 'zora_monuments')}

    # ---- location markers: shrines, towers, Divine Beasts, villages, stables and other places
    seen = set()
    for m in static.get('LocationMarker', []) + static.get('LocationPointer', []):
        sf, mid, icon = m.get('SaveFlag'), m.get('MessageID'), m.get('Icon')
        if not sf or sf in seen or not mid:
            continue
        name = places.get(mid) or mid
        p = pos(m['Translate'])
        if icon == 'Dungeon':
            seen.add(sf)
            o = {'internal_name': mid, 'display_name': name, 'position': p,
                 'completion_hash': flag(f'Clear_{mid}'), 'found_hash': flag(sf)}
            sub = dungeons.get(f'{mid}_sub')
            if sub:
                o['subtitle'] = sub
            data['shrines'].append(o)
        elif icon == 'Tower':
            seen.add(sf)
            n = re.sub(r'\D', '', mid)
            data['towers'].append({'internal_name': mid, 'display_name': name, 'position': p,
                                   'completion_hash': flag(f'MapTower_{int(n):02d}') if n else None})
        elif icon and icon.startswith('Remains'):
            if icon in {o['internal_name'] for o in data['divine_beasts']}:
                continue
            seen.add(sf)
            data['divine_beasts'].append({'internal_name': icon, 'display_name': places.get(icon) or name,
                                          'position': p, 'completion_hash': flag(f'Clear_{icon}')})
        elif (icon in PLACE_ICONS) and sf.startswith('Location_') and sf in flags:
            seen.add(sf)
            data['locations'].append({'internal_name': mid, 'display_name': name, 'position': p,
                                      'completion_hash': crc(sf)})
    # a Divine Beast marker moves with the beast; the location marker with Clear_ is the one that counts
    data['shrines'].sort(key=lambda o: o['internal_name'])
    data['towers'].sort(key=lambda o: o['internal_name'])

    # ---- Korok seeds: Static.smubin's KorokLocation list, with the hidden Korok's save flag, its puzzle (type and
    # path, from the game files) and a hint from the user's own korok_hints.json when there is one
    links = Links(objs)
    hints = korok_hints()
    for k in static.get('KorokLocation', []):
        f = k.get('Flag')
        if not f or f not in flags:
            continue
        hid = int(f.rsplit('_', 1)[1]) if f.rsplit('_', 1)[-1].isdigit() else None
        actor = links.by_hash.get(hid)
        o = {'completion_hash': crc(f), 'position': pos(k['Translate'])}
        if actor:
            kind, path = korok_puzzle(actor, k['Translate'], links, rails)
            if kind:
                o['type'] = kind
            if path:
                o['path'] = path
        if f in hints:
            o['hint'] = hints[f]
        data['koroks'].append(o)
    log(f'map data: Korok hints from korok_hints.json: {sum(1 for o in data["koroks"] if "hint" in o)}')

    # ---- Captured Memories (photos copied next to the map data) and the map's place names
    data['memories'] = memories(rf, objs, text, flags, Path(out).parent / 'memories')
    data['labels'] = place_labels(rf, static, places)

    # ---- placed actors: Great Fairies, bosses, Lynels, treasure chests
    for o in objs:
        u = o['UnitConfigName']
        hid = o.get('HashId')
        t = o.get('Translate')
        if t is None:
            continue
        if u.startswith('Npc_DressFairy_'):   # the Great Fairy of fountain N (RevivalGreatFairyN+1 when restored)
            n = int(u.rsplit('_', 1)[1]) + 1
            data['great_fairies'].append({'display_name': actor_name(u) or 'Great Fairy', 'position': pos(t),
                                          'completion_hash': flag(f'RevivalGreatFairy{n}'), 'actor': u})
            continue
        if u.startswith('FldObj_RockZoraRelief_A_M_'):   # the Zora Stone Monuments, 1-10 (ZoraRelief_Check_NN: read)
            n = int(u.split('_')[4])
            data['zora_monuments'].append({'number': n, 'position': pos(t), 'actor': u,
                                           'completion_hash': flag(f'ZoraRelief_Check_{n:02d}')})
            continue
        if u.startswith('HorseGod'):   # Malanya, who brings back a horse that died; TraversePos: where Link stands
            tp = (o.get('!Parameters') or {}).get('TraversePos')
            data['horse_gods'].append({'display_name': actor_name(u) or 'Malanya', 'actor': u,
                                       'position': pos([tp['X'], tp['Y'], tp['Z']] if tp else t)})
            continue
        for prefix, key in BOSSES:
            # (the Champions' Ballad's Igneo Talus Titan and Molduking, Enemy_Golem_Fire_R / Enemy_SandwormR, are
            # quest bosses: their IsDefeat flag stays 0 even in a 100% save)
            if u.startswith(prefix) and not u.endswith('R'):
                f = flag(f'MainField_IsDefeat_{u}_{hid}')
                if f is not None:
                    data[key].append({'display_name': actor_name(u) or u, 'completion_hash': f,
                                      'position': pos(t), 'actor': u})
                break
        if u.startswith('Enemy_Lynel'):
            data['lynels'].append({'display_name': actor_name(u) or 'Lynel', 'position': pos(t), 'actor': u})
        elif u.startswith('TBox_'):
            f = flag(f'MainField_{u}_{hid}')
            if f is None:
                continue
            drop = (o.get('!Parameters') or {}).get('DropActor') or ''
            key = 'armor_chests' if drop.startswith('Armor_') else 'treasure_chests'
            entry = {'completion_hash': f, 'position': pos(t), 'actor': u}
            if drop:
                entry['display_name'] = actor_name(drop) or drop
                entry['drop'] = drop
            data[key].append(entry)
    # the Great Fairy fountains before restoring: GreatFairyBeforeRevival stands where the fairy will be
    if not data['great_fairies']:
        for o in objs:
            if o['UnitConfigName'] == 'GreatFairyBeforeRevival':
                data['great_fairies'].append({'display_name': 'Great Fairy', 'position': pos(o['Translate'])})

    data['zora_monuments'].sort(key=lambda o: o['number'])
    # from the user's own local files, when there are any (see local_json)
    n = korok_types(data['koroks'], local_json('markers.json'))
    log(f'map data: Korok types from markers.json: {n}')
    data['dragons'] = dragons(local_json('markers-ext.json'), objs)
    log(f'map data: dragons from markers-ext.json: {len(data["dragons"])}')

    data = {k: [o for o in v if o.get('completion_hash') is not None or k in ('lynels', 'labels', 'horse_gods', 'dragons')] for k, v in data.items()}
    data['meta'] = {'lang': lang}
    write(data, out)
    summary = ', '.join(f'{k} {len(v)}' for k, v in data.items() if k != 'meta')
    log(f'map data: {summary}')
    return data


def write(data, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.part')
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--extracted', required=True, help="Savage Wind's extracted folder (romfs, aoc1, ...)")
    ap.add_argument('--out', default=str(OUT))
    ap.add_argument('--lang', default='USen')
    args = ap.parse_args()
    if not (Path(args.extracted) / 'romfs').is_dir():
        sys.exit('Not a Savage Wind extracted folder (no romfs inside).')
    build(args.extracted, args.out, args.lang)


if __name__ == '__main__':
    main()
