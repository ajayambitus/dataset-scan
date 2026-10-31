"""Scan one file and return one row of facts about it. No judgement here: the inventory step aggregates the rows.

Every handler reads metadata first (headers, embedded text, entity counts) and never needs the native CAD program,
following the review rule "inspect metadata first, open in the CAD tool only when needed". What a handler could not
read is recorded in `read` ("full", "metadata", "strings", "header", "failed") so nothing is silently skipped.

Row fields (all optional except path/ext/size/kind/read):
  path, ext, size, sha256, kind, family, read, error, partial
  software, version, created, authors, orgs          provenance from file metadata
  dims ("2d"/"3d"), pages, scanned_pages, drawing_like, entities
  refs                                               model files a drawing or assembly references
  imported_hint                                      signs of an imported dead solid (no feature history)
  revision                                           revision token from the file name
  rights, rights_snippets, pii, lang                 text checks
  machine, controller, axes                          G-code
"""
import hashlib
import io
import os
import re
import zipfile
from collections import Counter

from . import textchecks

TEXT_LIMIT = 2_000_000          # characters of text kept per file for the text checks
STRINGS_BYTES = 48 * 1024 * 1024  # bytes of a binary file scanned for embedded strings

# ext -> (kind, family). kind is what the file is; family groups kinds for the inventory sheet.
KINDS = {
    ".sldprt": ("part", "solidworks"), ".sldasm": ("assembly", "solidworks"), ".slddrw": ("drawing", "solidworks"),
    ".ipt": ("part", "inventor"), ".iam": ("assembly", "inventor"), ".idw": ("drawing", "inventor"),
    ".ipn": ("presentation", "inventor"),
    ".par": ("part", "solidedge"), ".psm": ("part", "solidedge"), ".asm": ("assembly", "solidedge_or_creo"),
    ".dft": ("drawing", "solidedge"),
    ".prt": ("part", "nx_or_creo"), ".drw": ("drawing", "creo"),
    ".catpart": ("part", "catia"), ".catproduct": ("assembly", "catia"), ".catdrawing": ("drawing", "catia"),
    ".f3d": ("part", "fusion"), ".f3z": ("assembly", "fusion"),
    ".step": ("model", "neutral"), ".stp": ("model", "neutral"), ".p21": ("model", "neutral"),
    ".iges": ("model", "neutral"), ".igs": ("model", "neutral"), ".x_t": ("model", "neutral"),
    ".x_b": ("model", "neutral"), ".sat": ("model", "neutral"), ".jt": ("model", "neutral"),
    ".dwg": ("drawing", "dwg_dxf"), ".dxf": ("drawing", "dwg_dxf"), ".dwf": ("drawing", "dwg_dxf"),
    ".pdf": ("document", "pdf"),
    ".rvt": ("model", "revit"), ".rfa": ("family", "revit"), ".rte": ("template", "revit"),
    ".ifc": ("model", "ifc"), ".ifczip": ("model", "ifc"),
    ".skp": ("model", "sketchup"), ".3dm": ("model", "rhino"), ".max": ("model", "3dsmax"),
    ".blend": ("model", "blender"), ".fbx": ("mesh", "mesh"), ".obj": ("mesh", "mesh"), ".stl": ("mesh", "mesh"),
    ".3ds": ("mesh", "mesh"), ".ply": ("mesh", "mesh"), ".glb": ("mesh", "mesh"), ".gltf": ("mesh", "mesh"),
    ".jcd": ("model", "jewellery"), ".mgx": ("mesh", "jewellery"), ".cap": ("model", "diamond"),
    ".e57": ("pointcloud", "pointcloud"), ".las": ("pointcloud", "pointcloud"), ".laz": ("pointcloud", "pointcloud"),
    ".rcp": ("pointcloud", "pointcloud"), ".rcs": ("pointcloud", "pointcloud"), ".pts": ("pointcloud", "pointcloud"),
    ".mcam": ("cam", "cam"), ".emcam": ("cam", "cam"), ".camproj": ("cam", "cam"), ".hsm": ("cam", "cam"),
    ".nc": ("gcode", "gcode"), ".tap": ("gcode", "gcode"), ".ngc": ("gcode", "gcode"), ".gcode": ("gcode", "gcode"),
    ".mpf": ("gcode", "gcode"), ".spf": ("gcode", "gcode"), ".eia": ("gcode", "gcode"), ".min": ("gcode", "gcode"),
    ".cnc": ("gcode", "gcode"), ".ptp": ("gcode", "gcode"), ".h": ("gcode_or_code", "gcode"),
    ".xlsx": ("document", "office"), ".xlsm": ("document", "office"), ".xls": ("document", "office"),
    ".docx": ("document", "office"), ".doc": ("document", "office"), ".pptx": ("document", "office"),
    ".csv": ("document", "text"), ".txt": ("document", "text"), ".md": ("document", "text"),
    ".eml": ("document", "email"), ".msg": ("document", "email"), ".json": ("document", "text"),
    ".xml": ("document", "text"), ".html": ("document", "text"), ".htm": ("document", "text"),
    ".jpg": ("image", "image"), ".jpeg": ("image", "image"), ".png": ("image", "image"),
    ".tif": ("image", "image"), ".tiff": ("image", "image"), ".bmp": ("image", "image"), ".gif": ("image", "image"),
    ".heic": ("image", "image"), ".webp": ("image", "image"),
    ".zip": ("archive", "archive"), ".7z": ("archive", "archive"), ".rar": ("archive", "archive"),
    ".tar": ("archive", "archive"), ".gz": ("archive", "archive"), ".tgz": ("archive", "archive"),
}
JUNK = {".ds_store", "thumbs.db", "desktop.ini"}
DWG_VERSIONS = {
    b"AC1009": "R11/12", b"AC1012": "R13", b"AC1014": "R14", b"AC1015": "2000", b"AC1018": "2004",
    b"AC1021": "2007", b"AC1024": "2010", b"AC1027": "2013", b"AC1032": "2018",
}
DXF_3D = {"3DSOLID", "BODY", "MESH", "3DFACE", "SURFACE", "EXTRUDEDSURFACE", "LOFTEDSURFACE",
          "REVOLVEDSURFACE", "SWEPTSURFACE", "PLANESURFACE", "NURBSURFACE", "POLYFACE", "POLYMESH"}
IFC_BUILT = ("IFCWALL", "IFCSLAB", "IFCBEAM", "IFCCOLUMN", "IFCDOOR", "IFCWINDOW", "IFCSTAIR", "IFCROOF",
             "IFCMEMBER", "IFCPLATE", "IFCFOOTING", "IFCPILE", "IFCRAILING", "IFCCOVERING", "IFCBUILDINGELEMENTPROXY",
             "IFCFLOWSEGMENT", "IFCFLOWTERMINAL", "IFCPIPESEGMENT", "IFCDUCTSEGMENT", "IFCSPACE")
REV_RE = re.compile(r"(?:^|[\s_\-.(\[])(?:rev|r|revision)[\s_\-.]?([0-9]{1,3}|[a-z])(?=$|[\s_\-.)\]])", re.I)
CREO_VERSION_RE = re.compile(r"\.(prt|asm|drw)\.\d+$", re.I)
MODEL_REF_RE = re.compile(rb"([\w\-. ()#&+]{1,120}\.(?:sldprt|sldasm|ipt|iam|prt|asm|par|catpart|catproduct))", re.I)
MODEL_REF_RE16 = re.compile(rb"((?:[\w\-. ()#&+]\x00){1,120}\.\x00(?:(?:s\x00l\x00d\x00p\x00r\x00t)|(?:s\x00l\x00d\x00a\x00s\x00m)|(?:i\x00p\x00t)|(?:i\x00a\x00m)|(?:p\x00r\x00t)|(?:a\x00s\x00m))\x00)", re.I)
IMPORTED_RE = re.compile(rb"Imported\d|I\x00m\x00p\x00o\x00r\x00t\x00e\x00d\x00[0-9]\x00|STEP import|Parasolid import|IGES import|\bImport(?:ed)? Body", re.I)


def ext_of(name):
    low = name.lower()
    m = CREO_VERSION_RE.search(low)
    if m:  # Creo keeps versions as part.prt.7
        return "." + m.group(1)
    for double in (".tar.gz",):
        if low.endswith(double):
            return ".tgz"
    return os.path.splitext(low)[1]


def revision_of(name):
    stem = os.path.basename(name)
    stem = CREO_VERSION_RE.sub("", stem)
    stem = os.path.splitext(stem)[0]
    m = REV_RE.search(stem)
    return m.group(1).upper() if m else None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def binary_strings(data, min_len=6):
    """ASCII and UTF-16LE runs from raw bytes (what a 'partial text scan' of a binary CAD file can see)."""
    out = [m.group().decode("ascii", "ignore") for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data)]
    out += [m.group().decode("utf-16le", "ignore") for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len, data)]
    return "\n".join(out)


def read_head(path, n):
    with open(path, "rb") as f:
        return f.read(n)


# ---------- handlers: each fills `row` and returns the text to check ----------

def h_dwg(path, row):
    data = read_head(path, STRINGS_BYTES)
    row["version"] = DWG_VERSIONS.get(data[:6], data[:6].decode("ascii", "replace"))
    row["software"] = "AutoCAD-format DWG"
    if not data[:2] == b"AC":
        row["read"], row["error"] = "failed", "not a DWG header"
        return ""
    row["read"] = "strings"  # R2004+ sections are compressed; strings recover title-block text only partly
    text = binary_strings(data)
    if data[:6] >= b"AC1018" and len(text) < 200:
        row["note"] = "compressed DWG: little text recoverable without a DWG reader"
    return text


def h_dxf(path, row):
    import ezdxf
    from ezdxf import recover
    try:
        doc, auditor = recover.readfile(path)
    except Exception as e:  # noqa: BLE001 - any parse failure is a finding
        row["read"], row["error"] = "failed", f"dxf: {e!r}"[:200]
        return ""
    row["read"] = "full"
    row["version"] = doc.dxfversion
    hdr = doc.header
    row["software"] = "DXF " + str(hdr.get("$ACADVER", ""))
    who = hdr.get("$LASTSAVEDBY")
    if who:
        row["authors"] = [str(who)]
    try:
        tdc = hdr.get("$TDCREATE")
        if tdc:
            row["created"] = ezdxf.math.juliandate.calendardate(float(tdc)).date().isoformat()
    except Exception:  # noqa: BLE001
        pass
    counts = Counter()
    texts = []
    for layout in list(doc.layouts) + list(doc.blocks):
        for e in layout:
            t = e.dxftype()
            if t == "POLYLINE":
                try:
                    if e.is_poly_face_mesh:
                        t = "POLYFACE"
                    elif e.is_polygon_mesh:
                        t = "POLYMESH"
                except Exception:  # noqa: BLE001
                    pass
            counts[t] += 1
            if t in ("TEXT", "ATTRIB", "ATTDEF"):
                texts.append(e.dxf.get("text", ""))
            elif t == "MTEXT":
                try:
                    texts.append(e.plain_text())
                except Exception:  # noqa: BLE001
                    texts.append(e.text)
    row["entities"] = dict(counts.most_common(15))
    row["dims"] = "3d" if any(counts[t] for t in DXF_3D) else "2d"
    layers = [layer.dxf.name for layer in doc.layers]
    row["layers"] = len(layers)
    texts += layers
    return "\n".join(texts)


def h_pdf(path, row):
    import fitz
    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        row["read"], row["error"] = "failed", f"pdf: {e!r}"[:200]
        return ""
    row["read"] = "full"
    meta = doc.metadata or {}
    row["software"] = " / ".join(x for x in (meta.get("creator"), meta.get("producer")) if x)[:120] or None
    if meta.get("author"):
        row["authors"] = [meta["author"][:80]]
    if meta.get("creationDate"):
        m = re.search(r"(\d{4})(\d{2})(\d{2})", meta["creationDate"])
        if m:
            row["created"] = "-".join(m.groups())
    row["pages"] = doc.page_count
    scanned = drawing_like = 0
    texts = []
    for i, page in enumerate(doc):
        if i >= 300:
            row["partial"] = True
            break
        t = page.get_text("text")
        w, h = page.rect.width, page.rect.height
        if w * h > 842 * 1191 * 0.95:  # larger than A3: a drawing sheet, not a document page
            drawing_like += 1
        if len(t.strip()) < 25 and page.get_images():
            scanned += 1
        if sum(len(x) for x in texts) < TEXT_LIMIT:
            texts.append(t)
    row["scanned_pages"] = scanned
    row["drawing_like"] = drawing_like
    row["kind"] = "drawing" if drawing_like >= max(1, row["pages"] // 2) else "document"
    return "\n".join(texts)


def step_header(text, row):
    m = re.search(r"FILE_NAME\s*\((.*?)\);", text, re.S)
    if m:
        parts = re.findall(r"'((?:[^']|'')*)'", m.group(1))
        # name, time_stamp, (author), (organization), preprocessor_version, originating_system, authorization
        if len(parts) >= 2:
            row["created"] = parts[1][:10] or None
        people = [p for p in parts[2:4] if p.strip()]
        if people:
            row["authors"] = people[:2]
        if len(parts) >= 6:
            row["software"] = (parts[5] or parts[4])[:120] or None
    m = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", text)
    if m:
        row["version"] = m.group(1)


def h_step(path, row):
    size = os.path.getsize(path)
    data = read_head(path, min(size, 400 * 1024 * 1024))
    text = data.decode("latin-1", "ignore")
    if "ISO-10303-21" not in text[:200]:
        row["read"], row["error"] = "failed", "no ISO-10303-21 header"
        return ""
    row["read"] = "full" if len(data) == size else "metadata"
    step_header(text[:20000], row)
    counts = Counter(re.findall(r"=\s*([A-Z_0-9]+)\s*\(", text))
    row["entities"] = {k: counts[k] for k in ("PRODUCT", "MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS",
                                              "NEXT_ASSEMBLY_USAGE_OCCURRENCE", "ADVANCED_FACE") if counts[k]}
    row["dims"] = "3d"
    if counts["NEXT_ASSEMBLY_USAGE_OCCURRENCE"]:
        row["kind"] = "assembly"
    names = re.findall(r"PRODUCT\s*\(\s*'((?:[^']|'')*)'", text)
    return "\n".join(names[:2000]) + "\n" + text[:20000]


def h_ifc(path, row):
    size = os.path.getsize(path)
    if path.lower().endswith(".ifczip"):
        try:
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".ifc"))
                data = z.read(name)[:400 * 1024 * 1024]
        except Exception as e:  # noqa: BLE001
            row["read"], row["error"] = "failed", f"ifczip: {e!r}"[:200]
            return ""
    else:
        data = read_head(path, min(size, 400 * 1024 * 1024))
    text = data.decode("latin-1", "ignore")
    if "ISO-10303-21" not in text[:200]:
        row["read"], row["error"] = "failed", "no ISO-10303-21 header"
        return ""
    row["read"] = "full" if len(data) >= size else "metadata"
    step_header(text[:20000], row)
    counts = Counter(re.findall(r"=\s*(IFC[A-Z0-9]+)\s*\(", text))
    row["entities"] = {k: counts[k] for k in IFC_BUILT if counts[k]}
    row["dims"] = "3d"
    m = re.search(r"IFCPROJECT\s*\([^;]*?'([^']*)'", text)
    head = text[:30000]
    names = re.findall(r"IFC(?:PROJECT|SITE|BUILDING|ORGANIZATION|PERSON|POSTALADDRESS)\s*\(([^;]{0,400})\);", text)
    return head + "\n" + "\n".join(names[:500]) + ("\n" + m.group(1) if m else "")


def ole_strings(path, row, prefer_streams=()):
    """Compound-file formats (Revit, Inventor, old SolidWorks, Office 97): stream list + strings."""
    import olefile
    if not olefile.isOleFile(path):
        return None
    try:
        ole = olefile.OleFileIO(path)
    except Exception as e:  # noqa: BLE001
        row["read"], row["error"] = "failed", f"ole: {e!r}"[:200]
        return ""
    streams = ["/".join(s) for s in ole.listdir()]
    row["streams"] = len(streams)
    meta = ole.get_metadata()
    who = [x.decode("latin-1", "ignore") for x in (meta.author, meta.last_saved_by) if x]
    if who:
        row["authors"] = list(dict.fromkeys(w for w in who if w.strip()))[:3]
    if meta.company:
        row["orgs"] = [meta.company.decode("latin-1", "ignore")]
    if meta.creating_application:
        row["software"] = meta.creating_application.decode("latin-1", "ignore")
    if meta.create_time:
        row["created"] = meta.create_time.date().isoformat()
    texts = []
    budget = STRINGS_BYTES
    order = [s for s in streams if any(p.lower() in s.lower() for p in prefer_streams)] + streams
    for s in dict.fromkeys(order):
        if budget <= 0:
            row["partial"] = True
            break
        try:
            data = ole.openstream(s).read(min(budget, 8 * 1024 * 1024))
        except Exception:  # noqa: BLE001
            continue
        budget -= len(data)
        texts.append(binary_strings(data))
    row["read"] = "metadata"
    return "\n".join(texts)


def h_revit(path, row):
    text = ole_strings(path, row, prefer_streams=("BasicFileInfo",))
    if text is None:
        row["read"], row["error"] = "failed", "not a compound file"
        return ""
    m = re.search(r"Autodesk Revit (\d{4})", text) or re.search(r"Format:\s*(\d{4})", text)
    if m:
        row["software"], row["version"] = "Autodesk Revit", m.group(1)
    row["dims"] = "3d"
    return text


def h_binary_cad(path, row):
    """SolidWorks, Inventor, NX/Creo, Solid Edge, CATIA, Rhino, SketchUp and other closed formats: header + strings."""
    ext = row["ext"]
    head = read_head(path, 4096)
    if ext == ".prt":
        if head.startswith(b"#UGC:") or b"#UGC" in head[:64]:
            row["family"], row["software"] = "creo", "Creo / Pro/ENGINEER"
        else:
            row["family"], row["software"] = "nx", "Siemens NX"
    if ext == ".asm":
        row["family"] = "creo" if head.startswith(b"#UGC:") else "solidedge"
    text = ole_strings(path, row)
    if text is None:
        data = read_head(path, STRINGS_BYTES)
        text = binary_strings(data)
        row["read"] = "strings"
        if os.path.getsize(path) > len(data):
            row["partial"] = True
    else:
        data = read_head(path, STRINGS_BYTES)
    if ext == ".skp":
        m = re.search(r"SketchUp Model\s*\{?([\d.]+)", text)
        row["software"], row["version"] = "SketchUp", (m.group(1) if m else None)
    elif ext == ".3dm":
        row["software"] = "Rhino"
        m = re.match(rb"3D Geometry File Format\s+(\d+)", head)
        row["version"] = m.group(1).decode() if m else None
        if re.search(r"Matrix|Gemvision", text):
            row["software"] = "Rhino + Matrix (Gemvision)"
    elif ext in (".sldprt", ".sldasm", ".slddrw"):
        m = re.search(r"SOLIDWORKS? ?(\d{4})", text, re.I)
        row["software"] = "SolidWorks" + (f" {m.group(1)}" if m else "")
    elif ext in (".ipt", ".iam", ".idw"):
        m = re.search(r"Autodesk Inventor (\d{4})", text)
        row["software"] = "Autodesk Inventor" + (f" {m.group(1)}" if m else "")
    if row["kind"] in ("drawing", "assembly"):
        refs = {m.decode("latin-1", "ignore").strip().lower() for m in MODEL_REF_RE.findall(data)}
        refs |= {m.decode("utf-16le", "ignore").strip().lower() for m in MODEL_REF_RE16.findall(data)}
        refs = {os.path.basename(r.replace("\\", "/")) for r in refs if len(r) < 160}
        refs.discard(os.path.basename(row["path"]).lower())
        if refs:
            row["refs"] = sorted(refs)[:500]
    if IMPORTED_RE.search(data):
        row["imported_hint"] = True
    if row["kind"] in ("part", "assembly", "model"):
        row["dims"] = "3d"
    return text


def h_stl(path, row):
    head = read_head(path, 84)
    row["dims"] = "3d"
    if head[:5].lower() == b"solid" and b"facet" in read_head(path, 2048):
        row["software"], row["read"] = "ASCII STL", "full"
    elif len(head) == 84:
        row["entities"] = {"triangles": int.from_bytes(head[80:84], "little")}
        row["software"], row["read"] = "binary STL", "full"
    return ""


GCODE_CONTROLLERS = [
    ("heidenhain", re.compile(r"BEGIN PGM|END PGM|TOOL CALL", re.I)),
    ("siemens", re.compile(r"\bCYCLE\d{3}\b|TRAFOORI|\bMSG\s*\(|;\s*SINUMERIK", re.I)),
    ("mazak", re.compile(r"MAZAK|MAZATROL|INTEGREX|VARIAXIS", re.I)),
    ("haas", re.compile(r"\bHAAS\b|\bUMC-\d|\bVF-\d", re.I)),
    ("okuma", re.compile(r"\bOKUMA\b|\bOSP\b", re.I)),
    ("mitsubishi", re.compile(r"MITSUBISHI|MELDAS", re.I)),
    ("fanuc", re.compile(r"\bFANUC\b|^O\d{4}", re.I | re.M)),
]


def h_gcode(path, row):
    data = read_head(path, 8 * 1024 * 1024)
    text = data.decode("latin-1", "ignore")
    gwords = len(re.findall(r"\bG0?[0-3]\b", text))
    if gwords < 5 and row["ext"] == ".h":
        row["kind"], row["family"] = "document", "text"  # a C header, not a Heidenhain program
        row["read"] = "full"
        return text[:TEXT_LIMIT]
    row["read"] = "full" if len(data) == os.path.getsize(path) else "metadata"
    row["controller"] = next((name for name, rx in GCODE_CONTROLLERS if rx.search(text[:200000])), None)
    m = re.search(r"\((?:POST|POSTPROCESSOR|MACHINE)[:\s]+([^)]{1,80})\)", text, re.I) or \
        re.search(r"(?:MACHINE|MACHINE NAME)\s*[:=]\s*([^\n;)]{1,80})", text, re.I)
    if m:
        row["machine"] = m.group(1).strip()
    for name, rx in (("Mastercam", r"MASTERCAM"), ("Fusion", r"FUSION|AUTODESK CAM|HSM"),
                     ("NX CAM", r"NX CAM|SIEMENS NX|UNIGRAPHICS"), ("hyperMILL", r"HYPERMILL"),
                     ("PowerMill", r"POWERMILL"), ("ESPRIT", r"ESPRIT")):
        if re.search(rx, text[:200000], re.I):
            row["software"] = name
            break
    body = text[:2_000_000]
    rot = bool(re.search(r"(?m)^[^(;\n]*\b[ABC]-?\d", body))
    if re.search(r"G43\.4|G234|M128|TCPM|TRAORI|G43\.5", body):
        row["axes"] = "5-axis simultaneous"
    elif re.search(r"G68\.2|G68\.3|PLANE SPATIAL|CYCLE800|G254", body):
        row["axes"] = "3+2"
    elif rot:
        row["axes"] = "4/5-axis (rotary words)"
    elif re.search(r"\bG9[56]\b", body) and not re.search(r"(?m)^[^(;\n]*\bY-?\d", body):
        row["axes"] = "turning"
    else:
        row["axes"] = "3-axis"
    comments = "\n".join(re.findall(r"\(([^)]{0,200})\)", body)[:5000])
    return comments


def zip_office(path, row):
    try:
        z = zipfile.ZipFile(path)
    except Exception as e:  # noqa: BLE001
        row["read"], row["error"] = "failed", f"office zip: {e!r}"[:200]
        return ""
    texts = []
    try:
        core = z.read("docProps/core.xml").decode("utf-8", "ignore")
        who = re.findall(r"<(?:dc:creator|cp:lastModifiedBy)>([^<]+)<", core)
        if who:
            row["authors"] = list(dict.fromkeys(who))[:3]
        m = re.search(r"<dcterms:created[^>]*>(\d{4}-\d{2}-\d{2})", core)
        if m:
            row["created"] = m.group(1)
    except KeyError:
        pass
    try:
        app = z.read("docProps/app.xml").decode("utf-8", "ignore")
        m = re.search(r"<Company>([^<]+)<", app)
        if m:
            row["orgs"] = [m.group(1)]
        m = re.search(r"<Application>([^<]+)<", app)
        if m:
            row["software"] = m.group(1)
    except KeyError:
        pass
    total = 0
    for n in z.namelist():
        if n.endswith(".xml") and ("word/" in n or "xl/sharedStrings" in n or "ppt/slides/" in n or "xl/worksheets/" in n):
            t = re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "ignore"))
            texts.append(t)
            total += len(t)
            if total > TEXT_LIMIT:
                row["partial"] = True
                break
    row["read"] = "full"
    return "\n".join(texts)


def h_office(path, row):
    if row["ext"] in (".xls", ".doc"):
        text = ole_strings(path, row)
        if text is None:
            row["read"], row["error"] = "failed", "not a compound file"
            return ""
        return text
    return zip_office(path, row)


def h_text(path, row):
    data = read_head(path, TEXT_LIMIT)
    row["read"] = "full" if len(data) == os.path.getsize(path) else "metadata"
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def h_image(path, row):
    try:
        from PIL import Image
        with Image.open(path) as im:
            row["entities"] = {"width": im.width, "height": im.height}
        row["read"] = "metadata"
    except Exception as e:  # noqa: BLE001
        row["read"], row["error"] = "failed", f"image: {e!r}"[:200]
    return ""


HANDLERS = {
    "dwg": h_dwg, "dxf": h_dxf, "pdf": h_pdf, "step": h_step, "ifc": h_ifc, "revit": h_revit,
    "stl": h_stl, "gcode": h_gcode, "office": h_office, "text": h_text, "image": h_image, "binary": h_binary_cad,
}


def handler_for(ext, family):
    if ext == ".dwg":
        return "dwg"
    if ext == ".dxf":
        return "dxf"
    if ext == ".pdf":
        return "pdf"
    if family == "neutral" and ext in (".step", ".stp", ".p21"):
        return "step"
    if family == "ifc":
        return "ifc"
    if family == "revit":
        return "revit"
    if ext == ".stl":
        return "stl"
    if family == "gcode":
        return "gcode"
    if family == "office":
        return "office"
    if family in ("text", "email"):
        return "text"
    if family == "image":
        return "image"
    return "binary"


def scan_file(path, rel, partial=False, size=None):
    """Return one row for the file at `path` (local), reported under the name `rel`."""
    name = os.path.basename(rel)
    ext = ext_of(name)
    size = os.path.getsize(path) if size is None else size
    kind, family = KINDS.get(ext, ("other", "other"))
    row = {"path": rel, "ext": ext or "(none)", "size": size, "kind": kind, "family": family, "read": "header"}
    if name.lower() in JUNK or name.startswith("._"):
        row.update(kind="junk", family="junk", read="skipped")
        return row
    if partial:
        row["partial"] = True
    else:
        row["sha256"] = sha256_of(path)
    if size == 0:
        row.update(read="failed", error="empty file")
        return row
    rev = revision_of(name)
    if rev:
        row["revision"] = rev
    if family == "archive":
        row["read"] = "expanded"  # members are scanned as their own rows
        return row
    try:
        text = HANDLERS[handler_for(ext, family)](path, row) or ""
    except Exception as e:  # noqa: BLE001 - the scan must never stop on one bad file
        row["read"], row["error"] = "failed", f"{type(e).__name__}: {e}"[:200]
        text = ""
    text += "\n" + rel  # file and folder names carry rights and PII signals too
    row.update(textchecks.check(text[:TEXT_LIMIT], binary=row.get("read") in ("strings", "metadata")
                                and handler_for(ext, family) in ("binary", "dwg", "revit", "office")
                                and ext not in (".docx", ".xlsx", ".xlsm", ".pptx")))
    return {k: v for k, v in row.items() if v not in (None, [], {}, "")}


def expand_archive(path, dest):
    """Unpack an archive into `dest`; return the list of (member_name, local_path). Unsupported -> []."""
    low = path.lower()
    out = []
    try:
        if low.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    try:
                        name = info.filename.encode("cp437").decode("utf-8")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        name = info.filename
                    target = os.path.join(dest, safe_member(name))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(info) as src, open(target, "wb") as dst:
                        while True:
                            block = src.read(1 << 20)
                            if not block:
                                break
                            dst.write(block)
                    out.append((name, target))
        elif low.endswith(".7z"):
            import py7zr
            with py7zr.SevenZipFile(path) as z:
                names = [n for n in z.getnames()]
                z.extractall(dest)
            for n in names:
                p = os.path.join(dest, n)
                if os.path.isfile(p):
                    out.append((n, p))
        elif low.endswith((".tar", ".tgz", ".tar.gz", ".gz")):
            import tarfile
            with tarfile.open(path) as t:
                for m in t.getmembers():
                    if not m.isfile():
                        continue
                    target = os.path.join(dest, safe_member(m.name))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with t.extractfile(m) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    out.append((m.name, target))
    except Exception as e:  # noqa: BLE001
        return out, f"{type(e).__name__}: {e}"[:200]
    return out, None


def safe_member(name):
    """Keep archive members inside the extraction folder (no absolute paths or ..)."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return os.path.join(*parts) if parts else "unnamed"


def scan_path(path, rel, workdir, depth=0):
    """Scan a file; if it is an archive, also scan its members (rows named archive!member). Yields rows."""
    row = scan_file(path, rel)
    if row["family"] != "archive" or depth > 2:
        yield row
        return
    import tempfile
    with tempfile.TemporaryDirectory(dir=workdir) as dest:
        members, err = expand_archive(path, dest)
        row["members"] = len(members)
        if err:
            row["read"], row["error"] = "failed", err
        elif not members and row["ext"] == ".rar":
            row["read"], row["error"] = "failed", "rar not supported by the scanner"
        yield row
        for name, local in members:
            yield from scan_path(local, f"{rel}!{name}", workdir, depth + 1)
