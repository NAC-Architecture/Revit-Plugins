# -*- coding: utf-8 -*-
"""NAC Tag Walls - stack-aware, room-aware wall tagging.

Pipeline (developed & field-tested on Marquez Charter ES, July 2026):
  1. Collect  : Basic walls in active view, phase-filtered, exclusions applied
  2. Classify : true perpendiculars, stacked-partner detection vs ALL walls,
                phase-correct room probing on both sides
  3. Decide   : stacked -> tag away from partner
                room on one side -> Int walls toward room, Ext walls away
                fallback -> exterior-direction quadrant
  4. Place    : family = nearest of Up/Down/Left/Right to chosen direction,
                head at midpoint + true perpendicular * offset, leader attached
  5. Solve    : TEXT-on-TEXT collisions only (shrunken boxes);
                slide along wall -> capped perpendicular growth -> same-type merge
  6. QA mode  : highlight problems in red, change nothing
"""

import json
import math
import os
import re

from pyrevit import DB, forms, revit, script

logger = script.get_logger()
output = script.get_output()

doc = revit.doc
uidoc = revit.uidoc

# ----------------------------------------------------------------- config ---

DEFAULT_CONFIG = {
    "phase_name": "New Construction",
    "exclude_keywords": ["S6AA", "STOREFRONT", "CURTAIN", "CURB",
                         "INT FIN", "FIN -", "FINISH", "T-1"],
    "exclude_prefixes": ["PT-", "WC-"],
    "prompt_wall_types": True,   # firm-wide: pick wall types live per run
    "min_wall_length_ft": 1.0,
    "proximity_merge_ft": 6.0,
    "base_offset_ft": 2.5,
    "tag_min_gap_ft": 1.0,
    "max_leader_ft": 12.0,
    "leader_attach_threshold_ft": 6.0,
    "max_perp_extra_ft": 3.0,
    "stack_separation_ft": 3.5,
    "parallel_dot_min": 0.985,
    "room_probe_dists_ft": [3.0, 5.0, 8.0],
    "text_box_shrink": 0.45,
    "slide_step_ft": 1.2,
    "dedup_only_when_colliding": True,
    "building_rotation_deg": None,   # None = auto-detect from wall angles
    "tag_families": {
        "Up":        ["TAG_WallTypeUp_NAC24", "Up"],
        "Down":      ["TAG_WallTypeDown_NAC24", "Down"],
        "Left":      ["TAG_WallTypeLeft_NAC24", "Left"],
        "Right":     ["TAG_WallTypeRight_NAC24", "Right"],
        "Up Opp":    ["TAG_WallTypeUp_NAC24", "Up Opp"],
        "Down Opp":  ["TAG_WallTypeDown_NAC24", "Down Opp"],
        "Left Opp":  ["TAG_WallTypeLeft_NAC24", "Left Opp"],
        "Right Opp": ["TAG_WallTypeRight_NAC24", "Right Opp"],
    },
}


def load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r") as f:
                cfg.update(json.load(f))
        except Exception as ex:
            logger.warning("config.json unreadable, using defaults: %s", ex)
    else:
        try:
            with open(cfg_path, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        except Exception:
            pass
    return cfg


CFG = load_config()

# ------------------------------------------------------------- utilities ---

SNP = DB.BuiltInParameter.SYMBOL_NAME_PARAM


def type_name(elem_type):
    p = elem_type.get_Parameter(SNP)
    return p.AsString() if p else "?"


def wall_type_name(wall):
    return type_name(doc.GetElement(wall.GetTypeId()))


def get_phase(view=None):
    """Configured phase by name if present; otherwise (firm-wide fallback)
    the active view's own phase, so the tool works in projects that don't
    have a phase literally named 'New Construction'."""
    for ph in doc.Phases:
        if ph.Name == CFG["phase_name"]:
            return ph
    if view is not None:
        try:
            pid = view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE).AsElementId()
            ph = doc.GetElement(pid)
            if isinstance(ph, DB.Phase):
                return ph
        except Exception:
            pass
    return None


def get_tag_type_ids():
    ids = {}
    coll = DB.FilteredElementCollector(doc)\
             .OfCategory(DB.BuiltInCategory.OST_WallTags)\
             .WhereElementIsElementType()
    for t in coll:
        for fam_key, (fam_name, tp_name) in CFG["tag_families"].items():
            if t.FamilyName == fam_name and type_name(t) == tp_name:
                ids[fam_key] = t.Id
    return ids


def is_excluded(tname):
    """Returns (excluded_bool, matched_rule_or_None) so callers can log
    exactly which keyword/prefix fired - needed to tell a real exclusion
    bug apart from a legitimately-different wall type with a similar name."""
    u = tname.upper()
    for kw in CFG["exclude_keywords"]:
        if kw.upper() in u:
            return True, "keyword:" + kw
    for pre in CFG["exclude_prefixes"]:
        if u.startswith(pre.upper()):
            return True, "prefix:" + pre
    return False, None


def needs_opp_variant(tname):
    """Opp rule (locked 2026-07-17): a single-substrate wall takes the Opp
    tag variant ONLY if its one substrate letter is an INTERIOR finish
    (uppercase A-L). M8/M12 (no letters), S6AA/S8XA (both sides), S4X
    (single exterior letter K-Z), and S4Zr (ext finish + sheathing) all
    stay standard. Empirical basis: drafter Opp usage concentrates
    exclusively on _Ext S4A / _Ext S6A / _Int S2A furring types."""
    m = re.search(r'\b[SM](\d+)([A-Za-z]*)', tname)
    if not m:
        return False
    letters = m.group(2)
    return len(letters) == 1 and "A" <= letters <= "L"


def wall_geometry(wall):
    """Return dict of curve-derived geometry, or None."""
    loc = wall.Location
    if not isinstance(loc, DB.LocationCurve):
        return None
    curve = loc.Curve
    p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
    length = curve.Length
    if length < 0.5:
        return None
    wdx, wdy = (p1.X - p0.X) / length, (p1.Y - p0.Y) / length
    return {
        "mid": ((p0.X + p1.X) / 2.0, (p0.Y + p1.Y) / 2.0),
        "wdx": wdx, "wdy": wdy,
        "perp": (-wdy, wdx),
        "length": length,
        "z": p0.Z,
        "p0": (p0.X, p0.Y),
        "p1": (p1.X, p1.Y),
    }

# ------------------------------------------------------------ collection ---


def list_view_wall_types(view):
    """Inventory Basic-wall TYPES that actually have instances in the active
    view. Returns {type_name: {count, excluded, rule}}. Feeds the live
    picker so the tool adapts to any project's naming instead of the
    Marquez-specific keyword list. View-scoped: only walls in the view can
    be tagged this run."""
    info = {}
    coll = DB.FilteredElementCollector(doc, view.Id)\
             .OfCategory(DB.BuiltInCategory.OST_Walls)\
             .WhereElementIsNotElementType()
    for w in coll:
        if not isinstance(w, DB.Wall):
            continue
        try:
            if doc.GetElement(w.GetTypeId()).Kind != DB.WallKind.Basic:
                continue
        except Exception:
            continue
        if w.IsHidden(view):
            continue
        tname = wall_type_name(w)
        rec = info.get(tname)
        if rec is None:
            excluded, rule = is_excluded(tname)
            rec = {"count": 0, "excluded": excluded, "rule": rule}
            info[tname] = rec
        rec["count"] += 1
    return info


def collect_eligible_walls(view, only_untagged, selected_types=None):
    walls, all_geo = [], []
    coll = DB.FilteredElementCollector(doc, view.Id)\
             .OfCategory(DB.BuiltInCategory.OST_Walls)\
             .WhereElementIsNotElementType()

    tagged_ids = set()
    if only_untagged:
        tcoll = DB.FilteredElementCollector(doc, view.Id)\
                  .OfCategory(DB.BuiltInCategory.OST_WallTags)\
                  .WhereElementIsNotElementType()
        for tag in tcoll:
            for eid in tag.GetTaggedLocalElementIds():
                tagged_ids.add(str(eid))

    for w in coll:
        if not isinstance(w, DB.Wall):
            continue
        geo = wall_geometry(w)
        if geo is None:
            continue
        # every wall (any kind/type) participates in partner detection
        all_geo.append(dict(geo, id=str(w.Id)))

        wtype = doc.GetElement(w.GetTypeId())
        if wtype.Kind != DB.WallKind.Basic:
            continue                       # drops curtain/storefront by kind
        if w.IsHidden(view):
            continue
        if geo["length"] < CFG["min_wall_length_ft"]:
            continue
        tname = wall_type_name(w)
        excluded, rule = is_excluded(tname)
        TYPE_LOG.setdefault(tname, {"seen": 0, "excluded": 0, "rule": rule})
        TYPE_LOG[tname]["seen"] += 1
        if selected_types is not None:
            # live picker is authoritative (portable across projects)
            if tname not in selected_types:
                TYPE_LOG[tname]["excluded"] += 1
                TYPE_LOG[tname]["rule"] = "not selected in picker"
                continue
        elif excluded:
            TYPE_LOG[tname]["excluded"] += 1
            continue
        if only_untagged and str(w.Id) in tagged_ids:
            continue
        walls.append((w, geo, tname))
    return walls, all_geo


# populated by collect_eligible_walls(); printed in the run summary so a
# type that looks wrong in the plan can be checked against what the script
# actually saw and decided, instead of guessing
TYPE_LOG = {}

# ---------------------------------------------------------- classification ---


def find_partner_side(geo, wall_id, all_geo):
    """Direction (unit vec) from this wall TOWARD a stacked partner, or None.
    Searches ALL walls in view - tagged or not (session lesson)."""
    for g in all_geo:
        if g["id"] == wall_id:
            continue
        if abs(geo["wdx"] * g["wdx"] + geo["wdy"] * g["wdy"]) \
                < CFG["parallel_dot_min"]:
            continue
        dx = g["mid"][0] - geo["mid"][0]
        dy = g["mid"][1] - geo["mid"][1]
        psep = dx * geo["perp"][0] + dy * geo["perp"][1]
        asep = dx * geo["wdx"] + dy * geo["wdy"]
        if 0.2 < abs(psep) < CFG["stack_separation_ft"] \
                and abs(asep) < (geo["length"] + g["length"]) / 2.0:
            s = 1 if psep > 0 else -1
            return (geo["perp"][0] * s, geo["perp"][1] * s)
    return None


def room_on_side(geo, direction, phase):
    """Phase-correct room probe (session lesson: default phase finds nothing)."""
    z = geo["z"] + 2.0
    for d in CFG["room_probe_dists_ft"]:
        pt = DB.XYZ(geo["mid"][0] + direction[0] * d,
                    geo["mid"][1] + direction[1] * d, z)
        try:
            if doc.GetRoomAtPoint(pt, phase):
                return True
        except Exception:
            pass
    return False


def tags_outward(tname):
    """Does this wall type tag OUTWARD (toward exterior) vs INTO the room?

    CRITICAL: the _Ext / _Int PREFIX DOES NOT DECIDE THIS. Both leaves of a
    perimeter assembly are named _Ext - the CMU/plaster outer leaf AND the
    furring inner leaf. A rule keyed on the prefix sends furring outward,
    which is wrong (regression seen 2026-07-20). Decide by SUBSTRATE instead:

      - Furring / interior-finish leaf: single interior-finish substrate
        letter A-L (S4A, S6A, S8A, S2A). Faces and finishes the room ->
        tags INTO the room. (These are exactly the Opp-variant walls.)
      - True exterior wall: CMU (M8, M12, no substrate letters) or a
        multi-substrate plaster assembly (S#XA, S#AX - exterior plaster X
        plus interior gyp) -> tags OUTWARD.
      - Single EXTERIOR-finish letter K-Z (S4X): exterior-facing -> OUTWARD.

    Returns True for outward, False for into-room.
    """
    u = tname.upper()
    m = re.search(r'\b[SM](\d+)([A-Za-z]*)', tname)
    if not m:
        return True                     # no core match -> treat as exterior
    letters = m.group(2)
    if len(letters) == 0:
        return True                     # CMU M8/M12 -> outward
    if len(letters) == 1:
        # single letter: interior finish A-L -> furring, into room;
        # exterior finish K-Z -> outward
        return not ("A" <= letters <= "L")
    # multi-letter (S#XA plaster assemblies etc.) -> outward
    return True


def choose_side(wall, geo, tname, all_geo, phase):
    """Returns unit direction for tag placement, or None if undeterminable.

    Side is decided by SUBSTRATE (see tags_outward), never by the _Ext/_Int
    prefix. Furring leaves tag into the room even though they're _Ext.

    Priority for BOTH directions:
      1. Room detection (phase-correct): pick the side that satisfies the
         outward/into-room intent based on which side actually has a room.
      2. wall.Orientation fallback: points to the exterior face by Revit
         convention. Reliable on corridor/unbounded walls with no room.
      3. Stacked-partner fallback for into-room walls with no room signal.
    """
    p1 = geo["perp"]
    p2 = (-p1[0], -p1[1])
    outward = tags_outward(tname)

    r1 = room_on_side(geo, p1, phase)
    r2 = room_on_side(geo, p2, phase)

    # 1. room on exactly one side -> resolve by intent
    if r1 != r2:
        room_dir = p1 if r1 else p2
        away_dir = p2 if r1 else p1
        return away_dir if outward else room_dir

    # 2. no clear room signal -> use wall.Orientation (exterior face)
    ori = None
    try:
        o = wall.Orientation
        n = math.hypot(o.X, o.Y)
        if n > 1e-6:
            ori = (o.X / n, o.Y / n)
    except Exception:
        ori = None

    if outward:
        return ori                      # exterior face (may be None)

    # into-room wall with no room signal:
    # 3. try stacked partner (tag away from the coincident partner leaf)
    partner = find_partner_side(geo, str(wall.Id), all_geo)
    if partner is not None:
        return (-partner[0], -partner[1])
    # else interior side = opposite of exterior face
    if ori is not None:
        return (-ori[0], -ori[1])
    return None


def nearest_family(direction):
    best, best_dot = None, -2.0
    for fam, vec in FAMILY_VECS.items():
        dot = direction[0] * vec[0] + direction[1] * vec[1]
        if dot > best_dot:
            best_dot, best = dot, fam
    return best


def compute_family_vecs(rotation_deg):
    """Family reference vectors in the BUILDING's own rotated frame, not
    world axes (session lesson: this project's grid runs ~51 deg off
    world north; a world-axis test misclassifies rotated wings)."""
    up_a = math.radians(rotation_deg + 90.0)
    right_a = math.radians(rotation_deg)
    up = (math.cos(up_a), math.sin(up_a))
    right = (math.cos(right_a), math.sin(right_a))
    return {
        "Up": up, "Down": (-up[0], -up[1]),
        "Right": right, "Left": (-right[0], -right[1]),
    }


def detect_building_rotation(view):
    """Dominant wall-angle histogram over Basic walls in the view, snapped
    to nearest degree mod 90 (session lesson: this project measured out
    to ~51 deg on the main wing, not 0)."""
    coll = DB.FilteredElementCollector(doc, view.Id)\
             .OfCategory(DB.BuiltInCategory.OST_Walls)\
             .WhereElementIsNotElementType()
    hist = {}
    for w in coll:
        if not isinstance(w, DB.Wall):
            continue
        try:
            if doc.GetElement(w.GetTypeId()).Kind != DB.WallKind.Basic:
                continue
        except Exception:
            continue
        geo = wall_geometry(w)
        if geo is None or geo["length"] < 5.0:
            continue
        ang = int(round(math.degrees(
            math.atan2(geo["wdy"], geo["wdx"])) % 90.0))
        hist[ang] = hist.get(ang, 0) + 1
    if not hist:
        return 0.0
    return float(max(hist, key=hist.get))


FAMILY_VECS = {
    "Up": (0.0, 1.0), "Down": (0.0, -1.0),
    "Right": (1.0, 0.0), "Left": (-1.0, 0.0),
}   # replaced at runtime in main() via compute_family_vecs(rotation)

# ------------------------------------------------------------- placement ---


OBSTACLE_CATEGORY_NAMES = [
    "OST_Walls",                 # other walls (not the tag's own)
    "OST_PlumbingFixtures",
    "OST_Casework",
    "OST_SpecialityEquipment",   # British spelling is the real API member
    "OST_SpecialtyEquipment",
    "OST_Furniture",
    "OST_FurnitureSystems",
    "OST_Doors",
    "OST_Windows",
    # annotation / text that must stay readable:
    "OST_RoomTags",
    "OST_GenericAnnotation",
    "OST_TextNotes",
    "OST_KeynoteTags",
    "OST_DoorTags",
    "OST_WindowTags",
]


def _resolve_obstacle_categories():
    cats = []
    for n in OBSTACLE_CATEGORY_NAMES:
        c = getattr(DB.BuiltInCategory, n, None)
        if c is not None:
            cats.append(c)
    return cats


_OBSTACLE_CACHE = {"view_id": None, "items": None}


def _all_obstacle_items(view):
    """Collect obstacle boxes ONCE per view as (box, wall_id_or_None) and
    cache them. Rebuilding per-wall was O(walls x elements) and slow on
    dense views; this makes both placement and QA O(elements + walls)."""
    if _OBSTACLE_CACHE["view_id"] == view.Id and _OBSTACLE_CACHE["items"] is not None:
        return _OBSTACLE_CACHE["items"]
    items = []
    for cat in _resolve_obstacle_categories():
        try:
            coll = DB.FilteredElementCollector(doc, view.Id)\
                     .OfCategory(cat).WhereElementIsNotElementType()
        except Exception:
            continue
        for el in coll:
            try:
                bb = el.get_BoundingBox(view)
                if bb is None:
                    continue
                wid = str(el.Id) if isinstance(el, DB.Wall) else None
                items.append(([bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y], wid))
            except Exception:
                continue
    _OBSTACLE_CACHE["view_id"] = view.Id
    _OBSTACLE_CACHE["items"] = items
    return items


def collect_static_obstacles(view, exclude_wall_ids):
    """Boxes a tag's TEXT must NOT overlap: other walls, plumbing, casework,
    specialty equipment, furniture, doors, windows, annotation/text. The
    tag's OWN wall (in exclude_wall_ids) is dropped - the leader attaches
    there. Dashed/grid/dimension lines are not collected, so tags may cross
    them freely (NAC graphics rule)."""
    out = []
    for box, wid in _all_obstacle_items(view):
        if wid is not None and wid in exclude_wall_ids:
            continue
        out.append(box)
    return out


def _candidate_positions(p):
    """Yield (offset, slide) candidates for a tag on wall p, CLOSEST to the
    wall first, expanding outward only when blocked, sweeping along the wall
    centered on the midpoint.

    Two tiers:
      NORMAL - offset up to base + max_perp_extra, slide within the wall
      length. These keep the leader short and are tried first.
      EXTENDED - if nothing clear in the normal tier, keep going: slide well
      past the wall ends and push the offset out to max_leader_ft. These are
      the congestion fallback (a longer leader to reach real open space) and
      are flagged so place_tags gives them a proper wall-anchored leader.

    Yields (offset, slide, extended_bool)."""
    base = CFG["base_offset_ft"]
    max_extra = CFG["max_perp_extra_ft"]
    slide_step = CFG["slide_step_ft"]
    max_leader = CFG.get("max_leader_ft", 12.0)

    # --- NORMAL tier ---
    normal_slide = max(0.0, p["length"] / 2.0 - 0.5)
    offset = base
    while offset <= base + max_extra + 1e-6:
        yield (offset, 0.0, False)
        s = slide_step
        while s <= normal_slide:
            yield (offset, s, False)
            yield (offset, -s, False)
            s += slide_step
        offset += 1.0

    # --- EXTENDED tier (congestion fallback: longer leader) ---
    ext_slide = p["length"] / 2.0 + 8.0
    offset = base
    while offset <= max_leader + 1e-6:
        s = normal_slide + slide_step
        while s <= ext_slide:
            yield (offset, s, True)
            yield (offset, -s, True)
            s += slide_step
        offset += 1.0


def _tag_text_box_at(p, offset, slide, tw, th):
    """Predicted shrunken text box (center +/- half-size*shrink) for wall p
    at a given offset/slide, without creating the tag."""
    cx = p["mid"][0] + p["dir"][0] * offset + p["wdx"] * slide
    cy = p["mid"][1] + p["dir"][1] * offset + p["wdy"] * slide
    shrink = CFG["text_box_shrink"]
    hw = tw / 2.0 * shrink
    hh = th / 2.0 * shrink
    return (cx, cy, [cx - hw, cy - hh, cx + hw, cy + hh])


def place_tags(view, plans, tag_ids):
    """Place each tag at the base offset on its pre-decided side (furring
    into room / exterior outward), with a short attached leader. Collision
    resolution is handled afterward by solve_collisions, which reads the
    REAL tag geometry and slides tags along their walls to clear other tags
    and architecture. (Earlier versions tried to search for clear space
    here using a PREDICTED tag footprint; that footprint didn't match the
    real tag, so the avoidance chased phantom boxes and tags still
    overlapped. Doing it on real geometry in the post-pass is reliable.)"""
    created = []
    with revit.Transaction("NAC Tag Walls - place"):
        for p in plans:
            fam = p["fam"]
            if fam not in tag_ids and fam.endswith(" Opp"):
                fam = fam[:-4]
            if fam not in tag_ids:
                continue
            hx = p["mid"][0] + p["dir"][0] * CFG["base_offset_ft"]
            hy = p["mid"][1] + p["dir"][1] * CFG["base_offset_ft"]
            head = DB.XYZ(hx, hy, p["z"])
            try:
                tag = DB.IndependentTag.Create(
                    doc, tag_ids[fam], view.Id, DB.Reference(p["wall"]),
                    True, DB.TagOrientation.Horizontal, head)
                tag.TagHeadPosition = head
                try:
                    tag.LeaderEndCondition = DB.LeaderEndCondition.Attached
                except Exception:
                    pass
                created.append((tag, p))
            except Exception as ex:
                logger.warning("tag failed on %s: %s", p["wall"].Id, ex)
    return created


def _apply_leader(tag, p, head, offset):
    """Give the tag a correct, visibly-connected leader.

    Close tags (offset <= leader_attach_threshold_ft) use the simple
    ATTACHED leader - Revit draws it to the wall, short and clean.

    Far tags (congestion fallback) use a FREE-end leader whose end point is
    anchored on the tagged wall's line (nearest point to the head) with an
    elbow just off the tag head, so the leader reads as a real line from the
    tag back to its wall instead of the tag appearing to float."""
    thresh = CFG.get("leader_attach_threshold_ft", 6.0)
    try:
        if offset <= thresh:
            tag.LeaderEndCondition = DB.LeaderEndCondition.Attached
            return
        refs = list(tag.GetTaggedReferences())
        if not refs:
            tag.LeaderEndCondition = DB.LeaderEndCondition.Attached
            return
        tag.LeaderEndCondition = DB.LeaderEndCondition.Free
        # nearest point on the wall line to the head = leader end (on wall)
        ax, ay = p["p0"]
        bx, by = p["p1"]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 < 1e-9:
            ex, ey = ax, ay
        else:
            t = max(0.0, min(1.0, ((head.X-ax)*dx + (head.Y-ay)*dy) / L2))
            ex, ey = ax + t*dx, ay + t*dy
        tag.SetLeaderEnd(refs[0], DB.XYZ(ex, ey, head.Z))
        # elbow near the tag (30% of the way from head to wall)
        elx = head.X + (ex - head.X) * 0.3
        ely = head.Y + (ey - head.Y) * 0.3
        tag.SetLeaderElbow(refs[0], DB.XYZ(elx, ely, head.Z))
    except Exception:
        try:
            tag.LeaderEndCondition = DB.LeaderEndCondition.Attached
        except Exception:
            pass


# accumulator for placed-tag boxes across the place_tags loop
placed_boxes_ref = {"boxes": []}

# ------------------------------------------------------ collision solving ---


def text_box(tag, view):
    """Shrunken bbox approximating the readable text zone (session lesson:
    full-bbox tests flag 3x too many false collisions)."""
    bb = tag.get_BoundingBox(view)
    if bb is None:
        return None
    cx, cy = (bb.Min.X + bb.Max.X) / 2.0, (bb.Min.Y + bb.Max.Y) / 2.0
    hw = (bb.Max.X - bb.Min.X) / 2.0 * CFG["text_box_shrink"]
    hh = (bb.Max.Y - bb.Min.Y) / 2.0 * CFG["text_box_shrink"]
    return [cx - hw, cy - hh, cx + hw, cy + hh]


def boxes_overlap(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def shift_box(box, dx, dy):
    return [box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy]


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def solve_collisions(view, created):
    """Real-geometry collision post-pass.

    Reads each placed tag's ACTUAL bounding box back from Revit (not a
    predicted footprint - that mismatch was the cause of earlier failures),
    then, for any tag whose real box overlaps another tag OR a must-avoid
    architectural element, slides it ALONG ITS WALL (both directions,
    growing) to the nearest clear spot. Tags stay attached to their wall at
    the base offset with a short leader - no perpendicular flinging, no
    long free leaders (those cascaded/floated in testing). The genuinely
    unresolvable few (dense cores where no clear spot exists within the
    wall length) are left for manual cleanup and reported as leftover so
    Check mode can flag them.
    """
    gap = CFG.get("tag_min_gap_ft", 1.0)
    shrink = CFG["text_box_shrink"]

    # static obstacles: non-wall bboxes + wall segments (own wall excluded
    # per tag). Doors omitted - their swing-arc bboxes over-flag.
    nonwall = []
    for name in ["OST_PlumbingFixtures", "OST_Casework", "OST_Furniture",
                 "OST_TextNotes", "OST_RoomTags", "OST_SpecialityEquipment",
                 "OST_Windows"]:
        cat = getattr(DB.BuiltInCategory, name, None)
        if cat is None:
            continue
        try:
            coll = DB.FilteredElementCollector(doc, view.Id)\
                     .OfCategory(cat).WhereElementIsNotElementType()
        except Exception:
            continue
        for el in coll:
            try:
                bb = el.get_BoundingBox(view)
                if bb:
                    nonwall.append([bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y])
            except Exception:
                pass
    # DOOR LEAF avoidance: a door's raw bbox includes its swing arc (7-12 ft
    # of empty space) which over-flags. Approximate the door LEAF/opening as a
    # small box centered on the door's location point (the opening in the
    # wall), sized ~door width, so we avoid the actual opening/panel but not
    # the swing sweep.
    for d in DB.FilteredElementCollector(doc, view.Id)\
               .OfCategory(DB.BuiltInCategory.OST_Doors)\
               .WhereElementIsNotElementType():
        try:
            loc = d.Location
            if not isinstance(loc, DB.LocationPoint):
                continue
            pt = loc.Point
            w = 3.0
            try:
                wp = d.Symbol.get_Parameter(DB.BuiltInParameter.DOOR_WIDTH)
                if wp:
                    w = max(1.5, wp.AsDouble())
            except Exception:
                pass
            half = w / 2.0 + 0.5
            nonwall.append([pt.X - half, pt.Y - half,
                            pt.X + half, pt.Y + half])
        except Exception:
            pass
    wall_segs = []
    for w in DB.FilteredElementCollector(doc, view.Id)\
               .OfCategory(DB.BuiltInCategory.OST_Walls)\
               .WhereElementIsNotElementType():
        if not isinstance(w, DB.Wall) or not isinstance(w.Location,
                                                        DB.LocationCurve):
            continue
        c = w.Location.Curve
        p0, p1 = c.GetEndPoint(0), c.GetEndPoint(1)
        wall_segs.append((str(w.Id), (p0.X, p0.Y), (p1.X, p1.Y)))

    # tag items with real half-size + current slide/offset in wall frame
    items = []
    for tag, p in created:
        bb = tag.get_BoundingBox(view)
        if bb is None:
            continue
        hw = (bb.Max.X - bb.Min.X) / 2.0
        hh = (bb.Max.Y - bb.Min.Y) / 2.0
        head = tag.TagHeadPosition
        mid = p["mid"]
        off = (head.X - mid[0]) * p["dir"][0] + (head.Y - mid[1]) * p["dir"][1]
        slide = (head.X - mid[0]) * p["wdx"] + (head.Y - mid[1]) * p["wdy"]
        items.append({"tag": tag, "p": p, "hw": hw, "hh": hh,
                      "off": off, "slide": slide, "z": head.Z,
                      "own": str(p["wall"].Id)})

    def box_of(it, slide):
        p = it["p"]
        cx = p["mid"][0] + p["dir"][0] * it["off"] + p["wdx"] * slide
        cy = p["mid"][1] + p["dir"][1] * it["off"] + p["wdy"] * slide
        return ([cx - it["hw"] * shrink, cy - it["hh"] * shrink,
                 cx + it["hw"] * shrink, cy + it["hh"] * shrink], cx, cy)

    def clear(idx, slide):
        it = items[idx]
        box, cx, cy = box_of(it, slide)
        reach = max(it["hw"], it["hh"]) * shrink
        for ob in nonwall:
            if boxes_overlap(box, ob):
                return False
        for wid, a, b in wall_segs:
            if wid == it["own"]:
                continue
            if _seg_dist(cx, cy, a[0], a[1], b[0], b[1]) < reach:
                return False
        for k, other in enumerate(items):
            if k == idx:
                continue
            ob, _, _ = box_of(other, other["slide"])
            ob = [ob[0] - gap, ob[1] - gap, ob[2] + gap, ob[3] + gap]
            if boxes_overlap(box, ob):
                return False
        return True

    def overlap_score(idx, slide):
        """Weighted overlap count at this position. 0 = fully clear. Used to
        pick the LEAST-overlap spot when no fully-clear spot exists - a tag
        with minimal unavoidable overlap beats flinging it far away. Other
        tags and text weigh most; walls/casework least."""
        it = items[idx]
        box, cx, cy = box_of(it, slide)
        reach = max(it["hw"], it["hh"]) * shrink
        score = 0.0
        for ob in nonwall:
            if boxes_overlap(box, ob):
                score += 5.0            # text/fixtures/casework/doors
        for wid, a, b in wall_segs:
            if wid == it["own"]:
                continue
            if _seg_dist(cx, cy, a[0], a[1], b[0], b[1]) < reach:
                score += 3.0            # other wall lines (cheapest)
        for k, other in enumerate(items):
            if k == idx:
                continue
            ob, _, _ = box_of(other, other["slide"])
            ob = [ob[0] - gap, ob[1] - gap, ob[2] + gap, ob[3] + gap]
            if boxes_overlap(box, ob):
                score += 10.0           # tag-on-tag weighs most
        return score

    step = CFG["slide_step_ft"]

    # ---- orientation-swap lever (learned from hand-tagging) ----
    # A Left/Right tag lays its flag ALONG the wall (collides with the wall
    # line and neighbors); an Up/Down tag lifts the text perpendicular, AWAY
    # from the wall into open space. When sliding can't clear a collision,
    # swapping to the perpendicular family often fits where the along-wall
    # one doesn't. Build a name->TypeId map of the loaded NAC24 tag types.
    tag_types = {}
    for sym in DB.FilteredElementCollector(doc)\
                 .OfCategory(DB.BuiltInCategory.OST_WallTags)\
                 .WhereElementIsElementType():
        fn = sym.FamilyName
        if "WallType" in fn and "NAC24" in fn:
            nm = sym.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)\
                    .AsString()
            if nm:
                tag_types[nm] = sym.Id

    # perpendicular alternatives for each base orientation
    PERP = {"Left": ["Up", "Down"], "Right": ["Up", "Down"],
            "Up": ["Left", "Right"], "Down": ["Left", "Right"]}

    def cur_orient(tag):
        nm = doc.GetElement(tag.GetTypeId())\
                .get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        opp = nm.endswith(" Opp")
        base = nm[:-4] if opp else nm
        return base, opp

    def try_orientation_swap(idx):
        """Retype the tag to a perpendicular orientation if that clears the
        collision (keeping Opp status). Returns True if a swap resolved it."""
        it = items[idx]
        tag = it["tag"]
        base, opp = cur_orient(tag)
        alts = PERP.get(base, [])
        orig_id = tag.GetTypeId()
        for alt in alts:
            nm = alt + (" Opp" if opp else "")
            tid = tag_types.get(nm)
            if tid is None:
                continue
            try:
                tag.ChangeTypeId(tid)
            except Exception:
                continue
            # refresh this item's real footprint after the swap
            bb = tag.get_BoundingBox(view)
            if bb is not None:
                it["hw"] = (bb.Max.X - bb.Min.X) / 2.0
                it["hh"] = (bb.Max.Y - bb.Min.Y) / 2.0
            if clear(idx, it["slide"]):
                return True
            # try sliding in the new orientation too
            ms = max(0.0, it["p"]["length"] / 2.0 - 0.5)
            s = step
            while s <= ms:
                for cand in (it["slide"] + s, it["slide"] - s):
                    if clear(idx, cand):
                        it["slide"] = cand
                        return True
                s += step
            # this alt didn't help - revert type before trying the next
            try:
                tag.ChangeTypeId(orig_id)
            except Exception:
                pass
            bb = tag.get_BoundingBox(view)
            if bb is not None:
                it["hw"] = (bb.Max.X - bb.Min.X) / 2.0
                it["hh"] = (bb.Max.Y - bb.Min.Y) / 2.0
        return False

    with revit.Transaction("NAC Tag Walls - real-geometry slide resolve"):
        for idx, it in enumerate(items):
            if clear(idx, it["slide"]):
                continue
            # Slide must keep the tag head OVER its own wall so the attached
            # leader always has wall beneath it. Sliding past the wall ends
            # detaches the tail (badly visible on short walls). Cap the slide
            # at half the wall length minus a small margin - never beyond the
            # ends. Very short walls therefore barely slide, which is correct:
            # the tag stays put and we rely on orientation-swap / least-overlap
            # instead of flinging it off the wall.
            half = it["p"]["length"] / 2.0
            max_slide = max(0.0, half - 0.5)
            found = False
            s = step
            while s <= max_slide and not found:
                for cand in (it["slide"] + s, it["slide"] - s):
                    if clear(idx, cand):
                        it["slide"] = cand
                        found = True
                        break
                s += step
            # sliding failed -> try swapping to a perpendicular orientation
            if not found:
                found = try_orientation_swap(idx)
            # STILL not clear -> settle at the LEAST-overlap slide position
            # (a small unavoidable overlap in the best spot beats flinging the
            # tag far away). Scan the slide range, keep minimum overlap_score.
            if not found:
                best_slide = it["slide"]
                best_score = overlap_score(idx, it["slide"])
                s = step
                while s <= max_slide:
                    for cand in (it["slide"] + s, it["slide"] - s):
                        sc = overlap_score(idx, cand)
                        if sc < best_score:
                            best_score = sc
                            best_slide = cand
                    s += step
                it["slide"] = best_slide
            # apply (moves to found spot, or stays if none)
            p = it["p"]
            cx = p["mid"][0] + p["dir"][0] * it["off"] + p["wdx"] * it["slide"]
            cy = p["mid"][1] + p["dir"][1] * it["off"] + p["wdy"] * it["slide"]
            try:
                it["tag"].TagHeadPosition = DB.XYZ(cx, cy, it["z"])
            except Exception:
                pass

    # report residual tag-on-tag overlaps (real boxes) for the summary
    leftover = 0
    boxes = [box_of(it, it["slide"])[0] for it in items]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxes_overlap(boxes[i], boxes[j]):
                leftover += 1
    return 0, leftover


# -------------------------------------------------------------- QA mode ---


def qa_highlight(view):
    """Flag tags that violate the SAME rules the placement search enforces,
    so Check mode and placement agree. Red overrides only - changes nothing.

    A tag is flagged if its (shrunken) text box:
      1. overlaps ANOTHER tag's text box  ................ "tag-on-tag overlap"
      2. sits within tag_min_gap_ft of another tag  ..... "tag-too-close"
      3. overlaps a must-avoid architectural element:
         other walls / plumbing / casework / equipment /
         furniture / doors / windows / text-annotation  .. "on-architecture"

    Dashed / grid / dimension lines are NOT flagged (allowed to cross) -
    they are simply not in the obstacle set. Wall overlap uses the same
    per-element bounding boxes collected for placement, with each tag's OWN
    wall excluded (the leader attaches there), so we do not reintroduce the
    huge-diagonal-bbox false positives."""
    _OBSTACLE_CACHE["view_id"] = None    # force fresh obstacle collection
    tags = list(DB.FilteredElementCollector(doc, view.Id)
                .OfCategory(DB.BuiltInCategory.OST_WallTags)
                .WhereElementIsNotElementType())
    infos = []
    for tag in tags:
        ids = list(tag.GetTaggedLocalElementIds())
        if not ids:
            continue
        wall = doc.GetElement(ids[0])
        if not isinstance(wall, DB.Wall):
            continue
        tb = text_box(tag, view)
        if tb is None:
            continue
        infos.append(dict(tag=tag, tb=tb, own_wall_id=str(wall.Id)))

    problems, why = set(), {}
    gap = CFG.get("tag_min_gap_ft", 1.0)

    # rules 1 & 2 - tag vs tag (overlap, and within-gap)
    for i in range(len(infos)):
        bi = infos[i]["tb"]
        bi_buf = [bi[0] - gap, bi[1] - gap, bi[2] + gap, bi[3] + gap]
        for j in range(i + 1, len(infos)):
            bj = infos[j]["tb"]
            if boxes_overlap(bi, bj):
                for k in (i, j):
                    problems.add(k)
                    why.setdefault(k, set()).add("tag-on-tag overlap")
            elif boxes_overlap(bi_buf, bj):
                for k in (i, j):
                    problems.add(k)
                    why.setdefault(k, set()).add("tag-too-close")

    # rule 3 - tag on must-avoid architecture. Collect obstacles per distinct
    # own-wall-id (so the tag's own wall is excluded), caching by wall id.
    for k, info in enumerate(infos):
        obstacles = collect_static_obstacles(view, set([info["own_wall_id"]]))
        for ob in obstacles:
            if boxes_overlap(info["tb"], ob):
                problems.add(k)
                why.setdefault(k, set()).add("on-architecture")
                break

    red = DB.Color(255, 0, 0)
    with revit.Transaction("NAC Tag Walls - QA highlight"):
        for k in problems:
            ogs = DB.OverrideGraphicSettings()
            ogs.SetProjectionLineColor(red)
            ogs.SetProjectionLineWeight(7)
            view.SetElementOverrides(infos[k]["tag"].Id, ogs)
    return len(infos), problems, why


def clear_highlights(view):
    tags = DB.FilteredElementCollector(doc, view.Id)\
             .OfCategory(DB.BuiltInCategory.OST_WallTags)\
             .WhereElementIsNotElementType()
    clean = DB.OverrideGraphicSettings()
    n = 0
    with revit.Transaction("NAC Tag Walls - clear highlights"):
        for tag in tags:
            try:
                view.SetElementOverrides(tag.Id, clean)
                n += 1
            except Exception:
                pass
    return n

# ------------------------------------------------------------------ main ---


def group_continuous_runs(walls):
    """Collapse eligible walls into CONTINUOUS RUNS so each run gets ONE tag.

    A run = walls of the SAME type, connected end-to-end, where each corner's
    change in direction is LESS THAN 90 degrees (gentle bends/jogs stay one
    run). A run BREAKS - starting a new run - when:
      - the corner turn is 90 degrees or sharper (a real corner), OR
      - a different wall type meets, OR
      - a window interrupts the wall (window-hosting walls end a run).
    Breaks are where the user tags BOTH sides to show the change; since each
    resulting run is tagged once, tagging both runs meeting at a break
    naturally puts a tag on each side of it.

    Input: list of (wall, geo, tname). Output: list of runs, each a list of
    (wall, geo, tname). Endpoint connectivity uses a small tolerance."""
    TOL = 0.5                      # ft, endpoints considered coincident
    COS90 = 0.0                    # cos(90 deg); turn <90 => dot > 0

    # index walls by rounded endpoints for connectivity lookup
    def key(pt):
        return (round(pt[0] / TOL), round(pt[1] / TOL))

    # window-hosting walls: a wall that hosts a window ends a run at that end.
    # We approximate by flagging walls that have hosted windows.
    windowed = set()
    try:
        for win in DB.FilteredElementCollector(doc)\
                     .OfCategory(DB.BuiltInCategory.OST_Windows)\
                     .WhereElementIsNotElementType():
            try:
                h = win.Host
                if h is not None:
                    windowed.add(str(h.Id))
            except Exception:
                pass
    except Exception:
        pass

    items = []
    for wall, geo, tname in walls:
        items.append({"wall": wall, "geo": geo, "tname": tname,
                      "p0": geo["p0"], "p1": geo["p1"],
                      "dir": (geo["wdx"], geo["wdy"]),
                      "id": str(wall.Id), "used": False,
                      "win": str(wall.Id) in windowed})

    # endpoint -> list of item indices
    from collections import defaultdict
    epmap = defaultdict(list)
    for i, it in enumerate(items):
        epmap[key(it["p0"])].append(i)
        epmap[key(it["p1"])].append(i)

    def connects(a_end, b):
        """Does point a_end coincide with an endpoint of item b? Return the
        other end of b and b's direction oriented away from the joint."""
        for bp, other, d in ((b["p0"], b["p1"], b["dir"]),
                             (b["p1"], b["p0"], (-b["dir"][0], -b["dir"][1]))):
            if abs(a_end[0]-bp[0]) < TOL and abs(a_end[1]-bp[1]) < TOL:
                return other, d
        return None, None

    runs = []
    for start in range(len(items)):
        if items[start]["used"]:
            continue
        it = items[start]
        it["used"] = True
        run = [(it["wall"], it["geo"], it["tname"])]

        # extend from BOTH ends of the starting wall
        for from_pt, cur_dir in ((it["p1"], it["dir"]),
                                 (it["p0"], (-it["dir"][0], -it["dir"][1]))):
            if it["win"]:
                continue                       # window ends the run here
            end_pt = from_pt
            dir_in = cur_dir
            while True:
                # among ALL same-type unused walls meeting at this junction,
                # pick the MOST COLLINEAR continuation (highest dot), not the
                # first. At a junction where a straight wall and a corner both
                # meet, this keeps the run going straight instead of turning.
                best = None
                best_dot = COS90            # must beat 90deg (dot>0) to continue
                for j in epmap.get(key(end_pt), []):
                    cand = items[j]
                    if cand["used"] or cand["tname"] != it["tname"]:
                        continue
                    other, d_away = connects(end_pt, cand)
                    if other is None:
                        continue
                    dot = dir_in[0]*d_away[0] + dir_in[1]*d_away[1]
                    if dot > best_dot:
                        best_dot = dot
                        best = (j, other, d_away)
                if best is None:
                    break
                j, other, d_away = best
                cand = items[j]
                cand["used"] = True
                run.append((cand["wall"], cand["geo"], cand["tname"]))
                if cand["win"]:
                    break                       # window ends the run
                end_pt = other
                dir_in = d_away
        runs.append(run)
    return runs


def merge_runs_by_proximity(runs, radius_ft=6.0):
    """After continuous-run grouping, further collapse runs of the SAME wall
    type whose representative (longest) walls are within radius_ft of each
    other into a single run. In tight clusters (e.g. three short same-type
    walls around a small room), this yields ONE tag for the cluster instead
    of one per wall. Nearest distance is measured wall-to-wall (segment to
    segment). Keeps all walls together so the longest is chosen as carrier."""
    import math

    def rep(run):
        # representative wall of a run = its longest segment
        w, g, t = max(run, key=lambda r: r[1]["length"])
        return g, t

    def seg_seg_dist(g1, g2):
        # min distance between two wall segments (endpoints-to-segment approx)
        def d_pt_seg(px, py, ax, ay, bx, by):
            dx, dy = bx-ax, by-ay
            L2 = dx*dx + dy*dy
            if L2 < 1e-9:
                return math.hypot(px-ax, py-ay)
            t = max(0.0, min(1.0, ((px-ax)*dx+(py-ay)*dy)/L2))
            return math.hypot(px-(ax+t*dx), py-(ay+t*dy))
        a0, a1 = g1["p0"], g1["p1"]
        b0, b1 = g2["p0"], g2["p1"]
        return min(
            d_pt_seg(a0[0], a0[1], b0[0], b0[1], b1[0], b1[1]),
            d_pt_seg(a1[0], a1[1], b0[0], b0[1], b1[0], b1[1]),
            d_pt_seg(b0[0], b0[1], a0[0], a0[1], a1[0], a1[1]),
            d_pt_seg(b1[0], b1[1], a0[0], a0[1], a1[0], a1[1]))

    reps = [rep(r) for r in runs]
    n = len(runs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    for i in range(n):
        gi, ti = reps[i]
        for j in range(i+1, n):
            gj, tj = reps[j]
            if ti != tj:
                continue
            if seg_seg_dist(gi, gj) <= radius_ft:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).extend(runs[i])
    return list(groups.values())


def main():
    view = doc.ActiveView
    if not isinstance(view, DB.ViewPlan):
        forms.alert("Open a floor plan view first.", exitscript=True)

    mode = forms.CommandSwitchWindow.show(
        ["Tag Walls", "Check Tags (highlight only)", "Clear Highlights"],
        message="NAC Tag Walls - choose mode:")
    if not mode:
        script.exit()

    if mode == "Clear Highlights":
        n = clear_highlights(view)
        forms.alert("Cleared overrides on {} tags.".format(n))
        return

    if mode == "Check Tags (highlight only)":
        total, problems, why = qa_highlight(view)
        counts = {}
        for reasons in why.values():
            for r in reasons:
                counts[r] = counts.get(r, 0) + 1
        forms.alert(
            "Checked {} tags.\nFlagged red: {}\n\n{}".format(
                total, len(problems),
                "\n".join("{}: {}".format(k, v)
                          for k, v in counts.items()) or "All clean."))
        return

    # ---- Tag Walls ----
    only_untagged = forms.alert(
        "Tag only walls that are not yet tagged?",
        yes=True, no=True)

    # ---- live wall-type picker (firm-wide: adapts to any project) ----
    selected_types = None
    if CFG.get("prompt_wall_types", True):
        wt_info = list_view_wall_types(view)
        if not wt_info:
            forms.alert("No Basic wall types found in this view.",
                        exitscript=True)

        class _WallTypeChoice(forms.TemplateListItem):
            @property
            def name(self):
                r = self.item
                note = ("   -- normally excluded ({})".format(r["rule"])
                        if r["excluded"] else "")
                return "{}   (x{}){}".format(r["tname"], r["count"], note)

        items = []
        for tname in sorted(wt_info):
            rec = dict(wt_info[tname], tname=tname)
            choice = _WallTypeChoice(rec)
            choice.checked = not rec["excluded"]   # default = current behavior
            items.append(choice)

        picked = forms.SelectFromList.show(
            items, title="NAC Tag Walls - select wall types to tag",
            button_name="Tag selected types", multiselect=True)
        if not picked:
            script.exit()
        selected_types = set(r["tname"] for r in picked)

    phase = get_phase(view)
    if phase is None:
        forms.alert("No usable phase found: configured '{}' is missing and "
                    "the active view has no phase set.\nCheck config.json or "
                    "the view's Phase property."
                    .format(CFG["phase_name"]), exitscript=True)

    global FAMILY_VECS
    rotation = CFG.get("building_rotation_deg")
    if rotation is None:
        rotation = detect_building_rotation(view)
    FAMILY_VECS = compute_family_vecs(rotation)
    output.print_md("- Building grid rotation used: **{:.1f} deg**"
                    .format(rotation))

    tag_ids = get_tag_type_ids()
    missing = [k for k in FAMILY_VECS if k not in tag_ids]
    if missing:
        forms.alert("Tag families not loaded: {}\nCheck config.json."
                    .format(", ".join(missing)), exitscript=True)
    missing_opp = [k + " Opp" for k in FAMILY_VECS
                   if (k + " Opp") not in tag_ids]
    if missing_opp:
        logger.warning("Opp variants not loaded (%s) - interior-finish "
                       "furring walls will fall back to standard variants",
                       ", ".join(missing_opp))

    walls, all_geo = collect_eligible_walls(view, only_untagged,
                                            selected_types)
    if not walls:
        forms.alert("No eligible walls found for the selected types.",
                    exitscript=True)

    runs = group_continuous_runs(walls)
    runs = merge_runs_by_proximity(runs, CFG.get("proximity_merge_ft", 6.0))

    # obstacle boxes (once) for clear-space carrier selection: text, fixtures,
    # casework, furniture, windows, doors - the things a tag should sit clear
    # of. Used to pick which wall in a proximity cluster reads cleanest.
    _clear_obs = []
    for _nm in ["OST_TextNotes", "OST_RoomTags", "OST_PlumbingFixtures",
                "OST_Casework", "OST_Furniture", "OST_Windows"]:
        _c = getattr(DB.BuiltInCategory, _nm, None)
        if _c is None:
            continue
        try:
            for _el in DB.FilteredElementCollector(doc, view.Id)\
                         .OfCategory(_c).WhereElementIsNotElementType():
                _bb = _el.get_BoundingBox(view)
                if _bb:
                    _clear_obs.append([_bb.Min.X, _bb.Min.Y,
                                       _bb.Max.X, _bb.Max.Y])
        except Exception:
            pass

    def _carrier_overlap(geo, direction):
        """Rough count of obstacles a tag at this wall's base position would
        hit - lower = clearer surroundings. direction may be None."""
        if direction is None:
            return 9999
        bo = CFG["base_offset_ft"]
        cx = geo["mid"][0] + direction[0] * bo
        cy = geo["mid"][1] + direction[1] * bo
        half = 3.2                      # ~half a tag footprint
        box = [cx - half, cy - half, cx + half, cy + half]
        n = 0
        for ob in _clear_obs:
            if not (box[2] < ob[0] or ob[2] < box[0]
                    or box[3] < ob[1] or ob[3] < box[1]):
                n += 1
        return n

    plans, skipped = [], 0
    with forms.ProgressBar(title="Classifying runs") as pb:
        for n, run in enumerate(runs):
            pb.update_progress(n, len(runs))
            # CARRIER = the wall in the cluster whose tag reads CLEAREST
            # (least surrounding overlap), tie-broken by longest. This lets a
            # jamb pile-up put its one tag on a nearby short same-type wall
            # sitting in open space, instead of forcing it into the crowded
            # junction. (Earlier rule was strictly longest; clear-space wins.)
            scored = []
            for w, g, t in run:
                d = choose_side(w, g, t, all_geo, phase)
                scored.append((_carrier_overlap(g, d), -g["length"], w, g, t, d))
            scored.sort(key=lambda x: (x[0], x[1]))
            _, _, wall, geo, tname, direction = scored[0]
            if direction is None:
                skipped += 1
                continue
            fam = nearest_family(direction)
            if needs_opp_variant(tname):
                fam = fam + " Opp"
            run_pts = []
            for _, g, _ in run:
                run_pts.append(g["p0"])
                run_pts.append(g["p1"])
            plans.append(dict(
                wall=wall, tname=tname, dir=direction,
                fam=fam,
                mid=geo["mid"], wdx=geo["wdx"], wdy=geo["wdy"],
                length=geo["length"], z=geo["z"],
                p0=geo["p0"], p1=geo["p1"],
                run_pts=run_pts))

    placed_boxes_ref["boxes"] = []   # fresh per run
    _OBSTACLE_CACHE["view_id"] = None  # force obstacle recollect
    created = place_tags(view, plans, tag_ids)
    merged, leftover = solve_collisions(view, created)

    output.print_md("### NAC Tag Walls - run summary")
    output.print_md("- Eligible walls: **{}**".format(len(walls)))
    output.print_md("- Continuous runs (one tag each): **{}**"
                    .format(len(runs)))
    output.print_md("- Tags placed: **{}**".format(len(created)))
    output.print_md("- Side undeterminable (skipped): **{}**".format(skipped))
    output.print_md("- Same-type tags merged in congestion: **{}**"
                    .format(merged))
    output.print_md("- Unresolved text collisions: **{}** "
                    "(run *Check Tags* to highlight)".format(leftover))

    output.print_md("\n### Wall types seen this run "
                    "(check this if a type looks wrong in the plan)")
    output.print_md("| Type | Seen | Excluded | Rule |\n|---|---|---|---|")
    for tname in sorted(TYPE_LOG):
        row = TYPE_LOG[tname]
        output.print_md("| {} | {} | {} | {} |".format(
            tname, row["seen"], row["excluded"], row["rule"] or "-"))


main()
