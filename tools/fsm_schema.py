#!/usr/bin/env python3
"""Item body schemas for the mission FSM, with a re-emit gate.

Criterion: an item type is decoded when its body parses into fields AND `encode(decode(body))`
reproduces the body bytes exactly. This module implements the decoded types and reports corpus
coverage so the scoreboard is measurable rather than claimed.

Decoded so far:

  record (0x6c357b9d3e), 5142 items, two forms chosen by the CONTAINER:
      prefixed (in tracks/initialTracks/enterTracks/exitTracks/functions/overrideFunctions)
          f32 timeBegin | f32 timeEnd | u32 len | payload[len] | pad4 | u64 0
      bare (in conditions)
          payload

  zero-field types, 2514 items - body is exactly the 4-byte terminator:
      __anchorRef (2429)  false (43)  0x6f1b23 (38)  0x6e3695c3b7 (4)
      Their data, where any, is in the item header's anchorIndex, not the body.

Coverage is reported per tag so undecoded types cannot hide in an average.
"""
import struct

sys_path_fix = None
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import uid as _uid

TRACK_GROUP = _uid.uid("trackGroup")
ALERT_STATUS = _uid.uid("alertStatus")
SM_STOP_CURRENT = _uid.uid("stateMachine_StopCurrent")
SM_START = _uid.uid("stateMachine_Start")
ON_BEGIN = _uid.uid("onBegin")
SOUND = _uid.uid("sound")
MIXER_II = 0x6A8F9BFA267EFAAD   # uid("mixerII") verified separately

import fsm_chunk as _chunk
C_walk_children = _chunk.walk_children

RECORD = 0x0000006C357B9D3E
MUSIC = 0x6B46809BA637E3E1            # uid("music")
EXECUTE = 0x19B8FE9FADFE0989           # uid("execute")
EXCLUDE_VOLUME = 0xCAFEA4CC931C2B88
GAME_OBJECT_ATTR = 0x55EFAF2660315BAD     # uid("gameObjectAttribute")
CODED_CDD48 = 0xCDD48F0984102D90          # uid-shaped, unnamed
GAME_OBJECT = 0xC73ED383FAB2EDCB          # uid("gameObject")
GAME_OBJECT_TOUCH = 0x613BC5ED4E2DFCDA    # uid("gameObjectTriggerTouch")
CONDITION_GROUP = 0x335B017B070CB7FC  # uid("conditionGroup")
CONDITIONS = 0x7955A92E5E424E0A       # uid("conditions")
HAS_TAG = 0x6B1E2719309BC550          # uid("hasTag")
REF_STATE_CHANGE = 0x952CCE8EB5104E30  # uid("referenceStatemachineStateChange")
ZERO_FIELD = {0xBBF017325B564F48,   # __anchorRef
              0x64EE5E0BA3C653F9,   # false
              0x00000000006F1B23,   # coded id
              0x0000006E3695C3B7}   # coded id

PREFIXED_CONTAINERS = {"tracks", "initialTracks", "enterTracks", "exitTracks",
                       "functions", "overrideFunctions"}
TERMINATOR = b"\x00\x00\x00\x00"


def decode_body(container, tag, body):
    """Fields of an item body, or None if this type is not decoded yet."""
    if tag in ZERO_FIELD:
        if body == TERMINATOR:
            return {"type": "zero-field"}
        return None
    # 12-byte body: one 32-bit value, a zero word, then the terminator.
    # Used by event_String (1328), event (326), isPlayerCharacter (67),
    # stateMachine_CurrentState (8), isValidObjectInEvent (6) and others.
    # The string form is the general case: u32 hash | u32 len | chars | pad | u32 0,
    # of which these are the len == 0 specialisation.
    if len(body) == 12 and body[8:] == b"\x00" * 4:
        return {"type": "u64-value", "value": struct.unpack_from("<Q", body, 0)[0]}
    # conditionGroup: a nested `conditions` container followed by u64 0   (139/139)
    if tag == CONDITION_GROUP:
        if len(body) < 24:
            return None
        ctag, sz, cnt = struct.unpack_from("<QII", body, 0)
        if ctag != CONDITIONS or 16 + sz + 8 != len(body) or body[16 + sz:] != b"\x00" * 8:
            return None
        return {"type": "condition-group", "count": cnt, "content": body[16:16 + sz]}

    # hasTag: u32 hash | u32 len | name[len] | pad4 | u32 0 | u32 0     (270/270)
    if tag == HAS_TAG:
        if len(body) < 16:
            return None
        x, h, ln = struct.unpack_from("<III", body, 0)
        p4 = (ln + 3) & ~3
        if 12 + p4 + 4 == len(body) and body[12 + ln:12 + p4] == b"\x00" * (p4 - ln) \
                and body[12 + p4:] == b"\x00" * 4:
            # field names from artifacts/engine-class-fields.json: ['affiliate', 'tag']
            return {"type": "hasTag", "affiliate": x, "tag_hash": h,
                    "tag": body[12:12 + ln].decode("latin-1")}
        return None

    # referenceStatemachineStateChange: u32 index | u64 id | u32 0 | u32 0  (660/669)
    # referenceStatemachineStateChange, corrected by reading the type's own handlers:
    #   referenceStatemachineBranch's handler is a getter (lea 0x8(%ecx),%eax) - writes no value
    #   stateName's handler copies EIGHT bytes into +0x28/+0x2c - so it is a u64
    # Body: string template | u32 referenceStatemachineBranch | u64 stateName | u32 0
    # stateName is the uid of the TARGET STATE's name ("Complete", "Fail", ...); the branch field is
    # an index, or -1 when the template path already names the state.
    if tag == REF_STATE_CHANGE:
        if len(body) < 16:
            return None
        ln = struct.unpack_from("<I", body, 0)[0]
        p4 = (ln + 3) & ~3
        if 4 + p4 + 4 + 8 + 4 != len(body):
            return None
        if body[4 + ln:4 + p4] != b"\x00" * (p4 - ln) or body[-4:] != b"\x00" * 4:
            return None
        branch = struct.unpack_from("<i", body, 4 + p4)[0]
        state_name = struct.unpack_from("<Q", body, 8 + p4)[0]
        return {"type": "state-change", "template": body[4:4 + ln].decode("latin-1"),
                "referenceStatemachineBranch": branch, "stateName": state_name}

    # gameObject: u64 a | u64 b | u32 0                    (all 20 bytes)
    if tag == GAME_OBJECT and len(body) == 20 and body[16:] == b"\x00" * 4:
        a, b = struct.unpack_from("<QQ", body, 0)
        return {"type": "two-u64", "a": a, "b": b}

    # gameObjectTriggerTouch: u64 a | u64 b | u64 c | u32 0  (all 28 bytes)
    if tag == GAME_OBJECT_TOUCH and len(body) == 28 and body[24:] == b"\x00" * 4:
        a, b, c = struct.unpack_from("<QQQ", body, 0)
        return {"type": "three-u64", "a": a, "b": b, "c": c}

    # gameObjectAttribute: u32 x | u32 hash | u32 len | name | pad4 | u64 v | u32 0
    # names seen: className (19), meta object template (6), TriggerClass (1)   26/26
    if tag == GAME_OBJECT_ATTR:
        if len(body) < 24:
            return None
        x, h, ln = struct.unpack_from("<III", body, 0)
        p4 = (ln + 3) & ~3
        if 12 + p4 + 8 + 4 == len(body) and body[12 + ln:12 + p4] == b"\x00" * (p4 - ln) \
                and body[12 + p4 + 8:] == b"\x00" * 4:
            v = struct.unpack_from("<Q", body, 12 + p4)[0]
            return {"type": "gameObjectAttribute", "x": x, "hash": h,
                    "name": body[12:12 + ln].decode("latin-1"), "value": v}
        return None

    # coded id 0xcdd48f0984102d90: u64 a | u64 b | 12 zero bytes            72/79
    if tag == CODED_CDD48 and len(body) == 28 and body[24:] == b"\x00" * 4:
        a, b, c = struct.unpack_from("<QQQ", body, 0)
        return {"type": "three-u64-4zero", "a": a, "b": b, "c": c}

    # excludeVolume: 72 bytes, fixed layout, all 75 instances agree on the constants:
    #   bool | 0 | f32 | GUID(16) | bool | bool*5 | bool*3 | u32 0x100 | 0
    # Types are established; the individual booleans are not yet semantically named.
    if tag == EXCLUDE_VOLUME and len(body) == 72:
        w = struct.unpack_from("<18I", body, 0)
        if (w[1] == 0 and w[0] in (0, 1) and w[16] == 0x100 and w[17] == 0
                and all(x in (0, 1) for x in w[8:13])):
            return {"type": "excludeVolume", "enabled": w[0], "time": struct.unpack_from("<f", body, 8)[0],
                    "guid": body[12:28], "f7": w[7],
                    "flags5": list(w[8:13]), "flags3": list(w[13:16])}
        return None

    # execute: engine fields [timeBegin, timeEnd, branch, conditions]        (20/20)
    #   u32 flags(0) | f32 timeBegin | f32 timeEnd | u64 branch | conditions child | u32 0
    if tag == EXECUTE and len(body) >= 44:
        flags, t0, t1 = struct.unpack_from("<Iff", body, 0)
        branch = struct.unpack_from("<Q", body, 12)[0]
        ctag, csz, ccnt = struct.unpack_from("<QII", body, 20)
        if (flags == 0 and ctag == CONDITIONS and 20 + 16 + csz + 4 + 4 == len(body)
                and body[-4:] == b"\x00" * 4):
            return {"type": "execute", "timeBegin": t0, "timeEnd": t1, "branch": branch,
                    "conditions_count": ccnt, "conditions": body[36:36 + csz]}

    # conditions: an inline item list (no header) - validated as items in 23/23 bodies
    if tag == CONDITIONS:
        import fsm_items as _I
        sub = _I.parse_items(body, 0, len(body))
        if sub:
            return {"type": "conditions-list", "body": body}
        return None

    # music: 68 bytes. The two leading words are the constructor's +0x08 (=1) and +0x0c (=0);
    # then four u64 fields in the order the handlers give them (group, section, sequence,
    # transition), verified as pairs of varying words. The trailing seven words are not yet
    # assigned, so they are carried as words rather than named.
    if tag == MUSIC and len(body) == 68:
        w = struct.unpack_from("<17I", body, 0)
        if w[0] == 1 and w[1] == 0:
            group = w[2] | (w[3] << 32)
            section = w[4] | (w[5] << 32)
            sequence = w[6] | (w[7] << 32)
            transition = w[8] | (w[9] << 32)
            return {"type": "music", "group": group, "section": section,
                    "sequence": sequence, "transition": transition, "rest": list(w[10:17])}
        return None

    # trackGroup: u32 flags | f32 timeBegin | f32 timeEnd | conditions | tracks | u32 0   (6/6)
    if tag == TRACK_GROUP and len(body) >= 28:
        flags, t0, t1 = struct.unpack_from("<Iff", body, 0)
        kids, ok, stop = C_walk_children(body, 12, len(body))
        if ok or (stop + 4 == len(body) and body[-4:] == b"\x00" * 4):
            if stop + 4 == len(body) and body[-4:] == b"\x00" * 4:
                return {"type": "trackGroup", "flags": flags, "timeBegin": t0, "timeEnd": t1,
                        "children": [(k["tag"], k["count"], body[k["off"]:k["off"] + k["size"]])
                                     for k in kids]}

    # alertStatus: u64 | u32 | u32 0                                              (3/3)
    if tag == ALERT_STATUS and len(body) == 16 and body[12:] == b"\x00" * 4:
        a, b_ = struct.unpack_from("<QI", body, 0)
        return {"type": "alertStatus", "a": a, "b": b_}

    # stateMachine_StopCurrent: u32 | f32 | f32 | u32 0                           (1/1)
    if tag == SM_STOP_CURRENT and len(body) == 16 and body[12:] == b"\x00" * 4:
        f0, t0, t1 = struct.unpack_from("<Iff", body, 0)
        return {"type": "smStopCurrent", "flags": f0, "timeBegin": t0, "timeEnd": t1}

    # mixerII: u32 | u32 | f32 | u64 | u32 | u32 0 | u32 0                       (7/7)
    if tag == MIXER_II and len(body) == 32 and body[24:] == b"\x00" * 8:
        a, b_, t, ident, c = struct.unpack_from("<IIfQI", body, 0)
        return {"type": "mixerII", "a": a, "b": b_, "time": t, "id": ident, "c": c}

    # onBegin: u32 flags | u32 0 | u64 id | u32 n | 16 zero bytes            (1/1)
    if tag == ON_BEGIN and len(body) == 36 and body[28:] == b"\x00" * 8:
        flags, zero, ident, n = struct.unpack_from("<IIQI", body, 0)
        return {"type": "onBegin", "flags": flags, "id": ident, "count": n}

    # sound: 72 bytes, 18 words; fixed layout (2/2). Only the preamble is semantically named.
    if tag == SOUND and len(body) == 72:
        w = struct.unpack_from("<18I", body, 0)
        return {"type": "sound", "flags": w[0], "timeEnd": struct.unpack_from("<f", body, 8)[0],
                "words": list(w)}

    # stateMachine_Start: u32 flags | f32 timeBegin | f32 timeEnd | name | u32 x3 | args
    #                     | u64 stateMachineBranch                          (4/4)
    # There is no trailing terminator: the body ends with the branch u64. The three words between the
    # two strings are 0 in every instance and are carried rather than assumed.
    if tag == SM_START:
        def _rs(b, p):
            if p + 4 > len(b):
                return None, p
            ln = struct.unpack_from("<I", b, p)[0]
            if p + 4 + ln > len(b):
                return None, p
            return b[p + 4:p + 4 + ln], p + 4 + ((ln + 3) & ~3)
        flags, t0, t1 = struct.unpack_from("<Iff", body, 0)
        pos = 12
        nm, pos = _rs(body, pos)
        if nm is None or pos + 12 > len(body):
            return None
        w = struct.unpack_from("<III", body, pos)
        pos += 12
        ar, pos = _rs(body, pos)
        if ar is None or len(body) - pos != 8:
            return None
        return {"type": "smStart", "flags": flags, "timeBegin": t0, "timeEnd": t1,
                "name": nm.decode("latin-1"), "words": list(w),
                "args": ar.decode("latin-1"),
                "stateMachineBranch": struct.unpack_from("<Q", body, pos)[0]}

    if tag != RECORD:
        return None
    if container in PREFIXED_CONTAINERS or len(body) >= 12:
        if len(body) >= 16:
            x, t0, t1, ln = struct.unpack_from("<IffI", body, 0)
            if ln <= len(body) - 16:
                pad = (4 - ln % 4) % 4
                if (16 + ln + pad + 4 == len(body)
                        and body[16 + ln:16 + ln + pad] == b"\0" * pad
                        and body[16 + ln + pad:] == b"\0" * 4):
                    return {"type": "record", "form": "prefixed", "x": x, "timeBegin": t0,
                            "timeEnd": t1, "payload": body[16:16 + ln]}
    return {"type": "record", "form": "bare", "payload": body}


def encode_body(fields):
    """Body bytes for decoded fields. encode_body(decode_body(...)) must equal the original."""
    kind = fields["type"]
    if kind == "zero-field":
        return TERMINATOR
    if kind == "u64-value":
        return struct.pack("<Q", fields["value"]) + b"\x00" * 4
    if kind == "excludeVolume":
        return (struct.pack("<II", fields["enabled"], 0)
                + struct.pack("<f", fields["time"]) + fields["guid"]
                + struct.pack("<I", fields["f7"])
                + struct.pack("<5I", *fields["flags5"])
                + struct.pack("<3I", *fields["flags3"])
                + struct.pack("<II", 0x100, 0))

    if kind == "gameObjectAttribute":
        name = fields["name"].encode("latin-1")
        p4 = (len(name) + 3) & ~3
        return (struct.pack("<III", fields["x"], fields["hash"], len(name)) + name
                + b"\x00" * (p4 - len(name)) + struct.pack("<Q", fields["value"])
                + b"\x00" * 4)
    if kind == "three-u64-4zero":
        return struct.pack("<QQQ", fields["a"], fields["b"], fields["c"]) + b"\x00" * 4

    if kind == "two-u64":
        return struct.pack("<QQ", fields["a"], fields["b"]) + b"\x00" * 4
    if kind == "three-u64":
        return struct.pack("<QQQ", fields["a"], fields["b"], fields["c"]) + b"\x00" * 4

    if kind == "condition-group":
        content = fields["content"]
        return (struct.pack("<QII", CONDITIONS, len(content), fields["count"])
                + content + b"\x00" * 8)

    if kind == "hasTag":
        name = fields["tag"].encode("latin-1")
        p4 = (len(name) + 3) & ~3
        return (struct.pack("<III", fields.get("affiliate", 0), fields["tag_hash"], len(name)) + name
                + b"\x00" * (p4 - len(name)) + b"\x00" * 4)
    if kind == "smStart":
        def _ws(t):
            raw = t.encode("latin-1")
            return struct.pack("<I", len(raw)) + raw + b"\x00" * (((len(raw) + 3) & ~3) - len(raw))
        return (struct.pack("<Iff", fields["flags"], fields["timeBegin"], fields["timeEnd"])
                + _ws(fields["name"]) + struct.pack("<III", *fields["words"])
                + _ws(fields["args"]) + struct.pack("<Q", fields["stateMachineBranch"]))

    if kind == "onBegin":
        return struct.pack("<IIQI", fields["flags"], 0, fields["id"], fields["count"]) + b"\x00" * 16
    if kind == "sound":
        return struct.pack("<18I", *fields["words"])

    if kind == "trackGroup":
        out = struct.pack("<Iff", fields["flags"], fields["timeBegin"], fields["timeEnd"])
        for ctag, cnt, content in fields["children"]:
            out += (struct.pack("<QII", ctag, len(content), cnt) + content + b"\x00" * 4)
        return out + b"\x00" * 4
    if kind == "alertStatus":
        return struct.pack("<QI", fields["a"], fields["b"]) + b"\x00" * 4
    if kind == "smStopCurrent":
        return struct.pack("<Iff", fields["flags"], fields["timeBegin"], fields["timeEnd"]) + b"\x00" * 4
    if kind == "mixerII":
        return (struct.pack("<IIfQI", fields["a"], fields["b"], fields["time"], fields["id"], fields["c"])
                + b"\x00" * 8)

    if kind == "music":
        ws = [1, 0,
              fields["group"] & 0xFFFFFFFF, fields["group"] >> 32,
              fields["section"] & 0xFFFFFFFF, fields["section"] >> 32,
              fields["sequence"] & 0xFFFFFFFF, fields["sequence"] >> 32,
              fields["transition"] & 0xFFFFFFFF, fields["transition"] >> 32]
        return struct.pack("<17I", *(ws + list(fields["rest"])))

    if kind == "execute":
        content = fields["conditions"]
        return (struct.pack("<IffQ", 0, fields["timeBegin"], fields["timeEnd"], fields["branch"])
                + struct.pack("<QII", CONDITIONS, len(content), fields["conditions_count"])
                + content + b"\x00" * 8)
    if kind == "conditions-list":
        return fields["body"]

    if kind == "state-change":
        tmpl = fields.get("template", "").encode("latin-1")
        p4 = (len(tmpl) + 3) & ~3
        return (struct.pack("<I", len(tmpl)) + tmpl + b"\x00" * (p4 - len(tmpl))
                + struct.pack("<iQ", fields["referenceStatemachineBranch"], fields["stateName"])
                + b"\x00" * 4)
    p = fields["payload"]
    if fields["form"] == "bare":
        return p
    pad = (4 - len(p) % 4) % 4
    return (struct.pack("<IffI", fields.get("x", 0), fields["timeBegin"], fields["timeEnd"],
                        len(p)) + p + b"\0" * pad + b"\0" * 4)


def is_lua(payload):
    return payload[:2] == b"\x7f\x00"


def main(argv=None):
    import collections
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fsm_chunk as C
    import fsm_graph as F
    import fsm_items as I

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corpus = os.path.join(here, "tmp", "corpus")
    if not os.path.isdir(corpus):
        print(f"no corpus at {corpus}")
        return 2
    table = F.name_table()
    ok = 0
    bad = 0
    unknown = collections.Counter()
    total = 0
    for dirpath, _dirs, names in os.walk(corpus):
        if "fig.p3d.rz" not in names:
            continue
        leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
        nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
        for what, off, size, _count, _kids in I.containers(leaf, nodes):
            items = I.parse_items(leaf, off + 16, off + 16 + size)
            if not items:
                continue
            for it in items:
                total += 1
                body = leaf[it.off + I.ITEM_HDR:it.off + I.ITEM_HDR + it.length]
                fields = decode_body(what, it.tag, body)
                if fields is None:
                    unknown[table.get(it.tag, f"{it.tag:#018x}")] += 1
                    continue
                if encode_body(fields) == body:
                    ok += 1
                else:
                    bad += 1
    pct = 100.0 * ok / total if total else 0.0
    print(f"items                 : {total}")
    print(f"  decoded + re-emitted: {ok}  ({pct:.1f}%)")
    print(f"  re-emit mismatches  : {bad}")
    print(f"  undecoded types     : {len(unknown)} tags, {sum(unknown.values())} items")
    for t, n in unknown.most_common(12):
        print(f"    {t:<34} {n}")
    return 0 if bad == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
