"""
PMX -> MH Rise armor (RE Engine) di Blender headless.

Alur:
  1. Import PMX (mmd_tools), skala ke meter.
  2. Bagi faces ke bagian armor (helm/body/arm/waist/leg) berdasar bobot bone (bonemap.json -> "parts").
  3. Per bagian: import mesh referensi asli Rise (ref/parts.json), buang geometrinya,
     pasang geometri baru ke armature + nama object + material milik ref.
  4. Remap vertex group MMD -> nama bone Rise (bonemap.json -> "bones").
  5. Export lewat operator RE-Mesh-Editor ke out/natives/...

CATATAN: nama operator addon RE-Mesh-Editor dicari otomatis (find_op) dan semua operator
yang ketemu dicetak ke log. Kalau export gagal, lihat bagian "RE OPS" di convert.log.
"""
import sys, os, json, re, argparse, traceback, faulthandler
faulthandler.enable(file=sys.stderr, all_threads=True)
import bpy, bmesh, addon_utils

_um = os.path.join(os.environ.get("BLENDER_USER_SCRIPTS", ""), "modules")
if os.path.isdir(_um) and _um not in sys.path:
    sys.path.insert(0, _um)


def parse():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--pmx-dir"); ap.add_argument("--ref-dir"); ap.add_argument("--bonemap")
    ap.add_argument("--out"); ap.add_argument("--parts", default="helm,body,arm,waist,leg")
    ap.add_argument("--scale", type=float, default=0.08); ap.add_argument("--hide", default="")
    ap.add_argument("--re-addon", default=""); ap.add_argument("--dump-only", default="false")
    ap.add_argument("--multi-material", action="store_true")
    a = ap.parse_args(argv)
    for k in ("pmx_dir", "ref_dir", "bonemap", "out"):
        setattr(a, k, os.path.abspath(getattr(a, k)))
    return a


PARENTS = {}

# import RE tanpa material/tekstur: jalur teks/GPU di background Blender diduga penyebab crash.
# Nama material mesh diambil dari nama objek (...__ALL), jadi .mdf2 asli tetap dipakai.
IMP_FLAGS = dict(loadMaterials=False, loadMDFData=False, loadShellFur=False, loadUnusedTextures=False,
                 importShadowMeshes=False, importBlendShapes=False, importOcclusionMeshes=False,
                 importBoundingBoxes=False)


def log(*a):
    print("[pmx2pak]", *a, flush=True)


def enable(name):
    for n in (name, "bl_ext.user_default." + name):
        try:
            if addon_utils.enable(n, default_set=True, persistent=True):
                log("addon enabled:", n)
                return True
        except Exception as e:
            log("enable error", n, e)
    log("GAGAL enable addon", name, "| paths:", addon_utils.paths())
    return False


def find_op(ns, *patterns):
    """cari operator di bpy.ops.<ns> yang namanya cocok salah satu regex."""
    names = [n for n in dir(getattr(bpy.ops, ns)) if not n.startswith("_")]
    for p in patterns:
        for n in names:
            if re.search(p, n, re.I):
                return getattr(getattr(bpy.ops, ns), n)
    return None


def describe(label, op):
    """log nama+default properti operator (buat debug versi addon)."""
    try:
        props = op.get_rna_type().properties
        d = {x.identifier: str(getattr(x, "default", ""))[:30] for x in props if x.identifier != "rna_type"}
        log("OP", label, d)
    except Exception as e:
        log("describe gagal", label, e)


def list_re_ops():
    log("=== RE OPS ===")
    for ns in dir(bpy.ops):
        if ns.startswith("re_") or ns.startswith("mmd") or "mesh" in ns:
            log(ns, [n for n in dir(getattr(bpy.ops, ns)) if not n.startswith("_")])


def clear_scene():
    for o in list(bpy.data.objects):
        if not o.get("p2p_keep"):
            bpy.data.objects.remove(o, do_unlink=True)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)
    for blk in (bpy.data.meshes, bpy.data.armatures, bpy.data.materials, bpy.data.images):
        for d in list(blk):
            if d.users == 0:
                blk.remove(d)


def dump_scene(tag):
    log("=== SCENE", tag, "===")
    def walk(c, depth=0):
        objs = [f"{o.name}({o.type})" for o in c.objects]
        log("  " * depth + f"[{c.name}]", objs[:12], f"+{max(0, len(objs) - 12)}" if len(objs) > 12 else "")
        for ch in c.children:
            walk(ch, depth + 1)
    walk(bpy.context.scene.collection)
    log("materials:", [m.name for m in bpy.data.materials][:30])


def import_pmx(pmx_dir, scale, hide):
    pmx = None
    for r, _, fs in os.walk(pmx_dir):
        for f in fs:
            if f.lower().endswith(".pmx"):
                pmx = os.path.join(r, f); break
        if pmx: break
    if not pmx:
        raise SystemExit("PMX tidak ditemukan")
    log("PMX:", pmx)
    try:
        bpy.ops.mmd_tools.import_model(filepath=pmx, scale=scale, types={"MESH", "ARMATURE"})
    except TypeError:
        bpy.ops.mmd_tools.import_model(filepath=pmx, scale=scale)
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    arm = next((o for o in bpy.context.scene.objects if o.type == "ARMATURE"), None)
    if arm:
        PARENTS.update({b.name: (b.parent.name if b.parent else None) for b in arm.data.bones})
        log("PMX bone:", len(PARENTS))
    hide_set = {h.strip() for h in hide.split(",") if h.strip()}
    for o in meshes:
        if hide_set:
            idx = [i for i, m in enumerate(o.data.materials) if m and m.name in hide_set]
            if idx:
                bm = bmesh.new(); bm.from_mesh(o.data)
                for f in [f for f in bm.faces if f.material_index in idx]:
                    bm.faces.remove(f)
                bm.to_mesh(o.data); bm.free()
    return meshes, arm


def join_meshes(meshes):
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    # bake MMD transform & modifier tidak perlu; join langsung
    bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def dominant_part(obj, face, rules):
    """bagian armor untuk satu face = bagian dgn total bobot terbesar."""
    score = {p: 0.0 for p in rules}
    names = {vg.index: vg.name for vg in obj.vertex_groups}
    for vi in face.verts:
        for g in obj.data.vertices[vi.index].groups:
            n = names.get(g.group, "")
            for part, bones in rules.items():
                if n in bones:
                    score[part] += g.weight
    best = max(score, key=score.get)
    return best if score[best] > 0 else None


def split_parts(obj, rules, wanted):
    if len(wanted) == 1:  # mod satu-mesh (mis. f_wst279): semua faces masuk satu bagian
        dup = obj.copy(); dup.data = obj.data.copy(); dup.name = f"new_{wanted[0]}"
        bpy.context.collection.objects.link(dup)
        log(f"part {wanted[0]}: {len(dup.data.polygons)} faces (semua)")
        return {wanted[0]: dup}
    bm = bmesh.new(); bm.from_mesh(obj.data); bm.faces.ensure_lookup_table()
    assign = {p: [] for p in wanted}
    unassigned = 0
    for f in bm.faces:
        p = dominant_part(obj, f, rules)
        if p in assign:
            assign[p].append(f.index)
        else:
            unassigned += 1
    bm.free()
    log("face tidak terassign ke bagian manapun:", unassigned)
    result = {}
    for p, idxs in assign.items():
        if not idxs:
            log("PERINGATAN: bagian kosong:", p); continue
        dup = obj.copy(); dup.data = obj.data.copy(); dup.name = f"new_{p}"
        bpy.context.collection.objects.link(dup)
        b = bmesh.new(); b.from_mesh(dup.data); b.faces.ensure_lookup_table()
        keep = set(idxs)
        kill = [f for f in b.faces if f.index not in keep]
        bmesh.ops.delete(b, geom=kill, context="FACES")
        b.to_mesh(dup.data); b.free()
        result[p] = dup
        log(f"part {p}: {len(dup.data.polygons)} faces")
    return result


MAX_W = 6


def remap_groups(obj, bones_map, valid_bones, fallback):
    """MMD vertex group -> bone Rise. Bone tak terpetakan: naik lewat parent MMD sampai ketemu yang terpetakan."""
    cache = {}

    def resolve(n):
        if n in cache:
            return cache[n]
        t, seen = None, set()
        m = n
        while m and m not in seen:
            seen.add(m)
            c = bones_map.get(m) or ""
            if c in valid_bones:
                t = c; break
            m = PARENTS.get(m)
        cache[n] = t or fallback
        return cache[n]

    gname = {vg.index: vg.name for vg in obj.vertex_groups}
    per_v = []
    for v in obj.data.vertices:
        acc = {}
        for g in v.groups:
            if g.weight <= 0:
                continue
            t = resolve(gname[g.group])
            acc[t] = acc.get(t, 0.0) + g.weight
        top = sorted(acc.items(), key=lambda kv: -kv[1])[:MAX_W]
        tot = sum(w for _, w in top) or 1.0
        per_v.append([(t, w / tot) for t, w in top])
    for vg in list(obj.vertex_groups):
        obj.vertex_groups.remove(vg)
    groups = {}
    for vi, lst in enumerate(per_v):
        for t, w in lst:
            vg = groups.get(t) or groups.setdefault(t, obj.vertex_groups.new(name=t))
            vg.add([vi], w, "REPLACE")
    unmapped = sorted(n for n in gname.values() if resolve(n) == fallback and not (bones_map.get(n) or ""))
    log(f"  vertex group baru: {len(groups)} bone Rise; {len(unmapped)} bone MMD jatuh ke fallback", unmapped[:15])


def tex_stem(m):
    if m and m.node_tree:
        for n in m.node_tree.nodes:
            if n.type == "TEX_IMAGE" and n.image:
                return os.path.splitext(os.path.basename(n.image.filepath.replace("\\", "/")))[0]
    return m.name if m else "none"


def assign_materials(obj, template, single=True):
    """satu material RE per tekstur PMX (salinan material ref 'ALL')."""
    base = template.data.materials[0] if template.data.materials else bpy.data.materials.new("ALL")
    log("props material ref:", base.name, list(base.keys()))
    if single:  # mode uji: pakai material ref apa adanya supaya .mdf2 asli tetap valid
        obj.data.materials.clear()
        obj.data.materials.append(base)
        return {i: 0 for i in range(max(1, len(set(p.material_index for p in obj.data.polygons)) + 64))}, [base.name]
    slot_group = [tex_stem(m) for m in obj.data.materials]
    names = []
    for g in slot_group:
        if g not in names:
            names.append(g)
    obj.data.materials.clear()
    for g in names:
        mm = base.copy(); mm.name = g
        obj.data.materials.append(mm)
    old = {i: names.index(g) for i, g in enumerate(slot_group)}
    return old, names


def main():
    a = parse()
    os.makedirs(a.out, exist_ok=True)
    enable("mmd_tools")
    if a.re_addon:
        enable(a.re_addon)
        # penyebab crash: addon memanggil bpy.ops.wm.console_toggle() (showConsole) -> access violation di mode background
        try:
            prefs = bpy.context.preferences.addons[a.re_addon].preferences
            prefs.showConsole = False
            log("RE addon: showConsole = False")
        except Exception as e:
            log("gagal set showConsole:", e)
    list_re_ops()

    parts_ref = json.load(open(os.path.join(a.ref_dir, "parts.json"), encoding="utf-8"))
    bm_cfg = json.load(open(a.bonemap, encoding="utf-8"))
    wanted = [p.strip() for p in a.parts.split(",") if p.strip()]

    if not hasattr(bpy.ops, "mmd_tools"):
        raise SystemExit("mmd_tools tidak termuat (lihat log enable di atas)")
    imp = find_op("re_mesh", r"^importfile$")
    exp = find_op("re_mesh", r"^exportfile$")
    if not imp or not exp:
        raise SystemExit("Operator import/export RE mesh tidak ketemu, cek bagian RE OPS")

    # salin dulu seluruh natives mod ref ke out (mesh baru nanti menimpa), supaya tekstur/chain/mdf2 ikut
    import shutil
    src_root = os.path.join(a.ref_dir, "natives")
    dst_root = os.path.join(a.out, "natives")
    if os.path.isdir(src_root) and a.dump_only.lower() != "true":
        shutil.copytree(src_root, dst_root, dirs_exist_ok=True)
        log("natives ref disalin ke", dst_root)

    # --- pass 1: dump bone ref ---
    for p in wanted:
        ref_path = os.path.join(a.ref_dir, parts_ref[p])
        clear_scene()
        log("import ref (armature saja):", ref_path); sys.stdout.flush()
        imp(filepath=ref_path, importArmatureOnly=True, **IMP_FLAGS)
        bones = sorted({b.name for ar in bpy.data.armatures for b in ar.bones})
        open(os.path.join(a.out, f"ref_bones_{p}.txt"), "w", encoding="utf-8").write("\n".join(bones))
        log(f"ref {p}: {len(bones)} bone")
    if a.dump_only.lower() == "true":
        log("dump_only selesai"); return

    # --- pass 2: konversi ---
    clear_scene()
    meshes, _ = import_pmx(a.pmx_dir, a.scale, a.hide)
    big = join_meshes(meshes)
    new_parts = split_parts(big, bm_cfg["parts"], wanted)
    PARENTS_SAVED = dict(PARENTS)
    for o in new_parts.values():
        o["p2p_keep"] = True
    clear_scene()  # buang PMX asli, sisakan bagian baru

    for p, nobj in new_parts.items():
        ref_path = os.path.join(a.ref_dir, parts_ref[p])
        clear_scene()
        for c in list(nobj.users_collection):
            c.objects.unlink(nobj)
        bpy.context.scene.collection.objects.link(nobj)
        log("import ref lengkap:", ref_path); sys.stdout.flush()
        imp(filepath=ref_path, clearScene=False, **IMP_FLAGS)
        dump_scene("setelah import ref " + p)
        arms = [o for o in bpy.data.objects if o.type == "ARMATURE" and not o.get("p2p_keep")]
        if not arms:
            raise SystemExit("armature ref tidak ada")
        arm = arms[0]
        ref_meshes = [o for o in bpy.data.objects if o.type == "MESH" and not o.get("p2p_keep")]
        if not ref_meshes:
            raise SystemExit("mesh ref tidak ada")
        template = ref_meshes[0]
        valid = {b.name for b in arm.data.bones}
        PARENTS.clear(); PARENTS.update(PARENTS_SAVED)
        remap_groups(nobj, bm_cfg["bones"], valid, bm_cfg.get("fallback", {}).get(p) or sorted(valid)[0])

        old_idx = [f.material_index for f in nobj.data.polygons]
        remap, mat_names = assign_materials(nobj, template, single=not a.multi_material)
        for f, oi in zip(nobj.data.polygons, old_idx):
            f.material_index = remap.get(oi, 0)
        log(f"{len(mat_names)} material RE:", mat_names)

        col = template.users_collection[0]
        old_name = template.name
        for o in ref_meshes:
            bpy.data.objects.remove(o, do_unlink=True)
        for c in list(nobj.users_collection):
            c.objects.unlink(nobj)
        col.objects.link(nobj)
        nobj.name = old_name
        nobj.parent = arm
        for mod in list(nobj.modifiers):
            nobj.modifiers.remove(mod)
        nobj.modifiers.new("Armature", "ARMATURE").object = arm
        dump_scene("sebelum export " + p)

        mesh_col = next((c for c in bpy.data.collections if ".mesh" in c.name), None)
        outpath = os.path.join(a.out, parts_ref[p])
        os.makedirs(os.path.dirname(outpath), exist_ok=True)
        log("export", p, "->", outpath, "| collection:", mesh_col.name if mesh_col else None); sys.stdout.flush()
        kw = dict(filepath=outpath, selectedOnly=False)
        if mesh_col:
            kw["targetCollection"] = mesh_col.name
        res = exp(**kw)
        log("export result:", res, "| ada file:", os.path.exists(outpath))
    log("SELESAI. mdf2 asli dipakai (mode material tunggal)" if not a.multi_material else "SELESAI (multi material, mdf2 belum diupdate!)")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
