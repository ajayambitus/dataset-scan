"""Turn per-file rows into a dataset report card, in the shape of Raghav's inventory sheet
(Dataset_inventory_manufacturing.docx, 10/30) plus the audit his 3-step review asks for:

  1. Totals            projects, unique parts, assemblies (+ parts in the largest), files, size
  2. Files by type     native CAD by tool, neutral, DWG/DXF, PDF drawings (+ scanned share), CAM, G-code, documents
  3. Coverage          native model, with feature history, neutral only, with drawing, complete assembly sets,
                       model + drawing at the same revision, G-code + drawing + model
  4. Machining         programs by controller and by type
  5. Samples           what can be checked from a sample: how complete its projects are
  + Audit              what the files really are, broken files, duplicates, rights marks, personal data, language;
                       each problem tagged who fixes it ("us" or "seller")
  + Task fit           counts of complete inputs/outputs per task type

All counts are from the files themselves. Pairing by name is a heuristic and the report says so.
"""
import os
import re
from collections import Counter, defaultdict

NATIVE_MECH = {"solidworks", "inventor", "solidedge", "solidedge_or_creo", "nx", "creo", "nx_or_creo", "catia", "fusion"}
NATIVE_3D = NATIVE_MECH | {"revit", "sketchup", "rhino", "3dsmax", "blender", "jewellery", "diamond"}
STEM_NOISE = re.compile(r"(?:[\s_\-.(]*(?:rev|r|revision)[\s_\-.]?(?:[0-9]{1,3}|[a-z])\)?$)|"
                        r"(?:[\s_\-.]*(?:drw|dwg|drawing|draw|sheet\d*|sht\d*|model|part|assy|asm)$)", re.I)
TOOL_LABEL = {
    "solidworks": "SolidWorks", "inventor": "Inventor", "solidedge": "Solid Edge", "solidedge_or_creo": "Solid Edge / Creo",
    "nx": "Siemens NX", "creo": "Creo", "nx_or_creo": "NX / Creo", "catia": "CATIA", "fusion": "Fusion",
    "revit": "Revit", "sketchup": "SketchUp", "rhino": "Rhino", "3dsmax": "3ds Max", "blender": "Blender",
    "jewellery": "JewelCAD / MGX", "diamond": "Sarine (.cap)", "ifc": "IFC", "neutral": "STEP / IGES / Parasolid",
    "dwg_dxf": "DWG / DXF", "pdf": "PDF", "mesh": "Meshes (STL/OBJ/FBX)", "pointcloud": "Point clouds",
    "cam": "CAM source", "gcode": "G-code / NC", "office": "Office documents", "text": "Text", "email": "Email",
    "image": "Images", "archive": "Archives", "other": "Other", "junk": "OS junk",
}


def part_key(path):
    name = os.path.basename(path.replace("!", "/")).lower()
    name = re.sub(r"\.(prt|asm|drw)\.\d+$", "", name)
    stem = os.path.splitext(name)[0]
    for _ in range(3):
        stem = STEM_NOISE.sub("", stem)
    stem = re.sub(r"[\s_\-.]+", " ", stem).strip()
    return stem


def project_of_rows(rows):
    """Projects = the shallowest folder level where the dataset splits into several folders."""
    dirs = [r["path"].replace("!", "/").split("/")[:-1] for r in rows]
    for depth in range(1, 5):
        prefixes = {tuple(d[:depth]) for d in dirs if len(d) >= depth}
        if len(prefixes) >= 2:
            break
    else:
        depth = 1
    return ["/".join(d[:depth]) if len(d) >= depth else "(top level)" for d in dirs], depth


def build(rows, name="dataset"):
    rows = [r for r in rows if r.get("kind") != "junk"]
    # A store that keeps both an archive and its unpacked copy (the platform does) would count every member twice.
    standalone = {r["sha256"] for r in rows if r.get("sha256") and "!" not in r["path"]}
    expanded_copies = sum(1 for r in rows if "!" in r["path"] and r.get("sha256") in standalone)
    rows = [r for r in rows if not ("!" in r["path"] and r.get("sha256") in standalone)]
    junk = 0
    projects, depth = project_of_rows(rows) if rows else ([], 1)
    for r, p in zip(rows, projects):
        r["_project"] = p
        r["_key"] = part_key(r["path"])

    files = [r for r in rows if r.get("family") != "archive"]
    total_bytes = sum(r.get("size", 0) for r in rows if "!" not in r["path"])  # archives counted once, as stored

    # ---------- 2. files by type ----------
    by_family = Counter()
    bytes_family = Counter()
    for r in files:
        by_family[r["family"]] += 1
        bytes_family[r["family"]] += r.get("size", 0)
    pdfs = [r for r in files if r["family"] == "pdf"]
    pdf_drawings = [r for r in pdfs if r.get("kind") == "drawing"]
    pdf_pages = sum(r.get("pages", 0) for r in pdfs)
    scanned_pages = sum(r.get("scanned_pages", 0) for r in pdfs)
    sw = Counter(r["ext"] for r in files if r["family"] == "solidworks")

    # ---------- 1/3. parts, assemblies, coverage ----------
    model_rows = [r for r in files if r.get("kind") in ("part", "assembly", "model") and r["family"] in NATIVE_MECH | {"neutral"}]
    drawing_rows = [r for r in files if r.get("kind") == "drawing"]
    cam_rows = [r for r in files if r.get("kind") == "cam"]
    gcode_rows = [r for r in files if r.get("kind") == "gcode"]
    keys = defaultdict(lambda: {"native": [], "neutral": [], "drawing": [], "cam": [], "gcode": [], "assembly": False})
    for r in model_rows:
        k = keys[(r["_project"], r["_key"])]
        (k["neutral"] if r["family"] == "neutral" else k["native"]).append(r)
        if r.get("kind") == "assembly":
            k["assembly"] = True
    by_name = defaultdict(list)
    for (proj, key) in list(keys):
        by_name[key].append((proj, key))
    for r in drawing_rows:
        targets = set()
        for ref in r.get("refs", []):  # exact pairing from the drawing's own references
            targets.update(by_name.get(part_key(ref), []))
        if not targets:
            t = (r["_project"], r["_key"])
            if t in keys:
                targets.add(t)
        for t in targets:
            keys[t]["drawing"].append(r)
    for r in cam_rows + gcode_rows:
        t = (r["_project"], r["_key"])
        if t not in keys:  # programs are often named by part number inside a job folder; fall back to name only
            cand = by_name.get(r["_key"], [])
            t = cand[0] if len(cand) == 1 else t
        if t in keys:
            keys[t]["cam" if r["kind"] == "cam" else "gcode"].append(r)

    parts = {t: k for t, k in keys.items() if not k["assembly"]}
    assemblies = {t: k for t, k in keys.items() if k["assembly"]}
    asm_rows = [r for r in model_rows if r.get("kind") == "assembly"]
    largest = max(asm_rows, key=lambda r: len(r.get("refs", [])), default=None)

    def rev_conflict(k):
        """Both sides name a revision and none agree (no revision marked on either side is not a conflict)."""
        mrev = {m.get("revision") for m in k["native"] + k["neutral"]} - {None}
        drev = {d.get("revision") for d in k["drawing"]} - {None}
        return bool(mrev and drev and not (mrev & drev))

    with_native = [k for k in parts.values() if k["native"]]
    with_history = [k for k in with_native if not all(m.get("imported_hint") for m in k["native"])]
    imported = [k for k in with_native if all(m.get("imported_hint") for m in k["native"])]
    neutral_only = [k for k in parts.values() if k["neutral"] and not k["native"]]
    with_drawing = [k for k in parts.values() if k["drawing"]]
    paired = [k for k in parts.values() if k["drawing"] and (k["native"] or k["neutral"])]
    rev_conflicts = [k for k in paired if rev_conflict(k)]
    with_prog = [k for k in parts.values() if k["cam"] or k["gcode"]]
    full_cam = [k for k in parts.values() if k["gcode"] and k["drawing"] and (k["native"] or k["neutral"])]

    complete_asm = 0
    for t, a in assemblies.items():
        if not a["drawing"]:
            continue
        refs = {part_key(x) for m in a["native"] for x in m.get("refs", [])}
        present = [keys[c] for ref in refs for c in by_name.get(ref, [])]
        if present and all(c["drawing"] for c in present):
            complete_asm += 1

    # ---------- architecture / BIM ----------
    proj_kinds = defaultdict(Counter)
    for r in files:
        proj_kinds[r["_project"]][r["family"] if r.get("kind") != "drawing" or r["family"] != "pdf" else "pdf_drawing"] += 1
    bim_pairs = [p for p, c in proj_kinds.items()
                 if (c["revit"] or c["ifc"] or c["sketchup"]) and (c["dwg_dxf"] or c["pdf_drawing"])]

    # ---------- 4. machining ----------
    controllers = Counter(r.get("controller") or "unknown" for r in gcode_rows)
    axes = Counter(r.get("axes") or "unknown" for r in gcode_rows)
    cam_sw = Counter(r.get("software") or "unknown" for r in gcode_rows + cam_rows)

    # ---------- audit ----------
    failed = [r for r in rows if r.get("read") == "failed"]
    partial = [r for r in rows if r.get("partial")]
    shallow = Counter(TOOL_LABEL.get(r["family"], r["family"]) for r in files if r.get("read") in ("strings", "metadata", "header"))
    dup_groups = defaultdict(list)
    for r in files:
        if r.get("sha256"):
            dup_groups[r["sha256"]].append(r)
    dups = [g for g in dup_groups.values() if len(g) > 1]
    dup_files = sum(len(g) - 1 for g in dups)
    dup_bytes = sum((len(g) - 1) * g[0].get("size", 0) for g in dups)
    rights_files = Counter()
    snippets = Counter()
    for r in files:
        for cat in r.get("rights", {}):
            rights_files[cat] += 1
        for s in r.get("rights_snippets", []):
            snippets[re.sub(r"\d", "#", s)[:180]] += 1
    pii_files = Counter()
    for r in files:
        for kind in r.get("pii", {}):
            pii_files[kind] += 1
    langs = Counter(r["lang"] for r in files if r.get("lang"))
    authors = Counter(a for r in files for a in r.get("authors", []))
    orgs = Counter(o for r in files for o in r.get("orgs", []))
    software = Counter(f"{r.get('software') or TOOL_LABEL.get(r['family'], r['family'])}"
                       + (f" {r['version']}" if r.get("version") and str(r["version"]) not in str(r.get("software")) else "")
                       for r in files if r["family"] not in ("image", "text", "junk"))
    years = Counter(str(r["created"])[:4] for r in files if re.match(r"(19|20)\d\d", str(r.get("created", ""))))
    dims = Counter(r["dims"] for r in files if r.get("dims"))
    lang_other = sum(v for k, v in langs.items() if k != "en")

    issues = []
    def issue(who, what, n, detail=""):
        if n:
            issues.append({"fix": who, "issue": what, "count": n, "detail": detail})
    issue("seller", "files that could not be opened (corrupt, truncated or empty)", len(failed),
          "; ".join(f"{r['path']}: {r.get('error', '')}" for r in failed[:5]))
    issue("seller", "native parts that look like imported solids (no feature history)", len(imported),
          "import markers in the file, e.g. 'Imported1' features; confirm in the CAD tool on a few")
    issue("seller", "parts with a model but no drawing", len(parts) - len(with_drawing) if parts else 0,
          "drawing → CAD tasks need the drawing")
    issue("seller", "files carrying rights marks (property of / not to be reproduced / confidential / copyright)",
          sum(1 for r in files if set(r.get("rights", {})) & {"property_of", "no_reproduce", "confidential", "copyright"}),
          "the seller must show they own or may resell these")
    issue("seller", "files marked sample / watermark / demo / synthetic",
          sum(1 for r in files if set(r.get("rights", {})) & {"watermark", "demo_synthetic"}),
          "labs reject watermarked samples; synthetic copies are not the real data")
    issue("seller", "model/drawing pairs whose revisions disagree", len(rev_conflicts), "the drawing may not match the model")
    issue("seller", "duplicate files (inflate the count)", dup_files, f"{dup_bytes / 1e9:.2f} GB of repeats")
    issue("us", "files with personal data (names, emails, phones, user paths)", sum(1 for r in files if r.get("pii")),
          "anonymize before delivery (not needed for samples)")
    issue("us", "files in a language other than English", lang_other,
          ", ".join(f"{k}: {v}" for k, v in langs.most_common() if k != "en"))
    issue("us", "scanned PDF pages (need OCR)", scanned_pages, f"of {pdf_pages} PDF pages")
    issue("us", "files only checked through metadata/strings (need conversion to check geometry)", sum(shallow.values()),
          ", ".join(f"{k}: {v}" for k, v in shallow.most_common(6)))

    tasks = {
        "drawing_to_cad (mechanical)": {"pairs": len(paired), "revision_conflicts": len(rev_conflicts),
                                         "note": "part with a native/neutral model and a drawing"},
        "drawing_to_ifc (architecture/civil)": {"projects_with_drawings_and_model": len(bim_pairs),
                                               "note": "project folder with Revit/IFC/SketchUp plus DWG/PDF drawings"},
        "cam_gcode": {"gcode_plus_drawing_plus_model": len(full_cam), "parts_with_programs": len(with_prog)},
    }

    report = {
        "dataset": name,
        "project_level": depth,
        "totals": {
            "projects": len({p for p in projects}),
            "unique_parts": len(parts),
            "assemblies": len(asm_rows),
            "parts_in_largest_assembly": len(largest.get("refs", [])) if largest else 0,
            "largest_assembly": largest["path"] if largest else None,
            "files": len(files),
            "archives": sum(1 for r in rows if r.get("family") == "archive"),
            "size_gb": round(total_bytes / 1e9, 3),
        },
        "files_by_type": {
            "solidworks": {"parts": sw[".sldprt"], "assemblies": sw[".sldasm"], "drawings": sw[".slddrw"]},
            **{TOOL_LABEL.get(f, f): {"files": n, "gb": round(bytes_family[f] / 1e9, 3)} for f, n in by_family.most_common()},
            "pdf_drawings": len(pdf_drawings),
            "pdf_scanned_page_share": round(scanned_pages / pdf_pages, 3) if pdf_pages else None,
        },
        "coverage": {
            "parts_with_native_model": len(with_native),
            "native_with_feature_history": len(with_history),
            "native_imported_solids": len(imported),
            "parts_with_only_neutral_model": len(neutral_only),
            "parts_with_drawing": len(with_drawing),
            "assemblies_with_complete_drawing_set": complete_asm,
            "model_and_drawing_pairs": len(paired),
            "pairs_with_conflicting_revisions": len(rev_conflicts),
            "parts_with_cam_or_gcode": len(with_prog),
            "gcode_plus_drawing_plus_model": len(full_cam),
        },
        "machining": {"programs": len(gcode_rows), "cam_files": len(cam_rows), "by_controller": dict(controllers),
                      "by_type": dict(axes), "by_software": dict(cam_sw)},
        "dims": dict(dims),
        "provenance": {"software": dict(software.most_common(15)), "authors": dict(authors.most_common(15)),
                       "orgs": dict(orgs.most_common(15)), "created_years": dict(sorted(years.items()))},
        "rights": {"files_by_mark": dict(rights_files), "top_snippets": [s for s, _ in snippets.most_common(15)]},
        "pii": {"files_by_kind": dict(pii_files)},
        "languages": dict(langs.most_common()),
        "quality": {"archive_members_also_stored_unpacked": expanded_copies, "failed": len(failed), "partial_reads": len(partial), "duplicate_files": dup_files,
                    "duplicate_gb": round(dup_bytes / 1e9, 3), "read_depth": dict(Counter(r.get("read") for r in files)),
                    "failed_files": [{"path": r["path"], "error": r.get("error")} for r in failed[:50]]},
        "task_fit": tasks,
        "issues": issues,
        "projects": [
            {"project": p, "files": sum(c.values()), "mix": dict(c.most_common(6))}
            for p, c in sorted(proj_kinds.items(), key=lambda x: -sum(x[1].values()))[:40]
        ],
        "caveats": [
            "Part/drawing pairing is by file name and, for SolidWorks/Inventor, by the references inside the drawing.",
            "Feature history is a heuristic: import markers in the file; confirm a few in the CAD tool.",
            "DWG R2004+ text is compressed, so DWG rights/PII counts are a lower bound.",
            "Rights marks are evidence to check, not a verdict on ownership.",
        ],
    }
    for r in rows:
        r.pop("_project", None)
        r.pop("_key", None)
    return report


def table(rows_):
    return "\n".join(f"| {a} | {b} |" for a, b in rows_)


def to_markdown(rep, seller=None):
    t, f, c = rep["totals"], rep["files_by_type"], rep["coverage"]
    seller = seller or {}

    def line(label, key, value):
        s = seller.get(key)
        return (label, f"{value}" + (f" (seller says {s})" if s is not None else ""))

    out = [f"# Dataset report card: {rep['dataset']}", "",
           "Counts come from the files themselves (Raghav's inventory sheet, filled in automatically).", "",
           "## 1. Totals", "", "| Item | Found |", "|---|---|",
           table([line("Projects (top folders)", "projects", t["projects"]),
                  line("Unique parts", "unique_parts", t["unique_parts"]),
                  line("Assemblies / parts in the largest", "assemblies", f"{t['assemblies']} / {t['parts_in_largest_assembly']}"),
                  line("Total files", "files", t["files"]), line("Total size (GB)", "size_gb", t["size_gb"])]),
           "", "## 2. Files by type", "", "| Type | Files | GB |", "|---|---|---|"]
    for k, v in f.items():
        if isinstance(v, dict) and "files" in v:
            out.append(f"| {k} | {v['files']} | {v['gb']} |")
    sw = f["solidworks"]
    out += ["", f"SolidWorks: {sw['parts']} parts, {sw['assemblies']} assemblies, {sw['drawings']} drawings. "
            f"PDF drawings: {f['pdf_drawings']}; scanned page share: {f['pdf_scanned_page_share']}.",
            "", "## 3. Coverage", "", "| Item | Found |", "|---|---|",
            table([(k.replace("_", " "), v) for k, v in c.items()]),
            "", "## 4. Machining", "",
            f"{rep['machining']['programs']} programs, {rep['machining']['cam_files']} CAM files. "
            f"Controllers: {rep['machining']['by_controller']}. Types: {rep['machining']['by_type']}.",
            "", "## Task fit", "", "| Task type | Complete inputs + outputs |", "|---|---|"]
    for k, v in rep["task_fit"].items():
        out.append(f"| {k} | " + ", ".join(f"{a.replace('_', ' ')}: {b}" for a, b in v.items() if a != "note") + " |")
    out += ["", "## Problems, by who fixes them", "", "| Fix | Problem | Files | Detail |", "|---|---|---|---|"]
    for i in sorted(rep["issues"], key=lambda x: (x["fix"] != "seller", -x["count"])):
        out.append(f"| {i['fix']} | {i['issue']} | {i['count']} | {i['detail'][:200]} |")
    p = rep["provenance"]
    out += ["", "## Provenance and rights", "",
            f"- Software: {p['software']}", f"- Authors in metadata: {p['authors']}", f"- Organisations: {p['orgs']}",
            f"- Created (years): {p['created_years']}", f"- Files by rights mark: {rep['rights']['files_by_mark']}",
            f"- Personal data (files by kind; values not stored): {rep['pii']['files_by_kind']}",
            f"- Languages (files): {rep['languages']}", f"- 2D / 3D files: {rep['dims']}", "", "Rights snippets:", ""]
    out += [f"- `{s}`" for s in rep["rights"]["top_snippets"]] or ["- none found"]
    out += ["", "## Projects (largest 40)", "", "| Project | Files | Mix |", "|---|---|---|"]
    out += [f"| {x['project']} | {x['files']} | {x['mix']} |" for x in rep["projects"]]
    q = rep["quality"]
    out += ["", "## Read depth", "", f"{q['read_depth']} (full = parsed; metadata/strings = header and embedded text only)",
            "", "## Caveats", ""] + [f"- {x}" for x in rep["caveats"]]
    return "\n".join(out) + "\n"
